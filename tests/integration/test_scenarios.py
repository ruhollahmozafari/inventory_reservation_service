"""
Integration scenarios (§12 of BUILD-SPEC).

Tests call the real HTTP API (via AsyncClient + ASGITransport).
External provider HTTP calls are mocked at ExternalReserveAdapter level — no fake server needed.
Internal provider flows hit the real DB (no mocking at all).
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.use_cases.create_reservation import CreateReservationUseCase
from domain.enums import ReservationStatus
from infra.providers.adapters.external_reserve import ExternalReserveAdapter
from infra.providers.port import ConfirmResult, ReserveResult
from tests.factories import create_inventory, create_product, create_provider


class TestInternalReserve:
    async def test_reserve_internal_stock_creates_pending_reservation(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Internal provider: reserve deducts stock directly from our DB — no HTTP call."""
        product = await create_product(db_session)
        provider = await create_provider(db_session, provider_type="internal")
        await create_inventory(db_session, product.id, provider.id, qty_on_hand=10)
        await db_session.commit()

        response = await client.post(
            "/api/v1/reservations",
            json={
                "user_id": "user-1",
                "idempotency_key": f"idem-{uuid.uuid4()}",
                "items": [{"product_id": str(product.id), "provider_id": str(provider.id), "qty": 3}],
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "PENDING"
        assert body["items"][0]["hold_status"] == "HELD"
        #todo check the data for the internal result on db, the qty should be update and the rows should created for reservation.


class TestExternalReserve:
    async def test_reserve_external_provider_mocks_http_call(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """External provider: reserve mocks the HTTP call, stores provider_ref."""
        product = await create_product(db_session)
        provider = await create_provider(
            db_session, provider_type="external", base_url="http://warehouse.test"
        )
        await create_inventory(db_session, product.id, provider.id, qty_on_hand=10)
        await db_session.commit()

        with patch.object(ExternalReserveAdapter, "reserve", new_callable=AsyncMock) as mock_reserve:
            mock_reserve.return_value = ReserveResult(success=True, provider_ref="hold-abc-123")

            response = await client.post(
                "/api/v1/reservations",
                json={
                    "user_id": "user-1",
                    "idempotency_key": f"idem-{uuid.uuid4()}",
                    "items": [{"product_id": str(product.id), "provider_id": str(provider.id), "qty": 2}],
                },
            )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "PENDING"
        assert body["items"][0]["provider_ref"] == "hold-abc-123"
        mock_reserve.assert_called_once()

    async def test_reserve_then_confirm_full_happy_path(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """End-to-end: reserve → confirm → reservation CONFIRMED, order created."""
        product = await create_product(db_session)
        provider = await create_provider(
            db_session, provider_type="external", base_url="http://warehouse.test"
        )
        await create_inventory(db_session, product.id, provider.id, qty_on_hand=10)
        await db_session.commit()

        with patch.object(ExternalReserveAdapter, "reserve", new_callable=AsyncMock) as mock:
            mock.return_value = ReserveResult(success=True, provider_ref="hold-xyz")
            reserve = await client.post(
                "/api/v1/reservations",
                json={
                    "user_id": "user-1",
                    "idempotency_key": f"idem-{uuid.uuid4()}",
                    "items": [{"product_id": str(product.id), "provider_id": str(provider.id), "qty": 1}],
                },
            )

        assert reserve.status_code == 201
        reservation_id = reserve.json()["id"]

        with patch.object(ExternalReserveAdapter, "confirm", new_callable=AsyncMock) as mock:
            mock.return_value = ConfirmResult(success=True)
            confirm = await client.post(
                f"/api/v1/reservations/{reservation_id}/confirm",
            )

        assert confirm.status_code == 200
        assert confirm.json()["status"] == "CONFIRMED"


class TestNoOversell:
    async def test_concurrent_reserves_on_last_unit_exactly_one_wins(self, session_factory):
        """5 concurrent reserves on 1 unit of internal stock → exactly 1 PENDING, 4 FAILED."""
        async with session_factory() as session:
            product = await create_product(session)
            provider = await create_provider(session, provider_type="internal")
            await create_inventory(session, product.id, provider.id, qty_on_hand=1)
            await session.commit()
            product_id, provider_id = product.id, provider.id

        async def try_reserve(n: int):
            async with session_factory() as session:
                return await CreateReservationUseCase(session).execute(
                    user_id=f"user-{n}",
                    idempotency_key=f"concurrent-{uuid.uuid4()}",
                    items=[(product_id, provider_id, 1)],
                )

        results = await asyncio.gather(*[try_reserve(n) for n in range(5)], return_exceptions=True)
        statuses = [r.status.value for r in results if not isinstance(r, Exception)]

        assert statuses.count("PENDING") == 1
        assert statuses.count("FAILED") == 4
