"""The news intelligence engine.

Turns stored articles into something a recommender can reason about:

    topics (multi-label)  ->  entities  ->  importance  ->  events

One batched LLM call does the first three, because they are the same reading of
the same headline - asking three times would cost three times as much and could
return three inconsistent views of one article.

Event clustering is deliberately *not* the LLM's job. Grouping N articles is an
O(N^2) comparison that rules do exactly as well and for nothing, and it is the
stage that makes a feed stop showing one funding round five times.

Everything degrades. If the model is unreachable, rule-based tagging and
scoring run instead and the articles still come out analysed - just less
precisely. `ai_used` reports which happened.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.db import Article, ArticleTopic, Event, Topic, utcnow
from app.services.ingestion_service import title_similarity, title_tokens
from app.services.openrouter_service import (
    OpenRouterError,
    OpenRouterService,
    get_openrouter_service,
)
from app.services.trend_filter import match_category
from app.services.user_service import get_or_create_topic

log = logging.getLogger(__name__)

# Above this headline overlap, two articles cover the same event. Higher than
# the ingestion threshold on purpose: ingestion is removing near-identical
# copies, while this is grouping genuinely different write-ups of one thing,
# and a wrong grouping hides a story rather than merely duplicating one.
EVENT_SIMILARITY_THRESHOLD = 0.55

# Articles per LLM request.
DEFAULT_BATCH_SIZE = 12

SYSTEM_PROMPT = """You are a news intelligence analyst.

For each article you are given, return:
  topics      1-3 short lowercase subject slugs, e.g. "ai", "fintech",
              "funding", "cricket", "e-commerce", "policy". Use the most
              specific ones that genuinely apply. Do not invent a topic that
              the headline does not support.
  entities    up to 5 named organisations, people or products mentioned.
              Copy names as written. Return [] if none are named.
  importance  0-100. How much this matters to a well-informed reader.
              A national policy change or a large funding round scores high;
              a routine product update or a listicle scores low.
  summary     one short factual sentence. No opinion, no adjectives of praise.

Judge only the text provided. Do not research, and do not state facts that are
not in the headline or description.

Return JSON only. No prose, no markdown, no code fences."""

_USER_TEMPLATE = """Analyse these articles.

{articles}

Return a JSON object with exactly this shape:
{{
  "results": [
    {{
      "id": <the id given above>,
      "topics": ["ai", "funding"],
      "entities": ["OpenAI"],
      "importance": 85,
      "summary": "One factual sentence."
    }}
  ]
}}

Return one entry per article, in the same order."""


@dataclass
class AnalysisResult:
    analyzed: int = 0
    ai_used: bool = False
    topics_assigned: int = 0
    events_created: int = 0
    events_updated: int = 0
    failures: int = 0
    topic_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "analyzed": self.analyzed,
            "ai_used": self.ai_used,
            "topics_assigned": self.topics_assigned,
            "events_created": self.events_created,
            "events_updated": self.events_updated,
            "failures": self.failures,
            "topic_counts": self.topic_counts,
        }


# ---------------------------------------------------------------------------
# Rule-based fallback
# ---------------------------------------------------------------------------
# The keyword groups already used for trend relevance, reused as topic slugs so
# a topic means the same thing whether it came from a trend or an article.
_CATEGORY_SLUGS = {
    "Artificial Intelligence": "ai",
    "Funding": "funding",
    "Startups": "startups",
    "Technology": "technology",
    "SaaS": "saas",
    "Fintech": "fintech",
    "Business": "business",
    "Sector Tech": "deeptech",
}

_HIGH_SIGNAL = {
    "acquires", "acquisition", "merger", "ipo", "raises", "funding", "layoffs",
    "ban", "banned", "ruling", "regulation", "policy", "launch", "launches",
    "record", "resigns", "shutdown", "breach", "outage",
}


def rule_topics(article: Article) -> list[str]:
    """Topic slugs from keyword matching.

    Single-label, unlike the model's output: keyword rules can only say which
    group matched first, and pretending otherwise would put false confidence
    into `article_topics`. "general" is the honest answer when nothing matched.
    """
    category = match_category(f"{article.title} {article.description}")
    if category is None:
        return ["general"]
    return [_CATEGORY_SLUGS.get(category, "general")]


def rule_importance(article: Article) -> float:
    """A defensible 0-100 without a model.

    Two signals only, both actually present in the row: whether the headline
    contains a word that marks a real event, and how fresh it is. No source
    authority table - inventing one would be a guess dressed as data.
    """
    tokens = title_tokens(article.title)
    signal = len(tokens & _HIGH_SIGNAL)
    score = 40.0 + min(signal, 3) * 12.0

    published = article.published_at
    if published is not None:
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600
        if age_hours <= 12:
            score += 12.0
        elif age_hours <= 36:
            score += 6.0
        elif age_hours > 168:
            score -= 10.0

    return max(0.0, min(100.0, score))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _clean_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = " ".join(str(item).split())
        if text and text.lower() not in {t.lower() for t in out}:
            out.append(text[:80])
        if len(out) >= limit:
            break
    return out


def _clean_score(value: Any) -> Optional[float]:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score != score:
        return None
    return max(0.0, min(100.0, score))


def parse_analysis(payload: dict, batch: Sequence[Article]) -> dict[int, dict]:
    """Model reply -> `{article_id: fields}`. Tolerant of shape drift."""
    rows: Any = None
    if isinstance(payload, dict):
        for key in ("results", "articles", "data", "items"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
    if not isinstance(rows, list):
        log.warning("intelligence: no result list in reply (keys=%s)", list(payload)[:6])
        return {}

    by_id = {a.id: a for a in batch}
    out: dict[int, dict] = {}

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue

        article_id = row.get("id")
        if article_id not in by_id:
            # The model dropped or rewrote the id; fall back to position, which
            # the prompt asks it to preserve.
            article_id = batch[index].id if index < len(batch) else None
        if article_id is None or article_id in out:
            continue

        out[article_id] = {
            "topics": [t.lower() for t in _clean_list(row.get("topics"), 3)],
            "entities": _clean_list(row.get("entities"), 5),
            "importance": _clean_score(row.get("importance")),
            "summary": " ".join(str(row.get("summary") or "").split())[:400],
        }
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class IntelligenceService:
    """Tags, scores and clusters stored articles."""

    def __init__(
        self,
        openrouter: Optional[OpenRouterService] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ):
        self.openrouter = openrouter or get_openrouter_service()
        self.batch_size = max(1, batch_size)

    # -- LLM ---------------------------------------------------------------
    async def _analyze_batch(self, batch: Sequence[Article]) -> dict[int, dict]:
        listing = "\n".join(
            f'- id {a.id}: "{a.title}"' + (f" — {a.description[:180]}" if a.description else "")
            for a in batch
        )
        payload = await self.openrouter.generate_structured_output(
            SYSTEM_PROMPT, _USER_TEMPLATE.format(articles=listing)
        )
        return parse_analysis(payload, batch)

    # -- persistence -------------------------------------------------------
    def _assign_topics(
        self, session: Session, article: Article, slugs: Iterable[str], result: AnalysisResult
    ) -> None:
        """Attach these topics to the article.

        Appended to the relationship rather than inserted as bare rows: adding
        an `ArticleTopic` with `session.add` writes the table but leaves
        `article.topic_links` stale in memory, so `article.topics` would come
        back empty for the rest of the request - which is exactly when the
        recommender reads it.
        """
        existing = {link.topic_id for link in article.topic_links}
        for slug in slugs:
            try:
                topic = get_or_create_topic(session, slug)
            except ValueError:
                continue                      # blank slug from the model
            if topic.id in existing:
                continue
            article.topic_links.append(ArticleTopic(topic_id=topic.id, confidence=1.0))
            existing.add(topic.id)
            result.topics_assigned += 1
            result.topic_counts[topic.slug] = result.topic_counts.get(topic.slug, 0) + 1

    # -- the entry point ---------------------------------------------------
    async def analyze(
        self, session: Session, articles: Sequence[Article]
    ) -> AnalysisResult:
        """Tag, score and summarise. Falls back to rules per batch."""
        result = AnalysisResult()
        pending = [a for a in articles if a is not None]
        if not pending:
            return result

        log.info("intelligence: analysing %d articles", len(pending))

        analyses: dict[int, dict] = {}
        if self.openrouter.enabled:
            for start in range(0, len(pending), self.batch_size):
                batch = pending[start:start + self.batch_size]
                try:
                    parsed = await self._analyze_batch(batch)
                except OpenRouterError as exc:
                    log.warning(
                        "intelligence: LLM failed for %d articles (%s); using rules",
                        len(batch), exc,
                    )
                    result.failures += 1
                    continue
                if parsed:
                    result.ai_used = True
                analyses.update(parsed)
        else:
            log.warning("intelligence: OpenRouter not configured; rules only")

        for article in pending:
            found = analyses.get(article.id) or {}

            slugs = found.get("topics") or rule_topics(article)
            self._assign_topics(session, article, slugs, result)

            importance = found.get("importance")
            article.importance_score = (
                importance if importance is not None else rule_importance(article)
            )
            article.entities = ", ".join(found.get("entities") or [])
            article.summary = found.get("summary") or ""
            article.analyzed_at = utcnow()
            result.analyzed += 1

        session.flush()
        log.info(
            "intelligence: %d analysed, %d topic links, ai_used=%s",
            result.analyzed, result.topics_assigned, result.ai_used,
        )
        return result

    # -- event clustering --------------------------------------------------
    def cluster_events(
        self, session: Session, articles: Sequence[Article], result: AnalysisResult
    ) -> None:
        """Group articles covering one happening into an `Event`.

        Compared against events already in the database as well as within this
        batch, so coverage arriving hours apart still lands on one event.
        """
        recent = list(session.scalars(
            select(Event).order_by(Event.last_seen_at.desc()).limit(200)
        ))
        # Event title -> Event, for cheap comparison.
        candidates: list[tuple[str, Event]] = [(e.title, e) for e in recent]

        for article in articles:
            if article.event_id is not None:
                continue

            match: Optional[Event] = None
            for title, event in candidates:
                if title_similarity(article.title, title) >= EVENT_SIMILARITY_THRESHOLD:
                    match = event
                    break

            if match is None:
                key = hashlib.sha256(
                    " ".join(sorted(title_tokens(article.title))).encode("utf-8")
                ).hexdigest()[:64]
                match = session.scalar(select(Event).where(Event.key == key))
                if match is None:
                    match = Event(
                        key=key,
                        title=article.title,
                        summary=article.summary or "",
                        article_count=0,
                        importance_score=article.importance_score,
                    )
                    session.add(match)
                    session.flush()
                    result.events_created += 1
                    candidates.append((match.title, match))
            else:
                result.events_updated += 1

            article.event_id = match.id
            match.article_count += 1
            match.last_seen_at = utcnow()
            # An event is as important as its strongest article: five outlets
            # covering one thing is itself the signal.
            match.importance_score = max(match.importance_score, article.importance_score)

        session.flush()
        log.info(
            "intelligence: %d events created, %d joined",
            result.events_created, result.events_updated,
        )


_default: Optional[IntelligenceService] = None


def get_intelligence_service() -> IntelligenceService:
    global _default
    if _default is None:
        _default = IntelligenceService()
    return _default
