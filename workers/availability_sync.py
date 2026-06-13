"""
Availability sync worker — §9 "Read-only availability sync".

Periodically fetches stock from external/read-only providers and updates
the local inventory cache (qty_on_hand) so soft-holds have fresh data.
This mitigates oversell risk for SoftHoldAdapter; residual risk is accepted + documented.
"""
import asyncio
import logging

from sqlalchemy import select

from infra.db.models import InventoryModel, ProviderModel
from infra.db.session import AsyncSessionLocal
from infra.http.auth import build_auth
from infra.http.provider_client import ProviderHttpClient
from infra.providers.adapters.external_reserve import ExternalReserveAdapter
from infra.providers.port import ProviderCapabilities
from infra.secrets.env_encrypted import EnvEncryptedSecretProvider

log = logging.getLogger(__name__)

SYNC_INTERVAL_SECONDS = 60


async def sync_once() -> None:
    secret_provider = EnvEncryptedSecretProvider()
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(InventoryModel, ProviderModel)
                .join(ProviderModel, InventoryModel.provider_id == ProviderModel.id)
                .where(ProviderModel.type == "external")
            )
        ).all()

    for inv_row, prov_row in rows:
        try:
            auth = build_auth(
                auth_type=prov_row.capabilities.get("auth_type", "bearer"),
                secret_ref=prov_row.secret_ref,
                secret_provider=secret_provider,
            )
            client = ProviderHttpClient(
                base_url=prov_row.base_url or "",
                auth=auth,
                timeout_s=prov_row.timeout_ms / 1000,
            )

            caps = ProviderCapabilities(**prov_row.capabilities)
            adapter = ExternalReserveAdapter(client=client, capabilities=caps)
            result = await adapter.check_availability(inv_row.product_id, inv_row.provider_id)
            async with AsyncSessionLocal() as session:
                inv_row.qty_on_hand = result.qty_available + inv_row.qty_reserved
                await session.commit()
        except Exception as exc:
            log.warning("Sync failed for inventory %s: %s", inv_row.id, exc)


async def run_availability_sync() -> None:
    log.info("Availability sync worker started")
    while True:
        try:
            await sync_once()
        except Exception as exc:
            log.exception("Availability sync error: %s", exc)
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_availability_sync())
