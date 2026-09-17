"""Client for the Cloudflare Email Worker that stores OTP e-mails.

The worker shipped in ``worker/`` writes every incoming message into a D1
table and exposes a deliberately tiny HTTP surface:

===========================  ==========================================
``GET /?email=<address>``    the OTP code, or ``404 NOT_FOUND``.
                             One-shot: the row is deleted on read.
``GET /recent``              the 30 most recent messages (debugging).
``GET /raw?email=<address>`` the full stored row (debugging).
===========================  ==========================================

Because reading consumes the code, the client is written to be polled exactly
once per successful read and to keep polling while the mailbox is still empty.
"""

from __future__ import annotations

import re
import time
from typing import Optional

import requests

#: Codes that show up in marketing footers far more often than as real OTPs.
JUNK_CODES = {
    "181818", "666666", "808080", "999999", "000000",
    "333333", "222222", "111111", "123456", "123123",
}

_CODE_RE = re.compile(r"\b(\d{4,8})\b")


class MailboxError(RuntimeError):
    """Raised when the mailbox cannot be reached."""


class Mailbox:
    """Thin wrapper around the OTP worker's HTTP API."""

    def __init__(self, base_url: str, *, timeout: int = 20, session: Optional[requests.Session] = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    # ── reads ───────────────────────────────────────────────────────────────
    def fetch(self, address: str) -> Optional[str]:
        """Return the OTP for ``address``, or ``None`` when it has not arrived.

        Reading is destructive: a successful read removes the message from the
        mailbox, which also prevents a stale code from being replayed.

        A dropped connection is retried once before giving up, because the
        Cloudflare Worker occasionally takes longer than the timeout on a cold
        start.
        """
        last_error = None
        for attempt in range(2):
            try:
                response = self.session.get(
                    f"{self.base_url}/", params={"email": address}, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1)
                    continue
                raise MailboxError(f"mailbox unreachable: {exc}") from exc

            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise MailboxError(
                    f"mailbox returned HTTP {response.status_code}: {response.text[:120]}"
                )
            code = response.text.strip()
            if not code or code.upper() == "NOT_FOUND":
                return None
            return _normalise_code(code)

        raise MailboxError(f"mailbox unreachable: {last_error}")

    def wait(self, address: str, *, timeout: int, poll: int = 5, log=None,
             max_errors: int = 6) -> Optional[str]:
        """Poll :meth:`fetch` until a code arrives or ``timeout`` elapses.

        Transient network errors are tolerated rather than fatal.  By the time
        polling starts the OTP has already been requested and the account is
        half-registered, so a single dropped connection must not throw that
        work away — the message is still sitting in the mailbox and the next
        poll will find it.  Only ``max_errors`` consecutive failures abort.
        """
        log = log or (lambda *_: None)
        deadline = time.monotonic() + timeout
        waited = 0
        errors = 0

        while time.monotonic() < deadline:
            time.sleep(poll)
            waited += poll
            try:
                code = self.fetch(address)
            except MailboxError as exc:
                errors += 1
                if errors >= max_errors:
                    raise
                log(f"mailbox poll failed ({errors}/{max_errors}), retrying: {exc}")
                continue
            errors = 0
            if code:
                log(f"OTP received after {waited}s")
                return code
        return None

    # ── diagnostics ─────────────────────────────────────────────────────────
    def recent(self, limit: int = 10) -> str:
        """Return the worker's recent-message listing (debug helper)."""
        try:
            response = self.session.get(f"{self.base_url}/recent", timeout=self.timeout)
        except requests.RequestException as exc:
            return f"(unreachable: {exc})"
        return response.text[:limit * 200]

    def raw(self, address: str, limit: int = 1200) -> str:
        """Return the stored row for ``address`` including the body snippet."""
        try:
            response = self.session.get(
                f"{self.base_url}/raw", params={"email": address}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            return f"(unreachable: {exc})"
        return response.text[:limit]

    def health(self) -> bool:
        """Return ``True`` when the worker answers with a well-formed response."""
        try:
            response = self.session.get(
                f"{self.base_url}/", params={"email": "healthcheck@invalid"}, timeout=self.timeout
            )
        except requests.RequestException:
            return False
        return response.status_code in (200, 404)


def _normalise_code(raw: str) -> Optional[str]:
    """Extract a code from a worker response that may be code or free text."""
    text = raw.strip()
    if not text:
        return None
    if text.isdigit() and 4 <= len(text) <= 8:
        return text
    for match in _CODE_RE.finditer(text):
        candidate = match.group(1)
        if candidate not in JUNK_CODES and int(candidate) != 0:
            return candidate
    return None
