from abc import ABC, abstractmethod
from uuid import UUID

from domain.entities.provider import Provider


class AbstractProviderRepository(ABC):
    @abstractmethod
    async def get_by_id(self, provider_id: UUID) -> Provider | None: ...

    @abstractmethod
    async def list_by_type(self, provider_type: str) -> list[Provider]: ...
