"""Server-rendered web UI (Jinja2 + HTMX)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app import config, service, signing
from app.database import get_db
from app.http_client import get_http_client
from app.schemas import DeliveryCreate
from app.security import InvalidIdempotencyKey, redacted_header_names, validate_idempotency_key

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _pretty_json(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, indent=2, ensure_ascii=False)


def _shorten(value: str | None, limit: int = 64) -> str:
    if not value:
        return ""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _ts_iso(value: int | None) -> str:
    if value is None:
        return ""
    return datetime.fromtimestamp(value, UTC).isoformat()


def _security_summary(delivery) -> dict:
    """What the Security panel shows. Never contains secret values — only names and modes."""
    signed = [a for a in delivery.attempts if a.signature_algorithm]
    if signed:
        signing_label = signed[-1].signature_algorithm
    elif config.settings.signing_enabled:
        signing_label = f"{signing.ALGORITHM} (enabled, no signed attempt yet)"
    else:
        signing_label = "disabled"
    return {
        "redacted_headers": redacted_header_names(delivery.headers),
        "signing": signing_label,
        "idempotency_key": delivery.idempotency_key,
    }


templates.env.filters["pretty_json"] = _pretty_json
templates.env.filters["shorten"] = _shorten
templates.env.filters["ts_iso"] = _ts_iso
templates.env.globals["security_summary"] = _security_summary


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


@router.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    items, total = service.list_deliveries(db, limit=100)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"deliveries": items, "total": total, "demo_destination": config.settings.demo_destination, "form_error": None},
    )


@router.get("/deliveries/rows", response_class=HTMLResponse)
def delivery_rows(request: Request, db: Session = Depends(get_db)):
    items, total = service.list_deliveries(db, limit=100)
    return templates.TemplateResponse(request, "partials/rows.html", {"deliveries": items, "total": total})


@router.post("/deliveries", response_class=HTMLResponse)
async def create_from_form(
    request: Request,
    destination_url: str = Form(...),
    method: str = Form("POST"),
    headers: str = Form(""),
    payload: str = Form(""),
    timeout_seconds: float = Form(config.settings.default_timeout),
    idempotency_key: str = Form(""),
    db: Session = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_http_client),
):
    try:
        parsed_headers = json.loads(headers) if headers.strip() else {}
        parsed_payload = json.loads(payload) if payload.strip() else None
        if not isinstance(parsed_headers, dict):
            raise ValueError("headers must be a JSON object")
        data = DeliveryCreate(
            destination_url=destination_url,
            method=method,
            headers=parsed_headers,
            payload=parsed_payload,
            timeout_seconds=timeout_seconds,
        )
    except (ValueError, ValidationError) as exc:
        items, total = service.list_deliveries(db, limit=100)
        ctx = {"deliveries": items, "total": total, "demo_destination": destination_url, "form_error": str(exc)}
        return templates.TemplateResponse(request, "partials/form.html" if _is_htmx(request) else "index.html", ctx)

    def _form_error(message: str):
        items, total = service.list_deliveries(db, limit=100)
        ctx = {"deliveries": items, "total": total, "demo_destination": destination_url, "form_error": message}
        return templates.TemplateResponse(request, "partials/form.html" if _is_htmx(request) else "index.html", ctx)

    replay = False
    if idempotency_key:
        try:
            validate_idempotency_key(idempotency_key)
            delivery, created = service.create_or_get_delivery(db, data, idempotency_key)
        except InvalidIdempotencyKey as exc:
            return _form_error(f"INVALID_IDEMPOTENCY_KEY: {exc}")
        except service.IdempotencyConflict as exc:
            return _form_error(f"IDEMPOTENCY_CONFLICT: {exc}")
        replay = not created
    else:
        delivery = service.create_delivery(db, data)
    if not replay:
        await service.send_attempt(db, delivery, client)
    delivery = service.get_delivery(db, delivery.id)
    return templates.TemplateResponse(
        request,
        "partials/created.html",
        {
            "delivery": delivery,
            "demo_destination": config.settings.demo_destination,
            "form_error": None,
            "replay": replay,
        },
    )


@router.get("/deliveries/{delivery_id}", response_class=HTMLResponse)
def delivery_detail(delivery_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        delivery = service.get_delivery(db, delivery_id)
    except service.DeliveryNotFound:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return templates.TemplateResponse(request, "detail.html", {"delivery": delivery, "error": None})


@router.post("/deliveries/{delivery_id}/retry", response_class=HTMLResponse)
async def retry_from_ui(
    delivery_id: str,
    request: Request,
    db: Session = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_http_client),
):
    error = None
    try:
        delivery = await service.retry_delivery(db, delivery_id, client)
    except service.DeliveryNotFound:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    except (service.RetryNotAllowed, service.SensitiveHeadersUnavailable) as exc:
        error = str(exc)
        delivery = service.get_delivery(db, delivery_id)
    template = "partials/detail_body.html" if _is_htmx(request) else "detail.html"
    return templates.TemplateResponse(request, template, {"delivery": delivery, "error": error})
