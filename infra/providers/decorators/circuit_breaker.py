"""
CircuitBreakerDecorator — three-state (closed/open/half-open), Redis-backed shared state.

State machine per §10 of BUILD-SPEC:
- CLOSED: calls go through; failures counted with INCR+EXPIRE.
- OPEN: fail-fast (no call); auto-expires via Redis TTL.
- HALF-OPEN: one trial probe allowed via SET NX; success → CLOSED.

Redis key scheme (per provider):
  cb:{provider_id}:failures  — INCR counter (window TTL)
  cb:{provider_id}:open      — exists → circuit is OPEN (value=1, TTL=cooldown)
  cb:{provider_id}:probe     — SET NX → acquired the half-open probe slot
"""
import logging
from typing import Any

log = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """Raised when a call is blocked by an open circuit breaker."""


class CircuitBreakerDecorator:
    def __init__(
        self,
        adapter: Any,
        provider_id: str,
        redis_client,
        failure_threshold: int = 5,
        window_seconds: int = 60,
        cooldown_seconds: int = 30,
    ) -> None:
        self._adapter = adapter
        self._pid = provider_id
        self._redis = redis_client
        self._threshold = failure_threshold
        self._window = window_seconds
        self._cooldown = cooldown_seconds

    @property
    def capabilities(self):
        return self._adapter.capabilities

    async def _is_open(self) -> bool:
        return bool(await self._redis.exists(f"cb:{self._pid}:open"))

    async def _try_acquire_probe(self) -> bool:
        """Returns True if this caller gets the single half-open probe slot."""
        result = await self._redis.set(
            f"cb:{self._pid}:probe", "1", nx=True, ex=self._cooldown
        )
        return result is not None

    async def _record_failure(self) -> None:
        key = f"cb:{self._pid}:failures"
        count = await self._redis.incr(key)
        await self._redis.expire(key, self._window)
        if count >= self._threshold:
            await self._redis.set(f"cb:{self._pid}:open", "1", ex=self._cooldown)
            await self._redis.delete(key)
            log.warning("Circuit breaker OPENED for provider %s", self._pid)

    async def _record_success(self) -> None:
        await self._redis.delete(f"cb:{self._pid}:open")
        await self._redis.delete(f"cb:{self._pid}:probe")
        await self._redis.delete(f"cb:{self._pid}:failures")

    async def _call(self, fn, *args, **kwargs):
        if await self._is_open():
            if not await self._try_acquire_probe():
                raise CircuitOpenError(f"Circuit breaker open for provider {self._pid}")
            # Half-open: allow this one probe through
        try:
            result = await fn(*args, **kwargs)
            await self._record_success()
            return result
        except (TimeoutError, Exception) as exc:
            await self._record_failure()
            raise

    async def check_availability(self, *args, **kwargs):
        return await self._call(self._adapter.check_availability, *args, **kwargs)

    async def reserve(self, *args, **kwargs):
        return await self._call(self._adapter.reserve, *args, **kwargs)

    async def confirm(self, *args, **kwargs):
        return await self._call(self._adapter.confirm, *args, **kwargs)

    async def release(self, *args, **kwargs):
        return await self._call(self._adapter.release, *args, **kwargs)

    async def unconfirm(self, *args, **kwargs):
        return await self._call(self._adapter.unconfirm, *args, **kwargs)
