"""The delivery and its attempt must be committed *before* the outbound HTTP call happens."""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import select

from app import service
from app.models import CallbackDelivery, DeliveryAttempt, DeliveryStatus
from app.schemas import DeliveryCreate
from tests.conftest import RECEIVER_URL


class _ExplodingTransport(httpx.AsyncBaseTransport):
    """Simulates the process dying / network exploding mid-send — but first checks the DB."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.seen_pending = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # A *separate* session sees committed data only. If the rows are visible here,
        # they were committed before the network call.
        with self.session_factory() as other:
            delivery = other.scalar(select(CallbackDelivery))
            attempt = other.scalar(select(DeliveryAttempt))
            self.seen_pending = (
                delivery is not None
                and attempt is not None
                and delivery.status == DeliveryStatus.PENDING
                and attempt.status == DeliveryStatus.PENDING
                and attempt.request_body is not None
            )
        raise httpx.ConnectError("boom")


@pytest.mark.asyncio
async def test_rows_are_committed_before_network_io(db_session_factory):
    transport = _ExplodingTransport(db_session_factory)
    db = db_session_factory()
    try:
        data = DeliveryCreate(destination_url=RECEIVER_URL, payload={"x": 1})
        delivery = service.create_delivery(db, data)

        # Persisted as pending right after creation, before any send.
        with db_session_factory() as other:
            fresh = other.get(CallbackDelivery, delivery.id)
            assert fresh is not None and fresh.status == DeliveryStatus.PENDING

        async with httpx.AsyncClient(transport=transport) as client:
            attempt = await service.send_attempt(db, delivery, client)

        assert transport.seen_pending is True
        assert attempt.status == DeliveryStatus.FAILED
        assert "ConnectError" in attempt.error
        assert delivery.status == DeliveryStatus.FAILED
        assert delivery.attempt_count == 1
    finally:
        db.close()
