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
    """Result of probing one API key."""

    email: str
    api_key: str
    alive: bool
    detail: str
    rate_limit: Optional[str] = None


def verify_key(email: str, api_key: str, *, timeout: int = 30,
               proxies: Optional[Dict[str, str]] = None) -> KeyStatus:
    """Probe a single key with a one-token chat request.

    ``/v1/models`` would be cheaper, but a completion is what actually proves
    the key can be used — a key can list models and still be refused on
    generation.  The response also carries ``x-rpm-remaining``, which is the
    only quota signal the API exposes.
    """
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {
        "model": "Atria-Dawn-Preview",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
    }
    try:
        response = requests.post(
            cfg.CHAT_URL, headers=headers, json=body, timeout=timeout, proxies=proxies
        )
    except requests.RequestException as exc:
        return KeyStatus(email, api_key, False, f"unreachable: {exc}")

    limit = response.headers.get("x-rpm-remaining")
    rpm = f"{limit}/{response.headers.get('x-rpm-limit', '?')}" if limit else None

    if response.status_code == 200:
        try:
            ok = bool((response.json() or {}).get("choices"))
        except ValueError:
            ok = False
        return KeyStatus(email, api_key, ok,
                         "active" if ok else "malformed response", rpm)

    detail = f"HTTP {response.status_code}"
    try:
        message = ((response.json() or {}).get("error") or {}).get("message")
        if message:
            detail = f"{detail}: {message[:80]}"
    except ValueError:
        pass

    lowered = detail.lower()
    # A quota-exhausted key is still a valid key.
    alive = any(word in lowered for word in ("quota", "limit", "exhausted", "credit"))
    return KeyStatus(email, api_key, alive, detail, rpm)


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
            log(f"{'OK  ' if status.alive else 'DEAD'} {status.email} — {status.detail}")
        return status

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, records))
    return results


def summarise_verification(results: List[KeyStatus]) -> str:
    """Render a short report for a verification run."""
    alive = [r for r in results if r.alive]
    dead = [r for r in results if not r.alive]
    lines = [
        "",
        f"  checked : {len(results)}",
        f"  alive   : {len(alive)}",
        f"  dead    : {len(dead)}",
    ]
    if dead:
        lines.append("  dead keys:")
        for item in dead[:20]:
            lines.append(f"    {item.email:<40} {item.detail}")
        if len(dead) > 20:
            lines.append(f"    … and {len(dead) - 20} more")
    rpm = next((r.rate_limit for r in results if r.rate_limit), None)
    if rpm:
        lines.append(f"  rate limit (last response): {rpm} requests/min remaining")
    return "\n".join(lines)
