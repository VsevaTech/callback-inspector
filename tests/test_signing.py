"""Optional HMAC-SHA256 signing of outbound callbacks + receiver-side verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3

import pytest

from app import config, main, service, signing
from tests.conftest import RECEIVER_URL, SIGNING_SECRET

BODY = b'{"event":"payment.succeeded","order_id":"ORD-1001","amount":4200}'
TS = 1790503200


def _create(client, headers=None, **overrides):
    body = {"destination_url": RECEIVER_URL, "headers": headers or {}, "payload": {"order_id": "ORD-1001", "n": 1}}
    body.update(overrides)
    return client.post("/api/deliveries", json=body)


# --------------------------------------------------------------------------- contract (pure)


def test_signature_format_is_documented_contract():
    expected = hmac.new(b"k", f"{TS}.".encode() + BODY, hashlib.sha256).hexdigest()
    assert signing.compute_signature("k", TS, BODY) == "sha256=" + expected


def _verify(**kw):
    args = {
        "secret": "k",
        "timestamp": str(TS),
        "signature": signing.compute_signature("k", TS, BODY),
        "body": BODY,
        "now": TS + 10,
    }
    args.update(kw)
    return signing.verify_signature(**args)


def test_valid_signature():
    assert _verify() == signing.Verification(True, None)


def test_wrong_secret_fails():
    assert _verify(secret="other").reason == "signature_mismatch"


def test_modified_payload_fails():
    assert _verify(body=BODY.replace(b"4200", b"4300")).reason == "signature_mismatch"


def test_modified_timestamp_fails():
    assert _verify(timestamp=str(TS + 1)).reason == "signature_mismatch"


def test_expired_timestamp_fails():
    assert _verify(now=TS + 301).reason == "timestamp_out_of_tolerance"
    assert _verify(now=TS - 301).reason == "timestamp_out_of_tolerance"
    assert _verify(now=TS + 300).valid


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"signature": None}, "missing_signature"),
        ({"timestamp": None}, "missing_timestamp"),
        ({"timestamp": "abc"}, "invalid_timestamp"),
        ({"timestamp": f"+{TS}"}, "invalid_timestamp"),
        ({"signature": "sha256=ünïcode"}, "signature_mismatch"),
    ],
)
def test_malformed_inputs(kw, reason):
    assert _verify(**kw).reason == reason


# --------------------------------------------------------------------------- configuration


def test_signing_enabled_without_secret_fails_fast(configure):
    s = config.Settings.from_env({"CALLBACK_SIGNING_ENABLED": "true", "CALLBACK_SIGNING_SECRET": "  "})
    with pytest.raises(config.ConfigurationError) as exc:
        s.validate()
    assert exc.value.code == "SIGNING_CONFIGURATION_ERROR"

    configure(signing_enabled=True, signing_secret="")
    with pytest.raises(config.ConfigurationError):
        main.create_app()


def test_invalid_bool_is_rejected():
    with pytest.raises(config.ConfigurationError):
        config.Settings.from_env({"CALLBACK_SIGNING_ENABLED": "maybe"})


def test_secret_not_in_settings_repr():
    s = config.Settings(signing_enabled=True, signing_secret="hide-me")
    assert "hide-me" not in repr(s)


# --------------------------------------------------------------------------- end-to-end


def test_signed_callback_verifies_at_receiver(client, receiver, signing_on):
    data = _create(client).json()
    assert data["status"] == "delivered", data
    got = receiver["received"][0]
    assert got["signature_valid"] is True
    assert got["headers"]["x-callback-signature"].startswith("sha256=")
    # the signature covers the exact bytes received
    ts = got["headers"]["x-callback-timestamp"]
    assert got["headers"]["x-callback-signature"] == signing.compute_signature(signing_on, ts, got["body"].encode())
    attempt = data["attempts"][0]
    assert attempt["signature_algorithm"] == "HMAC-SHA256"
    assert attempt["signature_timestamp"] == int(ts)
    assert json.loads(attempt["response_body"])["signature_valid"] is True


def test_receiver_rejects_wrong_secret_with_401(client, receiver, signing_on):
    receiver["signature_secret"] = "not-the-same"
    data = _create(client).json()
    assert data["status"] == "failed" and data["last_http_status"] == 401
    assert receiver["received"][0]["signature_reason"] == "signature_mismatch"


def test_receiver_rejects_missing_signature_when_signing_disabled(client, receiver):
    receiver["signature_secret"] = SIGNING_SECRET  # receiver expects signatures, sender does not sign
    data = _create(client).json()
    assert data["last_http_status"] == 401
    assert receiver["received"][0]["signature_reason"] == "missing_signature"
    assert data["attempts"][0]["signature_algorithm"] is None


def test_signing_disabled_sends_no_signature(client, receiver):
    data = _create(client).json()
    assert data["status"] == "delivered"
    got = receiver["received"][0]["headers"]
    assert "x-callback-signature" not in got and "x-callback-timestamp" not in got
    assert data["attempts"][0]["signature_algorithm"] is None


def test_caller_supplied_signature_is_overwritten(client, receiver, signing_on):
    data = _create(client, {"x-callback-signature": "sha256=forged", "X-Callback-Timestamp": "1"}).json()
    assert data["status"] == "delivered"
    got = receiver["received"][0]
    assert got["signature_valid"] is True
    assert got["headers"]["x-callback-signature"] != "sha256=forged"
    assert got["headers"]["x-callback-timestamp"] != "1"
    # exactly one signature header on the wire and in evidence (no case-duplicates)
    sent = data["attempts"][0]["request_headers"]
    assert [k for k in sent if k.lower() == "x-callback-signature"] == ["X-Callback-Signature"]


def test_retry_gets_new_valid_signature_and_timestamp(client, receiver, signing_on, monkeypatch):
    clock = iter([TS_NOW := signing.now_ts(), TS_NOW + 7])
    monkeypatch.setattr(service, "_signing_clock", lambda: next(clock))
    receiver["signature_tolerance"] = 60
    receiver["mode"] = 503
    delivery_id = _create(client).json()["id"]
    receiver["mode"] = 200
    data = client.post(f"/api/deliveries/{delivery_id}/retry").json()
    assert data["status"] == "delivered"
    first, second = receiver["received"]
    assert first["signature_valid"] is True and second["signature_valid"] is True
    assert int(second["headers"]["x-callback-timestamp"]) == int(first["headers"]["x-callback-timestamp"]) + 7
    assert first["headers"]["x-callback-signature"] != second["headers"]["x-callback-signature"]
    assert [a["signature_timestamp"] for a in data["attempts"]] == [TS_NOW, TS_NOW + 7]


def test_signing_secret_never_persisted_or_exposed(client, receiver, signing_on, db_session_factory):
    receiver["mode"] = 503
    delivery_id = _create(client).json()["id"]
    receiver["mode"] = 200
    client.post(f"/api/deliveries/{delivery_id}/retry")

    exposed = "".join(
        client.get(p).text
        for p in (
            f"/api/deliveries/{delivery_id}",
            "/api/deliveries",
            f"/deliveries/{delivery_id}",
            "/",
            "/openapi.json",
        )
    )
    assert signing_on not in exposed
    assert "HMAC-SHA256" in client.get(f"/deliveries/{delivery_id}").text

    path = db_session_factory.kw["bind"].url.database
    with open(path, "rb") as fh:
        assert signing_on.encode() not in fh.read()
    con = sqlite3.connect(path)
    try:
        cells = [
            str(c)
            for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
            for r in con.execute(f"SELECT * FROM {t}")
            for c in r
        ]
    finally:
        con.close()
    assert all(signing_on not in c for c in cells)
