from abc import ABC, abstractmethod
from uuid import UUID

from domain.entities.product import Product


class AbstractProductRepository(ABC):
    @abstractmethod
    async def get_by_id(self, product_id: UUID) -> Product | None:
        ...

    @abstractmethod
    async def get_by_sku(self, sku: str) -> Product | None:
        ...
