from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from domain.enums import ReservationStatus, HoldStatus


@dataclass
class ReservationItem:
    id: UUID
    reservation_id: UUID
    product_id: UUID
    provider_id: UUID
    qty: int
    hold_status: HoldStatus
    provider_ref: str | None = None   # remote hold id or "inventory_id:qty" for local
    idempotency_key: str | None = None  # stored for RESERVING items so reconciler can find the hold


@dataclass
class Reservation:
    """
    Aggregate root. Reservation + items is one transaction boundary.
    Status transitions are enforced via CAS in the DB; this class reflects
    the loaded state and exposes guard predicates.
    """
    id: UUID
    user_id: str
    idempotency_key: str
    status: ReservationStatus
    expires_at: datetime
    created_at: datetime
    items: list[ReservationItem] = field(default_factory=list)
    confirmed_at: datetime | None = None
    creation_deadline: datetime | None = None  # crash-recovery: INITIALIZING past this → roll back

    def is_terminal(self) -> bool:
        return self.status in (
            ReservationStatus.CONFIRMED,
            ReservationStatus.CANCELLED,
            ReservationStatus.EXPIRED,
            ReservationStatus.FAILED,
        )

    def is_pending(self) -> bool:
        return self.status == ReservationStatus.PENDING
