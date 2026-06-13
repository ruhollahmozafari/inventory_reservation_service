"""
Expiry sweeper — §6 + §4.7.

Claims PENDING reservations past expires_at → EXPIRED.
All local, no HTTP (per design). Enqueues RELEASE tasks to outbox per held item.
The slow release HTTP calls run in the outbox worker.
"""
import asyncio
import logging
import uuid

from config import settings
from domain.enums import HoldStatus, OutboxTaskType
from infra.db.repositories.outbox_repo import OutboxRepository
from infra.db.repositories.reservation_repo import ReservationRepository
from infra.db.repositories.inventory_repo import InventoryRepository
from infra.db.session import AsyncSessionLocal
from infra.db.models import ProviderModel

log = logging.getLogger(__name__)


async def sweep_once() -> int:
    """Returns number of reservations expired in this sweep."""
    async with AsyncSessionLocal() as session:
        repo = ReservationRepository(session)
        outbox_repo = OutboxRepository(session)
        inv_repo = InventoryRepository(session)

        expired = await repo.claim_expired(batch_size=settings.SWEEPER_BATCH_SIZE)
        if not expired:
            return 0

        for reservation in expired:
            for item in reservation.items:
                if item.hold_status != HoldStatus.HELD:
                    continue
                provider = await session.get(ProviderModel, item.provider_id)
                if provider and provider.type.value == "internal" and item.provider_ref:
                    # Internal: release inline (same tx)
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

        await session.commit()
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
