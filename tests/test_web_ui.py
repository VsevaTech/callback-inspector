"""Smoke tests for the HTMX web UI."""

from __future__ import annotations

from tests.conftest import RECEIVER_URL


def test_index_renders_and_lists_deliveries(client, receiver):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "No deliveries yet" in resp.text

    receiver["mode"] = 503
    resp = client.post(
        "/deliveries",
        data={
            "destination_url": RECEIVER_URL,
            "method": "POST",
            "headers": '{"X-Test": "1"}',
            "payload": '{"hello": "world"}',
            "timeout_seconds": "5",
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "failed" in resp.text

    resp = client.get("/deliveries/rows")
    assert resp.status_code == 200
    assert "details" in resp.text and "failed" in resp.text


def test_detail_page_shows_history_and_retry_button(client, receiver):
    receiver["mode"] = 503
    delivery_id = client.post("/api/deliveries", json={"destination_url": RECEIVER_URL, "payload": {"a": 1}}).json()[
        "id"
    ]

    page = client.get(f"/deliveries/{delivery_id}")
    assert page.status_code == 200
    assert "Retry now" in page.text
    assert "#1" in page.text
    assert "HTTP 503" in page.text

    receiver["mode"] = 200
    resp = client.post(f"/deliveries/{delivery_id}/retry", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "delivered" in resp.text
    assert "Retry now" not in resp.text
    assert "#2" in resp.text and "#1" in resp.text


def test_form_validation_error_is_shown(client):
    resp = client.post(
        "/deliveries",
        data={
            "destination_url": RECEIVER_URL,
            "method": "POST",
            "headers": "[1,2]",
            "payload": "",
            "timeout_seconds": "5",
        },
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 200
    assert "headers must be a JSON object" in resp.text


def test_unknown_delivery_page_is_404(client):
    assert client.get("/deliveries/nope").status_code == 404
