"""
Seed script — inserts reference data for development and integration tests.
Run: python scripts/seed.py
"""
import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.models import ProductModel, ProviderModel, InventoryModel
from infra.db.session import AsyncSessionLocal


INTERNAL_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
EXTERNAL_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")
READONLY_PROVIDER_ID = uuid.UUID("00000000-0000-0000-0000-000000000003")

PRODUCT_HEADPHONES_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
PRODUCT_HUB_ID = uuid.UUID("10000000-0000-0000-0000-000000000002")

now = datetime.now(timezone.utc)


async def seed(session: AsyncSession) -> None:
    # Providers
    internal_provider = ProviderModel(
        id=INTERNAL_PROVIDER_ID,
        name="InternalStock",
        type="internal",
        base_url=None,
        timeout_ms=1000,
        capabilities={"reserve": True, "confirm": True, "release": True, "unconfirm": False},
        secret_ref=None,
        created_at=now,
    )

    external_provider = ProviderModel(
        id=EXTERNAL_PROVIDER_ID,
        name="WarehouseProvider",
        type="external",
        base_url="http://fake-warehouse.local",
        timeout_ms=5000,
        capabilities={"reserve": True, "confirm": True, "release": True, "unconfirm": True},
        secret_ref=None,  # Would be encrypted API key in production
        created_at=now,
    )

    readonly_provider = ProviderModel(
        id=READONLY_PROVIDER_ID,
        name="DropshipProvider",
        type="external",
        base_url="http://dropship.local",
        timeout_ms=3000,
        capabilities={"reserve": False, "confirm": False, "release": False, "unconfirm": False},
        secret_ref=None,
        created_at=now,
    )

    # Products
    headphones = ProductModel(
        id=PRODUCT_HEADPHONES_ID,
        sku="SONY-WH-XM5-BLK",
        name="Sony WH-1000XM5 Headphones",
        created_at=now,
    )

    hub = ProductModel(
        id=PRODUCT_HUB_ID,
        sku="ANKR-HUB-7C",
        name="Anker USB-C Hub 7-in-1",
        created_at=now,
    )

    # Inventory
    headphones_external_inv = InventoryModel(
        id=uuid.uuid4(),
        product_id=PRODUCT_HEADPHONES_ID,
        provider_id=EXTERNAL_PROVIDER_ID,
        qty_on_hand=12,
        qty_reserved=0,
    )

    hub_internal_inv = InventoryModel(
        id=uuid.uuid4(),
        product_id=PRODUCT_HUB_ID,
        provider_id=INTERNAL_PROVIDER_ID,
        qty_on_hand=340,
        qty_reserved=0,
    )

    headphones_internal_inv = InventoryModel(
        id=uuid.uuid4(),
        product_id=PRODUCT_HEADPHONES_ID,
        provider_id=INTERNAL_PROVIDER_ID,
        qty_on_hand=5,
        qty_reserved=0,
    )

    for obj in [
        internal_provider, external_provider, readonly_provider,
        headphones, hub,
        headphones_external_inv, hub_internal_inv, headphones_internal_inv,
    ]:
        session.add(obj)

    await session.commit()
    print("Seed data inserted successfully.")
    print(f"  Internal provider:  {INTERNAL_PROVIDER_ID}")
    print(f"  External provider:  {EXTERNAL_PROVIDER_ID}")
    print(f"  Read-only provider: {READONLY_PROVIDER_ID}")
    print(f"  Product headphones: {PRODUCT_HEADPHONES_ID}")
    print(f"  Product hub:        {PRODUCT_HUB_ID}")


async def main():
    async with AsyncSessionLocal() as session:
        await seed(session)


if __name__ == "__main__":
    asyncio.run(main())
