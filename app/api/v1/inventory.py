from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter()


@router.get("/{product_id}")
async def get_inventory(product_id: str, db: AsyncSession = Depends(get_db)):
    # TODO: fetch inventory level for product (internal + external providers)
    raise HTTPException(status_code=501, detail="Not implemented")
