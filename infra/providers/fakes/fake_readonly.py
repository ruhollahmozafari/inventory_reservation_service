"""
FakeReadOnlyProvider — simulates a read-only external source.
Reserve is a local soft-hold; confirm does a re-check.
Supports stale-data injection for scenario testing.
"""
import uuid
from uuid import UUID

from infra.providers.port import (
    AvailabilityResult,
    ConfirmResult,
    ProviderCapabilities,
    ReleaseResult,
    ReserveResult,
)


class FakeReadOnlyProvider:
    """Soft-hold fake. Confirm-time re-check can be made to fail via stale_qty."""

    _caps = ProviderCapabilities(reserve=False, confirm=False, release=False, unconfirm=False)

    def __init__(
        self,
        provider_id: UUID,
        stock: dict[UUID, int] | None = None,
        *,
        stale_qty: dict[UUID, int] | None = None,  # override qty returned at re-check time
    ) -> None:
        self._provider_id = provider_id
        self._stock: dict[UUID, int] = stock or {}
        self._stale_qty = stale_qty or {}
        self._soft_holds: dict[str, tuple[UUID, int]] = {}  # key → (product_id, qty)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def check_availability(self, product_id: UUID, provider_id: UUID) -> AvailabilityResult:
        qty = self._stock.get(product_id, 0)
        return AvailabilityResult(
            product_id=product_id,
            provider_id=provider_id,
            qty_available=qty,
            is_stale=product_id in self._stale_qty,
        )

    async def reserve(self, product_id: UUID, provider_id: UUID, qty: int, idempotency_key: str) -> ReserveResult:
        available = self._stock.get(product_id, 0) - sum(
            q for pid, q in self._soft_holds.values() if pid == product_id
        )
        if available < qty:
            return ReserveResult(success=False, error="Insufficient cached stock")
        self._soft_holds[idempotency_key] = (product_id, qty)
        return ReserveResult(success=True, provider_ref=f"soft:{product_id}:{idempotency_key}")

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        _, product_id_str, hold_key = provider_ref.split(":", 2)
        product_id = UUID(product_id_str)
        held_qty = 0
        if hold_key in self._soft_holds:
            _, held_qty = self._soft_holds[hold_key]
        # Use stale_qty if set (simulates stale cache data)
        recheck_qty = self._stale_qty.get(product_id, self._stock.get(product_id, 0))
        if recheck_qty < held_qty:
            self._soft_holds.pop(hold_key, None)
            return ConfirmResult(success=False, definitive_rejection=True, error="Re-check: stale data — insufficient")
        self._soft_holds.pop(hold_key, None)
        return ConfirmResult(success=True)

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        _, _, hold_key = provider_ref.split(":", 2)
        self._soft_holds.pop(hold_key, None)
        return ReleaseResult(success=True)

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        return await self.release(provider_ref, idempotency_key)
