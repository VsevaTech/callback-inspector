"""Idempotency-Key protects the *creation* of a logical delivery — not manual attempts."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app import database, service
from app.database import get_db
from app.http_client import get_http_client
from app.main import create_app
from app.models import CallbackDelivery
from app.schemas import DeliveryCreate
from app.security import request_fingerprint
from tests.conftest import RECEIVER_URL

KEY = "ORD-1001-payment-succeeded"
PAYLOAD = {"event": "payment.succeeded", "order_id": "ORD-1001", "amount": 100, "currency": "AED"}


def _body(**overrides):
    body = {
        "destination_url": RECEIVER_URL,
        "method": "POST",
        "headers": {"X-Partner-Id": "acme", "Authorization": "Bearer T1"},
        "payload": PAYLOAD,
        "timeout_seconds": 5,
    }
    body.update(overrides)
    return body


def _post(client, key=KEY, **overrides):
    headers = {} if key is None else {"Idempotency-Key": key}
    return client.post("/api/deliveries", json=_body(**overrides), headers=headers)


def _count(db_session_factory) -> int:
    with db_session_factory() as db:
        return db.scalar(select(func.count()).select_from(CallbackDelivery))


def test_first_request_creates_and_replay_returns_existing(client, receiver, db_session_factory):
    first = _post(client)
    assert first.status_code == 201, first.text
    assert first.json()["idempotent_replay"] is False
    assert first.json()["idempotency_key"] == KEY
    assert len(first.json()["request_fingerprint"]) == 64

    replay = _post(client)
    assert replay.status_code == 200
    assert replay.headers["Idempotent-Replayed"] == "true"
    data = replay.json()
    assert data["idempotent_replay"] is True
    assert data["id"] == first.json()["id"]
    assert data["attempt_count"] == 1
    # the receiver was hit exactly once, and only one delivery exists
    assert len(receiver["received"]) == 1
    assert _count(db_session_factory) == 1


def test_replay_is_insensitive_to_json_key_order_and_header_case(client, receiver):
    first = _post(client).json()
    shuffled = dict(reversed(list(PAYLOAD.items())))
    replay = _post(client, payload=shuffled, headers={"authorization": "Bearer T1", "x-partner-id": "acme"})
    assert replay.status_code == 200 and replay.json()["id"] == first["id"]
    assert len(receiver["received"]) == 1


def test_rotated_credential_is_same_logical_request(client, receiver):
    first = _post(client).json()
    replay = _post(client, headers={"X-Partner-Id": "acme", "Authorization": "Bearer ROTATED"})
    assert replay.status_code == 200 and replay.json()["id"] == first["id"]


@pytest.mark.parametrize(
    "change",
    [
        {"payload": {**PAYLOAD, "amount": 200}},
        {"destination_url": "http://receiver.test/other"},
        {"method": "PUT"},
        {"timeout_seconds": 6},
        {"headers": {"X-Partner-Id": "other", "Authorization": "Bearer T1"}},
        {"headers": {"X-Partner-Id": "acme"}},  # sensitive header removed
    ],
)
def test_same_key_different_request_is_409(client, receiver, db_session_factory, change):
    assert _post(client).status_code == 201
    resp = _post(client, **change)
    assert resp.status_code == 409
    assert resp.json()["code"] == "IDEMPOTENCY_CONFLICT"
    assert resp.json()["message"] == "Idempotency-Key was already used with a different delivery request."
    assert len(receiver["received"]) == 1
    assert _count(db_session_factory) == 1


def test_generated_headers_do_not_affect_fingerprint(client, receiver):
    first = _post(client).json()
    spoof = {"X-Partner-Id": "acme", "Authorization": "Bearer T1", "X-Callback-Id": "x", "X-Callback-Attempt": "9"}
    spoof |= {"X-Callback-Signature": "sha256=abc", "X-Callback-Timestamp": "1"}
    assert _post(client, headers=spoof).json()["id"] == first["id"]


def test_no_key_keeps_existing_behaviour(client, receiver, db_session_factory):
    a = _post(client, key=None)
    b = _post(client, key=None)
    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]
    assert a.json()["idempotency_key"] is None and a.json()["idempotent_replay"] is False
    assert len(receiver["received"]) == 2 and _count(db_session_factory) == 2


@pytest.mark.parametrize("bad", ["", " leading", "trailing ", "x" * 256, "a\tb", "ключ"])
def test_invalid_key_rejected(client, receiver, bad):
    resp = client.post("/api/deliveries", json=_body(), headers={"Idempotency-Key": bad.encode()})
    assert resp.status_code == 400, (bad, resp.text)
    assert resp.json()["code"] == "INVALID_IDEMPOTENCY_KEY"
    assert receiver["received"] == []


def test_max_length_key_accepted(client):
    assert _post(client, key="k" * 255).status_code == 201


def test_manual_retry_still_creates_new_attempt(client, receiver):
    receiver["mode"] = 503
    first = _post(client).json()
    receiver["mode"] = 200
    # replay of a failed delivery does NOT resend — only retry does
    assert _post(client).json()["attempt_count"] == 1
    assert len(receiver["received"]) == 1

    retried = client.post(f"/api/deliveries/{first['id']}/retry").json()
    assert retried["attempt_count"] == 2 and retried["status"] == "delivered"
    assert [a["attempt_number"] for a in retried["attempts"]] == [1, 2]
    # and a replay after the retry reflects the updated delivery, still without sending
    replay = _post(client).json()
    assert replay["status"] == "delivered" and replay["attempt_count"] == 2
    assert len(receiver["received"]) == 2


def test_key_survives_restart(tmp_path, receiver):
    """New engine + new app on the same SQLite file == process restart."""
    url = f"sqlite:///{tmp_path / 'restart.db'}"

    def boot():
        engine = create_engine(url, connect_args={"check_same_thread": False})
        database.init_db(engine)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        app = create_app()

        def _db():
            with factory() as db:
                yield db

        async def _http():
            import httpx

            from app.mock_receiver import receiver_app

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=receiver_app)) as c:
                yield c

        app.dependency_overrides[get_db] = _db
        app.dependency_overrides[get_http_client] = _http
        return engine, app

    engine, app = boot()
    with TestClient(app) as c:
        first_id = _post(c).json()["id"]
    engine.dispose()
    service.credential_cache.clear()

    engine, app = boot()
    with TestClient(app) as c:
        replay = _post(c)
        conflict = _post(c, payload={**PAYLOAD, "amount": 200})
    engine.dispose()
    assert replay.status_code == 200 and replay.json()["id"] == first_id
    assert conflict.status_code == 409
    assert len(receiver["received"]) == 1


def test_unique_constraint_prevents_duplicate_mapping(db_session_factory):
    with db_session_factory() as db:
        db.add(CallbackDelivery(destination_url=RECEIVER_URL, idempotency_key="dup", request_fingerprint="a"))
        db.commit()
        db.add(CallbackDelivery(destination_url=RECEIVER_URL, idempotency_key="dup", request_fingerprint="a"))
        with pytest.raises(IntegrityError):
            db.commit()


def test_concurrent_same_key_creates_one_delivery(db_session_factory, monkeypatch):
    """Both requests pass the lookup before either inserts; the unique index decides."""
    barrier = threading.Barrier(2, timeout=5)
    original = service.find_by_idempotency_key
    calls = {"n": 0}
    lock = threading.Lock()

    def racing_lookup(db, key):
        result = original(db, key)
        with lock:
            calls["n"] += 1
            first_round = calls["n"] <= 2
        if first_round:
            barrier.wait()  # both threads have seen "no delivery yet"
        return result

    monkeypatch.setattr(service, "find_by_idempotency_key", racing_lookup)
    data = DeliveryCreate(destination_url=RECEIVER_URL, payload={"n": 1})
    results: list = []
    errors: list = []

    def worker():
        try:
            with db_session_factory() as db:
                delivery, created = service.create_or_get_delivery(db, data, "race-key")
                results.append((delivery.id, created))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert not errors, errors
    assert sorted(c for _, c in results) == [False, True]
    assert len({i for i, _ in results}) == 1
    with db_session_factory() as db:
        assert db.scalar(select(func.count()).select_from(CallbackDelivery)) == 1


def test_fingerprint_is_deterministic():
    kw = {
        "destination_url": "https://p.example/cb",
        "method": "post",
        "headers": {"B": "2", "a": "1"},
        "payload": {"z": 1, "a": [1, {"y": 2, "x": 1}]},
        "timeout_seconds": 5,
    }
    fp = request_fingerprint(**kw)
    assert fp == request_fingerprint(**{**kw, "headers": {"A": "1", "b": "2"}, "timeout_seconds": 5.0})
    assert fp != request_fingerprint(**{**kw, "payload": {"z": 2, "a": [1, {"y": 2, "x": 1}]}})


def test_web_form_idempotency(client, receiver):
    form = {
        "destination_url": RECEIVER_URL,
        "method": "POST",
        "headers": "{}",
        "payload": '{"a": 1}',
        "timeout_seconds": "5",
        "idempotency_key": "<script>alert(1)</script>",
    }
    first = client.post("/deliveries", data=form, headers={"HX-Request": "true"})
    assert first.status_code == 200 and "delivered" in first.text
    replay = client.post("/deliveries", data=form, headers={"HX-Request": "true"})
    assert "Idempotent replay" in replay.text
    conflict = client.post("/deliveries", data={**form, "payload": '{"a": 2}'}, headers={"HX-Request": "true"})
    assert "IDEMPOTENCY_CONFLICT" in conflict.text
    assert len(receiver["received"]) == 1

    delivery_id = client.get("/api/deliveries").json()["items"][0]["id"]
    page = client.get(f"/deliveries/{delivery_id}").text
    assert "<script>alert(1)</script>" not in page  # HTML-escaped
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_long_key_is_shortened_in_ui(client):
    key = "K" * 200
    delivery_id = _post(client, key=key).json()["id"]
    page = client.get(f"/deliveries/{delivery_id}").text
    assert "K" * 63 + "…" in page
