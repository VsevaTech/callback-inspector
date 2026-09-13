"""The core scenario: 503 → failed → switch receiver to 200 → retry → delivered."""

from __future__ import annotations

import json

from tests.conftest import RECEIVER_URL

PAYLOAD = {"event": "payment.succeeded", "order_id": "ORD-1001", "amount": 4200}


def _create(client, **overrides):
    body = {
        "destination_url": RECEIVER_URL,
        "method": "POST",
        "headers": {"X-Signature": "abc"},
        "payload": PAYLOAD,
        "timeout_seconds": 5,
    }
    body.update(overrides)
    return client.post("/api/deliveries", json=body)


def test_successful_delivery_is_marked_delivered(client, receiver):
    resp = _create(client)
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["status"] == "delivered"
    assert data["attempt_count"] == 1
    assert data["last_http_status"] == 200
    assert data["delivered_at"] is not None

    attempt = data["attempts"][0]
    assert attempt["attempt_number"] == 1
    assert attempt["status"] == "delivered"
    assert attempt["http_status"] == 200
    assert attempt["latency_ms"] is not None and attempt["latency_ms"] >= 0
    assert json.loads(attempt["request_body"]) == PAYLOAD
    assert attempt["request_headers"]["X-Signature"] == "abc"
    assert attempt["request_headers"]["X-Callback-Id"] == data["id"]
    assert json.loads(attempt["response_body"])["ok"] is True

    # The partner actually received it, with our tracing headers.
    assert len(receiver["received"]) == 1
    received = receiver["received"][0]
    assert json.loads(received["body"]) == PAYLOAD
    assert received["headers"]["x-callback-id"] == data["id"]
    assert received["headers"]["x-callback-attempt"] == "1"


def test_503_then_retry_after_switching_to_200(client, receiver):
    # 1. receiver is broken
    receiver["mode"] = 503

    # 2. send callback -> delivery marked failed
    resp = _create(client)
    assert resp.status_code == 201
    data = resp.json()
    delivery_id = data["id"]
    assert data["status"] == "failed"
    assert data["attempt_count"] == 1
    assert data["last_http_status"] == 503
    assert "503" in data["last_error"]
    assert data["attempts"][0]["status"] == "failed"
    assert data["attempts"][0]["http_status"] == 503
    assert "simulated" in data["attempts"][0]["response_body"]

    # 3. partner fixes their side
    receiver["mode"] = 200

    # 4. manual retry -> delivered
    resp = client.post(f"/api/deliveries/{delivery_id}/retry")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "delivered"
    assert data["attempt_count"] == 2
    assert data["last_http_status"] == 200
    assert data["last_error"] is None

    # 5. full history of both attempts is preserved
    assert [a["attempt_number"] for a in data["attempts"]] == [1, 2]
    assert [a["status"] for a in data["attempts"]] == ["failed", "delivered"]
    assert [a["http_status"] for a in data["attempts"]] == [503, 200]
    # both attempts carried the same body
    assert json.loads(data["attempts"][0]["request_body"]) == json.loads(data["attempts"][1]["request_body"])

    # attempts endpoint agrees
    resp = client.get(f"/api/deliveries/{delivery_id}/attempts")
    assert resp.status_code == 200
    assert len(resp.json()) == 2

    # partner saw two requests with increasing attempt header
    assert [r["headers"]["x-callback-attempt"] for r in receiver["received"]] == ["1", "2"]


def test_retry_of_delivered_callback_is_rejected(client):
    delivery_id = _create(client).json()["id"]
    resp = client.post(f"/api/deliveries/{delivery_id}/retry")
    assert resp.status_code == 409
    assert "only failed" in resp.json()["detail"]
    # no extra attempt was created
    assert client.get(f"/api/deliveries/{delivery_id}").json()["attempt_count"] == 1


def test_unknown_partner_endpoint_is_recorded_as_failed(client):
    # partner misconfigured their route -> 404, evidence still captured
    resp = _create(client, destination_url="http://receiver.test/no-such-endpoint", timeout_seconds=1)
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "failed"
    assert data["last_http_status"] == 404
    attempt = data["attempts"][0]
    assert attempt["status"] == "failed"
    assert attempt["http_status"] == 404
    assert "404" in attempt["error"]
    # request body is still stored even though nothing came back
    assert json.loads(attempt["request_body"]) == PAYLOAD


def test_list_and_get_and_404(client, receiver):
    receiver["mode"] = 503
    failed_id = _create(client).json()["id"]
    receiver["mode"] = 200
    ok_id = _create(client).json()["id"]

    resp = client.get("/api/deliveries")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    statuses = {d["id"]: d["status"] for d in body["items"]}
    assert statuses == {failed_id: "failed", ok_id: "delivered"}

    assert client.get(f"/api/deliveries/{ok_id}").status_code == 200
    assert client.get("/api/deliveries/does-not-exist").status_code == 404
    assert client.post("/api/deliveries/does-not-exist/retry").status_code == 404


def test_validation_errors(client):
    assert _create(client, destination_url="not a url").status_code == 422
    assert _create(client, method="BREW").status_code == 422
    assert _create(client, timeout_seconds=0).status_code == 422
