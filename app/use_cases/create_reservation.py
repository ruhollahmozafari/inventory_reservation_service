"""
Create-reservation saga — §7 "Create — SYNCHRONOUS".

Phase 1 (outside any transaction): call each provider's reserve().
Phase 2 (inside `atomic`): persist result — compensate held lines on failure,
  enqueue RECONCILE for unknown-outcome timeouts, save the reservation.

No DB lock is held during provider calls (§6 Rule 0).
"""
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from domain.entities.reservation import Reservation, ReservationItem
from domain.enums import HoldStatus, OutboxTaskType, ProviderType, ReservationStatus
from infra.db.models import ProviderModel
from infra.db.repositories.inventory_repo import InventoryRepository
from infra.db.repositories.outbox_repo import OutboxRepository
from infra.db.repositories.reservation_repo import ReservationRepository
from infra.db.transaction import atomic
from infra.providers.adapters.internal import InternalAdapter
from infra.providers.adapters.external_reserve import ExternalReserveAdapter
from infra.providers.adapters.soft_hold import SoftHoldAdapter
from infra.providers.decorators.timeout import TimeoutDecorator
from infra.providers.decorators.metrics import MetricsDecorator
from infra.secrets.env_encrypted import EnvEncryptedSecretProvider


class CreateReservationUseCase:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._reservation_repo = ReservationRepository(session)
        self._outbox_repo = OutboxRepository(session)
        self._secret_provider = EnvEncryptedSecretProvider()

    async def execute(
        self,
        user_id: str,
        idempotency_key: str,
        items: list[tuple[uuid.UUID, uuid.UUID, int]],
    ) -> Reservation:
        existing = await self._reservation_repo.get_by_idempotency_key(idempotency_key)
        if existing:
            return existing

        reservation_id = uuid.uuid4()
        now = datetime.now(timezone.utc)

        held_items: list[ReservationItem] = []
        failed = False
        reconcile_payload: dict | None = None

        # ── Phase 1: provider calls (no DB lock held) ─────────────────────────
        for product_id, provider_id, qty in items:
            item_id = uuid.uuid4()
            adapter = await self._build_adapter(provider_id)
            item_key = f"{idempotency_key}:{product_id}:{provider_id}"

            try:
                result = await adapter.reserve(product_id, provider_id, qty, item_key)
            except TimeoutError: # we are just checking the timeout exception what about the other stuff.
                held_items.append(ReservationItem(
                    id=item_id, reservation_id=reservation_id,
                    product_id=product_id, provider_id=provider_id,
                    qty=qty, hold_status=HoldStatus.PENDING_UNKNOWN, provider_ref=None,
                ))
                reconcile_payload = {
                    "reservation_id": str(reservation_id), "item_id": str(item_id),
                    "product_id": str(product_id), "provider_id": str(provider_id),
                    "qty": qty, "idempotency_key": item_key,
                }
                failed = True
                break

            if not result.success:
                held_items.append(ReservationItem(
                    id=item_id, reservation_id=reservation_id,
                    product_id=product_id, provider_id=provider_id,
                    qty=qty, hold_status=HoldStatus.FAILED, provider_ref=None,
                ))
                failed = True
                break

            held_items.append(ReservationItem(
                id=item_id, reservation_id=reservation_id,
                product_id=product_id, provider_id=provider_id,
                qty=qty, hold_status=HoldStatus.HELD, provider_ref=result.provider_ref,
            ))

        reservation = Reservation(
            id=reservation_id, user_id=user_id, idempotency_key=idempotency_key,
            status=ReservationStatus.FAILED if failed else ReservationStatus.PENDING,
            expires_at=now + timedelta(seconds=settings.RESERVATION_TTL_SECONDS),
            created_at=now, items=held_items,
        )

        # ── Phase 2: persist everything atomically ────────────────────────────
        async with atomic(self._session):
            if failed:
                await self._compensate(held_items, idempotency_key)
            if reconcile_payload:
                await self._outbox_repo.enqueue(
                    task_type=OutboxTaskType.RECONCILE,
                    payload=reconcile_payload,
                    idempotency_key=f"reconcile:{idempotency_key}",
                )
            await self._reservation_repo.save(reservation)

        return reservation

    async def _compensate(self, held_items: list[ReservationItem], idempotency_key: str) -> None:
        """Release HELD lines. PENDING_UNKNOWN lines already have RECONCILE tasks."""
        inv_repo = InventoryRepository(self._session)
        for item in held_items:
            if item.hold_status != HoldStatus.HELD:
                continue
            provider = await self._session.get(ProviderModel, item.provider_id)
            if provider and provider.type == ProviderType.INTERNAL and item.provider_ref:
                inv_id, qty = item.provider_ref.split(":")
                await inv_repo.release(uuid.UUID(inv_id), int(qty))
                item.hold_status = HoldStatus.RELEASED
            else:
                await self._outbox_repo.enqueue(
                    task_type=OutboxTaskType.RELEASE,
                    payload={
                        "item_id": str(item.id), "provider_id": str(item.provider_id),
                        "provider_ref": item.provider_ref,
                    },
                    idempotency_key=f"release:{idempotency_key}:{item.product_id}:{item.provider_id}",
                )
                item.hold_status = HoldStatus.RELEASED

    async def _build_adapter(self, provider_id: uuid.UUID):
        provider = await self._session.get(ProviderModel, provider_id)
        if provider is None:
            raise ValueError(f"Provider {provider_id} not found")
        timeout_s = provider.timeout_ms / 1000

        if provider.type == ProviderType.INTERNAL:
            adapter = InternalAdapter(self._session)
        elif provider.capabilities.get("reserve"):
            adapter = ExternalReserveAdapter(
                provider_id=provider_id,
                base_url=provider.base_url or "",
                secret_ref=provider.secret_ref,
                secret_provider=self._secret_provider,
                capabilities_cfg=provider.capabilities,
                timeout_ms=provider.timeout_ms,
            )
        else:
            adapter = SoftHoldAdapter(
                provider_id=provider_id,
                base_url=provider.base_url or "",
                secret_ref=provider.secret_ref,
                secret_provider=self._secret_provider,
                timeout_ms=provider.timeout_ms,
            )
        return MetricsDecorator(TimeoutDecorator(adapter, timeout_s), str(provider_id))
