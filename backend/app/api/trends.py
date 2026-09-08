"""Trend discovery endpoints.

Thin by design: validate, delegate to `TrendService`, serialise. The only real
work here is turning provider failures into the right status code - a source
being down is a 503, a bad region is a 422, and neither ever reaches the client
as a stack trace.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.config import Settings, get_settings
from app.models.schemas import (
    ErrorResponse,
    RawTrendsResponse,
    TrendDiscoveryResponse,
    TrendHealthResponse,
    TrendRequest,
    normalize_region,
)
from app.providers.base import TrendProviderError
from app.services.trend_service import TrendService, get_trend_service

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trends", tags=["trends"])

_UPSTREAM_UNAVAILABLE = 503


def _validated_region(raw: str) -> str:
    """Normalise a region or raise a 422. Never leaks provider details."""
    try:
        return normalize_region(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/health",
    response_model=TrendHealthResponse,
    summary="Liveness check for the trend discovery service",
)
def health() -> TrendHealthResponse:
    """Fixed shape, no dependencies - safe to point a monitor at."""
    return TrendHealthResponse(status="ok", service="trend-discovery")


@router.get(
    "",
    response_model=RawTrendsResponse,
    responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    summary="Raw normalised trending topics, without AI analysis",
)
async def list_trends(
    region: str = Query(default=None, description="ISO country code, or GLOBAL."),
    limit: int = Query(default=None, ge=1, le=100),
    service: TrendService = Depends(get_trend_service),
    settings: Settings = Depends(get_settings),
) -> RawTrendsResponse:
    """Collect trends and return them unfiltered.

    No LLM is involved, so this costs nothing and is the endpoint to use when
    checking whether the provider itself is healthy.
    """
    resolved_region = _validated_region(region or settings.trend_default_region)
    resolved_limit = limit or settings.trend_default_limit

    try:
        trends = await service.collect_trends(resolved_region, resolved_limit)
    except TrendProviderError as exc:
        log.warning("trend provider unavailable for region=%s: %s", resolved_region, exc)
        raise HTTPException(
            status_code=_UPSTREAM_UNAVAILABLE,
            detail=f"Trend source unavailable: {exc}",
        ) from exc

    return RawTrendsResponse(
        region=resolved_region, total=len(trends), trends=trends
    )


@router.post(
    "/discover",
    response_model=TrendDiscoveryResponse,
    responses={422: {"model": ErrorResponse}, 503: {"model": ErrorResponse}},
    summary="Run the full trend discovery pipeline",
)
async def discover(
    request: TrendRequest,
    service: TrendService = Depends(get_trend_service),
) -> TrendDiscoveryResponse:
    """Collect, filter, classify, score and rank.

    An empty result is a 200 with zero trends, not an error: "nothing today was
    startup-relevant" is a valid answer. Only the source being unreachable is a
    failure. The AI stage never fails the request - it degrades to rule-based
    scoring, which the `ai_used` flag reports.
    """
    try:
        return await service.discover(request.region, request.limit)
    except TrendProviderError as exc:
        log.warning("trend discovery failed for region=%s: %s", request.region, exc)
        raise HTTPException(
            status_code=_UPSTREAM_UNAVAILABLE,
            detail=f"Trend source unavailable: {exc}",
        ) from exc
