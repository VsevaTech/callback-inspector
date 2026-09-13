"""Minimal Python client: send a callback via Callback Inspector and retry it if it failed.

Usage (with `docker compose up` running):

    python examples/client.py http://receiver:8001/callback
"""

from __future__ import annotations

import json
import sys

import httpx

INSPECTOR = "http://localhost:8000"


def main(destination: str) -> int:
    body = {
        "destination_url": destination,
        "method": "POST",
        "headers": {"X-Signature": "demo"},
        "payload": {"event": "order.created", "order_id": "ORD-42"},
        "timeout_seconds": 5,
    }
    with httpx.Client(base_url=INSPECTOR, timeout=30) as client:
        delivery = client.post("/api/deliveries", json=body).raise_for_status().json()
        print(f"created {delivery['id']}: {delivery['status']} (HTTP {delivery['last_http_status']})")

        if delivery["status"] == "failed":
            input("Delivery failed. Fix the receiver, then press Enter to retry... ")
            delivery = client.post(f"/api/deliveries/{delivery['id']}/retry").raise_for_status().json()
            print(f"after retry: {delivery['status']} (HTTP {delivery['last_http_status']})")

        print("attempt history:")
        for attempt in delivery["attempts"]:
            print(json.dumps({k: attempt[k] for k in ("attempt_number", "status", "http_status", "latency_ms")}))
    return 0 if delivery["status"] == "delivered" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://receiver:8001/callback"))
