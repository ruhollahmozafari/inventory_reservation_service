"""
Expiry sweeper — §6 + §4.7.

Claims PENDING reservations past expires_at → EXPIRED.
All local DB work, no HTTP. Enqueues RELEASE tasks to the outbox for external items;
releases internal/soft-hold stock inline in the same transaction.
The actual HTTP release calls run in the outbox worker.
"""
import asyncio
import logging
import uuid

from config import settings
from domain.entities.provider import Provider
from domain.enums import HoldStatus, OutboxTaskType, ProviderType
from infra.db.repositories.inventory_repo import InventoryRepository
from infra.db.repositories.outbox_repo import OutboxRepository
from infra.db.repositories.provider_repo import ProviderRepository
from infra.db.repositories.reservation_repo import ReservationRepository
from infra.db.session import AsyncSessionLocal
from infra.db.transaction import atomic

log = logging.getLogger(__name__)


async def sweep_once() -> int:
    """Returns number of reservations expired in this sweep."""
    async with AsyncSessionLocal() as session:
        repo = ReservationRepository(session)
        outbox_repo = OutboxRepository(session)
        inv_repo = InventoryRepository(session)
        provider_repo = ProviderRepository(session)

        async with atomic(session):
            expired = await repo.claim_expired(batch_size=settings.SWEEPER_BATCH_SIZE)
            if not expired:
                return 0

            # Pre-load all providers needed — no DB queries inside the item loop.
            provider_ids = {item.provider_id for r in expired for item in r.items}
            providers: dict[uuid.UUID, Provider | None] = {}
            for pid in provider_ids:
                providers[pid] = await provider_repo.get_by_id(pid)

            for reservation in expired:
                for item in reservation.items:
                    if item.hold_status != HoldStatus.HELD:
                        continue
                    provider = providers.get(item.provider_id)
                    is_local = provider is None or (
                        provider.type == ProviderType.INTERNAL
                        or not provider.capabilities.get("reserve")
                    )
                    if is_local and item.provider_ref:
                        inv_id_str, qty_str = item.provider_ref.split(":")
                        await inv_repo.release(uuid.UUID(inv_id_str), int(qty_str))
                    elif item.provider_ref:
                        await outbox_repo.enqueue(
                            task_type=OutboxTaskType.RELEASE,
                            payload={
                                "item_id": str(item.id),
                                "provider_id": str(item.provider_id),
                                "provider_ref": item.provider_ref,
                            },
                            idempotency_key=f"release:expired:{reservation.id}:{item.id}",
                        )

        log.info("Sweeper: expired %d reservations", len(expired))
        return len(expired)


async def run_expiry_sweeper() -> None:
    log.info("Expiry sweeper started")
    while True:
        try:
            await sweep_once()
        except Exception as exc:
            log.exception("Sweeper error: %s", exc)
        await asyncio.sleep(settings.SWEEPER_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_expiry_sweeper())
