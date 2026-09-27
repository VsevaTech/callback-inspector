"""SQLAlchemy engine / session wiring."""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app import config


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    connect_args = {}
    if url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        path = url.removeprefix("sqlite:///")
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return create_engine(url, connect_args=connect_args, future=True)


engine = _make_engine(config.settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def init_db(bind: Engine | None = None) -> None:
    """Create missing tables, then upgrade an existing database in place (see app/migrations.py)."""
    # Import models so they are registered on Base.metadata.
    from app import models  # noqa: F401
    from app.migrations import run_migrations

    target = bind if bind is not None else engine
    Base.metadata.create_all(bind=target)
    run_migrations(target)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
