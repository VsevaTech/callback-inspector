"""An existing v0.1 callback_inspector.db keeps working after the update."""

from __future__ import annotations

import json
import sqlite3

from sqlalchemy import create_engine, inspect

from app import database
from app.migrations import run_migrations
from app.security import REDACTED

# Schema exactly as created by v0.1.0 (Base.metadata.create_all on the old models).
LEGACY_DDL = """
CREATE TABLE callback_deliveries (
    id VARCHAR(32) NOT NULL, destination_url VARCHAR(2048) NOT NULL, method VARCHAR(10) NOT NULL,
    headers JSON NOT NULL, payload JSON, timeout_seconds FLOAT NOT NULL,
    status VARCHAR(9) NOT NULL, attempt_count INTEGER NOT NULL, last_http_status INTEGER, last_error TEXT,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, delivered_at DATETIME,
    PRIMARY KEY (id)
);
CREATE INDEX ix_callback_deliveries_status ON callback_deliveries (status);
CREATE TABLE delivery_attempts (
    id VARCHAR(32) NOT NULL, delivery_id VARCHAR(32) NOT NULL, attempt_number INTEGER NOT NULL,
    destination_url VARCHAR(2048) NOT NULL, method VARCHAR(10) NOT NULL, request_headers JSON NOT NULL,
    request_body TEXT, status VARCHAR(9) NOT NULL, http_status INTEGER, response_headers JSON,
    response_body TEXT, error TEXT, latency_ms FLOAT, started_at DATETIME NOT NULL, finished_at DATETIME,
    PRIMARY KEY (id), FOREIGN KEY(delivery_id) REFERENCES callback_deliveries (id) ON DELETE CASCADE
);
CREATE INDEX ix_delivery_attempts_delivery_id ON delivery_attempts (delivery_id);
"""


def _legacy_db(path) -> None:
    con = sqlite3.connect(path)
    con.executescript(LEGACY_DDL)
    con.execute(
        "INSERT INTO callback_deliveries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "old1",
            "http://receiver.test/callback",
            "POST",
            json.dumps({"Authorization": "Bearer LEGACY_SECRET", "X-Partner-Id": "acme"}),
            json.dumps({"order_id": "ORD-1"}),
            5.0,
            "failed",
            1,
            503,
            "Non-2xx response: HTTP 503",
            "2026-09-13 10:00:00.000000",
            "2026-09-13 10:00:00.000000",
            None,
        ),
    )
    con.execute(
        "INSERT INTO delivery_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "att1",
            "old1",
            1,
            "http://receiver.test/callback",
            "POST",
            json.dumps({"Authorization": "Bearer LEGACY_SECRET", "X-Callback-Attempt": "1"}),
            '{"order_id":"ORD-1"}',
            "failed",
            503,
            json.dumps({"set-cookie": "sid=LEGACY_COOKIE", "server": "x"}),
            "{}",
            "Non-2xx response: HTTP 503",
            1.0,
            "2026-09-13 10:00:00.000000",
            "2026-09-13 10:00:00.000000",
        ),
    )
    con.commit()
    con.close()


def test_legacy_database_is_upgraded_in_place(tmp_path, client, receiver, monkeypatch):
    path = tmp_path / "legacy.db"
    _legacy_db(path)
    engine = create_engine(f"sqlite:///{path}")
    database.init_db(engine)

    cols = {c["name"] for c in inspect(engine).get_columns("callback_deliveries")}
    assert {"idempotency_key", "request_fingerprint"} <= cols
    cols = {c["name"] for c in inspect(engine).get_columns("delivery_attempts")}
    assert {"signature_algorithm", "signature_timestamp"} <= cols
    indexes = {i["name"]: i for i in inspect(engine).get_indexes("callback_deliveries")}
    assert indexes["uq_callback_deliveries_idempotency_key"]["unique"]

    # old evidence was redacted in place, nothing else touched
    with open(path, "rb") as fh:
        raw = fh.read()
    assert b"LEGACY_SECRET" not in raw and b"LEGACY_COOKIE" not in raw
    con = sqlite3.connect(path)
    headers = json.loads(con.execute("SELECT headers FROM callback_deliveries").fetchone()[0])
    con.close()
    assert headers == {"Authorization": REDACTED, "X-Partner-Id": "acme"}

    # idempotent: a second run applies nothing
    assert run_migrations(engine) == []

    # the upgraded DB is fully usable by the app
    from sqlalchemy.orm import sessionmaker

    from app.database import get_db

    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _db():
        with factory() as db:
            yield db

    client.app.dependency_overrides[get_db] = _db
    old = client.get("/api/deliveries/old1").json()
    assert old["status"] == "failed" and old["idempotency_key"] is None
    # legacy credentials are gone for good → retry asks for them instead of sending without them
    assert client.post("/api/deliveries/old1/retry").json()["code"] == "SENSITIVE_HEADERS_UNAVAILABLE"
    resp = client.post("/api/deliveries/old1/retry", json={"headers": {"Authorization": "Bearer FRESH"}})
    assert resp.json()["status"] == "delivered"
    assert client.post("/api/deliveries", json={"destination_url": "http://receiver.test/callback"}).status_code == 201
    engine.dispose()


def test_fresh_database_records_migrations(db_session_factory):
    engine = db_session_factory.kw["bind"]
    with engine.connect() as conn:
        names = [r[0] for r in conn.exec_driver_sql("SELECT name FROM schema_migrations ORDER BY name")]
    assert names == ["0001_security_columns", "0002_redact_existing_evidence"]
