from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4


@dataclass
class DomainEvent:
    event_id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ReservationCreated(DomainEvent):
    reservation_id: UUID = None
    user_id: str = None


@dataclass
class ReservationConfirmed(DomainEvent):
    reservation_id: UUID = None
    order_id: UUID = None


@dataclass
class ReservationCancelled(DomainEvent):
    reservation_id: UUID = None
    reason: str = None


@dataclass
class ReservationExpired(DomainEvent):
    reservation_id: UUID = None


@dataclass
class InventoryReleaseNeeded(DomainEvent):
    reservation_item_id: UUID = None
    provider_id: UUID = None
    provider_ref: str = None
