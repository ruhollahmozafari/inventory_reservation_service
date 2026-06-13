import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from domain.entities.reservation import Reservation, ReservationItem
from domain.enums import ReservationStatus, HoldStatus
from domain.repositories.reservation_repo import AbstractReservationRepository
from infra.db.models import ReservationModel, ReservationItemModel


class ReservationRepository(AbstractReservationRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, reservation_id: uuid.UUID) -> Reservation | None:
        row = await self._session.scalar(
            select(ReservationModel)
            .options(selectinload(ReservationModel.items))
            .where(ReservationModel.id == reservation_id)
        )
        return self._to_domain(row) if row else None

    async def get_by_idempotency_key(self, key: str) -> Reservation | None:
        row = await self._session.scalar(
            select(ReservationModel)
            .options(selectinload(ReservationModel.items))
            .where(ReservationModel.idempotency_key == key)
        )
        return self._to_domain(row) if row else None

    async def save(self, reservation: Reservation) -> Reservation:
        row = await self._session.get(ReservationModel, reservation.id)
        if row is None:
            row = ReservationModel(
                id=reservation.id,
                user_id=reservation.user_id,
                idempotency_key=reservation.idempotency_key,
                status=reservation.status,
                expires_at=reservation.expires_at,
                created_at=reservation.created_at,
                confirmed_at=reservation.confirmed_at,
            )
            self._session.add(row)
            for item in reservation.items:
                self._session.add(ReservationItemModel(
                    id=item.id,
                    reservation_id=reservation.id,
                    product_id=item.product_id,
                    provider_id=item.provider_id,
                    qty=item.qty,
                    provider_ref=item.provider_ref,
                    hold_status=item.hold_status,
                ))
        else:
            row.status = reservation.status
            row.confirmed_at = reservation.confirmed_at
        await self._session.flush()
        return reservation

    async def cas_status(
        self,
        reservation_id: uuid.UUID,
        expected: ReservationStatus,
        new: ReservationStatus,
    ) -> bool:
        """Compare-And-Swap: atomically transitions status only if it matches `expected`."""
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.id == reservation_id,
                ReservationModel.status == expected,
            )
            .values(status=new)
        )
        return result.rowcount == 1

    async def claim_expired(self, batch_size: int) -> list[Reservation]:
        """
        CAS: PENDING → EXPIRED where expires_at < now().
        Uses SKIP LOCKED so parallel sweepers don't double-claim.
        Returns the rows that were claimed.
        """
        now = datetime.now(timezone.utc)
        result = await self._session.execute(
            update(ReservationModel)
            .where(
                ReservationModel.status == ReservationStatus.PENDING,
                ReservationModel.expires_at < now,
            )
            .values(status=ReservationStatus.EXPIRED)
            .returning(ReservationModel.id)
        )
        claimed_ids = [row[0] for row in result.all()]
        if not claimed_ids:
            return []

        rows = (
            await self._session.scalars(
                select(ReservationModel)
                .options(selectinload(ReservationModel.items))
                .where(ReservationModel.id.in_(claimed_ids))
            )
        ).all()
        return [self._to_domain(r) for r in rows]

    @staticmethod
    def _to_domain(row: ReservationModel) -> Reservation:
        items = [
            ReservationItem(
                id=item.id,
                reservation_id=item.reservation_id,
                product_id=item.product_id,
                provider_id=item.provider_id,
                qty=item.qty,
                hold_status=item.hold_status,
                provider_ref=item.provider_ref,
            )
            for item in (row.items or [])
        ]
        return Reservation(
            id=row.id,
            user_id=row.user_id,
            idempotency_key=row.idempotency_key,
            status=row.status,
            expires_at=row.expires_at,
            created_at=row.created_at,
            confirmed_at=row.confirmed_at,
            items=items,
        )
