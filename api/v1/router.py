from fastapi import APIRouter

from api.v1 import reservations, inventory

v1_router = APIRouter()

v1_router.include_router(reservations.router, prefix="/reservations", tags=["reservations"])
v1_router.include_router(inventory.router, prefix="/inventory", tags=["inventory"])
