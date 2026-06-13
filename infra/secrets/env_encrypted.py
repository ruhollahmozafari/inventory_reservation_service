"""
EnvEncryptedSecretProvider — ship now; swap for VaultSecretProvider later.

Secrets are stored encrypted in the provider.secret_ref column (Fernet-encrypted).
The KEK (key-encryption key) lives in SECRET_KEK env var — never in the DB.
Fernet is initialized lazily so tests that never call get_secret don't need a valid key.
"""
import base64

from cryptography.fernet import Fernet, InvalidToken

from config import settings
from infra.secrets.port import AbstractSecretProvider


class EnvEncryptedSecretProvider(AbstractSecretProvider):
    def __init__(self) -> None:
        self._fernet: Fernet | None = None

    def _get_fernet(self) -> Fernet:
        if self._fernet is None:
            self._fernet = Fernet(settings.SECRET_KEK.encode())
        return self._fernet

    async def get_secret(self, secret_ref: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(secret_ref.encode())
            return self._get_fernet().decrypt(raw).decode()
        except (InvalidToken, Exception) as exc:
            raise ValueError(f"Failed to decrypt secret_ref: {exc}") from exc

    def encrypt_secret(self, plaintext: str) -> str:
        encrypted = self._get_fernet().encrypt(plaintext.encode())
        return base64.urlsafe_b64encode(encrypted).decode()
