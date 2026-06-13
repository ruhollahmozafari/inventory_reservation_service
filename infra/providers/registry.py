"""
ProviderRegistry — provider_id → adapter, resolved at runtime.
No switch statements or if-type branches anywhere in calling code.
"""
from uuid import UUID

from infra.providers.port import ReadableProvider, ReservableProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._adapters: dict[UUID, object] = {}

    def register(self, provider_id: UUID, adapter: object) -> None:
        self._adapters[provider_id] = adapter

    def get_readable(self, provider_id: UUID) -> ReadableProvider:
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise KeyError(f"No adapter registered for provider {provider_id}")
        if not isinstance(adapter, ReadableProvider):
            raise TypeError(f"Adapter for {provider_id} does not implement ReadableProvider")
        return adapter  # type: ignore[return-value]

    def get_reservable(self, provider_id: UUID) -> ReservableProvider:
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise KeyError(f"No adapter registered for provider {provider_id}")
        if not isinstance(adapter, ReservableProvider):
            raise TypeError(f"Adapter for {provider_id} does not implement ReservableProvider")
        return adapter  # type: ignore[return-value]

    def get_any(self, provider_id: UUID) -> object:
        adapter = self._adapters.get(provider_id)
        if adapter is None:
            raise KeyError(f"No adapter registered for provider {provider_id}")
        return adapter
