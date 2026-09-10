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

    # --- Database ---------------------------------------------------------
    # SQLite by default so the project runs with nothing installed. Point this
    # at PostgreSQL for anything real; no query here is dialect-specific.
    #   postgresql+psycopg://user:pass@host:5432/startuppulse
    database_url: str = Field(
        default="sqlite:///./data/startuppulse.db", alias="DATABASE_URL"
    )

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

    # --- Trend discovery --------------------------------------------------
    # The one place the threshold is defined. Nothing else may hardcode it.
    trend_relevance_threshold: float = Field(
        default=70.0, ge=0.0, le=100.0, alias="TREND_RELEVANCE_THRESHOLD"
    )
    trend_default_region: str = Field(default="IN", alias="TREND_DEFAULT_REGION")
    trend_default_limit: int = Field(default=20, ge=1, le=100, alias="TREND_DEFAULT_LIMIT")
    trend_provider_timeout_seconds: float = Field(
        default=15.0, alias="TREND_PROVIDER_TIMEOUT_SECONDS"
    )
    # Google Trends has no worldwide feed, so GLOBAL is composed from these.
    trend_global_regions: str = Field(default="US,GB,IN", alias="TREND_GLOBAL_REGIONS")
    # Trends change slowly; re-fetching per request wastes the provider's
    # goodwill and the LLM budget. 0 disables caching entirely.
    trend_cache_ttl_seconds: int = Field(
        default=900, ge=0, alias="TREND_CACHE_TTL_SECONDS"
    )
    # Topics per LLM request. One call for a normal run, chunked if larger.
    trend_ai_batch_size: int = Field(default=25, ge=1, alias="TREND_AI_BATCH_SIZE")

    # --- Recommendation ----------------------------------------------------
    # How the feed is scored. Kept here so the ranking can be tuned against
    # real output without editing code. The three weights are relative, not
    # required to sum to 1 - they are normalised at use.
    rec_weight_interest: float = Field(default=0.45, ge=0.0, alias="REC_WEIGHT_INTEREST")
    rec_weight_importance: float = Field(default=0.30, ge=0.0, alias="REC_WEIGHT_IMPORTANCE")
    rec_weight_recency: float = Field(default=0.25, ge=0.0, alias="REC_WEIGHT_RECENCY")
    # How far revealed preference may move a score, in points.
    rec_behavior_influence: float = Field(default=20.0, ge=0.0, alias="REC_BEHAVIOR_INFLUENCE")
    # Points deducted per repeat of a topic already used in the feed.
    rec_diversity_penalty: float = Field(default=12.0, ge=0.0, alias="REC_DIVERSITY_PENALTY")
    # Score given to an article matching none of the user's topics. Low, not
    # zero: a feed that can only ever show stated interests never widens.
    rec_baseline_interest: float = Field(default=12.0, ge=0.0, le=100.0, alias="REC_BASELINE_INTEREST")
    # Hours after which recency has decayed to half.
    rec_recency_halflife_hours: float = Field(
        default=24.0, gt=0.0, alias="REC_RECENCY_HALFLIFE_HOURS"
    )
    feed_default_limit: int = Field(default=10, ge=1, le=100, alias="FEED_DEFAULT_LIMIT")
    feed_max_age_hours: int = Field(default=72, ge=1, alias="FEED_MAX_AGE_HOURS")

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

    @property
    def global_regions(self) -> list[str]:
        """The regions GLOBAL is composed from, parsed from the CSV setting."""
        return [
            part.strip().upper()
            for part in self.trend_global_regions.split(",")
            if part.strip()
        ]

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
            "database": self.database_url.split("@")[-1],   # never the password
            "timeout_seconds": self.openrouter_timeout_seconds,
            "trend_relevance_threshold": self.trend_relevance_threshold,
            "trend_cache_ttl_seconds": self.trend_cache_ttl_seconds,
            "trend_global_regions": self.global_regions,
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
