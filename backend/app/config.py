from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """
    Project Aegis Backend Configuration managed via Pydantic v2 Settings.
    Reads from environment variables and optional .env file.
    """
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Project Aegis Private Backend"
    app_version: str = "1.0.0"
    env: str = Field(default="development", alias="AEGIS_ENV")
    host: str = Field(default="127.0.0.1", alias="AEGIS_HOST")
    port: int = Field(default=8080, alias="AEGIS_PORT")
    log_level: str = Field(default="info", alias="AEGIS_LOG_LEVEL")
    database_url: str = Field(
        default="sqlite:///./aegis_private_vault.db",
        alias="AEGIS_DATABASE_URL",
    )
    api_secret: str = Field(
        default="local_dev_device_token",
        alias="AEGIS_API_SECRET",
    )
    gemini_api_key: str = Field(
        default="",
        alias="GEMINI_API_KEY",
    )


settings = Settings()
