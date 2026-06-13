import uuid

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.inventory import Inventory
from domain.repositories.inventory_repo import AbstractInventoryRepository
from infra.db.models import InventoryModel


class InventoryRepository(AbstractInventoryRepository):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_product_provider(
        self, product_id: uuid.UUID, provider_id: uuid.UUID
    ) -> Inventory | None:
        row = await self._session.scalar(
            select(InventoryModel).where(
                InventoryModel.product_id == product_id,
                InventoryModel.provider_id == provider_id,
            )
        )
        return self._to_domain(row) if row else None

    async def reserve(self, inventory_id: uuid.UUID, qty: int) -> bool:
        """
        Atomic conditional UPDATE — the WHERE clause is the oversell guard.
        No explicit lock; rowcount==1 means stock was available and was reserved.
        """
        result = await self._session.execute(
            update(InventoryModel)
            .where(
                InventoryModel.id == inventory_id,
                (InventoryModel.qty_on_hand - InventoryModel.qty_reserved) >= qty,
            )
            .values(qty_reserved=InventoryModel.qty_reserved + qty)
        )
        return result.rowcount == 1

    async def release(self, inventory_id: uuid.UUID, qty: int) -> bool:
        """Atomic conditional release: qty_reserved -= qty (guard: qty_reserved >= qty)."""
        result = await self._session.execute(
            update(InventoryModel)
            .where(
                InventoryModel.id == inventory_id,
                InventoryModel.qty_reserved >= qty,
            )
            .values(qty_reserved=InventoryModel.qty_reserved - qty)
        )
        return result.rowcount == 1

    async def consume(self, inventory_id: uuid.UUID, qty: int) -> bool:
        """
        Confirm/consume: qty_on_hand -= qty AND qty_reserved -= qty.
        Guard: qty_reserved >= qty (confirming a held unit, so it must be reserved).
        """
        result = await self._session.execute(
            update(InventoryModel)
            .where(
                InventoryModel.id == inventory_id,
                InventoryModel.qty_reserved >= qty,
            )
            .values(
                qty_on_hand=InventoryModel.qty_on_hand - qty,
                qty_reserved=InventoryModel.qty_reserved - qty,
            )
        )
        return result.rowcount == 1

    @staticmethod
    def _to_domain(row: InventoryModel) -> Inventory:
        return Inventory(
            id=row.id,
            product_id=row.product_id,
            provider_id=row.provider_id,
            qty_on_hand=row.qty_on_hand,
            qty_reserved=row.qty_reserved,
            version=row.version,
        )
