"""
TimeoutDecorator — wraps any provider call with asyncio.wait_for.
Raises asyncio.TimeoutError on breach; callers treat that as PENDING_UNKNOWN.
"""
import asyncio
from functools import wraps
from typing import Any, Callable


def with_timeout(timeout_seconds: float):
    """Decorator factory: wraps an async method with a timeout."""
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout_seconds)
        return wrapper
    return decorator


class TimeoutDecorator:
    """Wraps any adapter to enforce per-call timeouts on all provider methods."""

    def __init__(self, adapter: Any, timeout_seconds: float) -> None:
        self._adapter = adapter
        self._timeout = timeout_seconds

    @property
    def capabilities(self):
        return self._adapter.capabilities

    async def check_availability(self, *args, **kwargs):
        return await asyncio.wait_for(self._adapter.check_availability(*args, **kwargs), self._timeout)

    async def reserve(self, *args, **kwargs):
        return await asyncio.wait_for(self._adapter.reserve(*args, **kwargs), self._timeout)

    async def confirm(self, *args, **kwargs):
        return await asyncio.wait_for(self._adapter.confirm(*args, **kwargs), self._timeout)

    async def release(self, *args, **kwargs):
        return await asyncio.wait_for(self._adapter.release(*args, **kwargs), self._timeout)

    async def unconfirm(self, *args, **kwargs):
        return await asyncio.wait_for(self._adapter.unconfirm(*args, **kwargs), self._timeout)
