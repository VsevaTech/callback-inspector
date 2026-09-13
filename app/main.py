"""Callback Inspector — FastAPI application entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.database import init_db
from app.web import router as web_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="Callback Inspector",
        version="0.1.0",
        description="Outbound callback proxy with full delivery history and manual retry.",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.get("/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
