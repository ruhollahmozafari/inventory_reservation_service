import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.product import Product
from domain.repositories.product_repo import AbstractProductRepository
from infra.db.models import ProductModel


class ProductRepository(AbstractProductRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, product_id: uuid.UUID) -> Product | None:
        row = await self._session.get(ProductModel, product_id)
        return self._to_domain(row) if row else None

    async def get_by_sku(self, sku: str) -> Product | None:
        row = await self._session.scalar(select(ProductModel).where(ProductModel.sku == sku))
        return self._to_domain(row) if row else None

    @staticmethod
    def _to_domain(row: ProductModel) -> Product:
        return Product(id=row.id, sku=row.sku, name=row.name, created_at=row.created_at)
