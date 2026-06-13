from abc import ABC, abstractmethod


class AbstractSecretProvider(ABC):
    @abstractmethod
    async def get_secret(self, secret_ref: str) -> str:
        """Resolve a secret_ref string to the actual secret value."""
        ...
