from fastapi import APIRouter

from app.api.v1 import reservations, inventory, orders, providers

api_router = APIRouter()

api_router.include_router(reservations.router, prefix="/reservations", tags=["reservations"])
api_router.include_router(inventory.router, prefix="/inventory", tags=["inventory"])
api_router.include_router(orders.router, prefix="/orders", tags=["orders"])
api_router.include_router(providers.router, prefix="/providers", tags=["providers"])
