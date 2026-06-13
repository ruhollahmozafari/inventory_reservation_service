"""
Test factories — helpers to insert domain entities into the test DB.
Every factory flushes (not commits) so callers control transaction boundaries.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from domain.enums import ProviderType
from infra.db.models import InventoryModel, ProductModel, ProviderModel


async def create_product(
    session: AsyncSession,
    *,
    sku: str | None = None,
    name: str = "Test Product",
) -> ProductModel:
    product = ProductModel(
        id=uuid.uuid4(),
        sku=sku or f"SKU-{uuid.uuid4().hex[:8].upper()}",
        name=name,
        created_at=datetime.now(timezone.utc),
    )
    session.add(product)
    await session.flush()
    return product


async def create_provider(
    session: AsyncSession,
    *,
    name: str | None = None,
    provider_type: str = "internal",
    capabilities: dict | None = None,
    base_url: str | None = None,
    timeout_ms: int = 5000,
) -> ProviderModel:
    if capabilities is None:
        if provider_type == "internal":
            capabilities = {"reserve": True, "confirm": True, "release": True, "unconfirm": False}
        else:
            capabilities = {"reserve": True, "confirm": True, "release": True, "unconfirm": True}

    provider = ProviderModel(
        id=uuid.uuid4(),
        name=name or f"provider-{uuid.uuid4().hex[:8]}",
        type=ProviderType(provider_type),
        base_url=base_url,
        timeout_ms=timeout_ms,
        capabilities=capabilities,
        secret_ref=None,
        created_at=datetime.now(timezone.utc),
    )
    session.add(provider)
    await session.flush()
    return provider


async def create_inventory(
    session: AsyncSession,
    product_id: uuid.UUID,
    provider_id: uuid.UUID,
    *,
    qty_on_hand: int = 10,
    qty_reserved: int = 0,
) -> InventoryModel:
    inventory = InventoryModel(
        id=uuid.uuid4(),
        product_id=product_id,
        provider_id=provider_id,
        qty_on_hand=qty_on_hand,
        qty_reserved=qty_reserved,
    )
    session.add(inventory)
    await session.flush()
    return inventory
