"""
SoftHoldAdapter — read-only provider (cannot hold stock remotely).

We maintain a mirror of their inventory in our DB, kept fresh by a background sync worker.
Reserve/release/confirm operate entirely against that local mirror — no external API call
during the hot reservation path. The residual risk is staleness: if the sync lags, we may
soft-hold more than the provider actually has. That is accepted and handled at confirm time
via re-check and NEEDS_RESOLUTION routing (see DESIGN-NOTES §2).

The provider_ref format is "inventory_id:qty", the same encoding used by InternalAdapter,
because the underlying InventoryRepository operations are identical.
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


class SoftHoldAdapter:
    """Implements ReadableProvider + ReservableProvider via local DB mirror."""

    _caps = ProviderCapabilities(reserve=False, confirm=False, release=False, unconfirm=False)

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
        # idempotency_key accepted per protocol but unused: the conditional UPDATE is atomic
        # and the use-case layer deduplicates via get_by_idempotency_key before reaching here.
        inv = await self._repo.get_by_product_provider(product_id, provider_id)
        if inv is None:
            return ReserveResult(success=False, error="No inventory row found")
        ok = await self._repo.reserve(inv.id, qty)
        if not ok:
            return ReserveResult(success=False, error="Insufficient cached stock")
        return ReserveResult(success=True, provider_ref=f"{inv.id}:{qty}")

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        # For read-only providers we don't own qty_on_hand — the background sync manages it.
        # Confirm = release the soft hold; the sync worker will correct qty_on_hand later.
        return await self.release(provider_ref, idempotency_key)

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        inventory_id_str, qty_str = provider_ref.split(":")
        ok = await self._repo.release(UUID(inventory_id_str), int(qty_str))
        return ReleaseResult(success=ok, error=None if ok else "Release failed")

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        return await self.release(provider_ref, idempotency_key)
