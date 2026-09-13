"""Pydantic request/response schemas for the JSON API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app.config import settings
from app.models import DeliveryStatus

HttpMethod = Literal["POST", "PUT", "PATCH", "DELETE", "GET"]


class DeliveryCreate(BaseModel):
    destination_url: HttpUrl
    method: HttpMethod = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    payload: Any = None
    timeout_seconds: float = Field(default=settings.default_timeout, gt=0, le=settings.max_timeout)

    @field_validator("headers")
    @classmethod
    def _strip_header_names(cls, value: dict[str, str]) -> dict[str, str]:
        return {k.strip(): v for k, v in value.items() if k.strip()}


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    attempt_number: int
    destination_url: str
    method: str
    request_headers: dict[str, str]
    request_body: str | None
    status: DeliveryStatus
    http_status: int | None
    response_headers: dict[str, str] | None
    response_body: str | None
    error: str | None
    latency_ms: float | None
    started_at: datetime
    finished_at: datetime | None


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    destination_url: str
    method: str
    headers: dict[str, str]
    payload: Any
    timeout_seconds: float
    status: DeliveryStatus
    attempt_count: int
    last_http_status: int | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None


class DeliveryDetailOut(DeliveryOut):
    attempts: list[AttemptOut]


class DeliveryListOut(BaseModel):
    items: list[DeliveryOut]
    total: int
