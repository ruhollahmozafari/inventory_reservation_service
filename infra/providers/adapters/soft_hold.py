"""
SoftHoldAdapter — read-only provider (cannot actually hold stock remotely).

Implements the same interface as ReservableProvider so the orchestrator
has zero type-conditionals. The "reserve" is a local soft-hold against
a cached qty; "confirm" is a re-check + local consume; "release" clears the cache entry.

Residual oversell risk is accepted and documented (see DESIGN-NOTES §2 row 3).
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx

from infra.providers.port import (
    AvailabilityResult,
    ConfirmResult,
    ProviderCapabilities,
    ReleaseResult,
    ReserveResult,
)
from infra.secrets.port import AbstractSecretProvider


class _CacheEntry:
    def __init__(self, qty: int, ttl_seconds: int) -> None:
        self.qty = qty
        self.soft_holds: dict[str, int] = {}  # idempotency_key → qty held
        self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    def is_stale(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    def available(self) -> int:
        return self.qty - sum(self.soft_holds.values())


class SoftHoldAdapter:
    """Implements ReadableProvider + ReservableProvider via local cache."""

    _caps = ProviderCapabilities(reserve=False, confirm=False, release=False, unconfirm=False)

    def __init__(
        self,
        provider_id: UUID,
        base_url: str,
        secret_ref: str | None,
        secret_provider: AbstractSecretProvider,
        cache_ttl_seconds: int = 60,
        timeout_ms: int = 5000,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._base_url = base_url.rstrip("/")
        self._secret_ref = secret_ref
        self._secret_provider = secret_provider
        self._ttl = cache_ttl_seconds
        self._timeout = timeout_ms / 1000
        self._http = http_client or httpx.AsyncClient(timeout=self._timeout)
        self._cache: dict[UUID, _CacheEntry] = {}
        self._lock = asyncio.Lock()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def _fetch_availability(self, product_id: UUID) -> int:
        headers: dict = {}
        if self._secret_ref:
            token = await self._secret_provider.get_secret(self._secret_ref)
            headers["Authorization"] = f"Bearer {token}"
        resp = await self._http.get(f"{self._base_url}/availability/{product_id}", headers=headers)
        resp.raise_for_status()
        return resp.json()["qty_available"]

    async def _ensure_cache(self, product_id: UUID) -> _CacheEntry:
        entry = self._cache.get(product_id)
        if entry is None or entry.is_stale():
            qty = await self._fetch_availability(product_id)
            entry = _CacheEntry(qty, self._ttl)
            self._cache[product_id] = entry
        return entry

    async def check_availability(self, product_id: UUID, provider_id: UUID) -> AvailabilityResult:
        async with self._lock:
            entry = await self._ensure_cache(product_id)
        return AvailabilityResult(
            product_id=product_id,
            provider_id=provider_id,
            qty_available=entry.available(),
            is_stale=entry.is_stale(),
        )

    async def reserve(self, product_id: UUID, provider_id: UUID, qty: int, idempotency_key: str) -> ReserveResult:
        async with self._lock:
            entry = await self._ensure_cache(product_id)
            if entry.available() < qty:
                return ReserveResult(success=False, error="Insufficient cached stock")
            entry.soft_holds[idempotency_key] = qty
        # provider_ref encodes the key so confirm/release can find it
        return ReserveResult(success=True, provider_ref=f"soft:{product_id}:{idempotency_key}")

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        """Re-check remote availability; consume from cache if still available."""
        _, product_id_str, hold_key = provider_ref.split(":", 2)
        product_id = UUID(product_id_str)
        async with self._lock:
            qty = await self._fetch_availability(product_id)
            entry = self._cache.get(product_id)
            held_qty = entry.soft_holds.get(hold_key, 0) if entry else 0
            if qty < held_qty:
                # Re-check failed — remove soft hold; sibling lines will be compensated
                if entry:
                    entry.soft_holds.pop(hold_key, None)
                return ConfirmResult(success=False, definitive_rejection=True, error="Re-check: insufficient stock")
            if entry:
                entry.soft_holds.pop(hold_key, None)
                entry.qty = max(0, qty - held_qty)
        return ConfirmResult(success=True)

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        _, product_id_str, hold_key = provider_ref.split(":", 2)
        product_id = UUID(product_id_str)
        async with self._lock:
            entry = self._cache.get(product_id)
            if entry:
                entry.soft_holds.pop(hold_key, None)
        return ReleaseResult(success=True)

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        # unconfirm capability is False — soft holds are always released locally
        return await self.release(provider_ref, idempotency_key)
