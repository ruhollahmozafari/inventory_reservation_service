"""
TimeoutDecorator — wraps any provider call with asyncio.wait_for.
Raises asyncio.TimeoutError on breach; callers treat that as PENDING_UNKNOWN.
"""
import asyncio
from uuid import UUID

from infra.providers.port import (
    AvailabilityResult,
    ConfirmResult,
    ProviderCapabilities,
    ReleaseResult,
    ReserveResult,
)


class TimeoutDecorator:
    """Wraps any adapter to enforce per-call timeouts on all provider methods."""

    def __init__(self, adapter: object, timeout_seconds: float) -> None:
        self._adapter = adapter
        self._timeout = timeout_seconds

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._adapter.capabilities  # type: ignore[union-attr]

    async def check_availability(self, product_id: UUID, provider_id: UUID) -> AvailabilityResult:
        return await asyncio.wait_for(
            self._adapter.check_availability(product_id, provider_id), self._timeout  # type: ignore[union-attr]
        )

    async def reserve(self, product_id: UUID, provider_id: UUID, qty: int, idempotency_key: str) -> ReserveResult:
        return await asyncio.wait_for(
            self._adapter.reserve(product_id, provider_id, qty, idempotency_key), self._timeout  # type: ignore[union-attr]
        )

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        return await asyncio.wait_for(
            self._adapter.confirm(provider_ref, idempotency_key), self._timeout  # type: ignore[union-attr]
        )

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        return await asyncio.wait_for(
            self._adapter.release(provider_ref, idempotency_key), self._timeout  # type: ignore[union-attr]
        )

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        return await asyncio.wait_for(
            self._adapter.unconfirm(provider_ref, idempotency_key), self._timeout  # type: ignore[union-attr]
        )
