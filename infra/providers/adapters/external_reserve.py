"""
ExternalReserveAdapter — full TCC lifecycle over HTTP.

The adapter owns business logic (request shape, response interpretation, status codes).
HTTP concerns (auth headers, base URL, idempotency header) are owned by ProviderHttpClient.
"""
from uuid import UUID

from infra.http.provider_client import ProviderHttpClient
from infra.providers.port import (
    AvailabilityResult,
    ConfirmResult,
    ProviderCapabilities,
    ReleaseResult,
    ReserveResult,
)


class ExternalReserveAdapter:
    """Implements ReadableProvider + ReservableProvider for external APIs."""

    def __init__(self, client: ProviderHttpClient, capabilities: ProviderCapabilities) -> None:
        self._client = client
        self._caps = capabilities

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def check_availability(self, product_id: UUID, provider_id: UUID) -> AvailabilityResult:
        resp = await self._client.get(f"/availability/{product_id}")
        resp.raise_for_status()
        qty = resp.json().get("qty_available")
        if qty is None:
            return AvailabilityResult(
                product_id=product_id, provider_id=provider_id,
                qty_available=0, is_stale=True,
            )
        return AvailabilityResult(
            product_id=product_id, provider_id=provider_id, qty_available=int(qty)
        )

    async def reserve(self, product_id: UUID, provider_id: UUID, qty: int, idempotency_key: str) -> ReserveResult:
        resp = await self._client.post(
            "/reserve",
            idempotency_key=idempotency_key,
            json={"product_id": str(product_id), "qty": qty},
        )
        if resp.status_code == 409:
            return ReserveResult(success=False, error="Provider: insufficient stock")
        resp.raise_for_status()
        hold_id = resp.json().get("hold_id")
        if not hold_id:
            return ReserveResult(success=False, error="Provider returned no hold_id")
        return ReserveResult(success=True, provider_ref=hold_id)

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        resp = await self._client.post(
            f"/confirm/{provider_ref}", idempotency_key=idempotency_key
        )
        if resp.status_code == 422:
            return ConfirmResult(success=False, definitive_rejection=True, error=resp.text)
        resp.raise_for_status()
        return ConfirmResult(success=True)

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        resp = await self._client.post(
            f"/release/{provider_ref}", idempotency_key=idempotency_key
        )
        resp.raise_for_status()
        return ReleaseResult(success=True)

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        if not self._caps.unconfirm:
            return await self.release(provider_ref, idempotency_key)
        resp = await self._client.post(
            f"/unconfirm/{provider_ref}", idempotency_key=idempotency_key
        )
        resp.raise_for_status()
        return ReleaseResult(success=True)
