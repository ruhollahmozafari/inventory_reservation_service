"""
ProviderHttpClient — thin HTTP gateway for all external provider calls.

Owns: auth header injection, base URL composition, idempotency-key header.
Doesn't own: response parsing, business logic (those stay in the adapter).
"""
import httpx

from infra.http.auth import AuthStrategy


class ProviderHttpClient:
    def __init__(self, base_url: str, auth: AuthStrategy, timeout_s: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._http = httpx.AsyncClient(timeout=timeout_s)

    async def get(self, path: str) -> httpx.Response:
        headers = await self._auth.headers()
        return await self._http.get(f"{self._base_url}{path}", headers=headers)

    async def post(
        self,
        path: str,
        idempotency_key: str,
        json: dict | None = None,
    ) -> httpx.Response:
        headers = {**await self._auth.headers(), "Idempotency-Key": idempotency_key}
        return await self._http.post(
            f"{self._base_url}{path}", json=json, headers=headers
        )
