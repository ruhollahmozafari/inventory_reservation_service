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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.use_cases.create_reservation import CreateReservationUseCase
from domain.enums import OutboxTaskType, ReservationStatus
from infra.db.models import OutboxModel
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


class TestExternalFailures:
    async def test_timeout_marks_item_pending_unknown_and_enqueues_reconcile(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """
        Provider timeout → PENDING_UNKNOWN item + RECONCILE outbox task.
        The system can't know whether the hold was placed, so it fails the reservation
        and enqueues a RECONCILE task to release the hold by idempotency key if it exists.
        """
        product = await create_product(db_session)
        provider = await create_provider(
            db_session, provider_type="external", base_url="http://warehouse.test"
        )
        await create_inventory(db_session, product.id, provider.id, qty_on_hand=10)
        await db_session.commit()

        with patch.object(ExternalReserveAdapter, "reserve", new_callable=AsyncMock) as mock:
            mock.side_effect = TimeoutError("simulated timeout")
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
        assert body["status"] == "FAILED"
        assert body["items"][0]["hold_status"] == "PENDING_UNKNOWN"

        tasks = (await db_session.scalars(
            select(OutboxModel).where(OutboxModel.task_type == OutboxTaskType.RECONCILE)
        )).all()
        assert len(tasks) == 1
        assert tasks[0].payload["provider_id"] == str(provider.id)

    async def test_provider_rejection_creates_failed_reservation(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """
        Provider returns success=False (e.g. 409 insufficient stock) → reservation FAILED,
        no RECONCILE needed because no hold was placed.
        """
        product = await create_product(db_session)
        provider = await create_provider(
            db_session, provider_type="external", base_url="http://warehouse.test"
        )
        await create_inventory(db_session, product.id, provider.id, qty_on_hand=10)
        await db_session.commit()

        with patch.object(ExternalReserveAdapter, "reserve", new_callable=AsyncMock) as mock:
            mock.return_value = ReserveResult(success=False, error="Provider: insufficient stock")
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
        assert body["status"] == "FAILED"
        assert body["items"][0]["hold_status"] == "FAILED"

        # Definitive failure — no orphan possible, so no RECONCILE task.
        reconcile_tasks = (await db_session.scalars(
            select(OutboxModel).where(OutboxModel.task_type == OutboxTaskType.RECONCILE)
        )).all()
        assert len(reconcile_tasks) == 0


class TestIdempotency:
    async def test_duplicate_request_returns_same_reservation(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """Same idempotency key on two requests → same reservation ID, no double-reserve."""
        product = await create_product(db_session)
        provider = await create_provider(db_session, provider_type="internal")
        await create_inventory(db_session, product.id, provider.id, qty_on_hand=10)
        await db_session.commit()

        idem_key = f"idem-{uuid.uuid4()}"
        body = {
            "user_id": "user-1",
            "idempotency_key": idem_key,
            "items": [{"product_id": str(product.id), "provider_id": str(provider.id), "qty": 1}],
        }

        first = await client.post("/api/v1/reservations", json=body)
        second = await client.post("/api/v1/reservations", json=body)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["status"] == "PENDING"


class TestSoftHold:
    async def test_soft_hold_reserves_against_local_db_mirror(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """
        Read-only provider (capabilities.reserve=False) → SoftHoldAdapter, zero HTTP calls.
        Reserve deducts from the local DB mirror kept fresh by the availability sync worker.
        provider_ref encodes "inventory_id:qty" so release can undo it without a network call.
        """
        product = await create_product(db_session)
        provider = await create_provider(
            db_session,
            provider_type="external",
            capabilities={"reserve": False, "confirm": False, "release": False, "unconfirm": False},
        )
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
        # provider_ref encodes "inventory_id:qty" for local release (no HTTP needed)
        ref = body["items"][0]["provider_ref"]
        assert ref is not None and ":" in ref
        qty_in_ref = int(ref.split(":")[1])
        assert qty_in_ref == 3

    async def test_insufficient_internal_stock_fails_cleanly(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        """
        Reserve qty exceeds available stock → reservation FAILED before any external call.
        TX1 rolls back cleanly; nothing is persisted in the DB.
        """
        product = await create_product(db_session)
        provider = await create_provider(db_session, provider_type="internal")
        await create_inventory(db_session, product.id, provider.id, qty_on_hand=1)
        await db_session.commit()

        response = await client.post(
            "/api/v1/reservations",
            json={
                "user_id": "user-1",
                "idempotency_key": f"idem-{uuid.uuid4()}",
                "items": [{"product_id": str(product.id), "provider_id": str(provider.id), "qty": 5}],
            },
        )

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "FAILED"
        # TX1 rolled back → no items persisted, nothing to compensate
        assert body["items"] == []
