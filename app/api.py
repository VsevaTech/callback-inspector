"""JSON API."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app import service
from app.database import get_db
from app.http_client import get_http_client
from app.schemas import AttemptOut, DeliveryCreate, DeliveryDetailOut, DeliveryListOut, DeliveryOut

router = APIRouter(prefix="/api", tags=["deliveries"])


@router.post("/deliveries", response_model=DeliveryDetailOut, status_code=status.HTTP_201_CREATED)
async def create_delivery(
    data: DeliveryCreate,
    db: Session = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> DeliveryDetailOut:
    """Create a callback delivery, persist it, then attempt to send it once."""
    delivery = service.create_delivery(db, data)
    await service.send_attempt(db, delivery, client)
    return DeliveryDetailOut.model_validate(service.get_delivery(db, delivery.id))


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


@router.post("/deliveries/{delivery_id}/retry", response_model=DeliveryDetailOut)
async def retry_delivery(
    delivery_id: str,
    db: Session = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_http_client),
) -> DeliveryDetailOut:
    try:
        delivery = await service.retry_delivery(db, delivery_id, client)
    except service.DeliveryNotFound:
        raise HTTPException(status_code=404, detail="Delivery not found") from None
    except service.RetryNotAllowed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return DeliveryDetailOut.model_validate(delivery)
