"""Account pipeline, worker pool, and run statistics.

The pipeline for a single account is deliberately linear so failures are easy
to reason about::

    solve captcha -> register -> wait for OTP -> confirm -> profile
                  -> mint API key -> (optionally) inject into 9router

Three behaviours deserve a note:

**Captcha retries** happen *inside* one account attempt.  Roughly half of the
structurally valid tokens are rejected, so retrying with a fresh token and a
fresh session is normal operation rather than an error path.

**Cooldown** is global.  When the platform answers ``429`` the whole pool
pauses until the server's ``retryAfter`` hint has elapsed, because hammering
the endpoint only extends the throttle.

**Failures carry their address.**  :class:`FarmError` records which e-mail was
being registered so the failure log is actionable instead of ``(unknown)``.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import config as cfg
from . import logto
from .captcha import AliyunSolver, CaptchaError, token_from_solution
from .mailbox import Mailbox, MailboxError
from .router9 import Router9
from .store import ResultStore, append_failure

FIRST_NAMES = [
    "james", "sarah", "michael", "emma", "david", "lisa", "john", "anna",
    "robert", "jennifer", "william", "mary", "richard", "linda", "thomas",
    "patricia", "charles", "elizabeth", "daniel", "barbara", "matthew",
    "susan", "anthony", "jessica", "mark", "margaret", "donald", "sandra",
    "steven", "ashley", "paul", "kimberly", "andrew", "emily", "joshua",
    "donna", "kenneth", "michelle", "kevin", "carol", "brian", "amanda",
    "george", "melissa", "edward", "deborah", "ronald", "stephanie",
    "alex", "rebecca", "tyler", "laura", "adam", "helen", "nathan",
    "kathleen", "ryan", "julia", "keith",
]

LAST_NAMES = [
    "smith", "jones", "brown", "davis", "wilson", "moore", "taylor",
    "anderson", "jackson", "white", "harris", "martin", "thompson", "garcia",
    "martinez", "robinson", "clark", "rodriguez", "lewis", "walker", "hall",
    "allen", "young", "king", "wright", "scott", "green", "baker", "adams",
    "nelson", "hill", "rivera", "campbell", "carter", "phillips", "evans",
    "turner", "torres", "parker", "collins", "edwards", "stewart", "flores",
    "morris", "nguyen", "murphy", "cook", "rogers", "morgan", "reed",
]


def random_local_part() -> str:
    """Generate a plausible e-mail local part (``firstlastNN``)."""
    return f"{random.choice(FIRST_NAMES)}{random.choice(LAST_NAMES)}{random.randint(10, 99)}"


class FarmError(RuntimeError):
    """A failed account attempt, carrying the address it was working on."""

    def __init__(self, email: str, cause: BaseException) -> None:
        self.email = email
        self.cause = cause
        super().__init__(f"{type(cause).__name__}: {cause}")


# ── statistics ───────────────────────────────────────────────────────────────
@dataclass
class Stats:
    """Run counters shared by every worker thread."""

    target: int
    succeeded: int = 0
    failed: int = 0
    attempts: int = 0
    started_at: float = field(default_factory=time.monotonic)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note_attempt(self) -> int:
        with self._lock:
            self.attempts += 1
            return self.attempts

    def note_success(self) -> int:
        with self._lock:
            self.succeeded += 1
            return self.succeeded

    def note_failure(self) -> int:
        with self._lock:
            self.failed += 1
            return self.failed

    def snapshot(self) -> tuple:
        with self._lock:
            return self.succeeded, self.failed, self.attempts

    @property
    def done(self) -> bool:
        with self._lock:
            return self.succeeded >= self.target

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at


class Cooldown:
    """A pool-wide pause, used when the platform asks us to slow down."""

    def __init__(self, log) -> None:
        self._until = 0.0
        self._lock = threading.Lock()
        self._log = log

    def engage(self, seconds: int, reason: str = "") -> None:
        seconds = max(1, min(int(seconds), 3600))
        with self._lock:
            deadline = time.monotonic() + seconds
            if deadline <= self._until:
                return
            self._until = deadline
        self._log(f"cooldown {seconds}s{': ' + reason if reason else ''}")

    def wait(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            with self._lock:
                remaining = self._until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))


# ── context ──────────────────────────────────────────────────────────────────
class NamePool:
    """Thread-safe allocator for unused e-mail local parts."""

    def __init__(self, taken: set) -> None:
        self._taken = {item.lower() for item in taken}
        self._lock = threading.Lock()

    def allocate(self, domain: str) -> str:
        while True:
            candidate = random_local_part()
            with self._lock:
                if candidate.lower() not in self._taken:
                    self._taken.add(candidate.lower())
                    return f"{candidate}@{domain}"

    def __len__(self) -> int:
        with self._lock:
            return len(self._taken)


@dataclass
class Context:
    """Everything a worker thread needs, assembled once."""

    config: cfg.Config
    solver: AliyunSolver
    mailbox: Mailbox
    router: Router9
    store: ResultStore
    stats: Stats
    names: NamePool
    stop_event: threading.Event
    cooldown: Cooldown
    log: Callable[[str], None]


# ── pipeline ─────────────────────────────────────────────────────────────────
def farm_one(ctx: Context, worker_id: int) -> Optional[logto.Account]:
    """Register exactly one account.

    Returns the account, or ``None`` when the run was stopped mid-flight.
    Any failure is raised as :class:`FarmError` with the address attached.
    """
    conf = ctx.config
    email = ctx.names.allocate(conf.cf_domain)
    started = time.monotonic()

    try:
        return _farm_one(ctx, worker_id, email, started)
    except Exception as exc:  # noqa: BLE001 - re-raised with context
        raise FarmError(email, exc) from exc


def _farm_one(ctx: Context, worker_id: int, email: str, started: float) -> logto.Account:
    conf = ctx.config

    # 1. captcha + open the registration interaction -------------------------
    session = None
    verification_id = None
    last_error: Optional[BaseException] = None

    for attempt in range(1, conf.captcha_attempts + 1):
        ctx.cooldown.wait(ctx.stop_event)
        if ctx.stop_event.is_set():
            raise KeyboardInterrupt("run stopped")

        try:
            solution = ctx.solver.solve()
        except CaptchaError as exc:
            last_error = exc
            ctx.log(
                f"[W{worker_id}] captcha solve failed "
                f"({attempt}/{conf.captcha_attempts}): {exc}"
            )
            continue

        token = token_from_solution(solution)
        try:
            session, verification_id = logto.register(
                email=email,
                captcha_token=token,
                user_agent=conf.user_agent,
                proxies=conf.proxies,
                timeout=conf.http_timeout,
                log=lambda msg: ctx.log(f"[W{worker_id}] {msg}"),
            )
            break
        except logto.CaptchaRejected as exc:
            last_error = exc
            ctx.log(
                f"[W{worker_id}] captcha token rejected "
                f"({attempt}/{conf.captcha_attempts}) — retrying with a fresh token"
            )
        except logto.RateLimited as exc:
            last_error = exc
            ctx.cooldown.engage(exc.retry_after, "rate limited during registration")
        except logto.RegistrationError as exc:
            last_error = exc
            ctx.log(f"[W{worker_id}] registration failed: {exc}")
            time.sleep(2)

    if session is None or verification_id is None:
        raise logto.RegistrationError(f"captcha/registration exhausted retries: {last_error}")

    # 2. OTP ----------------------------------------------------------------
    try:
        code = ctx.mailbox.wait(
            email,
            timeout=conf.otp_timeout,
            poll=conf.otp_poll,
            log=lambda msg: ctx.log(f"[W{worker_id}] {msg}"),
        )
    except MailboxError as exc:
        raise logto.RegistrationError(f"mailbox error: {exc}") from exc

    if not code:
        ctx.log(f"[W{worker_id}] no OTP within {conf.otp_timeout}s for {email}")
        raise logto.VerificationFailed("OTP did not arrive in time")

    confirmed = logto.confirm_otp(
        session,
        email=email,
        verification_id=verification_id,
        code=code,
        timeout=conf.http_timeout,
    )

    # 3. finish the profile and mint a key ----------------------------------
    redirect = logto.complete_profile(
        session, email=email, verification_id=confirmed, timeout=conf.http_timeout
    )
    if redirect:
        session.get(redirect, allow_redirects=True, timeout=conf.http_timeout)

    api_key = logto.create_api_key(
        session, name=f"key_{email.split('@')[0]}", timeout=conf.http_timeout
    )

    injected = ctx.router.inject(email=email, api_key=api_key)
    ctx.log(f"[W{worker_id}] {email} ready in {time.monotonic() - started:.0f}s")
    return logto.Account(
        email=email, api_key=api_key, created_at=time.time(), injected=injected
    )


def worker_loop(ctx: Context, worker_id: int, failure_log: str) -> None:
    """Keep registering accounts until the target is reached or we are stopped."""
    while not ctx.stop_event.is_set() and not ctx.stats.done:
        ctx.cooldown.wait(ctx.stop_event)
        if ctx.stop_event.is_set() or ctx.stats.done:
            return

        number = ctx.stats.note_attempt()
        if number > ctx.config.target * 4:
            # Safety valve: never spin forever on a broken configuration.
            ctx.log(f"[W{worker_id}] too many attempts — stopping the pool")
            ctx.stop_event.set()
            return

        try:
            account = farm_one(ctx, worker_id)
            if account is None:
                return
            ctx.store.append(account)
            done = ctx.stats.note_success()
            ctx.log(
                f"[W{worker_id}] [{done}/{ctx.config.target}] {account.email} "
                f"| {account.api_key[:12]}… | 9router:{'ok' if account.injected else 'skip'}"
            )
        except KeyboardInterrupt:
            return
        except FarmError as exc:
            ctx.stats.note_failure()
            ctx.log(f"[W{worker_id}] failed {exc.email}: {exc}")
            append_failure(failure_log, exc.email, str(exc))
            time.sleep(1)
        except Exception as exc:  # noqa: BLE001 - defensive: keep the pool alive
            ctx.stats.note_failure()
            ctx.log(f"[W{worker_id}] unexpected error: {type(exc).__name__}: {exc}")
            time.sleep(1)

        if ctx.config.request_pause:
            time.sleep(ctx.config.request_pause)
