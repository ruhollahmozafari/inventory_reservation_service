"""
RetryDecorator — bounded exponential-backoff retry for idempotent ops.
Only retries on transient errors (network, 5xx); not on TimeoutError (fail-closed).
"""
import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

# Errors that indicate a transient condition worth retrying
_RETRYABLE = (ConnectionError, OSError)


class RetryDecorator:
    def __init__(self, adapter: Any, max_attempts: int = 3, base_delay: float = 0.5) -> None:
        self._adapter = adapter
        self._max_attempts = max_attempts
        self._base_delay = base_delay

    @property
    def capabilities(self):
        return self._adapter.capabilities

    async def _call_with_retry(self, fn, *args, **kwargs):
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return await fn(*args, **kwargs)
            except _RETRYABLE as exc:
                last_exc = exc
                if attempt < self._max_attempts:
                    delay = self._base_delay * (2 ** (attempt - 1))
                    log.warning("Retrying after %s (attempt %d): %s", delay, attempt, exc)
                    await asyncio.sleep(delay)
            except Exception:
                raise  # non-retryable (TimeoutError, ValueError, etc.)
        raise last_exc

    async def check_availability(self, *args, **kwargs):
        return await self._call_with_retry(self._adapter.check_availability, *args, **kwargs)

    async def reserve(self, *args, **kwargs):
        # Reserve is idempotent (idempotency_key), safe to retry on network errors
        return await self._call_with_retry(self._adapter.reserve, *args, **kwargs)

    async def confirm(self, *args, **kwargs):
        return await self._call_with_retry(self._adapter.confirm, *args, **kwargs)

    async def release(self, *args, **kwargs):
        return await self._call_with_retry(self._adapter.release, *args, **kwargs)

    async def unconfirm(self, *args, **kwargs):
        return await self._call_with_retry(self._adapter.unconfirm, *args, **kwargs)
