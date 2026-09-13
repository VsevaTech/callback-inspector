"""ORM models: a CallbackDelivery and its DeliveryAttempts."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class DeliveryStatus(enum.StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class CallbackDelivery(Base):
    """One logical callback that we promised to deliver to a partner."""

    __tablename__ = "callback_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    destination_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False, default="POST")
    headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)

    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=DeliveryStatus.PENDING,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    attempts: Mapped[list[DeliveryAttempt]] = relationship(
        back_populates="delivery",
        cascade="all, delete-orphan",
        order_by="DeliveryAttempt.attempt_number",
    )


class DeliveryAttempt(Base):
    """A single HTTP attempt to deliver a callback — full request/response evidence."""

    __tablename__ = "delivery_attempts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    delivery_id: Mapped[str] = mapped_column(
        ForeignKey("callback_deliveries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    destination_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    request_headers: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    request_body: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        default=DeliveryStatus.PENDING,
    )
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_headers: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    delivery: Mapped[CallbackDelivery] = relationship(back_populates="attempts")
