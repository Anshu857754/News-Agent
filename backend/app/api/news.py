"""Ingestion endpoints.

Runs the pipeline and reports counts. Thin, like every router here: the work
lives in `ingestion_service` and `intelligence_service`, which are testable
without HTTP.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.schemas import ErrorResponse, IngestRequest, IngestResponse
from app.services import user_service
from app.services.ingestion_service import get_ingestion_service
from app.services.intelligence_service import get_intelligence_service
from app.services.user_service import UserNotFound

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])


@router.post(
    "/ingest",
    response_model=IngestResponse,
    responses={404: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
    summary="Collect, clean and store news, then analyse it",
)
async def ingest(
    request: IngestRequest, session: Session = Depends(get_db)
) -> IngestResponse:
    """Run the ingestion pipeline.

    Queries come from the body, or from a user's interest profile when
    `user_id` is given - which is how a personalised feed gets material that is
    actually about the things that user follows.
    """
    queries = list(request.queries)

    if request.user_id is not None:
        try:
            user = user_service.get_user(session, request.user_id)
        except UserNotFound:
            raise HTTPException(
                status_code=404, detail=f"No user with id {request.user_id}"
            ) from None
        if not queries:
            # Strongest interests first, so a small run still covers what
            # matters most to this person.
            queries = [
                row.topic.label
                for row in sorted(user.interests, key=lambda r: -r.weight)
                if row.topic
            ][:8]

    if not queries:
        raise HTTPException(
            status_code=422,
            detail="Provide queries, or a user_id whose interest profile is not empty",
        )

    result, rows = await get_ingestion_service().ingest(
        session, queries, request.region, request.limit_per_query
    )

    analysis = None
    if request.analyze and rows:
        engine = get_intelligence_service()
        outcome = await engine.analyze(session, rows)
        engine.cluster_events(session, rows, outcome)
        analysis = outcome.as_dict()

    session.commit()

    return IngestResponse(**result.as_dict(), analysis=analysis)
