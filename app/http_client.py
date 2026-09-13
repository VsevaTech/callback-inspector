"""Shared outbound httpx client (overridable in tests via FastAPI dependency overrides)."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx

from app.config import settings


async def get_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(follow_redirects=False, timeout=settings.default_timeout) as client:
        yield client
