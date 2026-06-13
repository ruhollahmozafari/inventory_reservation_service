from abc import ABC, abstractmethod
from datetime import datetime
from uuid import UUID

from domain.entities.outbox_task import OutboxTask
from domain.enums import OutboxTaskType


class AbstractOutboxRepository(ABC):
    @abstractmethod
    async def enqueue(
        self,
        task_type: OutboxTaskType,
        payload: dict,
        idempotency_key: str,
        next_run_at: datetime | None = None,
    ) -> UUID:
        ...

    @abstractmethod
    async def claim_batch(self, batch_size: int, lease_seconds: int) -> list[OutboxTask]:
        """SKIP LOCKED claim — does not commit; caller is responsible for committing."""
        ...

    @abstractmethod
    async def mark_done(self, task_id: UUID) -> None:
        ...

    @abstractmethod
    async def mark_failed(self, task_id: UUID, error: str, next_run_at: datetime) -> None:
        ...
