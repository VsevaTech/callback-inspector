"""Application settings, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./data/callback_inspector.db")
    default_timeout: float = float(os.getenv("DEFAULT_TIMEOUT_SECONDS", "10"))
    max_timeout: float = float(os.getenv("MAX_TIMEOUT_SECONDS", "60"))
    max_body_chars: int = int(os.getenv("MAX_STORED_BODY_CHARS", "65536"))
    # Pre-filled in the web form so the demo works out of the box.
    demo_destination: str = os.getenv("DEMO_DESTINATION_URL", "http://receiver:8001/callback")


settings = Settings()
