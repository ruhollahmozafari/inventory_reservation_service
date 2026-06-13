"""
Cancel-reservation use case — §7 "Cancel — BACKGROUND".
CAS PENDING → CANCELLED, release internal holds inline, enqueue RELEASE for external.
"""
import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.reservation import Reservation
from domain.enums import HoldStatus, OutboxTaskType, ReservationStatus
from infra.db.models import ProviderModel
from infra.db.repositories.inventory_repo import InventoryRepository
from infra.db.repositories.outbox_repo import OutboxRepository
from infra.db.repositories.reservation_repo import ReservationRepository
from infra.db.transaction import atomic


class CancelReservationUseCase:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._reservation_repo = ReservationRepository(session)
        self._outbox_repo = OutboxRepository(session)
        self._inv_repo = InventoryRepository(session)

    async def execute(self, reservation_id: uuid.UUID) -> Reservation:
        async with atomic(self._session):
            claimed = await self._reservation_repo.cas_status(
                reservation_id, ReservationStatus.PENDING, ReservationStatus.CANCELLED
            )
            reservation = await self._reservation_repo.get_by_id(reservation_id)

            if reservation is None:
                raise HTTPException(status_code=404, detail="Reservation not found")

            if not claimed:
                if reservation.status == ReservationStatus.CANCELLED:
                    return reservation
                raise HTTPException(
                    status_code=409, detail=f"Cannot cancel: status={reservation.status.value}"
                )

            for item in reservation.items:
                if item.hold_status != HoldStatus.HELD:
                    continue
                provider = await self._session.get(ProviderModel, item.provider_id)
                if provider and provider.type.value == "internal" and item.provider_ref:
                    inv_id, qty = item.provider_ref.split(":")
                    await self._inv_repo.release(uuid.UUID(inv_id), int(qty))
                    item.hold_status = HoldStatus.RELEASED
                elif item.provider_ref:
                    await self._outbox_repo.enqueue(
                        task_type=OutboxTaskType.RELEASE,
                        payload={"item_id": str(item.id), "provider_id": str(item.provider_id), "provider_ref": item.provider_ref},
                        idempotency_key=f"release:cancel:{reservation_id}:{item.id}",
                    )

        return reservation
