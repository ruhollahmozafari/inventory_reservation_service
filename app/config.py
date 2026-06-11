from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "Inventory Reservation Service"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False

    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/inventory_db"

    RESERVATION_TTL_SECONDS: int = 900  # 15 minutes before auto-release

    PROVIDER_REQUEST_TIMEOUT: float = 5.0  # seconds


settings = Settings()
