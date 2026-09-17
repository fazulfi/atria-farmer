"""Ledger operations: bulk verification, model discovery, blocklist checks.

These are the maintenance commands an operator reaches for after a harvest —
"are my keys still alive", "what models can I call", "is this domain even
allowed".  They are deliberately separate from :mod:`atria_farmer.worker`:
nothing here registers anything, so a mistake costs a request rather than an
account.
"""

from __future__ import annotations

import concurrent.futures
import json
import re
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional

import requests

from . import config as cfg


# ── blocklist ────────────────────────────────────────────────────────────────
_POLICY_RE = re.compile(r'"emailBlocklistPolicy":\s*(\{.*?\})\s*,\s*"', re.S)


class BlocklistError(RuntimeError):
    """The blocklist policy could not be read from the sign-in page."""


def fetch_blocklist(*, user_agent: str = cfg.DEFAULT_USER_AGENT,
                    timeout: int = 30) -> List[str]:
    """Return the platform's live e-mail blocklist.

    The policy ships with the sign-in page as ``emailBlocklistPolicy``.  Read
    it fresh rather than caching: it is the platform's own list and it changes.
    """
    try:
        response = requests.get(
            cfg.AUTH_BASE + "/sign-in",
            params={"app_id": cfg.DEFAULT_APP_ID},
            headers={"User-Agent": user_agent},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise BlocklistError(f"could not reach the sign-in page: {exc}") from exc

    match = _POLICY_RE.search(response.text)
    if not match:
        raise BlocklistError("blocklist policy not present in the sign-in page")
    try:
        policy = json.loads(match.group(1))
    except ValueError as exc:
        raise BlocklistError(f"blocklist policy is not valid JSON: {exc}") from exc
    return list(policy.get("customBlocklist") or [])


def match_blocklist(domain: str, blocklist: List[str]) -> Optional[str]:
    """Return the blocklist entry that covers ``domain``, if any.

    Entries are either an exact address (``@example.com``) or a wildcard
    suffix (``@*.example.com``), so a plain substring test is not enough —
    ``biz.id`` must not match ``notbiz.id``.
    """
    domain = domain.lower().lstrip("@")
    for entry in blocklist:
        pattern = entry.lower().lstrip("@")
        if pattern.startswith("*."):
            suffix = pattern[2:]
            if domain == suffix or domain.endswith("." + suffix):
                return entry
        elif pattern == domain:
            return entry
    return None


# ── models ───────────────────────────────────────────────────────────────────
def scan_models(api_key: str, *, timeout: int = 30,
                proxies: Optional[Dict[str, str]] = None) -> List[str]:
    """Return the model ids available to ``api_key``."""
    try:
        response = requests.get(
            cfg.MODELS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            proxies=proxies,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"models request failed: {exc}") from exc

    if response.status_code != 200:
        raise RuntimeError(
            f"models request returned HTTP {response.status_code}: {response.text[:120]}"
        )
    payload = response.json() or {}
    return [item.get("id") for item in payload.get("data", []) if item.get("id")]


# ── bulk verification ────────────────────────────────────────────────────────
@dataclass
class KeyStatus:
    """Result of probing one API key.

    ``state`` distinguishes the three outcomes that matter:

    ``active``
        The key answered with a completion.
    ``dead``
        The platform explicitly refused the key (auth error) — it is gone.
    ``unknown``
        The request never completed (timeout, DNS, proxy).  A network failure
        is *not* evidence that a key is dead, and treating it as such would
        throw away working keys.
    """

    email: str
    api_key: str
    state: str
    detail: str
    rate_limit: Optional[str] = None

    @property
    def alive(self) -> bool:
        return self.state != "dead"


def verify_key(email: str, api_key: str, *, timeout: int = 30,
               proxies: Optional[Dict[str, str]] = None,
               attempts: int = 3) -> KeyStatus:
    """Probe a single key with a one-token chat request.

    ``/v1/models`` would be cheaper, but a completion is what actually proves
    the key can be used — a key can list models and still be refused on
    generation.  The response also carries ``x-rpm-remaining``, which is the
    only quota signal the API exposes.

    Network failures are retried before being reported, because a single
    dropped connection says nothing about the key.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": "Atria-Dawn-Preview",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }

    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            response = requests.post(
                cfg.CHAT_URL, headers=headers, json=body, timeout=timeout,
                proxies=proxies,
            )
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < attempts:
                time.sleep(1.5 * attempt)
                continue
            return KeyStatus(email, api_key, "unknown", f"unreachable ({last_error})")

        limit = response.headers.get("x-rpm-remaining")
        rpm = f"{limit}/{response.headers.get('x-rpm-limit', '?')}" if limit else None

        if response.status_code == 200:
            try:
                ok = bool((response.json() or {}).get("choices"))
            except ValueError:
                ok = False
            return KeyStatus(email, api_key,
                             "active" if ok else "unknown",
                             "active" if ok else "malformed response", rpm)

        detail = f"HTTP {response.status_code}"
        try:
            message = ((response.json() or {}).get("error") or {}).get("message")
            if message:
                detail = f"{detail}: {message[:80]}"
        except ValueError:
            pass

        # 5xx and 429 are transient; only 4xx auth/quota answers are definitive.
        if response.status_code >= 500 or response.status_code == 429:
            last_error = detail
            if attempt < attempts:
                time.sleep(1.5 * attempt)
                continue
            return KeyStatus(email, api_key, "unknown", f"transient ({detail})", rpm)

        lowered = detail.lower()
        # A quota-exhausted key is still a valid key.
        if any(word in lowered for word in ("quota", "limit", "exhausted", "credit")):
            return KeyStatus(email, api_key, "active", f"quota exhausted ({detail})", rpm)
        return KeyStatus(email, api_key, "dead", detail, rpm)

    return KeyStatus(email, api_key, "unknown", f"unreachable ({last_error})")


def verify_many(records: List[tuple], *, workers: int = 8, timeout: int = 30,
                proxies: Optional[Dict[str, str]] = None, log=None) -> List[KeyStatus]:
    """Verify every key in ``records`` (a list of ``(email, api_key)``)."""
    log = log or (lambda *_: None)
    results: List[KeyStatus] = []
    lock = threading.Lock()

    def work(item):
        status = verify_key(item[0], item[1], timeout=timeout, proxies=proxies)
        with lock:
            results.append(status)
            mark = {"active": "OK  ", "dead": "DEAD", "unknown": "?   "}[status.state]
            log(f"{mark} {status.email} — {status.detail[:90]}")
        return status

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, records))
    return results


def summarise_verification(results: List[KeyStatus]) -> str:
    """Render a short report for a verification run.

    ``unknown`` is reported separately from ``dead`` on purpose: a timeout is
    not proof that a key has been revoked, and conflating the two would have
    the operator discard working keys.
    """
    active = [r for r in results if r.state == "active"]
    dead = [r for r in results if r.state == "dead"]
    unknown = [r for r in results if r.state == "unknown"]

    lines = [
        "",
        f"  checked   : {len(results)}",
        f"  active    : {len(active)}",
        f"  dead      : {len(dead)}",
    ]
    if unknown:
        lines.append(f"  unknown   : {len(unknown)}  (network/transient — not counted as dead)")

    if dead:
        lines.append("  dead keys:")
        for item in dead[:20]:
            lines.append(f"    {item.email:<40} {item.detail}")
        if len(dead) > 20:
            lines.append(f"    … and {len(dead) - 20} more")

    if unknown:
        lines.append("  retry these (network):")
        for item in unknown[:10]:
            lines.append(f"    {item.email:<40} {item.detail[:70]}")

    rpm = next((r.rate_limit for r in results if r.rate_limit), None)
    if rpm:
        lines.append(f"  rate limit (last response): {rpm} requests/min remaining")
    return "\n".join(lines)
