from abc import ABC, abstractmethod
from uuid import UUID

from domain.entities.inventory import Inventory


class AbstractInventoryRepository(ABC):
    @abstractmethod
    async def get_by_product_provider(self, product_id: UUID, provider_id: UUID) -> Inventory | None:
        ...

    @abstractmethod
    async def reserve(self, inventory_id: UUID, qty: int) -> bool:
        """Atomic conditional reserve. Returns True on success, False if insufficient stock."""
        ...

    @abstractmethod
    async def release(self, inventory_id: UUID, qty: int) -> bool:
        """Atomic conditional release (qty_reserved -= qty)."""
        ...

    @abstractmethod
    async def consume(self, inventory_id: UUID, qty: int) -> bool:
        """Atomic confirm/consume: qty_on_hand -= qty AND qty_reserved -= qty."""
        ...
