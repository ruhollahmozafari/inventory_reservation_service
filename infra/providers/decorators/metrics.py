"""
MetricsDecorator — lightweight timing/counting wrapper.
Logs structured metrics; swap for Prometheus/OpenTelemetry without touching core.
"""
import logging
import time
from typing import Any

log = logging.getLogger("metrics")


class MetricsDecorator:
    def __init__(self, adapter: Any, provider_id: str) -> None:
        self._adapter = adapter
        self._pid = provider_id

    @property
    def capabilities(self):
        return self._adapter.capabilities

    async def _timed(self, op: str, fn, *args, **kwargs):
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

    async def check_availability(self, *args, **kwargs):
        return await self._timed("check_availability", self._adapter.check_availability, *args, **kwargs)

    async def reserve(self, *args, **kwargs):
        return await self._timed("reserve", self._adapter.reserve, *args, **kwargs)

    async def confirm(self, *args, **kwargs):
        return await self._timed("confirm", self._adapter.confirm, *args, **kwargs)

    async def release(self, *args, **kwargs):
        return await self._timed("release", self._adapter.release, *args, **kwargs)

    async def unconfirm(self, *args, **kwargs):
        return await self._timed("unconfirm", self._adapter.unconfirm, *args, **kwargs)
