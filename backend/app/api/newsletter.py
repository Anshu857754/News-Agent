"""Startup Intelligence Newsletter endpoints.

Thin by design: validate, delegate to `NewsletterService`, serialise. No
pipeline logic lives here, so the generation stages can be built and tested
without going through HTTP.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from app.models.schemas import (
    GenerationResponse,
    HealthResponse,
    NewsletterRequest,
)
from app.services.newsletter_service import NewsletterService, get_newsletter_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/newsletter", tags=["newsletter"])


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness check for the newsletter service",
)
def health() -> HealthResponse:
    """Fixed shape, no dependencies - safe to point a monitor at."""
    return HealthResponse(status="ok", service="newsletter")


@router.post(
    "/generate",
    response_model=GenerationResponse,
    summary="Generate a startup intelligence newsletter",
)
async def generate(
    request: NewsletterRequest,
    service: NewsletterService = Depends(get_newsletter_service),
) -> GenerationResponse:
    """Request an issue.

    The request body is fully validated and the foundation is in place, but the
    generation pipeline itself is not built yet: this returns
    `status="not_implemented"` rather than a fabricated newsletter.
    """
    return await service.generate(request)
