"""The AI relevance stage.

Takes the topics that survived the rule-based pre-filter and asks the model, in
one request per batch, whether each is worth a startup newsletter's attention.

Two properties matter more than the prompt wording:

* **Batching.** Twenty separate calls to classify twenty topics is twenty times
  the cost and latency of one call that classifies twenty. Topics go up in
  chunks of `trend_ai_batch_size`.
* **It cannot take the pipeline down.** A missing key, a timeout, a rate limit,
  a reply that is not JSON, a reply that skips half the topics - each of those
  degrades to rule-based scoring for the affected topics. `classify_batch`
  returns a verdict for every topic it was given, always.

All LLM traffic goes through the Day 1 `OpenRouterService`. There is no second
client here, and there must never be one.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Optional

from app.models.schemas import TrendPriority, TrendRelevanceResult
from app.services.openrouter_service import (
    OpenRouterError,
    OpenRouterService,
    get_openrouter_service,
)
from app.services.trend_filter import match_category

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a startup ecosystem intelligence analyst.

You analyse trending search topics and decide whether each one is relevant to \
an AI-powered startup intelligence newsletter.

Evaluate relevance across: Startups, Technology, Artificial Intelligence, \
Venture Capital, Funding, SaaS, Fintech, Business Innovation, \
Entrepreneurship, Product Launches.

Rules:
- Judge ONLY the topic text you are given. Do not research it, and do not \
invent facts about it.
- If a topic is a person, a sports event, a film, a TV show or celebrity news, \
it is not relevant.
- relevance_score is an integer from 0 to 100.
- category must be a short, meaningful label (for example "Artificial \
Intelligence", "Fintech", "Entertainment").
- reason must be one short sentence.
- If you do not recognise a topic, say so in the reason and score it low \
rather than guessing.

Return JSON only. No prose, no markdown, no code fences."""

_USER_TEMPLATE = """Classify each of these trending topics.

Topics:
{topics}

Return a JSON object with exactly this shape:
{{
  "results": [
    {{
      "topic": "<the topic, copied exactly>",
      "relevant": true,
      "category": "Artificial Intelligence",
      "relevance_score": 95,
      "reason": "Short explanation."
    }}
  ]
}}

Return one entry per topic, in the same order."""

# What a rule-based verdict is worth when the model cannot be reached. HIGH
# clears the default threshold of 70; POSSIBLE deliberately does not - an
# unverified guess should not reach the newsletter on its own.
_FALLBACK_SCORES: dict[TrendPriority, float] = {
    TrendPriority.HIGH_PRIORITY: 75.0,
    TrendPriority.POSSIBLE: 40.0,
    TrendPriority.LOW_PRIORITY: 0.0,
}


def _chunk(items: list[str], size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start:start + size]


def _coerce_score(value: Any) -> float:
    """Clamp whatever the model sent into 0..100."""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score != score:  # NaN
        return 0.0
    return max(0.0, min(100.0, score))


def _coerce_bool(value: Any, *, score: float) -> bool:
    """Models answer with true/false, "true", 1, or nothing at all."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    # Not stated: infer from the score rather than discarding the verdict.
    return score >= 50.0


def rule_based_result(
    topic: str,
    priority: TrendPriority,
    category: Optional[str] = None,
    reason: Optional[str] = None,
) -> TrendRelevanceResult:
    """The verdict used when the model cannot answer for this topic."""
    score = _FALLBACK_SCORES.get(priority, 0.0)
    resolved = category or match_category(topic) or (
        "Technology" if priority is TrendPriority.HIGH_PRIORITY else "Uncategorised"
    )
    return TrendRelevanceResult(
        topic=topic,
        category=resolved,
        relevant=score >= 50.0,
        relevance_score=score,
        reason=reason or f"Scored by keyword rules ({priority.value}); AI unavailable.",
    )


def parse_results(payload: dict[str, Any], topics: list[str]) -> dict[str, TrendRelevanceResult]:
    """Turn one model reply into verdicts keyed by the original topic.

    Tolerant on purpose. The model is asked for `{"results": [...]}`, but a
    bare list, a differently named key, or entries missing fields are all
    handled - and any topic the reply simply omits is left out, for the caller
    to fill in from rules.
    """
    rows: Any = None
    if isinstance(payload, dict):
        for key in ("results", "trends", "topics", "data", "items"):
            if isinstance(payload.get(key), list):
                rows = payload[key]
                break
        if rows is None:
            # A single-object reply for a single-topic batch.
            if "topic" in payload or "relevance_score" in payload:
                rows = [payload]
    elif isinstance(payload, list):  # pragma: no cover - extract_json returns dicts
        rows = payload

    if not isinstance(rows, list):
        log.warning("AI relevance: no result list in reply (keys=%s)", list(payload)[:6])
        return {}

    # Case-insensitive lookup, so a model that changes capitalisation still matches.
    by_lower = {topic.lower(): topic for topic in topics}
    verdicts: dict[str, TrendRelevanceResult] = {}

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue

        raw_topic = str(row.get("topic") or "").strip()
        original = by_lower.get(raw_topic.lower())
        if original is None and index < len(topics):
            # The model dropped or rewrote the topic; fall back to position,
            # which the prompt asks it to preserve.
            original = topics[index]
        if original is None or original in verdicts:
            continue

        score = _coerce_score(row.get("relevance_score"))
        category = str(row.get("category") or "").strip() or "Uncategorised"
        reason = " ".join(str(row.get("reason") or "").split())[:300]

        verdicts[original] = TrendRelevanceResult(
            topic=original,
            category=category,
            relevant=_coerce_bool(row.get("relevant"), score=score),
            relevance_score=score,
            reason=reason or "No reason given by the model.",
        )

    return verdicts


class TrendRelevanceAnalyzer:
    """Classifies trend topics for startup relevance."""

    def __init__(self, openrouter: Optional[OpenRouterService] = None, batch_size: int = 25):
        self.openrouter = openrouter or get_openrouter_service()
        self.batch_size = max(1, batch_size)

    async def classify_batch(
        self, candidates: dict[str, tuple[TrendPriority, Optional[str]]]
    ) -> tuple[dict[str, TrendRelevanceResult], bool]:
        """Classify every candidate topic.

        `candidates` maps topic -> (rule priority, matched category).
        Returns `(verdicts, ai_used)`; there is a verdict for every input topic
        whether or not the model answered.
        """
        topics = list(candidates)
        if not topics:
            return {}, False

        if not self.openrouter.enabled:
            log.warning(
                "AI relevance skipped: OpenRouter is not configured; "
                "scoring %d topics by rules", len(topics),
            )
            return self._all_rule_based(candidates), False

        verdicts: dict[str, TrendRelevanceResult] = {}
        ai_used = False

        for batch in _chunk(topics, self.batch_size):
            try:
                payload = await self.openrouter.generate_structured_output(
                    SYSTEM_PROMPT,
                    _USER_TEMPLATE.format(
                        topics="\n".join(f"- {t}" for t in batch)
                    ),
                )
            except OpenRouterError as exc:
                # Unreachable, rate-limited, or unparseable: this batch falls
                # back to rules and the pipeline continues.
                log.warning(
                    "AI relevance failed for %d topics (%s); using rule-based scores",
                    len(batch), exc,
                )
                continue

            parsed = parse_results(payload, batch)
            if parsed:
                ai_used = True
            missing = [t for t in batch if t not in parsed]
            if missing:
                log.warning(
                    "AI relevance: model omitted %d of %d topics", len(missing), len(batch)
                )
            verdicts.update(parsed)

        # Anything the model never answered for is scored by rules.
        for topic, (priority, category) in candidates.items():
            if topic not in verdicts:
                verdicts[topic] = rule_based_result(topic, priority, category)

        log.info(
            "AI relevance: %d topics classified (ai_used=%s)", len(verdicts), ai_used
        )
        return verdicts, ai_used

    def _all_rule_based(
        self, candidates: dict[str, tuple[TrendPriority, Optional[str]]]
    ) -> dict[str, TrendRelevanceResult]:
        return {
            topic: rule_based_result(topic, priority, category)
            for topic, (priority, category) in candidates.items()
        }
