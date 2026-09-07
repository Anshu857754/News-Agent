"""Application settings and logging setup.

Secrets never appear in code. Everything here comes from the process
environment or from the project-root `.env`, which is git-ignored.

One `Settings` object is the single source of truth, so no other module ever
reads `os.environ` directly. That is what makes the app configurable without a
code change and testable without touching the real environment.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[3] is the project root holding .env
PROJECT_ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_OPENROUTER_MODEL = "google/gemini-2.5-flash"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class Settings(BaseSettings):
    """Everything tunable without editing code."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # --- application ------------------------------------------------------
    app_name: str = Field(default="Startup Intelligence Newsletter", alias="APP_NAME")
    app_version: str = Field(default="0.1.0", alias="APP_VERSION")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # --- OpenRouter -------------------------------------------------------
    openrouter_api_key: str = Field(default="", alias="OPENROUTER_API_KEY")
    openrouter_model: str = Field(
        default=DEFAULT_OPENROUTER_MODEL, alias="OPENROUTER_MODEL"
    )
    openrouter_base_url: str = Field(
        default=DEFAULT_OPENROUTER_BASE_URL, alias="OPENROUTER_BASE_URL"
    )
    openrouter_timeout_seconds: float = Field(
        default=30.0, alias="OPENROUTER_TIMEOUT_SECONDS"
    )
    openrouter_max_retries: int = Field(default=2, alias="OPENROUTER_MAX_RETRIES")
    openrouter_temperature: float = Field(default=0.3, alias="OPENROUTER_TEMPERATURE")

    # --- Apify (used by the collection stage, later) -----------------------
    # Accepts APIFY_API_KEY too, because that is what some existing .env files
    # already use. One less thing to get wrong when setting the project up.
    apify_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("APIFY_API_TOKEN", "APIFY_API_KEY"),
    )

    # --- properties -------------------------------------------------------
    @property
    def openrouter_enabled(self) -> bool:
        """True when an OpenRouter call could actually succeed."""
        return bool(self.openrouter_api_key.strip() and self.openrouter_model.strip())

    @property
    def apify_enabled(self) -> bool:
        return bool(self.apify_api_token.strip())

    def describe(self) -> dict[str, object]:
        """Safe to log and to return from a health endpoint - never the key."""
        return {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "openrouter_model": self.openrouter_model,
            "openrouter_base_url": self.openrouter_base_url,
            "openrouter_enabled": self.openrouter_enabled,
            "openrouter_key_present": bool(self.openrouter_api_key.strip()),
            "apify_key_present": self.apify_enabled,
            "timeout_seconds": self.openrouter_timeout_seconds,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings object. Cached, so `.env` is read once."""
    return Settings()


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"

# Libraries that log every HTTP request at INFO. Useful at DEBUG, noise here.
_NOISY_LOGGERS = ("httpx", "httpcore", "openai")


def configure_logging(level: Optional[str] = None) -> None:
    """Configure root logging once, at startup.

    Every other module then just uses `logging.getLogger(__name__)`: no custom
    logger classes and no per-module handler wiring, which is the part of
    logging setups that rots.
    """
    resolved = (level or get_settings().log_level).upper()
    logging.basicConfig(level=resolved, format=LOG_FORMAT, force=True)

    if resolved != "DEBUG":
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
