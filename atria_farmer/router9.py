"""Optional 9router integration.

9router keeps its provider registry in a SQLite database.  When the operator
points ``ATRIA_9ROUTER_DB`` at one, every freshly minted API key is registered
there as an ``openai-compatible`` connection so the new accounts are usable
immediately.

The integration is strictly best-effort: a missing database, a schema change,
or a permissions problem must never abort a farming run.  Every failure is
logged and the account is still saved to ``keys.txt``.

Schema notes
------------
Two tables are touched::

    providerNodes        (id, type, name, data, createdAt, updatedAt)
    providerConnections (id, provider, authType, name, email, priority,
                         isActive, data, createdAt, updatedAt)

``providerNodes.name`` is used as the lookup key, so re-running the script
reuses the existing node instead of creating duplicates.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import config as cfg

NODE_NAME = "Atria"
NODE_TYPE = "openai-compatible"
MODEL_ID = "Atria-Dawn-Preview"


class Router9:
    """Best-effort injector for a local 9router instance."""

    def __init__(self, db_path: Optional[str], log=None) -> None:
        self.db_path = db_path
        self._log = log or (lambda *_: None)
        self._lock = threading.Lock()
        self._node_id: Optional[str] = None
        self.enabled = bool(db_path) and Path(db_path).is_file()

    # ── node ────────────────────────────────────────────────────────────────
    def ensure_node(self) -> Optional[str]:
        """Create or reuse the ``Atria`` provider node; return its id."""
        if not self.enabled:
            return None
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT id FROM providerNodes WHERE name = ?", (NODE_NAME,)
                ).fetchone()
                if row:
                    self._node_id = row[0]
                    return self._node_id

                node_id = f"{NODE_TYPE}-chat-{uuid.uuid4()}"
                now = _now()
                payload = json.dumps(
                    {"prefix": "atria", "apiType": "chat", "baseUrl": cfg.API_BASE + "/v1"}
                )
                conn.execute(
                    "INSERT INTO providerNodes (id, type, name, data, createdAt, updatedAt)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (node_id, NODE_TYPE, NODE_NAME, payload, now, now),
                )
                conn.commit()
                self._node_id = node_id
                return node_id
        except sqlite3.Error as exc:
            self._log(f"9router: could not prepare node ({exc}) — skipping injection")
            self.enabled = False
            return None

    # ── connection ──────────────────────────────────────────────────────────
    def inject(self, *, email: str, api_key: str) -> bool:
        """Register ``api_key`` as a connection.  Returns ``True`` on success."""
        if not self.enabled:
            return False
        node_id = self._node_id or self.ensure_node()
        if not node_id:
            return False

        try:
            with self._lock, self._connect() as conn:
                exists = conn.execute(
                    "SELECT id FROM providerConnections WHERE data LIKE ?",
                    (f"%{api_key}%",),
                ).fetchone()
                if exists:
                    return True

                now = _now()
                tag = email.split("@")[0]
                conn_data = json.dumps(
                    {
                        "apiKey": api_key,
                        "testStatus": "active",
                        "providerSpecificData": {
                            "prefix": "atria",
                            "apiType": "chat",
                            "baseUrl": cfg.API_BASE + "/v1",
                            "nodeName": NODE_NAME,
                            "connectionProxyEnabled": False,
                            "connectionProxyUrl": "",
                            "connectionNoProxy": "",
                            "enabledModels": [MODEL_ID],
                        },
                        "errorCode": None,
                        "backoffLevel": 0,
                        "lastError": None,
                        "lastErrorAt": None,
                        "rateLimitedUntil": None,
                    }
                )
                conn.execute(
                    "INSERT INTO providerConnections"
                    " (id, provider, authType, name, email, priority, isActive,"
                    "  data, createdAt, updatedAt)"
                    " VALUES (?, ?, 'apikey', ?, ?, 1, 1, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        node_id,
                        f"{NODE_NAME} ({tag})",
                        email,
                        conn_data,
                        now,
                        now,
                    ),
                )
                conn.commit()
                return True
        except sqlite3.Error as exc:
            self._log(f"9router: injection failed for {email} ({exc})")
            return False

    # ── internals ───────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        assert self.db_path is not None
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def describe(self) -> str:
        if not self.db_path:
            return "(disabled)"
        if not self.enabled:
            return f"{self.db_path} (not writable / missing)"
        return self.db_path


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
