from dataclasses import dataclass
from uuid import UUID

from domain.enums import ProviderType


@dataclass
class Provider:
    id: UUID
    type: ProviderType
    base_url: str | None
    timeout_ms: int
    capabilities: dict
    secret_ref: str | None
