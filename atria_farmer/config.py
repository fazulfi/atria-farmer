"""Configuration loading, endpoint constants, and validation.

All operator-specific values (API keys, proxy credentials, worker URL) are
read from the process environment or from a local ``.env`` file that is
excluded from version control.  Anything that is public knowledge — the
sign-in URL, the captcha scene id, the API paths — lives here as a default so
a fresh clone only needs a handful of variables to run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# ── Public endpoints ─────────────────────────────────────────────────────────
AUTH_BASE = "https://auth.atria-asi.ai"
API_BASE = "https://api.atria-asi.ai"

CONSOLE_URL = f"{API_BASE}/console"
EXPERIENCE_URL = f"{AUTH_BASE}/api/experience"
CAPTCHA_VERIFY_URL = f"{AUTH_BASE}/api/experience/captcha/verify"
VERIFICATION_CODE_URL = f"{AUTH_BASE}/api/experience/verification/verification-code"
VERIFICATION_VERIFY_URL = f"{AUTH_BASE}/api/experience/verification/verification-code/verify"
PROFILE_URL = f"{AUTH_BASE}/api/experience/profile"
IDENTIFICATION_URL = f"{AUTH_BASE}/api/experience/identification"
SUBMIT_URL = f"{AUTH_BASE}/api/experience/submit"
KEYS_URL = f"{API_BASE}/api/keys"
MODELS_URL = f"{API_BASE}/v1/models"
CHAT_URL = f"{API_BASE}/v1/chat/completions"

# ── 2captcha ─────────────────────────────────────────────────────────────────
CAPTCHA_API = "https://api.2captcha.com"
CAPTCHA_TASK_TYPE = "AlibabaTask"
CAPTCHA_LIB_URL = (
    "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"
)

# ── Public defaults (visible in the sign-in page, not secrets) ───────────────
DEFAULT_APP_ID = "bldfnpl1bq5fekc85mcxi"
DEFAULT_CAPTCHA_SCENE = "1up68vza"
DEFAULT_CAPTCHA_PREFIX = "17qfjo"
DEFAULT_CAPTCHA_REGION = "cn"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
DEFAULT_TARGET = 100
DEFAULT_WORKERS = 5
DEFAULT_KEYS_FILE = "keys.txt"

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

_REQUIRED = (
    "ATRIA_CF_API_BASE",
    "ATRIA_CF_DOMAIN",
    "ATRIA_CAPTCHA_KEY",
)


class ConfigError(RuntimeError):
    """Raised when the configuration is missing or inconsistent."""


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Load a ``.env`` file without pulling in an extra dependency.

    Real environment variables always win, so secrets can be injected by the
    process manager in production while developers keep a local file.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Resolved runtime configuration."""

    # Required ─ mailbox
    cf_api_base: str
    cf_domain: str
    captcha_key: str

    # Optional ─ proxy
    proxy: Optional[str] = None

    # Optional ─ 9router
    ninerouter_db: Optional[str] = None

    # Tuning
    target: int = DEFAULT_TARGET
    workers: int = DEFAULT_WORKERS
    keys_file: str = DEFAULT_KEYS_FILE
    user_agent: str = DEFAULT_USER_AGENT

    app_id: str = DEFAULT_APP_ID
    captcha_scene: str = DEFAULT_CAPTCHA_SCENE
    captcha_prefix: str = DEFAULT_CAPTCHA_PREFIX
    captcha_region: str = DEFAULT_CAPTCHA_REGION
    captcha_lib: str = CAPTCHA_LIB_URL

    captcha_attempts: int = 4
    otp_timeout: int = 150
    otp_poll: int = 5
    http_timeout: int = 30
    request_pause: float = 0.0
    verbose: bool = False

    warnings: List[str] = field(default_factory=list)

    # ── construction ────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls, *, load_file: bool = True) -> "Config":
        if load_file:
            load_dotenv()

        missing = [name for name in _REQUIRED if not _env(name)]
        if missing:
            raise ConfigError(
                "missing required environment variable(s): "
                + ", ".join(missing)
                + "\nCopy .env.example to .env and fill it in "
                "(see docs/SETUP.md)."
            )

        cfg = cls(
            cf_api_base=(_env("ATRIA_CF_API_BASE") or "").rstrip("/"),
            cf_domain=_env("ATRIA_CF_DOMAIN") or "",
            captcha_key=_env("ATRIA_CAPTCHA_KEY") or "",
            proxy=_env("ATRIA_PROXY"),
            ninerouter_db=_env("ATRIA_9ROUTER_DB"),
            target=_env_int("ATRIA_TARGET", DEFAULT_TARGET),
            workers=_env_int("ATRIA_WORKERS", DEFAULT_WORKERS),
            keys_file=_env("ATRIA_KEYS_FILE", DEFAULT_KEYS_FILE) or DEFAULT_KEYS_FILE,
            user_agent=_env("ATRIA_USER_AGENT", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT,
            app_id=_env("ATRIA_APP_ID", DEFAULT_APP_ID) or DEFAULT_APP_ID,
            captcha_scene=_env("ATRIA_CAPTCHA_SCENE", DEFAULT_CAPTCHA_SCENE)
            or DEFAULT_CAPTCHA_SCENE,
            captcha_prefix=_env("ATRIA_CAPTCHA_PREFIX", DEFAULT_CAPTCHA_PREFIX)
            or DEFAULT_CAPTCHA_PREFIX,
            captcha_region=_env("ATRIA_CAPTCHA_REGION", DEFAULT_CAPTCHA_REGION)
            or DEFAULT_CAPTCHA_REGION,
            captcha_lib=_env("ATRIA_CAPTCHA_LIB", CAPTCHA_LIB_URL) or CAPTCHA_LIB_URL,
            captcha_attempts=_env_int("ATRIA_CAPTCHA_ATTEMPTS", 4),
            otp_timeout=_env_int("ATRIA_OTP_TIMEOUT", 150),
            otp_poll=_env_int("ATRIA_OTP_POLL", 5),
            http_timeout=_env_int("ATRIA_HTTP_TIMEOUT", 30),
            request_pause=float(_env("ATRIA_REQUEST_PAUSE", "0") or 0),
            verbose=_env_bool("ATRIA_VERBOSE", False),
        )
        cfg.validate()
        return cfg

    # ── validation ──────────────────────────────────────────────────────────
    def validate(self) -> None:
        if not self.cf_api_base.startswith("http"):
            raise ConfigError(
                "ATRIA_CF_API_BASE must be an http(s) URL, "
                f"got {self.cf_api_base!r}"
            )
        if "." not in self.cf_domain:
            raise ConfigError(
                f"ATRIA_CF_DOMAIN must look like a domain, got {self.cf_domain!r}"
            )
        if len(self.captcha_key) < 16:
            raise ConfigError("ATRIA_CAPTCHA_KEY looks too short to be valid")
        if self.target < 1:
            raise ConfigError("ATRIA_TARGET must be >= 1")
        if not 1 <= self.workers <= 50:
            raise ConfigError("ATRIA_WORKERS must be between 1 and 50")

        self.warnings.clear()
        if self.captcha_region != "cn":
            self.warnings.append(
                f"captcha region is {self.captcha_region!r}; the Atria tenant "
                "expects 'cn' and rejects tokens solved for other regions"
            )
        if not self.proxy:
            self.warnings.append(
                "no ATRIA_PROXY configured — OTP requests are rate limited to "
                "a few per IP per hour, so throughput will be very low"
            )
        if self.ninerouter_db and not Path(self.ninerouter_db).exists():
            self.warnings.append(
                f"ATRIA_9ROUTER_DB points at {self.ninerouter_db!r} which does "
                "not exist — 9router injection will be skipped"
            )

    # ── derived ─────────────────────────────────────────────────────────────
    @property
    def signin_url(self) -> str:
        return f"{AUTH_BASE}/sign-in?app_id={self.app_id}"

    @property
    def proxies(self) -> Optional[dict]:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    def redacted(self) -> dict:
        """Configuration summary safe to print to a log."""
        return {
            "mailbox_worker": self.cf_api_base,
            "mailbox_domain": self.cf_domain,
            "captcha_key": _mask(self.captcha_key),
            "captcha_region": self.captcha_region,
            "captcha_scene": self.captcha_scene,
            "proxy": _mask_url(self.proxy),
            "ninerouter_db": self.ninerouter_db or "(disabled)",
            "target": self.target,
            "workers": self.workers,
            "keys_file": self.keys_file,
        }


def _mask(secret: Optional[str], keep: int = 4) -> str:
    if not secret:
        return "(unset)"
    if len(secret) <= keep * 2:
        return "*" * len(secret)
    return f"{secret[:keep]}…{secret[-keep:]}"


def _mask_url(url: Optional[str]) -> str:
    """Strip credentials out of a proxy URL before logging it."""
    if not url:
        return "(disabled)"
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}" if scheme else f"***@{host}"
