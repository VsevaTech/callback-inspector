"""Pydantic request/response schemas for the JSON API."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from app import config
from app.models import DeliveryStatus

HttpMethod = Literal["POST", "PUT", "PATCH", "DELETE", "GET"]

# RFC 9110 token for header names; values must not smuggle CR/LF or other control characters.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HEADER_VALUE_BAD_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def _check_headers(value: dict[str, str]) -> dict[str, str]:
    cleaned: dict[str, str] = {}
    for k, v in value.items():
        name = k.strip()
        if not name:
            continue
        if not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid header name: {name!r}")
        if _HEADER_VALUE_BAD_RE.search(v):
            raise ValueError(f"header {name!r} contains control characters")
        cleaned[name] = v
    return cleaned


class DeliveryCreate(BaseModel):
    destination_url: HttpUrl
    method: HttpMethod = "POST"
    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Headers sent to the destination as-is. Sensitive ones (Authorization, Cookie, X-API-Key, … "
            "plus SENSITIVE_HEADERS) are delivered to the destination but redacted from stored evidence."
        ),
    )
    payload: Any = None
    timeout_seconds: float = Field(default=config.settings.default_timeout, gt=0, le=config.settings.max_timeout)

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_headers(value)


class RetryRequest(BaseModel):
    """Optional body for a manual retry."""

    headers: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Re-supply values for sensitive headers that were redacted from evidence. Only needed when the "
            "original values are no longer in process memory (e.g. after a restart). Only headers that were "
            "redacted on this delivery are accepted; they are used for sending and are not stored."
        ),
    )

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        return _check_headers(value)


class ApiErrorOut(BaseModel):
    code: str
    message: str
    detail: str


class AttemptOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    attempt_number: int
    destination_url: str
    method: str
    request_headers: dict[str, str] = Field(description="As sent, with sensitive values replaced by ***REDACTED***.")
    request_body: str | None
    status: DeliveryStatus
    http_status: int | None
    response_headers: dict[str, str] | None = Field(description="Partner response headers, sanitised.")
    response_body: str | None
    error: str | None
    latency_ms: float | None
    signature_algorithm: str | None = Field(default=None, description="HMAC-SHA256 when this attempt was signed.")
    signature_timestamp: int | None = Field(default=None, description="UTC Unix timestamp that was signed.")
    started_at: datetime
    finished_at: datetime | None


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    destination_url: str
    method: str
    headers: dict[str, str] = Field(description="Configured headers, sensitive values redacted.")
    payload: Any
    timeout_seconds: float
    idempotency_key: str | None = None
    request_fingerprint: str | None = Field(default=None, description="SHA-256 of the canonical logical request.")
    status: DeliveryStatus
    attempt_count: int
    last_http_status: int | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    delivered_at: datetime | None


class DeliveryDetailOut(DeliveryOut):
    attempts: list[AttemptOut]
    idempotent_replay: bool = Field(
        default=False,
        description="true when this response replays an existing delivery for a repeated Idempotency-Key.",
    )


class DeliveryListOut(BaseModel):
    items: list[DeliveryOut]
    total: int
