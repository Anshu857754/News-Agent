"""The recommendation engine.

Scores every candidate article for one user, then picks a feed:

    interest x importance x recency  ->  behaviour adjustment
                                     ->  diversity-aware selection

Four decisions worth stating, because they are what separate this from a
"sort by score" that reads badly:

* **Selection is greedy, not a sort.** Diversity depends on what has already
  been picked, so the penalty can only be applied while selecting. Sorting
  once and slicing would return five funding stories in a row.
* **One article per event.** The intelligence stage grouped coverage of one
  happening; showing three of them is showing the same news three times.
* **No match is a low score, not a zero.** An article on none of the user's
  topics still gets a baseline, so a feed can widen instead of narrowing onto
  the four topics someone typed once.
* **Component scores are stored.** `Recommendation` keeps interest, importance,
  recency, behaviour and the penalty separately, so "why am I seeing this?"
  has an answer and the weights can be tuned against real output.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.config import Settings, get_settings
from app.models.db import Article, Recommendation, User
from app.services.user_service import behavior_topic_bias, interest_weights

log = logging.getLogger(__name__)


@dataclass
class ScoredArticle:
    """One candidate with its score broken into the parts that produced it."""

    article: Article
    score: float
    interest: float
    importance: float
    recency: float
    behavior: float
    diversity_penalty: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "score": round(self.score, 2),
            "interest": round(self.interest, 2),
            "importance": round(self.importance, 2),
            "recency": round(self.recency, 2),
            "behavior": round(self.behavior, 2),
            "diversity_penalty": round(self.diversity_penalty, 2),
            "reason": self.reason,
        }


class RecommendationService:
    """Builds one user's personalised feed."""

    def __init__(self, settings: Optional[Settings] = None):
        self.settings = settings or get_settings()

    # -- components ---------------------------------------------------------
    def interest_score(self, topics: Sequence[str], weights: dict[str, float]) -> tuple[float, str]:
        """The strongest matching interest, and which topic it was.

        Strongest rather than average: an article about AI and cricket is
        interesting to someone who cares about either, and averaging would
        punish it for the topic they do not follow.
        """
        matches = [(weights[t], t) for t in topics if t in weights]
        if not matches:
            return self.settings.rec_baseline_interest, ""
        weight, topic = max(matches)
        return weight, topic

    def recency_score(self, published_at: Optional[datetime], now: datetime) -> float:
        """100 for brand new, halving every `rec_recency_halflife_hours`.

        Undated articles are treated as a day old rather than as new: a feed
        that rewards missing metadata teaches sources to omit it.
        """
        if published_at is None:
            age_hours = 24.0
        else:
            published = published_at
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            age_hours = max(0.0, (now - published).total_seconds() / 3600)

        halflife = self.settings.rec_recency_halflife_hours
        return 100.0 * math.pow(0.5, age_hours / halflife)

    def behavior_score(self, topics: Sequence[str], bias: dict[str, float]) -> float:
        """Revealed preference, in points, from -influence to +influence."""
        relevant = [bias[t] for t in topics if t in bias]
        if not relevant:
            return 0.0
        return (sum(relevant) / len(relevant)) * self.settings.rec_behavior_influence

    # -- candidates ---------------------------------------------------------
    def candidates(self, session: Session, limit: int = 400) -> list[Article]:
        """Recent analysed articles, newest first.

        Only analysed ones: an untagged article has no topics, so it could only
        ever score the baseline and would crowd out real matches.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.settings.feed_max_age_hours)
        return list(session.scalars(
            select(Article)
            .options(selectinload(Article.topic_links))
            .where(Article.analyzed_at.is_not(None))
            .where((Article.published_at.is_(None)) | (Article.published_at >= cutoff))
            .order_by(Article.published_at.desc().nullslast(), Article.id.desc())
            .limit(limit)
        ))

    # -- scoring ------------------------------------------------------------
    def score_all(
        self,
        articles: Sequence[Article],
        weights: dict[str, float],
        bias: dict[str, float],
        now: Optional[datetime] = None,
    ) -> list[ScoredArticle]:
        now = now or datetime.now(timezone.utc)
        s = self.settings
        total_weight = s.rec_weight_interest + s.rec_weight_importance + s.rec_weight_recency
        if total_weight <= 0:                       # a config mistake, not a crash
            total_weight = 1.0

        scored: list[ScoredArticle] = []
        for article in articles:
            topics = article.topics
            interest, matched = self.interest_score(topics, weights)
            importance = float(article.importance_score or 0.0)
            recency = self.recency_score(article.published_at, now)
            behavior = self.behavior_score(topics, bias)

            base = (
                s.rec_weight_interest * interest
                + s.rec_weight_importance * importance
                + s.rec_weight_recency * recency
            ) / total_weight

            scored.append(ScoredArticle(
                article=article,
                score=base + behavior,
                interest=interest,
                importance=importance,
                recency=recency,
                behavior=behavior,
                diversity_penalty=0.0,
                reason=self._reason(matched, interest, importance, recency),
            ))

        scored.sort(key=lambda s_: s_.score, reverse=True)
        return scored

    def _reason(self, matched: str, interest: float, importance: float, recency: float) -> str:
        """A sentence the user could actually be shown."""
        if matched:
            lead = f"Matches your interest in {matched} ({interest:.0f}/100)"
        else:
            lead = "Outside your stated interests"
        if importance >= 75:
            return f"{lead}; widely significant story."
        if recency >= 70:
            return f"{lead}; published in the last few hours."
        return f"{lead}."

    # -- selection ----------------------------------------------------------
    def select(self, scored: Sequence[ScoredArticle], limit: int) -> list[ScoredArticle]:
        """Greedy pick with a diversity penalty and one article per event."""
        penalty = self.settings.rec_diversity_penalty
        chosen: list[ScoredArticle] = []
        topic_counts: dict[str, int] = {}
        used_events: set[int] = set()
        remaining = list(scored)

        while remaining and len(chosen) < limit:
            best: Optional[ScoredArticle] = None
            best_adjusted = -math.inf
            best_penalty = 0.0

            for candidate in remaining:
                event_id = candidate.article.event_id
                if event_id is not None and event_id in used_events:
                    continue

                repeats = sum(topic_counts.get(t, 0) for t in candidate.article.topics)
                applied = repeats * penalty
                adjusted = candidate.score - applied

                if adjusted > best_adjusted:
                    best, best_adjusted, best_penalty = candidate, adjusted, applied

            if best is None:
                break

            best.diversity_penalty = best_penalty
            best.score = best_adjusted
            chosen.append(best)
            remaining.remove(best)

            for topic in best.article.topics:
                topic_counts[topic] = topic_counts.get(topic, 0) + 1
            if best.article.event_id is not None:
                used_events.add(best.article.event_id)

        return chosen

    # -- the entry point ----------------------------------------------------
    def build_feed(
        self, session: Session, user: User, limit: Optional[int] = None, persist: bool = True
    ) -> list[ScoredArticle]:
        """Score, select and (by default) record the feed for one user."""
        size = limit or self.settings.feed_default_limit
        weights = interest_weights(session, user.id)
        bias = behavior_topic_bias(session, user.id)

        pool = self.candidates(session)
        if not pool:
            log.warning("feed: no analysed articles available for user=%s", user.id)
            return []

        scored = self.score_all(pool, weights, bias)
        chosen = self.select(scored, size)

        if persist:
            self._persist(session, user, chosen)

        log.info(
            "feed built: user=%s candidates=%d selected=%d interests=%d",
            user.id, len(pool), len(chosen), len(weights),
        )
        return chosen

    def _persist(self, session: Session, user: User, chosen: Sequence[ScoredArticle]) -> None:
        """Record what was recommended, replacing the previous feed."""
        existing = {
            row.article_id: row
            for row in session.scalars(
                select(Recommendation).where(Recommendation.user_id == user.id)
            )
        }
        keep: set[int] = set()

        for item in chosen:
            keep.add(item.article.id)
            row = existing.get(item.article.id) or Recommendation(
                user_id=user.id, article_id=item.article.id
            )
            row.score = item.score
            row.interest_score = item.interest
            row.importance_score = item.importance
            row.recency_score = item.recency
            row.behavior_score = item.behavior
            row.diversity_penalty = item.diversity_penalty
            row.reason = item.reason
            if item.article.id not in existing:
                session.add(row)

        for article_id, row in existing.items():
            if article_id not in keep:
                session.delete(row)

        session.flush()


_default: Optional[RecommendationService] = None


def get_recommendation_service() -> RecommendationService:
    global _default
    if _default is None:
        _default = RecommendationService()
    return _default
