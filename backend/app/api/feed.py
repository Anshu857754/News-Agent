"""The personalised feed.

Lives under `/api/users/{id}/feed` rather than `/api/feed`, because a feed has
no meaning without the person it belongs to and the URL should say so.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.schemas import ErrorResponse, FeedArticle, FeedResponse
from app.services import user_service
from app.services.recommendation_service import get_recommendation_service
from app.services.user_service import UserNotFound

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["feed"])


@router.get(
    "/{user_id}/feed",
    response_model=FeedResponse,
    responses={404: {"model": ErrorResponse}},
    summary="A user's personalised feed",
)
def get_feed(
    user_id: int,
    limit: int = Query(default=None, ge=1, le=50),
    persist: bool = Query(default=True, description="Record the feed for later analysis."),
    session: Session = Depends(get_db),
) -> FeedResponse:
    """Score every recent article for this user and return the best few.

    An empty feed is a 200 with no articles, not an error: it means nothing has
    been ingested yet, which the caller fixes by running ingestion rather than
    by retrying this.
    """
    try:
        user = user_service.get_user(session, user_id)
    except UserNotFound:
        raise HTTPException(status_code=404, detail=f"No user with id {user_id}") from None

    chosen = get_recommendation_service().build_feed(session, user, limit, persist)
    if persist:
        session.commit()

    articles = [
        FeedArticle(
            id=item.article.id,
            title=item.article.title,
            url=item.article.url,
            source=item.article.source,
            summary=item.article.summary or "",
            topics=item.article.topics,
            entities=[e for e in (item.article.entities or "").split(", ") if e],
            published_at=item.article.published_at,
            importance_score=item.article.importance_score,
            score=round(item.score, 2),
            reason=item.reason,
            breakdown={
                "interest": round(item.interest, 2),
                "importance": round(item.importance, 2),
                "recency": round(item.recency, 2),
                "behavior": round(item.behavior, 2),
                "diversity_penalty": round(item.diversity_penalty, 2),
            },
        )
        for item in chosen
    ]

    return FeedResponse(
        user_id=user.id,
        total=len(articles),
        interests=user_service.to_user_out(user).interests,
        articles=articles,
    )
