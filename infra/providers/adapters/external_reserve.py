"""
ExternalReserveAdapter — full TCC lifecycle over HTTP.
Calls the external provider API for reserve/confirm/release.
Auth header injected from SecretProvider at call time.
"""
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

class ExternalReserveAdapter:
    """Implements ReadableProvider + ReservableProvider for external APIs."""

    def __init__(
        self,
        provider_id: UUID,
        base_url: str,
        secret_ref: str | None,
        secret_provider: AbstractSecretProvider,
        capabilities_cfg: dict,
        timeout_ms: int = 5000,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._base_url = base_url.rstrip("/")
        self._secret_ref = secret_ref
        self._secret_provider = secret_provider
        self._timeout = timeout_ms / 1000
        self._caps = ProviderCapabilities(**capabilities_cfg)
        self._http = http_client or httpx.AsyncClient(timeout=self._timeout)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._caps

    async def _auth_headers(self) -> dict:
        if not self._secret_ref:
            return {}
        token = await self._secret_provider.get_secret(self._secret_ref)
        return {"Authorization": f"Bearer {token}"}

    async def check_availability(self, product_id: UUID, provider_id: UUID) -> AvailabilityResult:
        headers = await self._auth_headers()
        resp = await self._http.get(
            f"{self._base_url}/availability/{product_id}",
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return AvailabilityResult(
            product_id=product_id,
            provider_id=provider_id,
            qty_available=data["qty_available"],
        )

    async def reserve(self, product_id: UUID, provider_id: UUID, qty: int, idempotency_key: str) -> ReserveResult:
        headers = {**await self._auth_headers(), "Idempotency-Key": idempotency_key}
        resp = await self._http.post(
            f"{self._base_url}/reserve",
            json={"product_id": str(product_id), "qty": qty},
            headers=headers,
        )
        if resp.status_code == 409:
            return ReserveResult(success=False, error="Provider: insufficient stock")
        resp.raise_for_status()
        data = resp.json()
        return ReserveResult(success=True, provider_ref=data["hold_id"])

    async def confirm(self, provider_ref: str, idempotency_key: str) -> ConfirmResult:
        headers = {**await self._auth_headers(), "Idempotency-Key": idempotency_key}
        resp = await self._http.post(
            f"{self._base_url}/confirm/{provider_ref}",
            headers=headers,
        )
        if resp.status_code == 422:
            return ConfirmResult(success=False, definitive_rejection=True, error=resp.text)
        resp.raise_for_status()
        return ConfirmResult(success=True)

    async def release(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        headers = {**await self._auth_headers(), "Idempotency-Key": idempotency_key}
        resp = await self._http.post(
            f"{self._base_url}/release/{provider_ref}",
            headers=headers,
        )
        resp.raise_for_status()
        return ReleaseResult(success=True)

    async def unconfirm(self, provider_ref: str, idempotency_key: str) -> ReleaseResult:
        if not self._caps.unconfirm:
            return await self.release(provider_ref, idempotency_key)
        headers = {**await self._auth_headers(), "Idempotency-Key": idempotency_key}
        resp = await self._http.post(
            f"{self._base_url}/unconfirm/{provider_ref}",
            headers=headers,
        )
        resp.raise_for_status()
        return ReleaseResult(success=True)