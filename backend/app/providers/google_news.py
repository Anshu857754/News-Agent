"""Google News, via its public RSS search endpoint.

    https://news.google.com/rss/search?q=<query>&hl=en-IN&gl=IN&ceid=IN:en

Free, keyless, and parsed with the standard library - the same choices the
trends provider makes, for the same reason: no new dependency and nothing to
break when an unofficial client goes stale.

Two things this feed does that the pipeline downstream has to know about:

* **Links are Google redirects.** `news.google.com/rss/articles/CBM...` rather
  than the publisher's URL. They resolve in a browser, so they are kept as-is
  and flagged via `is_google_redirect()`. Resolving them means an HTTP request
  per article, which belongs in a later enrichment stage, not here.
* **Titles carry the outlet.** "Headline - The Verge". The publisher is also in
  a `<source>` element, so the suffix is stripped from the title rather than
  left to show up twice in a feed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx

from app.models.schemas import NewsArticle
from app.providers.news_base import BaseNewsProvider, NewsProviderError

log = logging.getLogger(__name__)

SEARCH_URL = (
    "https://news.google.com/rss/search"
    "?q={query}&hl={lang}&gl={geo}&ceid={geo}:{lang_short}"
)

# Editions worth naming. Anything else falls back to the US English edition,
# which still returns results rather than failing.
_EDITIONS: dict[str, tuple[str, str]] = {
    "IN": ("en-IN", "en"),
    "US": ("en-US", "en"),
    "GB": ("en-GB", "en"),
    "GLOBAL": ("en-US", "en"),
}

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str) -> str:
    """Descriptions arrive as a blob of anchor tags."""
    return " ".join(_TAG_RE.sub(" ", value or "").split())


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_google_redirect(url: str) -> bool:
    """True for links that point at Google rather than a publisher."""
    return "news.google.com" in (url or "")


def _clean_title(title: str, source: str) -> str:
    """Drop the " - Outlet" suffix when it just repeats the source element."""
    text = " ".join((title or "").split())
    if source and text.endswith(f" - {source}"):
        return text[: -len(f" - {source}")].strip()
    return text


class GoogleNewsProvider(BaseNewsProvider):
    """Keyword search against one Google News edition."""

    name = "google_news"
    is_metered = False

    def __init__(self, timeout_seconds: float = 15.0, url_template: str = SEARCH_URL):
        self.timeout_seconds = timeout_seconds
        self.url_template = url_template

    async def fetch(self, query: str, region: str, limit: int) -> list[NewsArticle]:
        text = " ".join(str(query or "").split())
        if not text:
            return []

        geo = (region or "US").upper()
        lang, lang_short = _EDITIONS.get(geo, _EDITIONS["US"])
        if geo == "GLOBAL":
            geo = "US"

        url = self.url_template.format(
            query=quote_plus(text), lang=lang, geo=geo, lang_short=lang_short
        )
        xml = await self._get(url, text)
        articles = self._parse(xml, text, geo, limit)

        log.info("google_news: %r (%s) -> %d articles", text, geo, len(articles))
        return articles

    # -- transport ----------------------------------------------------------
    async def _get(self, url: str, query: str) -> str:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "startuppulse-ai/0.1"},
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise NewsProviderError(
                f"Google News timed out after {self.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise NewsProviderError(f"Google News unreachable: {exc}") from exc

        if response.status_code == 429:
            raise NewsProviderError("Google News rate-limited this client (HTTP 429)")
        if response.status_code >= 400:
            raise NewsProviderError(
                f"Google News returned HTTP {response.status_code} for {query!r}"
            )
        return response.text

    # -- parsing ------------------------------------------------------------
    def _parse(self, xml: str, query: str, geo: str, limit: int) -> list[NewsArticle]:
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise NewsProviderError(f"Google News returned unparseable XML: {exc}") from exc

        articles: list[NewsArticle] = []
        for item in root.iter("item"):
            link = (item.findtext("link") or "").strip()
            raw_title = (item.findtext("title") or "").strip()
            if not link or not raw_title:
                continue

            source_el = item.find("source")
            source = (source_el.text or "").strip() if source_el is not None else ""

            articles.append(
                NewsArticle(
                    title=_clean_title(raw_title, source),
                    description=_strip_html(item.findtext("description") or ""),
                    url=link,
                    source=source or "unknown",
                    published_at=_parse_date(item.findtext("pubDate")),
                    topic=query,
                    provider=self.name,
                    region=geo,
                )
            )
            if len(articles) >= limit:
                break

        return articles
