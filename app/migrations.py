"""Minimal, idempotent schema migrations for the SQLite MVP (no Alembic).

``init_db`` runs ``Base.metadata.create_all`` (creates missing tables on a fresh DB) and then
``run_migrations``, which brings an *existing* ``callback_inspector.db`` from an older release up to
date. Every migration is safe to re-run and is recorded in ``schema_migrations``.

Adding a migration: append ``(name, fn)`` to ``MIGRATIONS``; never edit or reorder old ones.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import Connection

from app.security import redact_headers

log = logging.getLogger("callback_inspector.migrations")


def _add_column_if_missing(conn: Connection, table: str, column: str, ddl_type: str) -> None:
    columns = {c["name"] for c in inspect(conn).get_columns(table)}
    if column not in columns:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def _0001_security_columns(conn: Connection) -> None:
    _add_column_if_missing(conn, "callback_deliveries", "idempotency_key", "VARCHAR(255)")
    _add_column_if_missing(conn, "callback_deliveries", "request_fingerprint", "VARCHAR(64)")
    _add_column_if_missing(conn, "delivery_attempts", "signature_algorithm", "VARCHAR(32)")
    _add_column_if_missing(conn, "delivery_attempts", "signature_timestamp", "INTEGER")
    conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_callback_deliveries_idempotency_key "
            "ON callback_deliveries (idempotency_key)"
        )
    )


def _redact_json_column(conn: Connection, table: str, column: str) -> int:
    changed = 0
    rows = conn.execute(text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")).all()
    for row_id, raw in rows:
        value = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(value, dict):
            continue
        redacted = redact_headers(value)
        if redacted != value:
            conn.execute(
                text(f"UPDATE {table} SET {column} = :v WHERE id = :id"),
                {"v": json.dumps(redacted, ensure_ascii=False), "id": row_id},
            )
            changed += 1
    return changed


def _0002_redact_existing_evidence(conn: Connection) -> None:
    """Evidence written by older releases may hold raw credentials: redact it in place."""
    changed = sum(
        _redact_json_column(conn, table, column)
        for table, column in (
            ("callback_deliveries", "headers"),
            ("delivery_attempts", "request_headers"),
            ("delivery_attempts", "response_headers"),
        )
    )
    if changed:
        log.warning("redacted sensitive headers in %d existing evidence rows", changed)


MIGRATIONS: list[tuple[str, Callable[[Connection], None]]] = [
    ("0001_security_columns", _0001_security_columns),
    ("0002_redact_existing_evidence", _0002_redact_existing_evidence),
]


def run_migrations(engine: Engine) -> list[str]:
    applied_now: list[str] = []
    with engine.begin() as conn:
        conn.execute(
            text("CREATE TABLE IF NOT EXISTS schema_migrations (name VARCHAR(128) PRIMARY KEY, applied_at VARCHAR(40))")
        )
        done = {r[0] for r in conn.execute(text("SELECT name FROM schema_migrations"))}
        for name, fn in MIGRATIONS:
            if name in done:
                continue
            fn(conn)
            conn.execute(
                text("INSERT INTO schema_migrations (name, applied_at) VALUES (:n, :t)"),
                {"n": name, "t": datetime.now(UTC).isoformat()},
            )
            applied_now.append(name)
    if applied_now:
        log.info("applied migrations: %s", ", ".join(applied_now))
    if "0002_redact_existing_evidence" in applied_now and engine.dialect.name == "sqlite":
        # Rewrite the file so pre-redaction values do not linger in free pages.
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.execute(text("VACUUM"))
    return applied_now
