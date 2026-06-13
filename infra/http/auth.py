from abc import ABC, abstractmethod

from infra.secrets.port import AbstractSecretProvider


class AuthStrategy(ABC):
    @abstractmethod
    async def headers(self) -> dict[str, str]: ...


class BearerAuth(AuthStrategy):
    def __init__(self, secret_ref: str, secret_provider: AbstractSecretProvider) -> None:
        self._secret_ref = secret_ref
        self._secret_provider = secret_provider

    async def headers(self) -> dict[str, str]:
        token = await self._secret_provider.get_secret(self._secret_ref)
        return {"Authorization": f"Bearer {token}"}


class ApiKeyAuth(AuthStrategy):
    def __init__(self, secret_ref: str, secret_provider: AbstractSecretProvider) -> None:
        self._secret_ref = secret_ref
        self._secret_provider = secret_provider

    async def headers(self) -> dict[str, str]:
        token = await self._secret_provider.get_secret(self._secret_ref)
        return {"X-API-Key": token}


class NoAuth(AuthStrategy):
    async def headers(self) -> dict[str, str]:
        return {}


def build_auth(
    auth_type: str,
    secret_ref: str | None,
    secret_provider: AbstractSecretProvider,
) -> AuthStrategy:
    if secret_ref and auth_type == "api_key":
        return ApiKeyAuth(secret_ref, secret_provider)
    if secret_ref and auth_type == "bearer":
        return BearerAuth(secret_ref, secret_provider)
    return NoAuth()
