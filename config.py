from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "Inventory Reservation Service"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/inventory_db"
    REDIS_URL: str = "redis://localhost:6379/0"

    # Reservation TTL in seconds before the expiry sweeper reclaims the hold
    RESERVATION_TTL_SECONDS: int = 900
    # Grace window for crash-recovery sweeper: INITIALIZING reservations past this are rolled back
    RESERVATION_CREATE_GRACE_SECONDS: int = 30

    # Key-encryption key for secrets stored in the provider table.
    # Must be a Fernet-compatible 32-byte url-safe base64 string.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    SECRET_KEK: str = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

    # Provider HTTP call timeout (seconds); individual adapters may override via provider.timeout_ms
    PROVIDER_REQUEST_TIMEOUT: float = 5.0

    # Outbox worker settings
    OUTBOX_BATCH_SIZE: int = 20
    OUTBOX_LEASE_SECONDS: int = 60
    OUTBOX_POLL_INTERVAL_SECONDS: float = 2.0
    OUTBOX_MAX_ATTEMPTS: int = 10
    OUTBOX_BASE_BACKOFF_SECONDS: float = 5.0

    # Expiry sweeper
    SWEEPER_BATCH_SIZE: int = 50
    SWEEPER_POLL_INTERVAL_SECONDS: float = 10.0

    # Circuit breaker
    CB_FAILURE_THRESHOLD: int = 5
    CB_WINDOW_SECONDS: int = 60
    CB_COOLDOWN_SECONDS: int = 30


settings = Settings()
