"""
FakeExternalProvider — stands in for a real external HTTP provider.
Configurable fault injection: latency, error rate, timeout simulation, stale data.
Used in integration tests; no separate HTTP service needed.
"""
import asyncio
import random
import uuid
from uuid import UUID

from infra.providers.port import (
    AvailabilityResult,
    ConfirmResult,
    ProviderCapabilities,
    ReleaseResult,
    ReserveResult,
)


class FakeExternalProvider:
    """Full TCC lifecycle fake with fault injection knobs."""

    _caps = ProviderCapabilities(reserve=True, confirm=True, release=True, unconfirm=True)

    def __init__(
        self,
        provider_id: UUID,
        stock: dict[UUID, int] | None = None,
        *,
        latency_ms: float = 0,
        error_rate: float = 0.0,       # 0-1: probability of raising an exception
        timeout_on_reserve: bool = False,  # simulate reserve timeout (scenario B)
        timeout_on_confirm: bool = False,
        rejection_on_confirm: bool = False,  # simulate definitive rejection on confirm
    ) -> None:
        self._provider_id = provider_id
        self._stock: dict[UUID, int] = stock or {}
        self._holds: dict[str, tuple[UUID, int]] = {}  # hold_id → (product_id, qty)
        self.latency_ms = latency_ms
        self.error_rate = error_rate
        self.timeout_on_reserve = timeout_on_reserve
        self.timeout_on_confirm = timeout_on_confirm
        self.rejection_on_confirm = rejection_on_confirm

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def _maybe_delay(self) -> None:
        if self.latency_ms:
            await asyncio.sleep(self.latency_ms / 1000)

    async def _maybe_error(self) -> None:
        if self.error_rate and random.random() < self.error_rate:
            raise RuntimeError("FakeExternalProvider: injected error")

    async def check_availability(self, product_id: UUID, provider_id: UUID) -> AvailabilityResult:
        await self._maybe_delay()
        await self._maybe_error()
        qty = self._stock.get(product_id, 0)
        return AvailabilityResult(product_id=product_id, provider_id=provider_id, qty_available=qty)

    async def reserve(self, product_id: UUID, provider_id: UUID, qty: int, idempotency_key: str) -> ReserveResult:
        await self._maybe_delay()
        if self.timeout_on_reserve:
            raise TimeoutError("FakeExternalProvider: simulated reserve timeout")
        await self._maybe_error()
        available = self._stock.get(product_id, 0)
        if available < qty:
            return ReserveResult(success=False, error="Insufficient stock")
        self._stock[product_id] = available - qty
        hold_id = str(uuid.uuid4())
        self._holds[hold_id] = (product_id, qty)
        return ReserveResult(success=True, provider_ref=hold_id)

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        await self._maybe_delay()
        if self.timeout_on_confirm:
            raise TimeoutError("FakeExternalProvider: simulated confirm timeout")
        if self.rejection_on_confirm:
            return ConfirmResult(success=False, definitive_rejection=True, error="Fake: definitive rejection")
        if provider_ref not in self._holds:
            # Idempotent — already confirmed or unknown ref; treat as success
            return ConfirmResult(success=True)
        del self._holds[provider_ref]
        return ConfirmResult(success=True)

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        await self._maybe_delay()
        if provider_ref in self._holds:
            product_id, qty = self._holds.pop(provider_ref)
            self._stock[product_id] = self._stock.get(product_id, 0) + qty
        # Idempotent: if ref not found, it was already released — that's fine
        return ReleaseResult(success=True)

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        return await self.release(provider_ref, idempotency_key)
