"""Callback Inspector — FastAPI application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import router as api_router
from app.database import init_db
from app.errors import install_error_handlers
from app.web import router as web_router

DESCRIPTION = """
Outbound callback proxy with full delivery history and manual retry.

* **Persist before send** — the delivery and each attempt are committed before any network I/O.
* **Sensitive headers are delivered to the destination but redacted from stored evidence**
  (Authorization, Proxy-Authorization, Cookie, Set-Cookie, X-API-Key, X-Auth-Token, X-Access-Token,
  X-Internal-Token + `SENSITIVE_HEADERS`), in request and response headers alike.
* **Outbound callbacks may be HMAC-SHA256 signed when configured** (`CALLBACK_SIGNING_ENABLED`).
* **`Idempotency-Key`** on `POST /api/deliveries` prevents duplicate delivery creation.
"""

_log = logging.getLogger("callback_inspector")
_log.setLevel(logging.INFO)
if not _log.handlers:  # log identifiers and outcomes only — never headers, bodies or secrets
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _log.addHandler(_handler)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    # Fail fast: never start with signing enabled and an empty secret.
    config.settings.validate()

    app = FastAPI(
        title="Callback Inspector",
        version="0.2.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    app.include_router(api_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok", "signing": config.settings.signing_enabled}

    return app


app = create_app()
