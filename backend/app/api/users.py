"""User and interest-profile endpoints.

Thin: validate, delegate to `user_service`, serialise. The session comes from
a dependency so one request is one transaction, committed here at the edge
rather than scattered through the service layer.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.schemas import (
    BehaviorIn,
    ErrorResponse,
    InterestUpdate,
    UserCreate,
    UserOut,
)
from app.services import user_service
from app.services.user_service import UserAlreadyExists, UserNotFound

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/users", tags=["users"])


def _load(session: Session, user_id: int):
    try:
        return user_service.get_user(session, user_id)
    except UserNotFound:
        raise HTTPException(status_code=404, detail=f"No user with id {user_id}") from None


@router.post(
    "",
    response_model=UserOut,
    status_code=201,
    responses={409: {"model": ErrorResponse}},
    summary="Create a user, optionally with an interest profile",
)
def create_user(payload: UserCreate, session: Session = Depends(get_db)) -> UserOut:
    try:
        user = user_service.create_user(session, payload)
    except UserAlreadyExists:
        raise HTTPException(
            status_code=409, detail="That email is already registered"
        ) from None
    session.commit()
    return user_service.to_user_out(user)


@router.get("", response_model=list[UserOut], summary="List users")
def list_users(limit: int = 50, session: Session = Depends(get_db)) -> list[UserOut]:
    return [user_service.to_user_out(u) for u in user_service.list_users(session, limit)]


@router.get(
    "/{user_id}",
    response_model=UserOut,
    responses={404: {"model": ErrorResponse}},
    summary="One user with their interest profile",
)
def get_user(user_id: int, session: Session = Depends(get_db)) -> UserOut:
    return user_service.to_user_out(_load(session, user_id))


@router.put(
    "/{user_id}/interests",
    response_model=UserOut,
    responses={404: {"model": ErrorResponse}},
    summary="Replace a user's interest profile",
)
def set_interests(
    user_id: int, payload: InterestUpdate, session: Session = Depends(get_db)
) -> UserOut:
    """The whole profile is replaced.

    A replace rather than a merge, so removing a topic is expressible - with a
    merge there would be no way to say "I no longer care about this".
    """
    user = _load(session, user_id)
    user_service.set_interests(session, user, payload.interests)
    session.commit()
    return user_service.to_user_out(user)


@router.post(
    "/{user_id}/behavior",
    status_code=204,
    responses={404: {"model": ErrorResponse}},
    summary="Record that a user viewed, opened or dismissed an article",
)
def record_behavior(
    user_id: int, payload: BehaviorIn, session: Session = Depends(get_db)
) -> None:
    """Feeds the recommender's revealed-preference signal.

    Returns no body: the caller is reporting, not asking for anything.
    """
    _load(session, user_id)
    user_service.record_behavior(session, user_id, payload.action, payload.article_id)
    session.commit()
