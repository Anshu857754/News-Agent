"""Newsletter orchestration.

This is where the Startup Intelligence pipeline will live. The API layer stays
thin and calls into here, so the stages are added in one place and are testable
without going through HTTP.

The planned pipeline:

    Google Trends
      -> Trending Topic Discovery
      -> Startup Relevance Filtering
      -> Dynamic News Search
      -> Google News / Apify Collection
      -> Article Processing
      -> Deduplication
      -> Story Ranking
      -> OpenRouter LLM Analysis
      -> AI Generated Startup Newsletter

Day 1 deliberately implements none of it. `generate()` validates the request,
logs it, and reports `not_implemented`. A convincing-looking fake issue would
be worse than nothing: it hides which stages are actually wired up.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional

from app.models.schemas import (
    GenerationResponse,
    GenerationStatus,
    NewsletterRequest,
)
from app.services.openrouter_service import OpenRouterService, get_openrouter_service

log = logging.getLogger(__name__)

_NOT_IMPLEMENTED_MESSAGE = "Newsletter generation pipeline foundation is ready"


class NewsletterService:
    """Owns the newsletter lifecycle. HTTP concerns stay in the API layer."""

    def __init__(self, openrouter: Optional[OpenRouterService] = None):
        # Injected rather than constructed, so tests can pass a fake.
        self.openrouter = openrouter or get_openrouter_service()

    def llm_status(self) -> dict[str, object]:
        """Whether the LLM leg is ready. Never includes the key."""
        return self.openrouter.describe()

    async def generate(self, request: NewsletterRequest) -> GenerationResponse:
        """Accept a generation request.

        Day 1: the request is validated by the schema before it arrives here,
        logged, and echoed back with `not_implemented`. This signature is
        already the one the real pipeline will keep, so wiring the stages in
        later does not change the API contract.
        """
        log.info(
            "newsletter requested: region=%s category=%s time_range=%s type=%s",
            request.region,
            request.category,
            request.time_range.value,
            request.newsletter_type.value,
        )

        if not self.openrouter.enabled:
            # Not an error on Day 1 - nothing calls the model yet - but the one
            # thing worth knowing before the pipeline is built.
            log.warning(
                "OPENROUTER_API_KEY is not configured; generation will not "
                "work once the pipeline is wired up"
            )

        return GenerationResponse(
            message=_NOT_IMPLEMENTED_MESSAGE,
            status=GenerationStatus.NOT_IMPLEMENTED,
            request=request,
        )


@lru_cache(maxsize=1)
def get_newsletter_service() -> NewsletterService:
    """The process-wide instance. Used as a FastAPI dependency."""
    return NewsletterService()
