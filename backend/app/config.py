from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    """
    Project Aegis Backend Configuration managed via Pydantic v2 Settings.
    Reads from environment variables and optional .env file.
    Does NOT contain working secret values by default.
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
    backup_dir: Optional[str] = Field(
        default=None,
        alias="AEGIS_BACKUP_DIR",
    )
    # Security: No working default secret! Must be configured explicitly in .env
    api_secret: str = Field(
        default="",
        alias="AEGIS_API_SECRET",
    )
    # Security: Dedicated server-side HMAC secret for 6-digit device pairing codes.
    # Stored strictly outside the database. Must NOT fallback to AEGIS_API_SECRET.
    pairing_secret: str = Field(
        default="",
        alias="AEGIS_PAIRING_SECRET",
    )
    # Security: Dedicated server-side secret for signing short-lived HS256 JWT access tokens.
    # Stored strictly outside the database. Must NOT fallback to or equal AEGIS_API_SECRET or AEGIS_PAIRING_SECRET.
    jwt_secret: str = Field(
        default="",
        alias="AEGIS_JWT_SECRET",
    )
    jwt_issuer: str = Field(
        default="aegis-backend",
        alias="AEGIS_JWT_ISSUER",
    )
    jwt_audience: str = Field(
        default="aegis-mobile-client",
        alias="AEGIS_JWT_AUDIENCE",
    )
    access_token_ttl_minutes: int = Field(
        default=30,
        ge=1,
        le=120,
        alias="AEGIS_ACCESS_TOKEN_TTL_MINUTES",
    )
    refresh_token_ttl_days: int = Field(
        default=30,
        ge=1,
        le=365,
        alias="AEGIS_REFRESH_TOKEN_TTL_DAYS",
    )
    # Comma-separated list of trusted reverse-proxy IPs allowed to supply X-Forwarded-For.
    # Empty by default (never trust proxy headers unless explicitly configured).
    trusted_proxy_ips: str = Field(
        default="",
        alias="AEGIS_TRUSTED_PROXIES",
    )
    max_active_pairing_codes: int = Field(
        default=10,
        ge=1,
        le=100,
        alias="AEGIS_MAX_ACTIVE_PAIRING_CODES",
    )
    gemini_api_key: str = Field(
        default="",
        alias="GEMINI_API_KEY",
    )
    gemini_flash_lite_model: str = Field(
        default="gemini-3.1-flash-lite",
        alias="AEGIS_GEMINI_FLASH_LITE_MODEL",
    )
    gemini_flash_model: str = Field(
        default="gemini-3.6-flash",
        alias="AEGIS_GEMINI_FLASH_MODEL",
    )
    gemini_pro_model: str = Field(
        default="gemini-3.1-pro-preview",
        alias="AEGIS_GEMINI_PRO_MODEL",
    )
    gemini_vision_model: str = Field(
        default="gemini-3.8-flash",
        alias="AEGIS_GEMINI_VISION_MODEL",
    )
    gemini_timeout_seconds: float = Field(
        default=60.0,
        ge=1.0,
        le=300.0,
        alias="AEGIS_GEMINI_TIMEOUT_SECONDS",
    )
    gemini_max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        alias="AEGIS_GEMINI_MAX_RETRIES",
    )
    gemini_initial_backoff_seconds: float = Field(
        default=1.0,
        ge=0.0,
        le=30.0,
        alias="AEGIS_GEMINI_INITIAL_BACKOFF_SECONDS",
    )
    gemini_base_url: str = Field(
        default="https://generativelanguage.googleapis.com/v1beta",
        alias="AEGIS_GEMINI_BASE_URL",
    )


settings = Settings()
