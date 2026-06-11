from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

router = APIRouter()


@router.post("/", status_code=201)
async def create_reservation(db: AsyncSession = Depends(get_db)):
    # TODO: validate items, check inventory, create reservation, call providers
    raise HTTPException(status_code=501, detail="Not implemented")


@router.get("/{reservation_id}")
async def get_reservation(reservation_id: str, db: AsyncSession = Depends(get_db)):
    # TODO: fetch reservation by id
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/{reservation_id}/confirm", status_code=200)
async def confirm_reservation(reservation_id: str, db: AsyncSession = Depends(get_db)):
    # TODO: confirm reservation → create order, consume inventory
    raise HTTPException(status_code=501, detail="Not implemented")


@router.post("/{reservation_id}/cancel", status_code=200)
async def cancel_reservation(reservation_id: str, db: AsyncSession = Depends(get_db)):
    # TODO: release reserved inventory back to available
    raise HTTPException(status_code=501, detail="Not implemented")
