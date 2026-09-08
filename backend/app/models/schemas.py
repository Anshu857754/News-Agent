"""The data contract for the whole service.

Closed vocabularies wherever the value space is closed. The generation pipeline
will eventually be driven by an LLM, and validating its output against a fixed
set is the only thing that keeps free-form model replies predictable.

`region` and `category` are deliberately *open* strings. Unlike time ranges,
they are editorial choices that will grow ("Fintech", "SEA", "YC W26"), and
freezing them into an enum would cost a schema change per addition.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------
class NewsletterType(str, Enum):
    """Cadence of the issue being produced."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class TimeRange(str, Enum):
    """The look-back window a request asks for."""

    LAST_24H = "24h"
    LAST_7D = "7d"
    LAST_30D = "30d"

    @property
    def hours(self) -> int:
        """The window in hours, for the collection stage."""
        return {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}[self.value]


class GenerationStatus(str, Enum):
    """Where a generation request got to."""

    COMPLETED = "completed"
    FAILED = "failed"
    NOT_IMPLEMENTED = "not_implemented"


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------
class NewsArticle(BaseModel):
    """One article, normalised across every source.

    Every collector must return this shape, so nothing downstream - filtering,
    dedupe, ranking, the LLM stage - ever sees a source's raw format.
    """

    title: str
    description: Optional[str] = None
    source: str = Field(default="unknown", description="Publisher name.")
    url: str
    published_at: Optional[datetime] = None
    topic: Optional[str] = Field(
        default=None, description="Trending topic that surfaced this article."
    )
    relevance_score: float = Field(
        default=0.0, ge=0.0, le=1.0, description="Startup relevance, 0..1."
    )

    @field_validator("published_at")
    @classmethod
    def _force_utc(cls, value: Optional[datetime]) -> Optional[datetime]:
        """Sources mix naive and aware timestamps; everything is UTC internally."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_validator("title", "source", mode="before")
    @classmethod
    def _collapse_space(cls, value: Any) -> str:
        return " ".join(str(value or "").split())


# ---------------------------------------------------------------------------
# Newsletter
# ---------------------------------------------------------------------------
class NewsletterRequest(BaseModel):
    """Body of POST /api/newsletter/generate."""

    region: str = Field(
        default="Global",
        max_length=60,
        description="Geographic scope, e.g. 'Global', 'India', 'Europe'.",
    )
    category: str = Field(
        default="Startups",
        max_length=60,
        description="Editorial slice, e.g. 'Startups', 'AI', 'Fintech'.",
    )
    time_range: TimeRange = Field(default=TimeRange.LAST_24H)
    newsletter_type: NewsletterType = Field(default=NewsletterType.DAILY)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "region": "Global",
                    "category": "Startups",
                    "time_range": "24h",
                    "newsletter_type": "daily",
                }
            ]
        }
    }

    @field_validator("region", "category", mode="before")
    @classmethod
    def _clean(cls, value: Any) -> Any:
        """Collapse whitespace; reject a field that is only spaces."""
        if isinstance(value, str):
            cleaned = " ".join(value.split())
            if not cleaned:
                raise ValueError("must not be blank")
            return cleaned
        return value


class NewsletterResponse(BaseModel):
    """A generated issue."""

    id: str = Field(default_factory=lambda: uuid4().hex, description="Issue id.")
    title: str
    summary: str = Field(description="One-paragraph standfirst.")
    content: str = Field(description="The issue body, Markdown.")
    generated_at: datetime = Field(default_factory=utcnow)
    region: str
    category: str
    newsletter_type: NewsletterType = NewsletterType.DAILY
    sources_count: int = Field(default=0, ge=0, description="Distinct articles used.")


class GenerationResponse(BaseModel):
    """What /generate returns while the pipeline is still being built.

    The request is echoed back so a caller can confirm what was understood -
    the field that most often surprises people is `time_range`, which is
    validated against a closed set.
    """

    message: str
    status: GenerationStatus = GenerationStatus.NOT_IMPLEMENTED
    request: Optional[NewsletterRequest] = None
    newsletter: Optional[NewsletterResponse] = None


class HealthResponse(BaseModel):
    """Body of GET /api/newsletter/health."""

    status: str = "ok"
    service: str = "newsletter"


class ErrorResponse(BaseModel):
    """The single error shape every endpoint uses."""

    detail: str


# ---------------------------------------------------------------------------
# Trend discovery
# ---------------------------------------------------------------------------
GLOBAL_REGION = "GLOBAL"

# Two letters (an ISO country code) or the literal GLOBAL. Validated here so a
# bad region is a 422 from the schema rather than a confusing provider error.
_REGION_PATTERN = re.compile(r"^([A-Z]{2}|GLOBAL)$")


class TrendPriority(str, Enum):
    """What the cheap rule-based pre-filter decided about a topic.

    The point of this stage is cost: only HIGH_PRIORITY and POSSIBLE reach the
    LLM. LOW_PRIORITY is discarded before a single token is spent.
    """

    HIGH_PRIORITY = "HIGH_PRIORITY"
    POSSIBLE = "POSSIBLE"
    LOW_PRIORITY = "LOW_PRIORITY"


def normalize_region(value: str) -> str:
    """Uppercase and validate a region code. Raises ValueError if unusable."""
    cleaned = " ".join(str(value or "").split()).upper()
    if not _REGION_PATTERN.match(cleaned):
        raise ValueError(
            f"invalid region {value!r}: expected a two-letter country code "
            f"(for example IN, US, GB) or {GLOBAL_REGION}"
        )
    return cleaned


class TrendRequest(BaseModel):
    """Body of POST /api/trends/discover."""

    region: str = Field(default="IN", description="ISO country code, or GLOBAL.")
    limit: int = Field(default=20, ge=1, le=100, description="Max trends to collect.")

    model_config = {
        "json_schema_extra": {"examples": [{"region": "IN", "limit": 20}]}
    }

    @field_validator("region", mode="before")
    @classmethod
    def _normalize_region(cls, value: Any) -> Any:
        return normalize_region(value) if isinstance(value, str) else value


class TrendItem(BaseModel):
    """One trending topic, normalised across every provider.

    Fields the source does not supply stay `None`. Google Trends' RSS feed has
    no growth figure, so `growth` is always None for that provider - inventing
    one would make the whole object untrustworthy.
    """

    topic: str
    region: str
    source: str = Field(default="google_trends", description="Which provider found it.")
    trend_score: float = Field(
        default=0.0, ge=0.0,
        description="Provider's relative signal, 0 when the source gives none.",
    )
    search_volume: Optional[int] = Field(
        default=None, description="Approximate searches, when the source reports it."
    )
    growth: Optional[float] = Field(
        default=None, description="Growth rate. None unless the source provides it."
    )
    collected_at: datetime = Field(default_factory=utcnow)

    @field_validator("topic", mode="before")
    @classmethod
    def _collapse_space(cls, value: Any) -> str:
        return " ".join(str(value or "").split())


class TrendRelevanceResult(BaseModel):
    """One verdict from the AI relevance stage (or its rule-based fallback)."""

    topic: str
    category: str = Field(default="Uncategorised")
    relevant: bool = False
    relevance_score: float = Field(default=0.0, ge=0.0, le=100.0)
    reason: str = Field(default="")


class RelevantTrend(BaseModel):
    """A trend that survived filtering, scoring and the threshold."""

    topic: str
    category: str
    relevance_score: float = Field(ge=0.0, le=100.0)
    reason: str
    region: str
    source: str
    trend_score: float = 0.0
    search_volume: Optional[int] = None
    collected_at: datetime


class RawTrendsResponse(BaseModel):
    """Body of GET /api/trends - normalised trends, no AI involved."""

    region: str
    total: int
    trends: list[TrendItem] = Field(default_factory=list)


class TrendDiscoveryResponse(BaseModel):
    """Body of POST /api/trends/discover - the full pipeline's output."""

    total_trends_collected: int = 0
    total_relevant_trends: int = 0
    region: str
    trends: list[RelevantTrend] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)
    ai_used: bool = Field(
        default=False,
        description="False when the LLM was unavailable and rules decided instead.",
    )


class TrendHealthResponse(BaseModel):
    """Body of GET /api/trends/health."""

    status: str = "ok"
    service: str = "trend-discovery"
