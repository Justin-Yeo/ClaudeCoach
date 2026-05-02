"""Centralised settings loaded from `.env` via Pydantic Settings.

Single source of truth for every env var declared in [spec.md §11.2](spec.md).
Imported wherever the app needs configuration — DB, Strava, Claude, Telegram.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database
    DATABASE_URL: str

    # --- Strava OAuth + webhook
    STRAVA_CLIENT_ID: str
    STRAVA_CLIENT_SECRET: str
    STRAVA_WEBHOOK_VERIFY_TOKEN: str = ""

    # --- Telegram bot
    TELEGRAM_BOT_TOKEN: str

    # --- Claude
    ANTHROPIC_API_KEY: str
    CLAUDE_MODEL: str = "claude-sonnet-4-6"
    CLAUDE_MAX_OUTPUT_TOKENS: int = 3500
    CLAUDE_MAX_RETRIES: int = 5

    # --- Admin bootstrap
    BOOTSTRAP_ADMIN_TELEGRAM_USER_ID: int

    # --- Deployment
    APP_BASE_URL: str = "http://localhost:8000"

    # --- Logging
    LOG_LEVEL: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton accessor for the app settings."""
    return Settings()
