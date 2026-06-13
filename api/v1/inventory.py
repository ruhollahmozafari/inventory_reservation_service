from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import get_session
from infra.db.repositories.inventory_repo import InventoryRepository

router = APIRouter()


class InventoryResponse(BaseModel):
    id: UUID
    product_id: UUID
    provider_id: UUID
    qty_on_hand: int
    qty_reserved: int
    qty_available: int


@router.get("/{product_id}/{provider_id}", response_model=InventoryResponse)
async def get_inventory(
    product_id: UUID,
    provider_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> InventoryResponse:
    repo = InventoryRepository(db)
    inv = await repo.get_by_product_provider(product_id, provider_id)
    if inv is None:
        raise HTTPException(status_code=404, detail="Inventory record not found")
    return InventoryResponse(
        id=inv.id,
        product_id=inv.product_id,
        provider_id=inv.provider_id,
        qty_on_hand=inv.qty_on_hand,
        qty_reserved=inv.qty_reserved,
        qty_available=inv.qty_available,
    )
