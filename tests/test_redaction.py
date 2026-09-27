"""Sensitive headers are delivered to the destination but never retained as evidence."""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from app import config, service
from app.security import REDACTED, redact_headers
from tests.conftest import RECEIVER_URL

SECRET_TOKEN = "VERY_SECRET_TOKEN"
SECRET_KEY = "super-secret-key"


def _create(client, headers, **overrides):
    body = {"destination_url": RECEIVER_URL, "headers": headers, "payload": {"order_id": "ORD-1001"}}
    body.update(overrides)
    return client.post("/api/deliveries", json=body)


def _db_path(db_session_factory) -> str:
    return db_session_factory.kw["bind"].url.database


def _dump_db(db_session_factory) -> str:
    """Everything persisted, as text: every cell of every table, straight from SQLite."""
    con = sqlite3.connect(_db_path(db_session_factory))
    try:
        chunks = []
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            for row in con.execute(f"SELECT * FROM {table}"):
                chunks.extend(str(cell) for cell in row)
        return "\n".join(chunks)
    finally:
        con.close()


# --------------------------------------------------------------------------- unit


def test_redact_is_case_insensitive_and_preserves_normal_headers():
    out = redact_headers(
        {
            "authorization": "Bearer x",
            "AUTHORIZATION": "Bearer y",
            "Proxy-Authorization": "Basic z",
            "cookie": "sid=1",
            "Set-Cookie": "sid=2",
            "x-api-key": "k",
            "X-Auth-Token": "t",
            "x-access-token": "a",
            "X-Normal-Header": "hello",
        }
    )
    assert out.pop("X-Normal-Header") == "hello"
    assert set(out.values()) == {REDACTED}


def test_empty_sensitive_headers_env_keeps_secure_defaults():
    s = config.Settings.from_env({"SENSITIVE_HEADERS": ""})
    assert {"authorization", "cookie", "set-cookie", "x-api-key"} <= s.sensitive_headers
    s = config.Settings.from_env({"SENSITIVE_HEADERS": " , ,"})
    assert "authorization" in s.sensitive_headers


def test_custom_sensitive_headers_extend_defaults():
    s = config.Settings.from_env({"SENSITIVE_HEADERS": "X-Partner-Secret, x-other "})
    assert {"x-partner-secret", "x-other", "authorization", "x-api-key"} <= s.sensitive_headers


# --------------------------------------------------------------------------- end-to-end


def test_secret_reaches_destination_but_not_the_database(client, receiver, db_session_factory):
    resp = _create(
        client,
        {"Authorization": f"Bearer {SECRET_TOKEN}", "X-API-Key": SECRET_KEY, "X-Normal-Header": "hello"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["status"] == "delivered"

    # 1. the destination got the ORIGINAL credentials
    received = receiver["received"][0]["headers"]
    assert received["authorization"] == f"Bearer {SECRET_TOKEN}"
    assert received["x-api-key"] == SECRET_KEY
    assert received["x-normal-header"] == "hello"

    # 2. the API shows redacted evidence, normal headers intact
    assert data["headers"] == {"Authorization": REDACTED, "X-API-Key": REDACTED, "X-Normal-Header": "hello"}
    sent = data["attempts"][0]["request_headers"]
    assert sent["Authorization"] == REDACTED and sent["X-API-Key"] == REDACTED
    assert sent["X-Normal-Header"] == "hello"
    detail = client.get(f"/api/deliveries/{data['id']}").text + client.get("/api/deliveries").text
    detail += client.get(f"/api/deliveries/{data['id']}/attempts").text
    assert SECRET_TOKEN not in detail and SECRET_KEY not in detail

    # 3. SQLite itself: the secrets are physically absent (cells AND raw file bytes)
    dump = _dump_db(db_session_factory)
    assert REDACTED in dump and "hello" in dump
    assert SECRET_TOKEN not in dump and SECRET_KEY not in dump
    with open(_db_path(db_session_factory), "rb") as fh:
        raw = fh.read()
    assert SECRET_TOKEN.encode() not in raw and SECRET_KEY.encode() not in raw

    # 4. web UI
    page = client.get(f"/deliveries/{data['id']}").text
    assert SECRET_TOKEN not in page and SECRET_KEY not in page
    assert REDACTED in page and "hello" in page
    assert "redacted (Authorization, X-API-Key)" in page


def test_cookie_and_custom_sensitive_header_redacted(client, receiver, configure):
    configure(extra_sensitive_headers=("X-Partner-Secret",))
    data = _create(client, {"Cookie": "session=abc123", "x-partner-secret": "p4rtner"}).json()
    assert data["headers"] == {"Cookie": REDACTED, "x-partner-secret": REDACTED}
    got = receiver["received"][0]["headers"]
    assert got["cookie"] == "session=abc123" and got["x-partner-secret"] == "p4rtner"


def test_response_sensitive_headers_redacted(client, receiver, db_session_factory):
    from app.mock_receiver import receiver_app

    route_path = "/leaky"

    async def leaky(_: Request):
        resp = JSONResponse({"ok": True})
        resp.headers["Set-Cookie"] = "partner_session=RESPONSE_COOKIE_SECRET"
        resp.headers["Authorization"] = "Bearer RESPONSE_AUTH_SECRET"
        resp.headers["X-Internal-Token"] = "RESPONSE_INTERNAL_SECRET"
        resp.headers["X-Request-Id"] = "req-42"
        return resp

    receiver_app.add_api_route(route_path, leaky, methods=["POST"])
    try:
        data = _create(client, {}, destination_url="http://receiver.test/leaky").json()
    finally:
        receiver_app.router.routes = [r for r in receiver_app.router.routes if getattr(r, "path", None) != route_path]

    rh = data["attempts"][0]["response_headers"]
    assert rh["set-cookie"] == REDACTED and rh["authorization"] == REDACTED and rh["x-internal-token"] == REDACTED
    assert rh["x-request-id"] == "req-42"
    dump = _dump_db(db_session_factory)
    for secret in ("RESPONSE_COOKIE_SECRET", "RESPONSE_AUTH_SECRET", "RESPONSE_INTERNAL_SECRET"):
        assert secret not in dump


def test_retry_resends_original_credentials_from_memory(client, receiver):
    receiver["mode"] = 503
    delivery_id = _create(client, {"Authorization": f"Bearer {SECRET_TOKEN}"}).json()["id"]
    receiver["mode"] = 200
    resp = client.post(f"/api/deliveries/{delivery_id}/retry")
    assert resp.status_code == 200 and resp.json()["status"] == "delivered"
    assert [r["headers"]["authorization"] for r in receiver["received"]] == [f"Bearer {SECRET_TOKEN}"] * 2


def test_retry_after_restart_requires_resupplied_credentials(client, receiver):
    receiver["mode"] = 503
    delivery_id = _create(client, {"Authorization": f"Bearer {SECRET_TOKEN}", "X-Normal-Header": "n"}).json()["id"]
    service.credential_cache.clear()  # == process restart: originals are gone
    receiver["mode"] = 200

    resp = client.post(f"/api/deliveries/{delivery_id}/retry")
    assert resp.status_code == 409
    assert resp.json()["code"] == "SENSITIVE_HEADERS_UNAVAILABLE"
    assert len(receiver["received"]) == 1  # nothing was sent without credentials
    assert client.get(f"/api/deliveries/{delivery_id}").json()["attempt_count"] == 1

    # only redacted header names may be re-supplied
    bad = client.post(f"/api/deliveries/{delivery_id}/retry", json={"headers": {"X-Normal-Header": "x"}})
    assert bad.status_code == 422 and bad.json()["code"] == "INVALID_RETRY_HEADERS"

    resp = client.post(f"/api/deliveries/{delivery_id}/retry", json={"headers": {"authorization": "Bearer NEW_TOKEN"}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "delivered"
    assert receiver["received"][-1]["headers"]["authorization"] == "Bearer NEW_TOKEN"
    assert "NEW_TOKEN" not in resp.text


def test_ui_retry_without_credentials_shows_error(client, receiver):
    receiver["mode"] = 503
    delivery_id = _create(client, {"X-API-Key": SECRET_KEY}).json()["id"]
    service.credential_cache.clear()
    resp = client.post(f"/deliveries/{delivery_id}/retry", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "not available in memory" in resp.text
    assert SECRET_KEY not in resp.text


@pytest.mark.parametrize("bad", [{"X Bad": "v"}, {"X-Ok": "line\r\nInjected: 1"}])
def test_header_injection_rejected(client, bad):
    assert _create(client, bad).status_code == 422


def test_logs_never_contain_secrets(client, receiver, caplog, signing_on):
    caplog.set_level("DEBUG")
    receiver["mode"] = 503
    delivery_id = _create(
        client, {"Authorization": f"Bearer {SECRET_TOKEN}", "X-API-Key": SECRET_KEY, "Cookie": "sid=COOKIE_SECRET"}
    ).json()["id"]
    receiver["mode"] = 200
    client.post(f"/api/deliveries/{delivery_id}/retry")
    text = caplog.text
    assert "attempt finished" in text  # the app does log…
    for secret in (SECRET_TOKEN, SECRET_KEY, "COOKIE_SECRET", signing_on):
        assert secret not in text  # …but never values
    assert signing_on not in repr(config.settings)
