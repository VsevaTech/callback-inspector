"""JSON API."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app import service
from app.database import get_db
from app.errors import ApiError
from app.http_client import get_http_client
from app.schemas import (
    ApiErrorOut,
    AttemptOut,
    DeliveryCreate,
    DeliveryDetailOut,
    DeliveryListOut,
    DeliveryOut,
    RetryRequest,
)
from app.security import InvalidIdempotencyKey, validate_idempotency_key

router = APIRouter(prefix="/api", tags=["deliveries"])

CREATE_DESCRIPTION = """
Create a callback delivery, **persist it, then attempt to send it once**.

**Optional `Idempotency-Key` header** (1-255 printable ASCII characters):

| Situation | Result |
|---|---|
| no key | current behaviour: always a new delivery, `201` |
| new key | delivery created and sent, `201` |
| same key + same logical request | existing delivery, **nothing sent again**, `200`, `idempotent_replay: true` |
| same key + different request | `409` `IDEMPOTENCY_CONFLICT`, nothing is sent |

The logical request is fingerprinted as SHA-256 of canonical JSON of `destination_url`, `method`, `headers`
(names lower-cased; generated `X-Callback-*` headers excluded; sensitive values not included), `payload`,
`timeout_seconds`. Idempotency protects against duplicate *creation* for repeated requests using the same
key; it is not exactly-once delivery. Replays also carry the `Idempotent-Replayed: true` response header.

**Security:** sensitive headers are delivered to the destination but redacted from stored evidence
(`***REDACTED***`). Outbound callbacks may be HMAC-SHA256 signed when configured
(`X-Callback-Timestamp` + `X-Callback-Signature: sha256=<hex>` over `"{timestamp}." + raw_body`);
caller-supplied signature/timestamp headers are overwritten when signing is on.
"""

_ERR = {"model": ApiErrorOut}


@router.post(
    "/deliveries",
    response_model=DeliveryDetailOut,
    status_code=status.HTTP_201_CREATED,
    description=CREATE_DESCRIPTION,
    responses={
        200: {"model": DeliveryDetailOut, "description": "Idempotent replay: existing delivery, nothing sent"},
        400: {**_ERR, "description": "INVALID_IDEMPOTENCY_KEY"},
        409: {**_ERR, "description": "IDEMPOTENCY_CONFLICT"},
    },
)
async def create_delivery(
    data: DeliveryCreate,
    response: Response,
    idempotency_key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        description="Optional. Same key + same request → existing delivery; same key + different request → 409.",
    ),
    db: Session = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> DeliveryDetailOut:
    if idempotency_key is None:
        delivery, created = service.create_delivery(db, data), True
    else:
        try:
            validate_idempotency_key(idempotency_key)
        except InvalidIdempotencyKey as exc:
            raise ApiError(400, "INVALID_IDEMPOTENCY_KEY", str(exc)) from None
        try:
            delivery, created = service.create_or_get_delivery(db, data, idempotency_key)
        except service.IdempotencyConflict as exc:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", str(exc)) from None

    if created:
        await service.send_attempt(db, delivery, client)
    else:
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replayed"] = "true"
    out = DeliveryDetailOut.model_validate(service.get_delivery(db, delivery.id))
    out.idempotent_replay = not created
    return out


@router.get("/deliveries", response_model=DeliveryListOut)
def list_deliveries(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> DeliveryListOut:
    items, total = service.list_deliveries(db, limit=limit, offset=offset)
    return DeliveryListOut(items=[DeliveryOut.model_validate(d) for d in items], total=total)


@router.get("/deliveries/{delivery_id}", response_model=DeliveryDetailOut)
def get_delivery(delivery_id: str, db: Session = Depends(get_db)) -> DeliveryDetailOut:
    try:
        return DeliveryDetailOut.model_validate(service.get_delivery(db, delivery_id))
    except service.DeliveryNotFound:
        raise HTTPException(status_code=404, detail="Delivery not found") from None


@router.get("/deliveries/{delivery_id}/attempts", response_model=list[AttemptOut])
def list_attempts(delivery_id: str, db: Session = Depends(get_db)) -> list[AttemptOut]:
    try:
        delivery = service.get_delivery(db, delivery_id)
    except service.DeliveryNotFound:
        raise HTTPException(status_code=404, detail="Delivery not found") from None
    return [AttemptOut.model_validate(a) for a in delivery.attempts]


@router.post(
    "/deliveries/{delivery_id}/retry",
    response_model=DeliveryDetailOut,
    description=(
        "Manual retry of a **failed** delivery: always creates a new attempt #N+1 (not affected by "
        "Idempotency-Key, which guards creation only). Signed deliveries get a fresh timestamp and signature. "
        "Redacted credentials are resent from process memory; after a restart re-supply them in the body "
        '(`{"headers": {"Authorization": "..."}}`), otherwise `409 SENSITIVE_HEADERS_UNAVAILABLE` and nothing is sent.'
    ),
    responses={409: {"description": "not failed, or SENSITIVE_HEADERS_UNAVAILABLE"}, 422: _ERR},
)
async def retry_delivery(
    delivery_id: str,
    body: RetryRequest | None = Body(default=None),
    db: Session = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> DeliveryDetailOut:
    try:
        delivery = await service.retry_delivery(
            db, delivery_id, client, resupplied_headers=body.headers if body else None
        )
    except service.DeliveryNotFound:
        raise HTTPException(status_code=404, detail="Delivery not found") from None
    except service.RetryNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except service.SensitiveHeadersUnavailable as exc:
        raise ApiError(409, "SENSITIVE_HEADERS_UNAVAILABLE", str(exc)) from None
    except service.InvalidRetryHeaders as exc:
        raise ApiError(422, "INVALID_RETRY_HEADERS", str(exc)) from None
    return DeliveryDetailOut.model_validate(delivery)
