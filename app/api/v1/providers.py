from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter()


@router.get("/")
async def list_providers(db: AsyncSession = Depends(get_db)):
    # TODO: list all registered inventory providers
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/{provider_id}")
async def get_provider(provider_id: str, db: AsyncSession = Depends(get_db)):
    # TODO: fetch provider details
    raise HTTPException(status_code=501, detail="Not implemented")
