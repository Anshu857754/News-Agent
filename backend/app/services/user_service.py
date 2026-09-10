"""Users, their interest profiles, and what they actually read.

Every database write in this module goes through a `Session` handed in by the
caller, so the request decides the transaction boundary and nothing here opens
its own connection.

The one rule worth stating: topics are looked up by slug and created on first
use. That is what keeps "AI", "ai" and "Artificial Intelligence" from becoming
three unrelated rows that no recommender could reconcile.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.db import Topic, User, UserBehavior, UserInterest
from app.models.schemas import InterestIn, InterestOut, UserCreate, UserOut, slugify_topic

log = logging.getLogger(__name__)


class UserAlreadyExists(Exception):
    """That email is already registered."""


class UserNotFound(Exception):
    """No user with that id."""


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------
def get_or_create_topic(session: Session, name: str) -> Topic:
    """Find a topic by slug, creating it the first time it is seen."""
    slug = slugify_topic(name)
    topic = session.scalar(select(Topic).where(Topic.slug == slug))
    if topic is None:
        topic = Topic(slug=slug, label=" ".join(str(name).split()) or slug)
        session.add(topic)
        session.flush()
        log.info("topic created: %s", slug)
    return topic


def list_topics(session: Session) -> list[Topic]:
    return list(session.scalars(select(Topic).order_by(Topic.slug)))


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def create_user(session: Session, payload: UserCreate) -> User:
    existing = session.scalar(select(User).where(User.email == payload.email))
    if existing is not None:
        raise UserAlreadyExists(payload.email)

    user = User(
        email=payload.email,
        display_name=payload.display_name or payload.email.split("@")[0],
        region=payload.region,
    )
    session.add(user)
    session.flush()

    if payload.interests:
        set_interests(session, user, payload.interests)

    log.info("user created: id=%s region=%s", user.id, user.region)
    return user


def get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise UserNotFound(str(user_id))
    return user


def list_users(session: Session, limit: int = 50) -> list[User]:
    return list(session.scalars(select(User).order_by(User.id).limit(limit)))


# ---------------------------------------------------------------------------
# Interest profiles
# ---------------------------------------------------------------------------
def set_interests(
    session: Session, user: User, interests: Iterable[InterestIn], source: str = "explicit"
) -> list[UserInterest]:
    """Replace the user's profile with exactly these rows.

    A replace, not a merge: the profile is what the person last said it is, and
    a merge would make a removed topic impossible to express.
    """
    wanted: dict[int, float] = {}
    for entry in interests:
        topic = get_or_create_topic(session, entry.topic)
        # Last value wins if the same topic is listed twice.
        wanted[topic.id] = entry.weight

    current = {row.topic_id: row for row in user.interests}

    for topic_id, weight in wanted.items():
        row = current.get(topic_id)
        if row is None:
            session.add(UserInterest(
                user_id=user.id, topic_id=topic_id, weight=weight, source=source
            ))
        else:
            row.weight = weight
            row.source = source

    for topic_id, row in current.items():
        if topic_id not in wanted:
            session.delete(row)

    session.flush()
    session.refresh(user)
    log.info("interests set: user=%s topics=%d", user.id, len(wanted))
    return list(user.interests)


def interest_weights(session: Session, user_id: int) -> dict[str, float]:
    """`{topic_slug: weight}` — what the recommender reads."""
    rows = session.execute(
        select(Topic.slug, UserInterest.weight)
        .join(UserInterest, UserInterest.topic_id == Topic.id)
        .where(UserInterest.user_id == user_id)
    ).all()
    return {slug: weight for slug, weight in rows}


# ---------------------------------------------------------------------------
# Behaviour
# ---------------------------------------------------------------------------
def record_behavior(
    session: Session, user_id: int, action: str, article_id: Optional[int] = None
) -> UserBehavior:
    row = UserBehavior(user_id=user_id, article_id=article_id, action=action)
    session.add(row)
    session.flush()
    return row


def behavior_topic_bias(session: Session, user_id: int, limit: int = 200) -> dict[str, float]:
    """What the user's reading says about them, as a -1..1 nudge per topic.

    Opens count for, dismissals count against, a plain view barely registers.
    Returned as a bias rather than a score because it adjusts the stated
    profile - it does not replace it.
    """
    from app.models.db import Article, ArticleTopic

    weights = {"open": 1.0, "view": 0.15, "dismiss": -1.0}

    rows = session.execute(
        select(Topic.slug, UserBehavior.action)
        .join(Article, Article.id == UserBehavior.article_id)
        .join(ArticleTopic, ArticleTopic.article_id == Article.id)
        .join(Topic, Topic.id == ArticleTopic.topic_id)
        .where(UserBehavior.user_id == user_id)
        .order_by(UserBehavior.created_at.desc())
        .limit(limit)
    ).all()

    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for slug, action in rows:
        totals[slug] = totals.get(slug, 0.0) + weights.get(action, 0.0)
        counts[slug] = counts.get(slug, 0) + 1

    return {
        slug: max(-1.0, min(1.0, total / counts[slug]))
        for slug, total in totals.items()
        if counts[slug]
    }


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def to_user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        region=user.region,
        created_at=user.created_at,
        interests=[
            InterestOut(
                topic=row.topic.slug,
                label=row.topic.label,
                weight=row.weight,
                source=row.source,
            )
            for row in sorted(user.interests, key=lambda r: -r.weight)
            if row.topic
        ],
    )
