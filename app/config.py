"""Application settings, loaded from environment variables.

``settings`` is read at call time by the rest of the app (``config.settings``), so tests can swap
it with :func:`dataclasses.replace`. Secrets (the signing secret) live only here, in process
memory: they are excluded from ``repr`` and never written to the database.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

# Secure defaults: always treated as sensitive, whatever SENSITIVE_HEADERS says.
DEFAULT_SENSITIVE_HEADERS: tuple[str, ...] = (
    "Authorization",
    "Proxy-Authorization",
    "Cookie",
    "Set-Cookie",
    "X-API-Key",
    "X-Auth-Token",
    "X-Access-Token",
    "X-Internal-Token",
)

DEFAULT_SIGNATURE_HEADER = "X-Callback-Signature"
DEFAULT_TIMESTAMP_HEADER = "X-Callback-Timestamp"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


class ConfigurationError(RuntimeError):
    """Raised at startup when the runtime configuration is unsafe or inconsistent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _parse_bool(name: str, raw: str | None) -> bool:
    value = (raw or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigurationError("CONFIGURATION_ERROR", f"{name} must be true/false, got {raw!r}")


def _parse_header_list(raw: str | None) -> tuple[str, ...]:
    return tuple(h.strip() for h in (raw or "").split(",") if h.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str = "sqlite:///./data/callback_inspector.db"
    default_timeout: float = 10.0
    max_timeout: float = 60.0
    max_body_chars: int = 65536
    # Pre-filled in the web form so the demo works out of the box.
    demo_destination: str = "http://receiver:8001/callback"

    # Extra sensitive header names; they EXTEND the defaults, never replace them.
    extra_sensitive_headers: tuple[str, ...] = ()

    signing_enabled: bool = False
    signing_secret: str = field(default="", repr=False)
    signature_header: str = DEFAULT_SIGNATURE_HEADER
    timestamp_header: str = DEFAULT_TIMESTAMP_HEADER

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            database_url=env.get("DATABASE_URL") or cls.database_url,
            default_timeout=float(env.get("DEFAULT_TIMEOUT_SECONDS") or cls.default_timeout),
            max_timeout=float(env.get("MAX_TIMEOUT_SECONDS") or cls.max_timeout),
            max_body_chars=int(env.get("MAX_STORED_BODY_CHARS") or cls.max_body_chars),
            demo_destination=env.get("DEMO_DESTINATION_URL") or cls.demo_destination,
            extra_sensitive_headers=_parse_header_list(env.get("SENSITIVE_HEADERS")),
            signing_enabled=_parse_bool("CALLBACK_SIGNING_ENABLED", env.get("CALLBACK_SIGNING_ENABLED")),
            signing_secret=(env.get("CALLBACK_SIGNING_SECRET") or "").strip(),
            signature_header=(env.get("CALLBACK_SIGNATURE_HEADER") or "").strip() or DEFAULT_SIGNATURE_HEADER,
            timestamp_header=(env.get("CALLBACK_TIMESTAMP_HEADER") or "").strip() or DEFAULT_TIMESTAMP_HEADER,
        )

    @property
    def sensitive_headers(self) -> frozenset[str]:
        """Lower-cased sensitive header names: secure defaults + SENSITIVE_HEADERS."""
        return frozenset(h.lower() for h in (*DEFAULT_SENSITIVE_HEADERS, *self.extra_sensitive_headers))

    def validate(self) -> None:
        """Fail fast on unsafe configuration. Called when the app is created."""
        if self.signing_enabled and not self.signing_secret:
            raise ConfigurationError(
                "SIGNING_CONFIGURATION_ERROR",
                "CALLBACK_SIGNING_ENABLED=true but CALLBACK_SIGNING_SECRET is empty",
            )
        if self.signing_enabled and self.signature_header.lower() == self.timestamp_header.lower():
            raise ConfigurationError(
                "SIGNING_CONFIGURATION_ERROR",
                "CALLBACK_SIGNATURE_HEADER and CALLBACK_TIMESTAMP_HEADER must differ",
            )


settings = Settings.from_env()
