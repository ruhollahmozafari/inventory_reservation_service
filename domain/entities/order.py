from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from domain.enums import OrderStatus


@dataclass
class OrderItem:
    id: UUID
    order_id: UUID
    product_id: UUID
    provider_id: UUID
    qty: int
    unit_ref: str | None = None


@dataclass
class Order:
    id: UUID
    reservation_id: UUID
    user_id: str
    status: OrderStatus
    created_at: datetime
    items: list[OrderItem] = field(default_factory=list)
