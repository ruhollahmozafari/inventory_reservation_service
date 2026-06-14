"""
Create-reservation saga — §7 "Create — SYNCHRONOUS".

Three transaction windows (per RESERVE_FLOW.md §2):

  TX1  (intent-first): load all provider data, reserve internal/soft-hold stock via
       their adapters (all DB work, no network), write INITIALIZING reservation + all
       items. Commits before any external API call — after any crash we have the
       idempotency key needed to release or confirm the hold.

  TX_n (per external item): update item status after each API call. Each is its own
       atomic block so a crash between calls is recoverable via the RESERVING intent.

  TX_final: CAS INITIALIZING → PENDING (with real expires_at) or → FAILED.

No DB session is active during external provider API calls.
"""
import uuid
from datetime import datetime, timezone, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from domain.entities.outbox_payloads import ReconcilePayload, ReleasePayload
from domain.entities.provider import Provider
from domain.entities.reservation import Reservation, ReservationItem
from domain.enums import HoldStatus, OutboxTaskType, ProviderType, ReservationStatus
from infra.db.repositories.inventory_repo import InventoryRepository
from infra.db.repositories.outbox_repo import OutboxRepository
from infra.db.repositories.provider_repo import ProviderRepository
from infra.db.repositories.reservation_repo import ReservationRepository
from infra.db.transaction import atomic
from infra.http.auth import build_auth
from infra.http.provider_client import ProviderHttpClient
from infra.providers.adapters.internal import InternalAdapter
from infra.providers.adapters.external_reserve import ExternalReserveAdapter
from infra.providers.adapters.soft_hold import SoftHoldAdapter
from infra.providers.decorators.timeout import TimeoutDecorator
from infra.providers.decorators.metrics import MetricsDecorator
from infra.providers.port import ProviderCapabilities
from infra.secrets.env_encrypted import EnvEncryptedSecretProvider


class _LocalStockError(Exception):
    """Raised inside TX1 when local stock is insufficient — rolls back TX1 cleanly."""


class CreateReservationUseCase:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._reservation_repo = ReservationRepository(session)
        self._outbox_repo = OutboxRepository(session)
        self._provider_repo = ProviderRepository(session)
        self._secret_provider = EnvEncryptedSecretProvider()
        self._inv_repo = InventoryRepository(self._session)

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
        grace = timedelta(seconds=settings.RESERVATION_CREATE_GRACE_SECONDS)
        held_items: list[ReservationItem] = []
        # Adapter map keyed by item_id — only external adapters are needed post-TX1.
        # Built inside TX1 from pre-loaded provider data; no DB access after TX1 commits.
        external_adapters: dict[uuid.UUID, MetricsDecorator] = {}
        # Provider map for compensate logic — populated in TX1, reused without DB.
        providers: dict[uuid.UUID, Provider] = {}

        # ── TX1: intent-first ──────────────────────────────────────────────────
        # All DB work happens here: provider lookup (via repo), inventory reserve,
        # write-ahead RESERVING intents, reservation INSERT. Commits before any HTTP call.
        # _LocalStockError rolls TX1 back cleanly; nothing is in the DB.
        try:
            async with atomic(self._session):
                for product_id, provider_id, qty in items:
                    item_id = uuid.uuid4()
                    item_key = f"{idempotency_key}:{product_id}:{provider_id}"

                    provider = await self._provider_repo.get_by_id(provider_id)
                    if provider is None:
                        raise ValueError(f"Provider {provider_id} not found")
                    providers[provider_id] = provider

                    adapter = self._build_adapter(provider)

                    if provider.type != ProviderType.INTERNAL and provider.capabilities.get("reserve"):
                        # External provider with a reserve API: write-ahead intent only.
                        # API call happens after TX1 so no DB lock is held during HTTP.
                        external_adapters[item_id] = adapter
                        held_items.append(ReservationItem(
                            id=item_id, reservation_id=reservation_id,
                            product_id=product_id, provider_id=provider_id,
                            qty=qty, hold_status=HoldStatus.RESERVING,
                            provider_ref=None, idempotency_key=item_key,
                        ))
                    else:
                        # Internal or soft-hold: reserve against local DB inside TX1.
                        # This makes the stock deduction atomic with the reservation INSERT.
                        result = await adapter.reserve(product_id, provider_id, qty, item_key)
                        if not result.success:
                            raise _LocalStockError(result.error or "reserve failed")
                        held_items.append(ReservationItem(
                            id=item_id, reservation_id=reservation_id,
                            product_id=product_id, provider_id=provider_id,
                            qty=qty, hold_status=HoldStatus.HELD,
                            provider_ref=result.provider_ref,
                        ))

                reservation = Reservation(
                    id=reservation_id, user_id=user_id, idempotency_key=idempotency_key,
                    status=ReservationStatus.INITIALIZING,
                    expires_at=now + grace,
                    creation_deadline=now + grace,
                    created_at=now, items=held_items,
                )
                await self._reservation_repo.save(reservation)

        except _LocalStockError:
            # TX1 rolled back cleanly — no external calls made, nothing persisted.
            # Retrying with the same key gets a fresh attempt (stock may free up).
            return Reservation(
                id=reservation_id, user_id=user_id, idempotency_key=idempotency_key,
                status=ReservationStatus.FAILED, expires_at=now, created_at=now, items=[],
            )

        # ── External API calls — no DB session active during HTTP ──────────────
        # All provider data is already loaded; adapters are pre-built. Pure HTTP from here.
        failed = False
        for item in held_items:
            if item.hold_status != HoldStatus.RESERVING:
                continue

            adapter = external_adapters[item.id]
            try:
                result = await adapter.reserve(
                    item.product_id, item.provider_id, item.qty, item.idempotency_key
                )
            except TimeoutError:
                # Unknown outcome — RECONCILE will confirm or release by idempotency key.
                item.hold_status = HoldStatus.PENDING_UNKNOWN
                async with atomic(self._session):
                    await self._reservation_repo.save_item(item)
                    await self._outbox_repo.enqueue(
                        task_type=OutboxTaskType.RECONCILE,
                        payload=ReconcilePayload(
                            reservation_id=reservation_id, item_id=item.id,
                            product_id=item.product_id, provider_id=item.provider_id,
                            qty=item.qty, idempotency_key=item.idempotency_key,
                        ).to_dict(),
                        idempotency_key=f"reconcile:{item.idempotency_key}",
                    )
                failed = True
                break
            except Exception:
                # Definitive failure — no hold placed, no orphan possible.
                item.hold_status = HoldStatus.FAILED
                async with atomic(self._session):
                    await self._reservation_repo.save_item(item)
                failed = True
                break

            if not result.success:
                item.hold_status = HoldStatus.FAILED
                async with atomic(self._session):
                    await self._reservation_repo.save_item(item)
                failed = True
                break

            item.hold_status = HoldStatus.HELD
            item.provider_ref = result.provider_ref
            async with atomic(self._session):
                await self._reservation_repo.save_item(item)

        # ── Finalize ──────────────────────────────────────────────────────────
        if failed:
            reservation.status = ReservationStatus.FAILED
            async with atomic(self._session):
                await self._compensate(held_items, providers, idempotency_key)
                await self._reservation_repo.save(reservation)
        else:
            reservation.status = ReservationStatus.PENDING
            reservation.expires_at = now + timedelta(seconds=settings.RESERVATION_TTL_SECONDS)
            async with atomic(self._session):
                await self._reservation_repo.save(reservation)

        return reservation

    async def _compensate(
        self,
        held_items: list[ReservationItem],
        providers: dict[uuid.UUID, Provider],
        idempotency_key: str,
    ) -> None:
        """
        Release all non-failed holds. Called inside an atomic block.
        Uses the pre-loaded providers dict — no DB queries.

        - FAILED: nothing was held — skip.
        - PENDING_UNKNOWN: RECONCILE already enqueued by the caller — skip.
        - RESERVING: API was never called — mark FAILED (no hold exists on provider).
        - HELD (internal/soft-hold): release inline against local DB.
        - HELD (external): enqueue RELEASE; outbox worker calls the provider.
        """
        inv_repo = InventoryRepository(self._session)
        for item in held_items:
            if item.hold_status in (HoldStatus.FAILED, HoldStatus.RELEASED):
                continue
            if item.hold_status == HoldStatus.PENDING_UNKNOWN:
                continue
            if item.hold_status == HoldStatus.RESERVING:
                # Loop broke before reaching this item — API never called.
                item.hold_status = HoldStatus.FAILED
                await self._reservation_repo.save_item(item)
                continue

            # HELD — determine release strategy from pre-loaded provider data.
            provider = providers.get(item.provider_id)
            is_local = provider and (
                provider.type == ProviderType.INTERNAL
                or not provider.capabilities.get("reserve")
            )
            if is_local and item.provider_ref:
                inv_id, qty = item.provider_ref.split(":")
                await inv_repo.release(uuid.UUID(inv_id), int(qty))
            else:
                await self._outbox_repo.enqueue(
                    task_type=OutboxTaskType.RELEASE,
                    payload=ReleasePayload(
                        item_id=item.id, provider_id=item.provider_id,
                        provider_ref=item.provider_ref,
                    ).to_dict(),
                    idempotency_key=f"release:{idempotency_key}:{item.product_id}:{item.provider_id}",
                )
            item.hold_status = HoldStatus.RELEASED
            await self._reservation_repo.save_item(item)

    def _build_adapter(self, provider: Provider) -> MetricsDecorator:
        """Pure construction — no DB access, no network. Session passed for local adapters."""
        timeout_s = provider.timeout_ms / 1000
        if provider.type == ProviderType.INTERNAL:
            adapter = InternalAdapter(self._session)
        elif provider.capabilities.get("reserve"):
            auth = build_auth(
                auth_type=provider.capabilities.get("auth_type", "bearer"),
                secret_ref=provider.secret_ref,
                secret_provider=self._secret_provider,
            )
            client = ProviderHttpClient(
                base_url=provider.base_url or "",
                auth=auth,
                timeout_s=timeout_s,
            )
            caps = ProviderCapabilities(**provider.capabilities)
            adapter = ExternalReserveAdapter(client=client, capabilities=caps)
        else:
            adapter = SoftHoldAdapter(self._session)
        return MetricsDecorator(TimeoutDecorator(adapter, timeout_s), str(provider.id))
