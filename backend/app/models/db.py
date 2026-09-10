"""The database schema.

The eight tables the architecture calls for. Two shapes here are worth
explaining, because they are what make personalisation possible later:

* **Topics are rows, not strings.** An article can carry several topics with
  different confidences, so `article_topics` is a real association table with
  its own payload rather than a comma-separated column. Flattening it would
  make "how strongly is this article about AI?" unanswerable.

* **Interests and behaviour are separate.** `user_interests` is what a person
  *says* they care about; `user_behavior` is what they actually opened. The
  recommender reads both, and conflating them would destroy the signal that
  makes the second one useful.

Nothing here uses a dialect-specific type, so the same models run on SQLite in
development and PostgreSQL in production.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------
class User(Base):
    """Someone who receives a feed."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    region: Mapped[str] = mapped_column(String(16), default="IN")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    interests: Mapped[list["UserInterest"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    behaviors: Mapped[list["UserBehavior"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    recommendations: Mapped[list["Recommendation"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Topic(Base):
    """A subject an article can be about and a user can care about.

    `slug` is the identity - it is what the LLM is constrained to return and
    what interests are keyed on, so "AI" and "ai" can never become two topics.
    """

    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    article_links: Mapped[list["ArticleTopic"]] = relationship(back_populates="topic")


class UserInterest(Base):
    """How much one user cares about one topic, 0-100.

    The same scale as trend relevance, deliberately: a score means the same
    thing everywhere in the system.
    """

    __tablename__ = "user_interests"
    __table_args__ = (UniqueConstraint("user_id", "topic_id", name="uq_user_topic"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    weight: Mapped[float] = mapped_column(Float, default=50.0)
    # Set by the person, or inferred from what they read.
    source: Mapped[str] = mapped_column(String(16), default="explicit")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    user: Mapped["User"] = relationship(back_populates="interests")
    topic: Mapped["Topic"] = relationship()


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------
class Article(Base):
    """One story from one outlet, after normalisation.

    `url_hash` carries the uniqueness rather than `url` itself: URLs run long
    and some databases refuse to index them at full length.
    """

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(120), default="unknown", index=True)
    provider: Mapped[str] = mapped_column(String(40), default="", index=True)
    region: Mapped[str] = mapped_column(String(16), default="", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Filled by the intelligence engine.
    importance_score: Mapped[float] = mapped_column(Float, default=0.0)
    entities: Mapped[str] = mapped_column(Text, default="")      # comma-separated
    summary: Mapped[str] = mapped_column(Text, default="")
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # What surfaced it, when the trend pipeline was the origin.
    matched_trend: Mapped[str] = mapped_column(String(200), default="")

    topic_links: Mapped[list["ArticleTopic"]] = relationship(
        back_populates="article", cascade="all, delete-orphan"
    )
    event_id: Mapped[int | None] = mapped_column(
        ForeignKey("events.id", ondelete="SET NULL"), index=True
    )
    event: Mapped["Event | None"] = relationship(back_populates="articles")

    __table_args__ = (Index("ix_articles_published_importance", "published_at", "importance_score"),)

    @property
    def topics(self) -> list[str]:
        return [link.topic.slug for link in self.topic_links if link.topic]


class ArticleTopic(Base):
    """An article is about a topic, with a confidence.

    A real table rather than a plain many-to-many, because the confidence is
    what lets the recommender weigh a passing mention differently from the
    subject of the piece.
    """

    __tablename__ = "article_topics"
    __table_args__ = (UniqueConstraint("article_id", "topic_id", name="uq_article_topic"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)
    topic_id: Mapped[int] = mapped_column(ForeignKey("topics.id", ondelete="CASCADE"), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)

    article: Mapped["Article"] = relationship(back_populates="topic_links")
    topic: Mapped["Topic"] = relationship(back_populates="article_links")


class Event(Base):
    """One real-world happening, covered by several articles.

    This is what stops a feed showing the same funding round five times from
    five outlets: articles cluster into an event, and the feed ranks events.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    article_count: Mapped[int] = mapped_column(Integer, default=0)
    importance_score: Mapped[float] = mapped_column(Float, default=0.0)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    articles: Mapped[list["Article"]] = relationship(back_populates="event")


# ---------------------------------------------------------------------------
# Personalisation
# ---------------------------------------------------------------------------
class Recommendation(Base):
    """One article placed in one user's feed, with the reason it scored.

    The component scores are stored, not just the total. Without them
    "why did I get this?" is unanswerable and the weights cannot be tuned
    against real output.
    """

    __tablename__ = "recommendations"
    __table_args__ = (UniqueConstraint("user_id", "article_id", name="uq_user_article"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id", ondelete="CASCADE"), index=True)

    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    interest_score: Mapped[float] = mapped_column(Float, default=0.0)
    importance_score: Mapped[float] = mapped_column(Float, default=0.0)
    recency_score: Mapped[float] = mapped_column(Float, default=0.0)
    diversity_penalty: Mapped[float] = mapped_column(Float, default=0.0)
    behavior_score: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped["User"] = relationship(back_populates="recommendations")
    article: Mapped["Article"] = relationship()


class UserBehavior(Base):
    """What a user actually did - viewed, opened, dismissed.

    Kept apart from `user_interests` on purpose: stated preference and revealed
    preference disagree often, and that disagreement is the signal.
    """

    __tablename__ = "user_behavior"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    article_id: Mapped[int | None] = mapped_column(
        ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(24), index=True)   # view | open | dismiss
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    user: Mapped["User"] = relationship(back_populates="behaviors")
    article: Mapped["Article | None"] = relationship()
