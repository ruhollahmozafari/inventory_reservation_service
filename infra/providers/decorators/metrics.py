"""
MetricsDecorator — lightweight timing/counting wrapper.
Logs structured metrics; swap for Prometheus/OpenTelemetry without touching core.
"""
import logging
import time
from typing import Any, Awaitable, Callable
from uuid import UUID

from infra.providers.port import (
    AvailabilityResult,
    ConfirmResult,
    ProviderCapabilities,
    ReleaseResult,
    ReserveResult,
)

log = logging.getLogger("metrics")


class MetricsDecorator:
    def __init__(self, adapter: object, provider_id: str) -> None:
        self._adapter = adapter
        self._pid = provider_id

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._adapter.capabilities  # type: ignore[union-attr]

    async def _timed(self, op: str, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        start = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            elapsed = (time.monotonic() - start) * 1000
            log.info("provider=%s op=%s status=ok latency_ms=%.1f", self._pid, op, elapsed)
            return result
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            log.warning("provider=%s op=%s status=error latency_ms=%.1f error=%s", self._pid, op, elapsed, exc)
            raise

    async def check_availability(self, product_id: UUID, provider_id: UUID) -> AvailabilityResult:
        return await self._timed("check_availability", self._adapter.check_availability, product_id, provider_id)  # type: ignore[union-attr]

    async def reserve(self, product_id: UUID, provider_id: UUID, qty: int, idempotency_key: str) -> ReserveResult:
        return await self._timed("reserve", self._adapter.reserve, product_id, provider_id, qty, idempotency_key)  # type: ignore[union-attr]

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        return await self._timed("confirm", self._adapter.confirm, provider_ref, idempotency_key)  # type: ignore[union-attr]

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        return await self._timed("release", self._adapter.release, provider_ref, idempotency_key)  # type: ignore[union-attr]

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        return await self._timed("unconfirm", self._adapter.unconfirm, provider_ref, idempotency_key)  # type: ignore[union-attr]
