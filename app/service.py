"""Delivery service: persist first, then send, then record the evidence.

The key invariant: a CallbackDelivery row and its DeliveryAttempt row are
committed *before* any network I/O happens. If the process dies mid-request,
the pending row is still there and the outcome can never silently disappear.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import CallbackDelivery, DeliveryAttempt, DeliveryStatus, utcnow
from app.schemas import DeliveryCreate


class DeliveryNotFound(Exception):
    pass


class RetryNotAllowed(Exception):
    pass


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    limit = settings.max_body_chars
    if len(text) > limit:
        return text[:limit] + f"... [truncated {len(text) - limit} chars]"
    return text


def _serialize_payload(payload: Any) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def list_deliveries(db: Session, limit: int = 100, offset: int = 0) -> tuple[list[CallbackDelivery], int]:
    total = db.scalar(select(func.count()).select_from(CallbackDelivery)) or 0
    stmt = select(CallbackDelivery).order_by(CallbackDelivery.created_at.desc()).limit(limit).offset(offset)
    return list(db.scalars(stmt)), total


def get_delivery(db: Session, delivery_id: str) -> CallbackDelivery:
    stmt = (
        select(CallbackDelivery)
        .options(selectinload(CallbackDelivery.attempts))
        .where(CallbackDelivery.id == delivery_id)
    )
    delivery = db.scalar(stmt)
    if delivery is None:
        raise DeliveryNotFound(delivery_id)
    return delivery


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def create_delivery(db: Session, data: DeliveryCreate) -> CallbackDelivery:
    """Step 1: persist the callback as *pending* — before anything is sent."""
    delivery = CallbackDelivery(
        destination_url=str(data.destination_url),
        method=data.method,
        headers=dict(data.headers),
        payload=data.payload,
        timeout_seconds=data.timeout_seconds,
        status=DeliveryStatus.PENDING,
    )
    db.add(delivery)
    db.commit()
    db.refresh(delivery)
    return delivery


def _build_request_headers(delivery: CallbackDelivery, attempt_number: int) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "User-Agent": "callback-inspector/0.1"}
    headers.update(delivery.headers or {})
    headers["X-Callback-Id"] = delivery.id
    headers["X-Callback-Attempt"] = str(attempt_number)
    return headers


async def send_attempt(db: Session, delivery: CallbackDelivery, client: httpx.AsyncClient) -> DeliveryAttempt:
    """Step 2: record a pending attempt, do the HTTP call, record the outcome."""
    attempt_number = delivery.attempt_count + 1
    request_headers = _build_request_headers(delivery, attempt_number)
    request_body = _serialize_payload(delivery.payload)

    attempt = DeliveryAttempt(
        delivery_id=delivery.id,
        attempt_number=attempt_number,
        destination_url=delivery.destination_url,
        method=delivery.method,
        request_headers=request_headers,
        request_body=request_body,
        status=DeliveryStatus.PENDING,
        started_at=utcnow(),
    )
    delivery.attempt_count = attempt_number
    delivery.status = DeliveryStatus.PENDING
    db.add(attempt)
    db.commit()  # <-- durable before the network call

    started = time.perf_counter()
    try:
        response = await client.request(
            delivery.method,
            delivery.destination_url,
            content=request_body.encode("utf-8") if request_body is not None else None,
            headers=request_headers,
            timeout=delivery.timeout_seconds,
        )
    except httpx.HTTPError as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        attempt.error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        attempt.status = DeliveryStatus.FAILED
    else:
        latency_ms = (time.perf_counter() - started) * 1000
        attempt.http_status = response.status_code
        attempt.response_headers = dict(response.headers)
        attempt.response_body = _truncate(response.text)
        if 200 <= response.status_code < 300:
            attempt.status = DeliveryStatus.DELIVERED
        else:
            attempt.status = DeliveryStatus.FAILED
            attempt.error = f"Non-2xx response: HTTP {response.status_code}"

    attempt.latency_ms = round(latency_ms, 2)
    attempt.finished_at = utcnow()

    delivery.status = attempt.status
    delivery.last_http_status = attempt.http_status
    delivery.last_error = attempt.error
    if attempt.status == DeliveryStatus.DELIVERED:
        delivery.delivered_at = attempt.finished_at
    db.commit()
    db.refresh(delivery)
    db.refresh(attempt)
    return attempt


async def retry_delivery(db: Session, delivery_id: str, client: httpx.AsyncClient) -> CallbackDelivery:
    """Manual retry: only allowed for failed deliveries."""
    delivery = get_delivery(db, delivery_id)
    if delivery.status != DeliveryStatus.FAILED:
        raise RetryNotAllowed(
            f"Delivery {delivery_id} is {delivery.status.value}, only failed deliveries can be retried"
        )
    await send_attempt(db, delivery, client)
    db.expire(delivery, ["attempts"])
    return get_delivery(db, delivery_id)
