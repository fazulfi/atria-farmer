"""Optional 9router integration.

9router exposes an HTTP API on localhost, so freshly minted API keys are
registered as provider connections over HTTP rather than by writing to its
SQLite file.  That choice matters:

* no filesystem permissions to arrange, and no risk of corrupting a database
  that a live service has open;
* the running service picks the new connection up immediately;
* the API validates each key before accepting it, so a bad key is rejected at
  the source instead of silently landing in the pool.

The integration is strictly best-effort.  A missing service, a bad password, or
a schema change must never abort a farming run: every failure is logged and the
account is still saved to ``keys.txt``.

Authentication quirk
--------------------
9router issues its session cookie with the ``Secure`` attribute.  Python's
``requests`` therefore refuses to send it back over plain HTTP — the login call
succeeds and every subsequent request answers ``401``.  The fix is to pass the
token explicitly as a cookie on each request, which is what this client does.
"""

from __future__ import annotations

import threading
from typing import Optional

import requests

DEFAULT_URL = "http://127.0.0.1:20228"
DEFAULT_NODE_NAME = "Atria"
DEFAULT_NODE_PREFIX = "atr"
NODE_TYPE = "openai-compatible"
MODEL_ID = "Atria-Dawn-Preview"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class Router9:
    """Best-effort injector for a local 9router instance."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_URL,
        password: Optional[str] = None,
        node_name: str = DEFAULT_NODE_NAME,
        api_base: str = "https://api.atria-asi.ai/v1",
        log=None,
    ) -> None:
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        self.password = password
        self.node_name = node_name
        self.api_base = api_base
        self._log = log or (lambda *_: None)
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._node_id: Optional[str] = None
        self._names: Optional[set] = None
        self.enabled = bool(password)

        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": BROWSER_UA, "Content-Type": "application/json"}
        )

    # ── transport ───────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        return {"User-Agent": BROWSER_UA, "Content-Type": "application/json"}

    def _cookies(self) -> dict:
        return {"auth_token": self._token} if self._token else {}

    def _request(self, method: str, path: str, **kwargs):
        return self._session.request(
            method,
            f"{self.base_url}{path}",
            cookies=self._cookies(),
            headers=self._headers(),
            timeout=30,
            **kwargs,
        )

    # ── auth ────────────────────────────────────────────────────────────────
    def login(self) -> bool:
        """Authenticate and cache the session token."""
        if not self.enabled:
            return False
        try:
            response = self._session.post(
                f"{self.base_url}/api/auth/login",
                json={"password": self.password},
                headers=self._headers(),
                timeout=20,
            )
        except requests.RequestException as exc:
            self._log(f"9router: unreachable at {self.base_url} ({exc})")
            self.enabled = False
            return False

        if response.status_code != 200:
            self._log(f"9router: login failed (HTTP {response.status_code})")
            self.enabled = False
            return False

        token = self._session.cookies.get("auth_token")
        if not token:
            self._log("9router: login returned no auth cookie")
            self.enabled = False
            return False

        self._token = token
        return True

    def _ensure_login(self) -> bool:
        return bool(self._token) or self.login()

    # ── node ────────────────────────────────────────────────────────────────
    def ensure_node(self) -> Optional[str]:
        """Return the id of the provider node, creating it when absent.

        Reuse is matched on ``baseUrl`` first so an existing node is adopted
        even if it was created under a different name.
        """
        if not self._ensure_login():
            return None
        if self._node_id:
            return self._node_id

        with self._lock:
            if self._node_id:
                return self._node_id
            try:
                response = self._request("GET", "/api/provider-nodes")
                nodes = response.json().get("nodes", [])
            except Exception as exc:
                self._log(f"9router: could not list provider nodes ({exc})")
                return None

            for node in nodes:
                if node.get("baseUrl", "").rstrip("/") == self.api_base.rstrip("/"):
                    self._node_id = node["id"]
                    self._log(f"9router: reusing node {node.get('name')!r}")
                    return self._node_id
                if node.get("name") == self.node_name:
                    self._node_id = node["id"]
                    self._log(f"9router: reusing node {node.get('name')!r}")
                    return self._node_id

            try:
                response = self._request(
                    "POST",
                    "/api/provider-nodes",
                    json={
                        "name": self.node_name,
                        "prefix": DEFAULT_NODE_PREFIX,
                        "baseUrl": self.api_base,
                        "apiType": "chat",
                    },
                )
                if response.status_code in (200, 201):
                    self._node_id = response.json().get("node", {}).get("id")
                    self._log(f"9router: created node {self.node_name!r}")
                    return self._node_id
                self._log(
                    f"9router: node creation failed (HTTP {response.status_code}): "
                    f"{response.text[:120]}"
                )
            except Exception as exc:
                self._log(f"9router: node creation error ({exc})")
            return None

    # ── connections ─────────────────────────────────────────────────────────
    def validate(self, api_key: str) -> tuple:
        """Ask 9router to test the key.  Returns ``(valid, detail)``."""
        if not self._ensure_login():
            return False, "9router not reachable"
        try:
            response = self._request(
                "POST",
                "/api/providers/validate",
                json={"provider": self._node_id, "apiKey": api_key},
            )
            payload = response.json()
        except Exception as exc:
            return False, f"validate error: {exc}"
        return bool(payload.get("valid")), payload.get("error") or ""

    def existing_names(self) -> set:
        """Return the connection names already registered for this node.

        The API deliberately does not echo stored API keys back, so duplicate
        detection keys off the connection name (the e-mail local part).
        """
        if not self._ensure_login():
            return set()
        node_id = self._node_id or self.ensure_node()
        try:
            response = self._request("GET", "/api/providers")
            connections = response.json().get("connections", [])
        except Exception:
            return set()
        return {
            (c.get("name") or "").strip()
            for c in connections
            if c.get("provider") == node_id
        }

    def inject(self, *, email: str, api_key: str) -> bool:
        """Register ``api_key`` as a connection.  Returns ``True`` on success.

        Injection is idempotent: a connection whose name (the e-mail local
        part) is already registered is left alone, so re-running a harvest
        never produces duplicates.
        """
        if not self._ensure_login():
            return False
        node_id = self._node_id or self.ensure_node()
        if not node_id:
            return False

        tag = email.split("@")[0]

        with self._lock:
            if self._names is None:
                self._names = self._fetch_names(node_id)
            if tag in self._names:
                return True

            try:
                response = self._request(
                    "POST",
                    "/api/providers",
                    json={
                        "provider": node_id,
                        "apiKey": api_key,
                        "name": tag,
                        "authType": "apikey",
                        "isActive": True,
                    },
                )
            except requests.RequestException as exc:
                self._log(f"9router: inject failed for {email} ({exc})")
                return False

            if response.status_code in (200, 201):
                self._names.add(tag)
                return True

            # A non-2xx reply does not prove the connection was rejected.  The
            # router stores connections in SQLite, and a busy database can make
            # it answer 5xx *after* the row has already been committed.  Ask the
            # router what it actually holds before calling this a failure --
            # otherwise the ledger records a false negative and the key looks
            # missing when it is in fact live.
            self._names = self._fetch_names(node_id)
            if tag in self._names:
                return True

        self._log(
            f"9router: inject failed for {email} "
            f"(HTTP {response.status_code}: {response.text[:100]})"
        )
        return False

    def _fetch_names(self, node_id: str) -> set:
        try:
            connections = self._request("GET", "/api/providers").json().get(
                "connections", []
            )
        except Exception:
            return set()
        return {
            (c.get("name") or "").strip()
            for c in connections
            if c.get("provider") == node_id
        }

    # ── reporting ───────────────────────────────────────────────────────────
    def count(self) -> int:
        """Number of active connections on this node."""
        if not self._ensure_login():
            return 0
        node_id = self._node_id or self.ensure_node()
        try:
            connections = self._request("GET", "/api/providers").json().get(
                "connections", []
            )
        except Exception:
            return 0
        return sum(
            1
            for c in connections
            if c.get("provider") == node_id and c.get("isActive", True)
        )

    def describe(self) -> str:
        if not self.password:
            return "(disabled — no ATRIA_9ROUTER_PASSWORD)"
        if not self.enabled:
            return f"{self.base_url} (unreachable)"
        return f"{self.base_url} (node={self.node_name})"
