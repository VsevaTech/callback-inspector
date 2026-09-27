"""Delivery service: persist first, then send, then record the evidence.

Key invariants:

* **Persist before send** — a CallbackDelivery row and its DeliveryAttempt row are committed
  *before* any network I/O happens. If the process dies mid-request, the pending row is still there.
* **Original headers on the wire, sanitised headers in evidence** — sensitive header values
  (Authorization, Cookie, X-API-Key, …) are sent to the partner unchanged but are replaced by
  ``***REDACTED***`` in everything persisted. Their original values live only in process memory
  (:data:`credential_cache`) so a manual retry can resend them.
* **Signing happens last** — the HMAC is computed over the exact bytes that are sent.
* **Idempotent creation** — a repeated ``Idempotency-Key`` with the same logical request returns the
  existing delivery and sends nothing; the DB unique index settles concurrent races.
"""

from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from threading import Lock
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app import config, signing
from app.models import CallbackDelivery, DeliveryAttempt, DeliveryStatus, utcnow
from app.schemas import DeliveryCreate
from app.security import REDACTED, redact_headers, request_fingerprint, sensitive_values

log = logging.getLogger("callback_inspector.service")

USER_AGENT = "callback-inspector/0.2"


class DeliveryNotFound(Exception):
    pass


class RetryNotAllowed(Exception):
    pass


class IdempotencyConflict(Exception):
    def __init__(self, delivery_id: str) -> None:
        super().__init__("Idempotency-Key was already used with a different delivery request.")
        self.delivery_id = delivery_id


class SensitiveHeadersUnavailable(Exception):
    def __init__(self, names: list[str]) -> None:
        super().__init__(
            "Original values of redacted headers are not available in memory (e.g. after a restart): "
            + ", ".join(names)
            + '. Re-supply them in the retry request body: {"headers": {...}}. Nothing was sent.'
        )
        self.names = names


class InvalidRetryHeaders(ValueError):
    pass


class SigningConfigurationError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Process-memory credential cache (NOT a secret manager)
# --------------------------------------------------------------------------- #


class CredentialCache:
    """Bounded in-memory map: delivery id → original sensitive header values.

    Exists only so that a manual retry can resend the partner credentials that were redacted from
    evidence. Never persisted; lost on restart (then retry asks the caller to re-supply them).
    """

    def __init__(self, max_entries: int = 10_000) -> None:
        self._data: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._max = max_entries
        self._lock = Lock()

    def put(self, delivery_id: str, values: dict[str, str]) -> None:
        if not values:
            return
        with self._lock:
            self._data[delivery_id] = dict(values)
            self._data.move_to_end(delivery_id)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def get(self, delivery_id: str) -> dict[str, str]:
        with self._lock:
            return dict(self._data.get(delivery_id, {}))

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __repr__(self) -> str:  # never print values
        return f"<CredentialCache entries={len(self._data)}>"


credential_cache = CredentialCache()


def _signing_clock() -> int:
    """UTC Unix seconds used for the signed timestamp (patchable in tests)."""
    return signing.now_ts()


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    limit = config.settings.max_body_chars
    if len(text) > limit:
        return text[:limit] + f"... [truncated {len(text) - limit} chars]"
    return text


def _serialize_payload(payload: Any) -> str | None:
    if payload is None:
        return None
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _set_header(headers: dict[str, str], name: str, value: str) -> None:
    """Set a header, replacing any existing one with the same name in any letter case."""
    for existing in [k for k in headers if k.lower() == name.lower()]:
        del headers[existing]
    headers[name] = value


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


def find_by_idempotency_key(db: Session, key: str) -> CallbackDelivery | None:
    return db.scalar(select(CallbackDelivery).where(CallbackDelivery.idempotency_key == key))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def fingerprint_of(data: DeliveryCreate) -> str:
    return request_fingerprint(
        destination_url=str(data.destination_url),
        method=data.method,
        headers=data.headers,
        payload=data.payload,
        timeout_seconds=data.timeout_seconds,
    )


def create_delivery(
    db: Session,
    data: DeliveryCreate,
    *,
    idempotency_key: str | None = None,
    fingerprint: str | None = None,
) -> CallbackDelivery:
    """Step 1: persist the callback as *pending* — before anything is sent.

    Only the sanitised headers are persisted; original sensitive values go to the in-memory cache.
    """
    if idempotency_key is not None and fingerprint is None:
        fingerprint = fingerprint_of(data)
    delivery = CallbackDelivery(
        destination_url=str(data.destination_url),
        method=data.method,
        headers=redact_headers(data.headers),
        payload=data.payload,
        timeout_seconds=data.timeout_seconds,
        status=DeliveryStatus.PENDING,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
    )
    db.add(delivery)
    db.commit()  # may raise IntegrityError on a duplicate idempotency key
    db.refresh(delivery)
    credential_cache.put(delivery.id, sensitive_values(data.headers))
    log.info("delivery created id=%s idempotent=%s", delivery.id, idempotency_key is not None)
    return delivery


def _replay_or_conflict(existing: CallbackDelivery, fingerprint: str) -> CallbackDelivery:
    if existing.request_fingerprint != fingerprint:
        log.info("idempotency conflict delivery=%s", existing.id)
        raise IdempotencyConflict(existing.id)
    log.info("idempotent replay delivery=%s", existing.id)
    return existing


def create_or_get_delivery(db: Session, data: DeliveryCreate, idempotency_key: str) -> tuple[CallbackDelivery, bool]:
    """Idempotent creation. Returns ``(delivery, created)``; raises :class:`IdempotencyConflict`."""
    fingerprint = fingerprint_of(data)
    existing = find_by_idempotency_key(db, idempotency_key)
    if existing is not None:
        return _replay_or_conflict(existing, fingerprint), False
    try:
        delivery = create_delivery(db, data, idempotency_key=idempotency_key, fingerprint=fingerprint)
    except IntegrityError:
        # Lost a race: another request inserted the same key between our lookup and our commit.
        db.rollback()
        existing = find_by_idempotency_key(db, idempotency_key)
        if existing is None:  # pragma: no cover - integrity error for some other reason
            raise
        return _replay_or_conflict(existing, fingerprint), False
    return delivery, True


def _build_request_headers(
    delivery: CallbackDelivery, attempt_number: int, originals: dict[str, str]
) -> dict[str, str]:
    """Outbound headers: stored headers with redacted values restored from ``originals``."""
    missing = sorted(k for k, v in (delivery.headers or {}).items() if v == REDACTED and k.lower() not in originals)
    if missing:
        raise SensitiveHeadersUnavailable(missing)

    headers: dict[str, str] = {}
    _set_header(headers, "Content-Type", "application/json")
    _set_header(headers, "User-Agent", USER_AGENT)
    for name, value in (delivery.headers or {}).items():
        _set_header(headers, name, originals[name.lower()] if value == REDACTED else value)
    _set_header(headers, "X-Callback-Id", delivery.id)
    _set_header(headers, "X-Callback-Attempt", str(attempt_number))
    return headers


async def send_attempt(
    db: Session,
    delivery: CallbackDelivery,
    client: httpx.AsyncClient,
    *,
    sensitive_overrides: dict[str, str] | None = None,
) -> DeliveryAttempt:
    """Step 2: record a pending attempt, do the HTTP call, record the outcome."""
    s = config.settings
    if s.signing_enabled and not s.signing_secret:  # defence in depth; startup validation should catch it
        raise SigningConfigurationError("signing enabled but CALLBACK_SIGNING_SECRET is empty")

    originals = credential_cache.get(delivery.id)
    originals.update({k.lower(): v for k, v in (sensitive_overrides or {}).items()})

    attempt_number = delivery.attempt_count + 1
    request_headers = _build_request_headers(delivery, attempt_number, originals)
    request_body = _serialize_payload(delivery.payload)
    # Serialise once: these exact bytes are signed AND sent.
    body_bytes = request_body.encode("utf-8") if request_body is not None else None

    signature_ts: int | None = None
    if s.signing_enabled:
        # Server-side signing never trusts caller-supplied signature/timestamp headers: overwrite them.
        signature_ts = _signing_clock()
        _set_header(request_headers, s.timestamp_header, str(signature_ts))
        signature = signing.compute_signature(s.signing_secret, signature_ts, body_bytes or b"")
        _set_header(request_headers, s.signature_header, signature)

    attempt = DeliveryAttempt(
        delivery_id=delivery.id,
        attempt_number=attempt_number,
        destination_url=delivery.destination_url,
        method=delivery.method,
        request_headers=redact_headers(request_headers),  # evidence is sanitised; the wire is not
        request_body=request_body,
        status=DeliveryStatus.PENDING,
        signature_algorithm=signing.ALGORITHM if signature_ts is not None else None,
        signature_timestamp=signature_ts,
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
            content=body_bytes,
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
        attempt.response_headers = redact_headers(dict(response.headers))
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
    # Never log headers or bodies — only identifiers and outcomes.
    log.info(
        "attempt finished delivery=%s attempt=%d status=%s http=%s signed=%s",
        delivery.id,
        attempt_number,
        attempt.status.value,
        attempt.http_status,
        signature_ts is not None,
    )
    return attempt


async def retry_delivery(
    db: Session,
    delivery_id: str,
    client: httpx.AsyncClient,
    *,
    resupplied_headers: dict[str, str] | None = None,
) -> CallbackDelivery:
    """Manual retry: only allowed for failed deliveries. Always a NEW attempt (#N+1).

    Not affected by idempotency: Idempotency-Key guards *creation* of a delivery, not attempts.
    """
    delivery = get_delivery(db, delivery_id)
    if delivery.status != DeliveryStatus.FAILED:
        raise RetryNotAllowed(
            f"Delivery {delivery_id} is {delivery.status.value}, only failed deliveries can be retried"
        )
    overrides: dict[str, str] = {}
    if resupplied_headers:
        redacted = {k.lower() for k, v in (delivery.headers or {}).items() if v == REDACTED}
        unknown = sorted(k for k in resupplied_headers if k.lower() not in redacted)
        if unknown:
            raise InvalidRetryHeaders(
                "Only headers redacted on this delivery can be re-supplied; not redacted: " + ", ".join(unknown)
            )
        overrides = {k.lower(): v for k, v in resupplied_headers.items()}
    await send_attempt(db, delivery, client, sensitive_overrides=overrides)
    if overrides:
        credential_cache.put(delivery.id, {**credential_cache.get(delivery.id), **overrides})
    db.expire(delivery, ["attempts"])
    return get_delivery(db, delivery_id)
