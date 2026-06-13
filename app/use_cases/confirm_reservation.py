"""
Confirm-reservation use case — §7 "Confirm — SYNC happy path, failures → background".

Two atomic windows:
  1. CAS PENDING → CONFIRMING  (committed immediately so other workers see it)
  2. Confirm each line (internal inline, external via adapter), create Order,
     finalize reservation status. All provider data pre-loaded before Window 2.
"""
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from domain.entities.outbox_payloads import ConfirmPayload, ReleasePayload
from domain.entities.provider import Provider
from domain.entities.reservation import Reservation
from domain.enums import HoldStatus, OrderStatus, OutboxTaskType, ProviderType, ReservationStatus
from infra.db.models import OrderItemModel, OrderModel
from infra.db.repositories.inventory_repo import InventoryRepository
from infra.db.repositories.outbox_repo import OutboxRepository
from infra.db.repositories.provider_repo import ProviderRepository
from infra.db.repositories.reservation_repo import ReservationRepository
from infra.db.transaction import atomic
from infra.http.auth import build_auth
from infra.http.provider_client import ProviderHttpClient
from infra.providers.adapters.external_reserve import ExternalReserveAdapter
from infra.providers.decorators.metrics import MetricsDecorator
from infra.providers.decorators.timeout import TimeoutDecorator
from infra.providers.port import ProviderCapabilities
from infra.secrets.env_encrypted import EnvEncryptedSecretProvider


class ConfirmReservationUseCase:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._reservation_repo = ReservationRepository(session)
        self._outbox_repo = OutboxRepository(session)
        self._inv_repo = InventoryRepository(session)
        self._provider_repo = ProviderRepository(session)
        self._secret_provider = EnvEncryptedSecretProvider()

    async def execute(self, reservation_id: uuid.UUID) -> Reservation:
        # ── Window 1: CAS claim ───────────────────────────────────────────────
        async with atomic(self._session):
            claimed = await self._reservation_repo.cas_status(
                reservation_id, ReservationStatus.PENDING, ReservationStatus.CONFIRMING
            )

        if not claimed:
            reservation = await self._reservation_repo.get_by_id(reservation_id)
            if reservation is None:
                raise HTTPException(status_code=404, detail="Reservation not found")
            if reservation.status == ReservationStatus.CONFIRMED:
                return reservation
            raise HTTPException(
                status_code=409,
                detail=f"Cannot confirm: status={reservation.status.value}",
            )

        # ── Window 2: confirm each line + create order ────────────────────────
        async with atomic(self._session):
            reservation = await self._reservation_repo.get_by_id(reservation_id)

            # Pre-load all providers for this reservation's items — no DB queries in the loop.
            provider_ids = {item.provider_id for item in reservation.items}
            providers: dict[uuid.UUID, Provider] = {}
            for pid in provider_ids:
                p = await self._provider_repo.get_by_id(pid)
                if p is not None:
                    providers[pid] = p

            confirmed_items: list[uuid.UUID] = []
            has_pending_fulfilment = False
            has_needs_resolution = False

            for item in reservation.items:
                provider = providers.get(item.provider_id)
                key = f"confirm:{reservation_id}:{item.product_id}:{item.provider_id}"

                if provider and provider.type == ProviderType.INTERNAL:
                    if item.provider_ref:
                        inv_id, qty = item.provider_ref.split(":")
                        ok = await self._inv_repo.consume(uuid.UUID(inv_id), int(qty))
                        if ok:
                            item.hold_status = HoldStatus.CONFIRMED
                            confirmed_items.append(item.id)
                        else:
                            has_needs_resolution = True
                else:
                    if item.provider_ref:
                        adapter = self._build_adapter(provider)
                        confirm_payload = ConfirmPayload(
                            reservation_id=reservation_id, item_id=item.id,
                            provider_id=item.provider_id, provider_ref=item.provider_ref,
                        )
                        try:
                            result = await adapter.confirm(item.provider_ref, key)
                            if result.success:
                                item.hold_status = HoldStatus.CONFIRMED
                                confirmed_items.append(item.id)
                            elif result.definitive_rejection:
                                has_needs_resolution = True
                            else:
                                has_pending_fulfilment = True
                                await self._outbox_repo.enqueue(
                                    task_type=OutboxTaskType.CONFIRM,
                                    payload=confirm_payload.to_dict(),
                                    idempotency_key=key,
                                )
                        except Exception:
                            # Timeout or any other adapter failure — retry via outbox.
                            has_pending_fulfilment = True
                            await self._outbox_repo.enqueue(
                                task_type=OutboxTaskType.CONFIRM,
                                payload=confirm_payload.to_dict(),
                                idempotency_key=key,
                            )

            if has_needs_resolution:
                await self._compensate_confirmed(reservation, confirmed_items, providers)

            order_status = (
                OrderStatus.NEEDS_RESOLUTION if has_needs_resolution
                else OrderStatus.PENDING_FULFILMENT if has_pending_fulfilment
                else OrderStatus.CONFIRMED
            )

            now = datetime.now(timezone.utc)
            order_id = uuid.uuid4()
            self._session.add(OrderModel(
                id=order_id, reservation_id=reservation_id,
                user_id=reservation.user_id, status=order_status, created_at=now,
            ))
            for item in reservation.items:
                if item.hold_status == HoldStatus.CONFIRMED:
                    self._session.add(OrderItemModel(
                        id=uuid.uuid4(), order_id=order_id,
                        product_id=item.product_id, provider_id=item.provider_id,
                        qty=item.qty, unit_ref=item.provider_ref,
                    ))

            reservation.status = ReservationStatus.CONFIRMED
            reservation.confirmed_at = now
            await self._reservation_repo.save(reservation)

        return reservation

    async def _compensate_confirmed(
        self,
        reservation: Reservation,
        confirmed_item_ids: list[uuid.UUID],
        providers: dict[uuid.UUID, Provider],
    ) -> None:
        for item in reservation.items:
            if item.id not in confirmed_item_ids:
                continue
            provider = providers.get(item.provider_id)
            if provider and provider.type == ProviderType.INTERNAL and item.provider_ref:
                inv_id, qty = item.provider_ref.split(":")
                await self._inv_repo.release(uuid.UUID(inv_id), int(qty))
            elif item.provider_ref:
                caps = provider.capabilities if provider else {}
                task_type = OutboxTaskType.UNCONFIRM if caps.get("unconfirm") else OutboxTaskType.RELEASE
                await self._outbox_repo.enqueue(
                    task_type=task_type,
                    payload=ReleasePayload(
                        item_id=item.id, provider_id=item.provider_id,
                        provider_ref=item.provider_ref,
                    ).to_dict(),
                    idempotency_key=f"compensate:{reservation.id}:{item.id}",
                )

    def _build_adapter(self, provider: Provider | None) -> MetricsDecorator:
        """Pure construction — no DB access. Only called for external providers."""
        if provider is None:
            raise ValueError("Cannot build adapter for unknown provider")
        timeout_s = provider.timeout_ms / 1000
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
        return MetricsDecorator(TimeoutDecorator(adapter, timeout_s), str(provider.id))
