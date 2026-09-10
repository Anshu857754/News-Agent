"""Startup Intelligence Newsletter endpoints.

Thin by design: validate, delegate to `NewsletterService`, serialise. No
pipeline logic lives here, so generation can be built and tested without going
through HTTP.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.schemas import (
    ErrorResponse,
    GenerationResponse,
    HealthResponse,
    NewsletterRequest,
)
from app.services import user_service
from app.services.newsletter_service import NewsletterService, get_newsletter_service
from app.services.user_service import UserNotFound

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
    responses={404: {"model": ErrorResponse}},
    summary="Generate a personalised newsletter",
)
async def generate(
    request: NewsletterRequest,
    service: NewsletterService = Depends(get_newsletter_service),
    session: Session = Depends(get_db),
) -> GenerationResponse:
    """Write an issue for one reader.

    With a `user_id`, this builds that user's feed and writes it up. Without
    one there is no interest profile to write against, so it reports
    `not_implemented` rather than inventing a generic issue.
    """
    user = None
    if request.user_id is not None:
        try:
            user = user_service.get_user(session, request.user_id)
        except UserNotFound:
            raise HTTPException(
                status_code=404, detail=f"No user with id {request.user_id}"
            ) from None

    return await service.generate(request, session=session, user=user)
