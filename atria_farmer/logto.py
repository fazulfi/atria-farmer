"""Atria registration flow (Logto experience API).

The sign-in page is a Logto deployment, so registration is a short sequence of
``/api/experience`` calls rather than a form post.  The order matters and the
captcha token is bound to the interaction that requested it, which is why
:func:`register` builds a brand-new session for every attempt.

Flow, in order::

    GET  /console                              seed cookies
    PUT  /api/experience        {Register}     open the interaction
    POST /api/experience/captcha/verify        exchange the captcha token
    PUT  /api/experience        {Register, captchaToken}
    POST .../verification-code                 request the e-mail OTP
    POST .../verification-code/verify          confirm the OTP
    POST /api/experience/profile
    POST /api/experience/identification
    POST /api/experience/submit                -> redirectTo
    GET  redirectTo                            finish the OIDC hand-off
    POST /api/keys                             mint the API key

The captcha token is deliberately *not* sent to ``verification-code``: that
endpoint answers ``422 session.captcha_required`` when the token is missing
from the experience update, and ``422`` again if it is sent in the wrong
place.  The dedicated ``captcha/verify`` call is what actually satisfies the
requirement.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

import requests

from . import config as cfg


# ── errors ───────────────────────────────────────────────────────────────────
class RegistrationError(RuntimeError):
    """Base class for every recoverable registration failure."""


class RateLimited(RegistrationError):
    """The platform refused the request because of a per-IP throttle."""

    def __init__(self, retry_after: int, message: str = "") -> None:
        self.retry_after = retry_after
        super().__init__(
            message or f"rate limited, retry after {retry_after}s"
        )


class CaptchaRejected(RegistrationError):
    """The captcha token was well-formed but the backend refused it."""


class OtpRequestFailed(RegistrationError):
    """The OTP could not be requested."""


class VerificationFailed(RegistrationError):
    """The OTP was rejected."""


class KeyCreationFailed(RegistrationError):
    """Registration succeeded but no API key could be minted."""


@dataclass
class Account:
    """A successfully registered account."""

    email: str
    api_key: str
    created_at: float
    injected: bool = False

    def as_row(self, timestamp: str) -> str:
        return (
            f"{timestamp} | {self.email} | {self.api_key} | "
            f"100M_TOKENS | 9router:{self.injected}"
        )


# ── session helpers ──────────────────────────────────────────────────────────
def new_session(
    *,
    user_agent: str,
    proxies: Optional[Dict[str, str]] = None,
    timeout: int = 30,
) -> requests.Session:
    """Create a browser-shaped session with the headers Logto expects."""
    session = requests.Session()
    if proxies:
        session.proxies.update(proxies)
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": cfg.AUTH_BASE,
            "Referer": cfg.AUTH_BASE + "/",
        }
    )
    session.request_timeout = timeout  # type: ignore[attr-defined]
    return session


def _timeout(session: requests.Session, fallback: int) -> int:
    return getattr(session, "request_timeout", fallback)


def _check_rate_limit(response: requests.Response) -> None:
    """Translate a ``429`` into :class:`RateLimited` with the server hint."""
    if response.status_code != 429:
        return
    retry_after = 3600
    try:
        payload = response.json()
        retry_after = int(
            (payload.get("data") or {}).get("retryAfter", retry_after)
        )
    except Exception:  # pragma: no cover - non-JSON body
        header = response.headers.get("Retry-After")
        if header and header.isdigit():
            retry_after = int(header)
    raise RateLimited(retry_after, f"rate limited: {response.text[:160]}")


# ── registration ─────────────────────────────────────────────────────────────
def register(
    *,
    email: str,
    captcha_token: str,
    user_agent: str,
    proxies: Optional[Dict[str, str]] = None,
    timeout: int = 30,
    log=None,
):
    """Run the registration flow up to (but excluding) OTP confirmation.

    Returns ``(session, verification_id)`` — the authenticated session and the
    id needed to confirm the OTP later.  Raises :class:`CaptchaRejected` when
    the token is refused so the caller can retry with a freshly solved one.
    """
    log = log or (lambda *_: None)
    session = new_session(user_agent=user_agent, proxies=proxies, timeout=timeout)

    session.get(cfg.CONSOLE_URL, timeout=timeout)
    _open_interaction(session, timeout)

    response = session.post(
        cfg.CAPTCHA_VERIFY_URL, json={"captchaToken": captcha_token}, timeout=timeout
    )
    _check_rate_limit(response)
    if response.status_code != 200:
        raise CaptchaRejected(
            f"captcha/verify returned HTTP {response.status_code}: {response.text[:160]}"
        )
    try:
        accepted = bool(response.json().get("success"))
    except ValueError:
        accepted = False
    if not accepted:
        raise CaptchaRejected("captcha token was rejected by the backend")

    response = session.put(
        cfg.EXPERIENCE_URL,
        json={"interactionEvent": "Register", "captchaToken": captcha_token},
        timeout=timeout,
    )
    _check_rate_limit(response)
    if response.status_code not in (200, 204):
        raise RegistrationError(
            f"experience update returned HTTP {response.status_code}: {response.text[:160]}"
        )

    verification_id = _request_otp(session, email, timeout)
    log(f"OTP requested (verificationId={verification_id})")
    return session, verification_id


def _open_interaction(session: requests.Session, timeout: int) -> None:
    response = session.put(
        cfg.EXPERIENCE_URL, json={"interactionEvent": "Register"}, timeout=timeout
    )
    _check_rate_limit(response)
    if response.status_code not in (200, 204):
        raise RegistrationError(
            f"could not open registration interaction "
            f"(HTTP {response.status_code}): {response.text[:160]}"
        )


def _request_otp(session: requests.Session, email: str, timeout: int) -> str:
    response = session.post(
        cfg.VERIFICATION_CODE_URL,
        json={
            "interactionEvent": "Register",
            "identifier": {"type": "email", "value": email},
        },
        timeout=timeout,
    )
    _check_rate_limit(response)
    if response.status_code != 200:
        raise OtpRequestFailed(
            f"OTP request returned HTTP {response.status_code}: {response.text[:160]}"
        )
    verification_id = (response.json() or {}).get("verificationId")
    if not verification_id:
        raise OtpRequestFailed("OTP response contained no verificationId")
    return verification_id


def confirm_otp(
    session: requests.Session,
    *,
    email: str,
    verification_id: str,
    code: str,
    timeout: int = 30,
) -> str:
    """Submit the OTP and return the confirmed verification id."""
    response = session.post(
        cfg.VERIFICATION_VERIFY_URL,
        json={
            "identifier": {"type": "email", "value": email},
            "verificationId": verification_id,
            "code": code,
        },
        timeout=timeout,
    )
    _check_rate_limit(response)
    if response.status_code != 200:
        raise VerificationFailed(
            f"OTP verification returned HTTP {response.status_code}: {response.text[:160]}"
        )
    return (response.json() or {}).get("verificationId", verification_id)


def complete_profile(session: requests.Session, *, email: str, verification_id: str,
                     timeout: int = 30) -> Optional[str]:
    """Finish the profile / identification / submit steps.

    Returns the ``redirectTo`` URL that completes the OIDC hand-off, if the
    platform supplies one.
    """
    session.post(
        cfg.PROFILE_URL,
        json={"type": "email", "verificationId": verification_id},
        timeout=timeout,
    )
    session.post(cfg.IDENTIFICATION_URL, json={}, timeout=timeout)

    response = session.post(cfg.SUBMIT_URL, json={}, timeout=timeout)
    _check_rate_limit(response)
    if response.status_code != 200:
        raise RegistrationError(
            f"submit returned HTTP {response.status_code}: {response.text[:160]}"
        )
    try:
        return (response.json() or {}).get("redirectTo")
    except ValueError:
        return None


def create_api_key(session: requests.Session, *, name: str, timeout: int = 30) -> str:
    """Mint an API key for the freshly registered account."""
    response = session.post(cfg.KEYS_URL, json={"name": name}, timeout=timeout)
    _check_rate_limit(response)
    if response.status_code not in (200, 201):
        raise KeyCreationFailed(
            f"key creation returned HTTP {response.status_code}: {response.text[:160]}"
        )
    key = (response.json() or {}).get("key")
    if not key:
        raise KeyCreationFailed("key creation response contained no 'key' field")
    return key


# ── verification helpers ─────────────────────────────────────────────────────
def check_api_key(api_key: str, *, timeout: int = 60, proxies: Optional[Dict[str, str]] = None):
    """Smoke-test an API key against ``/v1/models``.

    Returns ``(ok, detail)``.  Used by ``--verify`` and by the optional
    post-registration check.
    """
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(
            cfg.MODELS_URL, headers=headers, timeout=timeout, proxies=proxies
        )
    except requests.RequestException as exc:
        return False, f"unreachable: {exc}"
    if response.status_code != 200:
        return False, f"HTTP {response.status_code}: {response.text[:120]}"
    try:
        models = [item.get("id") for item in (response.json() or {}).get("data", [])]
    except ValueError:
        return False, "malformed JSON from /v1/models"
    return True, ", ".join(m for m in models if m) or "(no models listed)"
