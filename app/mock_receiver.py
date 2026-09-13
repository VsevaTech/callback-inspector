"""Mock partner receiver for demos and tests.

* ``POST /callback``   — answers with the current mode (200 or 503) and records the request
* ``GET  /mode``       — show current mode
* ``POST /mode/{code}``— switch mode (200 or 503)
* ``GET  /received``   — list everything received so far
* ``DELETE /received`` — clear the inbox
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

receiver_app = FastAPI(title="Mock Callback Receiver", version="0.1.0")

state: dict = {"mode": 200, "received": []}


@receiver_app.get("/mode")
def get_mode() -> dict:
    return {"mode": state["mode"]}


@receiver_app.post("/mode/{code}")
def set_mode(code: Literal["200", "503"]) -> dict:
    state["mode"] = int(code)
    return {"mode": state["mode"]}


@receiver_app.api_route("/callback", methods=["POST", "PUT", "PATCH", "DELETE", "GET"])
async def receive_callback(request: Request) -> Response:
    body = (await request.body()).decode("utf-8", errors="replace")
    state["received"].append(
        {
            "received_at": datetime.now(UTC).isoformat(),
            "method": request.method,
            "headers": dict(request.headers),
            "body": body,
            "answered_with": state["mode"],
        }
    )
    if state["mode"] == 503:
        return JSONResponse({"error": "service unavailable (simulated)"}, status_code=503)
    return JSONResponse({"ok": True, "received_bytes": len(body)}, status_code=200)


@receiver_app.get("/received")
def list_received() -> dict:
    return {"count": len(state["received"]), "items": state["received"]}


@receiver_app.delete("/received")
def clear_received() -> dict:
    state["received"].clear()
    return {"count": 0}


@receiver_app.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": state["mode"]}
