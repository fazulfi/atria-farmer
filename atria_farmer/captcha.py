"""Aliyun Captcha 2.0 solver backed by 2captcha's ``AlibabaTask``.

The Atria sign-in page embeds Aliyun Captcha 2.0 and requires a valid
``captchaToken`` before it will send a registration OTP.  Getting that token
is the single most fragile step of the whole pipeline, and three details cost
a lot of debugging time:

``region``
    Must match the tenant configuration.  Atria uses ``cn``.  Solving with
    ``sgp`` *succeeds* — 2captcha returns a well-formed token — but the
    backend silently refuses it (``{"success": false}``).  ``intl`` is not
    solvable at all.

``failover``
    The returned solution object must be serialised verbatim.  The backend
    parses the token and rejects it outright if a ``failover`` key is present,
    so never "clean up" or extend the payload.

transport
    Solving for the ``cn`` region through a European or UK proxy fails with
    ``ERROR_CAPTCHA_UNSOLVABLE``.  This client therefore always talks to
    2captcha directly and lets the caller route the *registration* traffic
    through a proxy instead.

Acceptance is also probabilistic: roughly half of the tokens are rejected on
the first try even though they are structurally valid.  Callers must retry,
and every retry needs a fresh interaction because the token is bound to the
session that requested it.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from . import config as cfg


class CaptchaError(RuntimeError):
    """Raised when a captcha token could not be obtained."""


class AliyunSolver:
    """Minimal client for the 2captcha JSON API."""

    def __init__(
        self,
        api_key: str,
        *,
        scene_id: str,
        prefix: str,
        region: str = "cn",
        website_url: str,
        lib_url: str = cfg.CAPTCHA_LIB_URL,
        user_agent: str = cfg.DEFAULT_USER_AGENT,
        task_type: str = cfg.CAPTCHA_TASK_TYPE,
        api_base: str = cfg.CAPTCHA_API,
        poll_interval: float = 5.0,
        timeout: float = 240.0,
        log=None,
    ) -> None:
        self.api_key = api_key
        self.scene_id = scene_id
        self.prefix = prefix
        self.region = region
        self.website_url = website_url
        self.lib_url = lib_url
        self.user_agent = user_agent
        self.task_type = task_type
        self.api_base = api_base.rstrip("/")
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._log = log or (lambda *_: None)

    # ── low level ───────────────────────────────────────────────────────────
    def _call(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        request = urllib.request.Request(
            f"{self.api_base}/{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise CaptchaError(
                f"2captcha {endpoint} returned HTTP {exc.code}"
            ) from exc
        except urllib.error.URLError as exc:
            raise CaptchaError(f"2captcha unreachable: {exc.reason}") from exc

    # ── public API ──────────────────────────────────────────────────────────
    def balance(self) -> Optional[float]:
        """Return the account balance in USD, or ``None`` when unavailable."""
        try:
            data = self._call("getBalance", {"clientKey": self.api_key})
        except CaptchaError:
            return None
        if data.get("errorId"):
            return None
        return data.get("balance")

    def solve(self) -> Dict[str, Any]:
        """Create a task and block until 2captcha returns a solution.

        Returns the raw ``solution`` object.  Serialise it with
        :func:`token_from_solution` before sending it to the backend.
        """
        created = self._call(
            "createTask",
            {
                "clientKey": self.api_key,
                "task": {
                    "type": self.task_type,
                    "websiteUrl": self.website_url,
                    "sceneId": self.scene_id,
                    "prefix": self.prefix,
                    "region": self.region,
                    "apiGetLib": self.lib_url,
                    "userAgent": self.user_agent,
                },
            },
        )
        if created.get("errorId"):
            raise CaptchaError(
                "createTask failed: "
                f"{created.get('errorCode')} {created.get('errorDescription', '')}".strip()
            )
        task_id = created.get("taskId")
        if not task_id:
            raise CaptchaError(f"createTask returned no taskId: {created!r}")

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            time.sleep(self.poll_interval)
            result = self._call(
                "getTaskResult", {"clientKey": self.api_key, "taskId": task_id}
            )
            if result.get("errorId"):
                raise CaptchaError(
                    "solve failed: "
                    f"{result.get('errorCode')} {result.get('errorDescription', '')}".strip()
                )
            status = result.get("status")
            if status == "ready":
                solution = result.get("solution")
                if not isinstance(solution, dict):
                    raise CaptchaError(f"unexpected solution payload: {solution!r}")
                return solution
            if status == "processing":
                continue
        raise CaptchaError(f"timed out after {self.timeout:.0f}s waiting for a token")


def token_from_solution(solution: Dict[str, Any]) -> str:
    """Serialise a solution object into the ``captchaToken`` wire format.

    The backend hashes / parses this string and refuses anything it did not
    produce itself, so the only safe transformation is *none*: compact JSON,
    no added or removed keys.
    """
    return json.dumps(solution, separators=(",", ":"), ensure_ascii=False)
