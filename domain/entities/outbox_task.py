from dataclasses import dataclass
from uuid import UUID

from domain.enums import OutboxTaskType


@dataclass
class OutboxTask:
    id: UUID
    task_type: OutboxTaskType
    payload: dict
    idempotency_key: str
    attempts: int
