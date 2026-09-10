"""Newsletter generation.

The last stage of the pipeline: a user's personalised feed, written up as an
issue by the LLM.

    feed -> per-story summaries + editorial intro -> Markdown

Two rules hold this together:

* **A newsletter is written for someone.** Without a `user_id` there is no
  interest profile to write against, so the endpoint keeps its original
  `not_implemented` answer instead of inventing a generic issue nobody asked
  for.
* **The model writes prose, not facts.** Every headline, link and source in the
  output comes from the database. The LLM is given the stories and asked to
  introduce and summarise them; it is never asked what the news is.

If the model is unreachable the issue is still produced - assembled from the
stored fields, with `ai_used` false - because a plain digest beats no digest.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Optional, Sequence

from sqlalchemy.orm import Session

from app.models.db import User
from app.models.schemas import (
    GenerationResponse,
    GenerationStatus,
    NewsletterRequest,
    NewsletterResponse,
)
from app.services.openrouter_service import (
    OpenRouterError,
    OpenRouterService,
    get_openrouter_service,
)
from app.services.recommendation_service import ScoredArticle, get_recommendation_service
from app.services.user_service import interest_weights

log = logging.getLogger(__name__)

_NOT_IMPLEMENTED_MESSAGE = (
    "Newsletter generation needs a user_id: an issue is written for one "
    "reader's interest profile"
)

SYSTEM_PROMPT = """You are the editor of a personalised intelligence briefing.

You will be given a reader's interests and the stories selected for them.

Write:
  intro     2-3 sentences introducing today's issue for this reader. Refer to
            what is actually in the stories. No greetings, no filler, no
            "in today's newsletter".
  stories   for each story, one or two sentences saying what happened and why
            it matters to someone with these interests.

Rules:
- Use ONLY the titles and descriptions provided. Do not add facts, figures,
  company names or outcomes that are not there.
- If a story is thin, say less. Never pad.
- Plain, specific language. No hype, no adjectives of praise.

Return JSON only. No prose, no markdown, no code fences."""

_USER_TEMPLATE = """Reader interests (0-100): {interests}

Stories:
{stories}

Return a JSON object with exactly this shape:
{{
  "title": "A short issue title",
  "intro": "Two or three sentences.",
  "stories": [
    {{"id": <the id given above>, "summary": "One or two sentences."}}
  ]
}}"""


def _story_lines(items: Sequence[ScoredArticle]) -> str:
    lines = []
    for item in items:
        article = item.article
        line = f'- id {article.id}: "{article.title}" ({article.source})'
        blurb = article.summary or article.description
        if blurb:
            line += f" — {blurb[:200]}"
        lines.append(line)
    return "\n".join(lines)


def _parse_issue(payload: dict, items: Sequence[ScoredArticle]) -> tuple[str, str, dict[int, str]]:
    """Model reply -> `(title, intro, {article_id: summary})`."""
    title = " ".join(str(payload.get("title") or "").split())[:160]
    intro = " ".join(str(payload.get("intro") or "").split())[:1200]

    summaries: dict[int, str] = {}
    rows: Any = payload.get("stories")
    if isinstance(rows, list):
        by_id = {i.article.id for i in items}
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            article_id = row.get("id")
            if article_id not in by_id:
                article_id = items[index].article.id if index < len(items) else None
            if article_id is None:
                continue
            text = " ".join(str(row.get("summary") or "").split())[:600]
            if text:
                summaries[article_id] = text

    return title, intro, summaries


def _render_markdown(
    intro: str, items: Sequence[ScoredArticle], summaries: dict[int, str]
) -> str:
    """Assemble the issue body. Links and sources come from the database."""
    parts: list[str] = []
    if intro:
        parts.append(intro)
        parts.append("")

    for position, item in enumerate(items, 1):
        article = item.article
        parts.append(f"## {position}. {article.title}")
        body = summaries.get(article.id) or article.summary or article.description
        if body:
            parts.append(body)
        meta = [article.source]
        if article.published_at:
            meta.append(article.published_at.strftime("%d %b %Y"))
        if article.topics:
            meta.append(", ".join(article.topics))
        parts.append(f"*{' · '.join(m for m in meta if m)}* — [read]({article.url})")
        parts.append("")

    return "\n".join(parts).strip()


class NewsletterService:
    """Turns a user's feed into a written issue."""

    def __init__(self, openrouter: Optional[OpenRouterService] = None):
        self.openrouter = openrouter or get_openrouter_service()

    def llm_status(self) -> dict[str, object]:
        """Whether the LLM leg is ready. Never includes the key."""
        return self.openrouter.describe()

    async def generate(
        self,
        request: NewsletterRequest,
        session: Optional[Session] = None,
        user: Optional[User] = None,
    ) -> GenerationResponse:
        """Produce an issue for `user`, or report that one is needed."""
        log.info(
            "newsletter requested: user=%s region=%s category=%s time_range=%s type=%s",
            request.user_id, request.region, request.category,
            request.time_range.value, request.newsletter_type.value,
        )

        if user is None or session is None:
            return GenerationResponse(
                message=_NOT_IMPLEMENTED_MESSAGE,
                status=GenerationStatus.NOT_IMPLEMENTED,
                request=request,
            )

        items = get_recommendation_service().build_feed(
            session, user, limit=request.limit, persist=False
        )
        if not items:
            return GenerationResponse(
                message=(
                    "No analysed articles are available yet. "
                    "Run POST /api/news/ingest first."
                ),
                status=GenerationStatus.FAILED,
                request=request,
            )

        title, intro, summaries, ai_used = await self._write(session, user, items)

        newsletter = NewsletterResponse(
            title=title,
            summary=intro or f"{len(items)} stories selected for {user.display_name}.",
            content=_render_markdown(intro, items, summaries),
            region=request.region,
            category=request.category,
            newsletter_type=request.newsletter_type,
            sources_count=len({i.article.source for i in items}),
        )

        log.info(
            "newsletter generated: user=%s stories=%d sources=%d ai_used=%s",
            user.id, len(items), newsletter.sources_count, ai_used,
        )
        return GenerationResponse(
            message=(
                "Newsletter generated"
                if ai_used
                else "Newsletter assembled without the LLM (OpenRouter unavailable)"
            ),
            status=GenerationStatus.COMPLETED,
            request=request,
            newsletter=newsletter,
        )

    async def _write(
        self, session: Session, user: User, items: Sequence[ScoredArticle]
    ) -> tuple[str, str, dict[int, str], bool]:
        """Ask the model for a title, intro and per-story summaries."""
        fallback_title = (
            f"{user.display_name or 'Your'} briefing — "
            f"{datetime.now(timezone.utc).strftime('%d %b %Y')}"
        )

        if not self.openrouter.enabled:
            log.warning("newsletter: OpenRouter not configured; assembling without it")
            return fallback_title, "", {}, False

        weights = interest_weights(session, user.id)
        interests = ", ".join(f"{k} {v:.0f}" for k, v in
                              sorted(weights.items(), key=lambda kv: -kv[1])) or "none stated"

        try:
            payload = await self.openrouter.generate_structured_output(
                SYSTEM_PROMPT,
                _USER_TEMPLATE.format(interests=interests, stories=_story_lines(items)),
            )
        except OpenRouterError as exc:
            log.warning("newsletter: LLM failed (%s); assembling without it", exc)
            return fallback_title, "", {}, False

        title, intro, summaries = _parse_issue(payload, items)
        return title or fallback_title, intro, summaries, True


@lru_cache(maxsize=1)
def get_newsletter_service() -> NewsletterService:
    """The process-wide instance. Used as a FastAPI dependency."""
    return NewsletterService()
