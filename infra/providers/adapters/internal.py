"""
InternalAdapter — stock lives in our own DB.
reserve/confirm/release delegate directly to InventoryRepository
(same DB transaction, strong consistency, no network call).
"""
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.repositories.inventory_repo import InventoryRepository
from infra.providers.port import (
    AvailabilityResult,
    ConfirmResult,
    ProviderCapabilities,
    ReleaseResult,
    ReserveResult,
)


class InternalAdapter:
    """Implements ReadableProvider + ReservableProvider for internal stock."""

    _caps = ProviderCapabilities(reserve=True, confirm=True, release=True, unconfirm=False)

    def __init__(self, session: AsyncSession) -> None:
        self._repo = InventoryRepository(session)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def check_availability(self, product_id: UUID, provider_id: UUID) -> AvailabilityResult:
        inv = await self._repo.get_by_product_provider(product_id, provider_id)
        qty = inv.qty_available if inv else 0
        return AvailabilityResult(product_id=product_id, provider_id=provider_id, qty_available=qty)

    async def reserve(self, product_id: UUID, provider_id: UUID, qty: int, idempotency_key: str) -> ReserveResult:
        inv = await self._repo.get_by_product_provider(product_id, provider_id)
        if inv is None:
            return ReserveResult(success=False, error="No inventory row found")
        ok = await self._repo.reserve(inv.id, qty)
        if not ok:
            return ReserveResult(success=False, error="Insufficient stock")
        return ReserveResult(success=True, provider_ref=f"{inv.id}:{qty}")

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        # provider_ref encodes "inventory_id:qty" for internal lines
        inventory_id_str, qty_str = provider_ref.split(":")
        from uuid import UUID as _UUID
        ok = await self._repo.consume(_UUID(inventory_id_str), int(qty_str))
        if not ok:
            return ConfirmResult(success=False, definitive_rejection=True, error="Consume failed")
        return ConfirmResult(success=True)

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        inventory_id_str, qty_str = provider_ref.split(":")
        from uuid import UUID as _UUID
        ok = await self._repo.release(_UUID(inventory_id_str), int(qty_str))
        return ReleaseResult(success=ok, error=None if ok else "Release failed")

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        # Internal: unconfirm = release (restore hold)
        return await self.release(provider_ref, idempotency_key)
