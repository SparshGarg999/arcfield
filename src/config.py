"""Configuration settings for Arcfield."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded from environment variables and optionally a .env file."""
    
    database_url: str = "postgresql+asyncpg://arcfield:arcfield@localhost:5433/arcfield"
    idempotency_retention_hours: int = 24
    testing: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
