from dataclasses import dataclass
from uuid import UUID


@dataclass
class Inventory:
    """
    Aggregate root for stock at a given (product, provider) pair.
    Invariants: qty_reserved >= 0, qty_reserved <= qty_on_hand.
    Mutations are enforced at the DB level via conditional UPDATE — this
    class is the in-memory projection used for reads and event modelling.
    """
    id: UUID
    product_id: UUID
    provider_id: UUID
    qty_on_hand: int
    qty_reserved: int
    version: int = 0

    @property
    def qty_available(self) -> int:
        return self.qty_on_hand - self.qty_reserved

    def can_reserve(self, qty: int) -> bool:
        return self.qty_available >= qty
