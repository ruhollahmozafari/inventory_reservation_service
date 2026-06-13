"""
Outbox worker — §6 "Worker coordination" (lease + SKIP LOCKED).

Loop:
  1. Claim batch (short tx, committed immediately to release the SKIP LOCKED)
  2. For each task: call provider (no lock held)
  3. atomic: mark_done or mark_failed + exponential backoff
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from domain.entities.outbox_task import OutboxTask
from domain.enums import OrderStatus, OutboxTaskType
from infra.db.models import OrderModel, ProviderModel
from infra.db.repositories.outbox_repo import OutboxRepository
from infra.db.session import AsyncSessionLocal
from infra.db.transaction import atomic
from infra.providers.adapters.external_reserve import ExternalReserveAdapter
from infra.providers.decorators.timeout import TimeoutDecorator
from infra.providers.decorators.metrics import MetricsDecorator
from infra.secrets.env_encrypted import EnvEncryptedSecretProvider

log = logging.getLogger(__name__)


async def process_task(task: OutboxTask, session: AsyncSession) -> None:
    payload = task.payload
    secret_provider = EnvEncryptedSecretProvider()

    provider_id = uuid.UUID(payload["provider_id"])
    provider = await session.get(ProviderModel, provider_id)
    if provider is None:
        log.error("Task %s: provider %s not found", task.id, provider_id)
        return

    adapter = ExternalReserveAdapter(
        provider_id=provider_id,
        base_url=provider.base_url or "",
        secret_ref=provider.secret_ref,
        secret_provider=secret_provider,
        capabilities_cfg=provider.capabilities,
        timeout_ms=provider.timeout_ms,
    )
    adapter = MetricsDecorator(TimeoutDecorator(adapter, provider.timeout_ms / 1000), str(provider_id))

    ikey = task.idempotency_key

    if task.task_type == OutboxTaskType.RELEASE:
        await adapter.release(payload["provider_ref"], ikey)

    elif task.task_type == OutboxTaskType.CONFIRM:
        result = await adapter.confirm(payload["provider_ref"], ikey)
        if result.definitive_rejection:
            log.warning("Task %s: definitive confirm rejection — marking order NEEDS_RESOLUTION", task.id)
            reservation_id = uuid.UUID(payload["reservation_id"])
            await session.execute(
                sa_update(OrderModel)
                .where(OrderModel.reservation_id == reservation_id)
                .values(status=OrderStatus.NEEDS_RESOLUTION)
            )

    elif task.task_type == OutboxTaskType.UNCONFIRM:
        await adapter.unconfirm(payload["provider_ref"], ikey)

    elif task.task_type == OutboxTaskType.RECONCILE:
        provider_ref = payload.get("provider_ref")
        if provider_ref:
            await adapter.release(provider_ref, ikey)
        else:
            log.info("RECONCILE task %s: no provider_ref — assuming no hold placed", task.id)


async def run_outbox_worker() -> None:
    log.info("Outbox worker started")
    while True:
        tasks: list[OutboxTask] = []
        async with AsyncSessionLocal() as session:
            repo = OutboxRepository(session)
            async with atomic(session):
                tasks = await repo.claim_batch(
                    batch_size=settings.OUTBOX_BATCH_SIZE,
                    lease_seconds=settings.OUTBOX_LEASE_SECONDS,
                )

        for task in tasks:
            async with AsyncSessionLocal() as session:
                repo = OutboxRepository(session)
                try:
                    await process_task(task, session)
                    async with atomic(session):
                        await repo.mark_done(task.id)
                except Exception as exc:
                    await session.rollback()
                    attempts = task.attempts
                    backoff = min(
                        settings.OUTBOX_BASE_BACKOFF_SECONDS * (2 ** (attempts - 1)),
                        3600,
                    )
                    next_run = datetime.now(timezone.utc) + timedelta(seconds=backoff)
                    if attempts >= settings.OUTBOX_MAX_ATTEMPTS:
                        log.error("Task %s exceeded max attempts: %s", task.id, exc)
                    else:
                        log.warning("Task %s failed (attempt %d): %s", task.id, attempts, exc)
                    async with atomic(session):
                        await repo.mark_failed(task.id, str(exc), next_run)

        await asyncio.sleep(settings.OUTBOX_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_outbox_worker())
