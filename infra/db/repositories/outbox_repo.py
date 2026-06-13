import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.outbox_task import OutboxTask
from domain.enums import OutboxTaskType, OutboxStatus
from domain.repositories.outbox_repo import AbstractOutboxRepository
from infra.db.models import OutboxModel


class OutboxRepository(AbstractOutboxRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def enqueue(
        self,
        task_type: OutboxTaskType,
        payload: dict,
        idempotency_key: str,
        next_run_at: datetime | None = None,
    ) -> uuid.UUID:
        task_id = uuid.uuid4()
        row = OutboxModel(
            id=task_id,
            task_type=task_type,
            payload=payload,
            idempotency_key=idempotency_key,
            status=OutboxStatus.PENDING,
            next_run_at=next_run_at or datetime.now(timezone.utc),
        )
        self._session.add(row)
        await self._session.flush()
        return task_id

    async def claim_batch(self, batch_size: int, lease_seconds: int) -> list[OutboxTask]:
        """
        Three-phase worker claim (per §6 rule 0):
        1. SELECT … FOR UPDATE SKIP LOCKED — find claimable rows
        2. UPDATE status=PROCESSING, locked_until=now+lease
        3. Return claimed domain objects — caller must commit to release the lock

        Reclaims rows where status=PROCESSING AND locked_until < now (dead-worker recovery).
        """
        now = datetime.now(timezone.utc)
        locked_until = now + timedelta(seconds=lease_seconds)

        rows = (
            await self._session.scalars(
                select(OutboxModel)
                .where(
                    (OutboxModel.status == OutboxStatus.PENDING)
                    | (
                        (OutboxModel.status == OutboxStatus.PROCESSING)
                        & (OutboxModel.locked_until < now)
                    ),
                    OutboxModel.next_run_at <= now,
                )
                .order_by(OutboxModel.next_run_at)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()

        if not rows:
            return []

        claimed_ids = [r.id for r in rows]
        await self._session.execute(
            update(OutboxModel)
            .where(OutboxModel.id.in_(claimed_ids))
            .values(
                status=OutboxStatus.PROCESSING,
                locked_until=locked_until,
                attempts=OutboxModel.attempts + 1,
            )
        )

        return [
            OutboxTask(
                id=r.id,
                task_type=r.task_type,
                payload=r.payload,
                idempotency_key=r.idempotency_key,
                attempts=r.attempts + 1,
            )
            for r in rows
        ]

    async def mark_done(self, task_id: uuid.UUID) -> None:
        await self._session.execute(
            update(OutboxModel)
            .where(OutboxModel.id == task_id)
            .values(status=OutboxStatus.DONE)
        )

    async def mark_failed(self, task_id: uuid.UUID, error: str, next_run_at: datetime) -> None:
        await self._session.execute(
            update(OutboxModel)
            .where(OutboxModel.id == task_id)
            .values(
                status=OutboxStatus.PENDING,
                last_error=error,
                next_run_at=next_run_at,
                locked_until=None,
            )
        )
