"""Test fixtures: isolated SQLite DB per test, in-process mock receiver wired via httpx ASGITransport."""

from __future__ import annotations

import dataclasses
import os
import tempfile
from collections.abc import AsyncGenerator, Callable, Iterator

# Must happen before ``app`` is imported: never touch ./data during tests.
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/unused.db")

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import config, database, service
from app.database import get_db
from app.http_client import get_http_client
from app.main import create_app
from app.mock_receiver import receiver_app
from app.mock_receiver import state as receiver_state

RECEIVER_URL = "http://receiver.test/callback"


@pytest.fixture()
def db_session_factory(tmp_path) -> Iterator[sessionmaker]:
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False}, future=True)
    database.init_db(engine)  # create_all + migrations, exactly like app startup
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    yield factory
    engine.dispose()


def _reset_receiver() -> None:
    receiver_state["mode"] = 200
    receiver_state["received"].clear()
    receiver_state["signature_secret"] = None
    receiver_state["signature_tolerance"] = 300


@pytest.fixture(autouse=True)
def _clean_process_state() -> Iterator[None]:
    service.credential_cache.clear()
    yield
    service.credential_cache.clear()


@pytest.fixture()
def receiver() -> Iterator[dict]:
    _reset_receiver()
    yield receiver_state
    _reset_receiver()


@pytest.fixture()
def configure(monkeypatch) -> Callable[..., config.Settings]:
    """Swap runtime settings for one test: ``configure(signing_enabled=True, signing_secret="x")``."""

    def _apply(**changes) -> config.Settings:
        new = dataclasses.replace(config.settings, **changes)
        monkeypatch.setattr(config, "settings", new)
        return new

    return _apply


SIGNING_SECRET = "test-only-signing-secret"


@pytest.fixture()
def signing_on(configure, receiver) -> str:
    """Server-side signing enabled + receiver verifying with the same synthetic secret."""
    configure(signing_enabled=True, signing_secret=SIGNING_SECRET)
    receiver["signature_secret"] = SIGNING_SECRET
    return SIGNING_SECRET


@pytest.fixture()
def client(db_session_factory, receiver, monkeypatch) -> Iterator[TestClient]:
    # Point the app's engine at the temp DB as well (used by init_db in lifespan).
    monkeypatch.setattr(database, "engine", db_session_factory.kw["bind"])
    monkeypatch.setattr(database, "SessionLocal", db_session_factory)

    app = create_app()

    def override_get_db():
        db = db_session_factory()
        try:
            yield db
        finally:
            db.close()

    async def override_http_client() -> AsyncGenerator[httpx.AsyncClient, None]:
        # Requests to the "partner" go straight into the mock receiver ASGI app — no sockets involved.
        transport = httpx.ASGITransport(app=receiver_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://receiver.test") as c:
            yield c

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_http_client] = override_http_client

    with TestClient(app) as tc:
        yield tc
