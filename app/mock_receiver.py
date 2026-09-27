"""Mock partner receiver for demos and tests.

* ``POST /callback``   — answers with the current mode (200 or 503) and records the request
* ``GET  /mode``       — show current mode
* ``POST /mode/{code}``— switch mode (200 or 503)
* ``GET  /received``   — list everything received so far
* ``DELETE /received`` — clear the inbox

Optional HMAC verification (demo): set ``SIGNATURE_SECRET`` (and optionally
``SIGNATURE_TOLERANCE_SECONDS``, default 300, ``SIGNATURE_HEADER`` / ``TIMESTAMP_HEADER``).
When set, every callback must carry a valid ``X-Callback-Timestamp`` + ``X-Callback-Signature``;
otherwise the receiver answers ``401 {"signature_valid": false, "reason": ...}`` regardless of mode.
Valid callbacks then get the usual 200/503 answer with ``"signature_valid": true``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from app import signing

receiver_app = FastAPI(title="Mock Callback Receiver", version="0.2.0")


def _initial_state() -> dict:
    return {
        "mode": 200,
        "received": [],
        # Demo-only verification secret (synthetic value from the demo environment, never a real one).
        "signature_secret": os.getenv("SIGNATURE_SECRET") or None,
        "signature_tolerance": int(os.getenv("SIGNATURE_TOLERANCE_SECONDS") or signing.DEFAULT_TOLERANCE_SECONDS),
        "signature_header": os.getenv("SIGNATURE_HEADER") or "X-Callback-Signature",
        "timestamp_header": os.getenv("TIMESTAMP_HEADER") or "X-Callback-Timestamp",
    }


state: dict = _initial_state()


@receiver_app.get("/mode")
def get_mode() -> dict:
    return {"mode": state["mode"]}


@receiver_app.post("/mode/{code}")
def set_mode(code: Literal["200", "503"]) -> dict:
    state["mode"] = int(code)
    return {"mode": state["mode"]}


@receiver_app.api_route("/callback", methods=["POST", "PUT", "PATCH", "DELETE", "GET"])
async def receive_callback(request: Request) -> Response:
    raw = await request.body()  # raw bytes: exactly what was signed
    body = raw.decode("utf-8", errors="replace")

    verification: signing.Verification | None = None
    if state["signature_secret"]:
        verification = signing.verify_signature(
            secret=state["signature_secret"],
            timestamp=request.headers.get(state["timestamp_header"]),
            signature=request.headers.get(state["signature_header"]),
            body=raw,
            tolerance_seconds=state["signature_tolerance"],
        )

    answered_with = 401 if verification is not None and not verification.valid else state["mode"]
    state["received"].append(
        {
            "received_at": datetime.now(UTC).isoformat(),
            "method": request.method,
            "headers": dict(request.headers),
            "body": body,
            "answered_with": answered_with,
            "signature_valid": None if verification is None else verification.valid,
            "signature_reason": None if verification is None else verification.reason,
        }
    )
    if verification is not None and not verification.valid:
        return JSONResponse({"signature_valid": False, "reason": verification.reason}, status_code=401)

    extra = {} if verification is None else {"signature_valid": True}
    if state["mode"] == 503:
        return JSONResponse({"error": "service unavailable (simulated)", **extra}, status_code=503)
    return JSONResponse({"ok": True, "received_bytes": len(raw), **extra}, status_code=200)


@receiver_app.get("/received")
def list_received() -> dict:
    return {"count": len(state["received"]), "items": state["received"]}


@receiver_app.delete("/received")
def clear_received() -> dict:
    state["received"].clear()
    return {"count": 0}


@receiver_app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": state["mode"], "signature_verification": bool(state["signature_secret"])}
