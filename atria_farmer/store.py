"""Thread-safe result storage.

Two artefacts are produced while farming:

``keys.txt``
    One human-readable line per account, appended in the same format the
    original script used so existing tooling keeps working.

``keys.jsonl``
    The same records as JSON Lines, which is far easier to post-process
    (import into a database, diff two runs, feed another tool).

Both files are opened in append mode and guarded by a lock, so any number of
worker threads can write to them safely.  Writes are flushed and ``fsync``-ed
immediately — losing an account because the process was killed is worse than
paying for the extra syscall.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from .logto import Account


class ResultStore:
    """Append-only writer for account credentials."""

    def __init__(self, keys_file: str) -> None:
        self.text_path = Path(keys_file)
        self.json_path = self.text_path.with_suffix(".jsonl")
        self._lock = threading.Lock()
        self.text_path.parent.mkdir(parents=True, exist_ok=True)

    # ── writing ─────────────────────────────────────────────────────────────
    def append(self, account: Account) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        record = {
            "email": account.email,
            "api_key": account.api_key,
            "created_at": stamp,
            "injected_9router": account.injected,
            "quota": "100M_TOKENS",
        }
        with self._lock:
            self._append_text(account.as_row(stamp))
            self._append_json(record)

    def _append_text(self, line: str) -> None:
        with open(self.text_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _append_json(self, record: dict) -> None:
        with open(self.json_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # ── reading ─────────────────────────────────────────────────────────────
    def load_records(self) -> list:
        """Return every stored account as a ``(email, api_key)`` tuple.

        Falls back to the JSON Lines file when the text file is absent, so a
        ledger written by a different tool is still usable.
        """
        records = []
        if self.text_path.is_file():
            for line in self.text_path.read_text(encoding="utf-8").splitlines():
                parts = [part.strip() for part in line.split("|")]
                if len(parts) >= 3 and "@" in parts[1]:
                    records.append((parts[1], parts[2]))
            if records:
                return records

        if self.json_path.is_file():
            for line in self.json_path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if item.get("email") and item.get("api_key"):
                    records.append((item["email"], item["api_key"]))
        return records

    def load_emails(self) -> set:
        """Return every e-mail address already recorded in ``keys.txt``.

        Used to skip work when a run is resumed after a crash.
        """
        return {email.lower() for email, _ in self.load_records()}

    def count(self) -> int:
        """Number of accounts already stored."""
        return len(self.load_records())

    def describe(self) -> str:
        return f"{self.text_path} (+{self.json_path.name})"


def append_failure(path: str, email: str, reason: str) -> None:
    """Record a failed attempt so it can be inspected or retried later."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{stamp} | {email} | {reason}\n")


def summarise(succeeded: int, failures: int, elapsed: float) -> str:
    """Render the final run report."""
    lines = [
        "",
        "=" * 72,
        "RUN COMPLETE",
        "=" * 72,
        f"  accounts created : {succeeded}",
        f"  attempts failed  : {failures}",
        f"  elapsed          : {elapsed:.1f}s ({elapsed / 60:.1f} min)",
    ]
    if succeeded:
        lines.append(f"  average per account: {elapsed / succeeded:.1f}s")
        lines.append(f"  quota unlocked     : {succeeded * 100:,}M tokens")
    lines.append("=" * 72)
    return "\n".join(lines)
