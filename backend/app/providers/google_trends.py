"""Google Trends, via the public daily trending-searches RSS feed.

    https://trends.google.com/trending/rss?geo=IN

Why this endpoint and not something else:

* **No key, no quota, no browser.** It is a plain XML document over HTTPS.
* **No new dependency.** `httpx` is already used by the project and the parsing
  is stdlib `xml.etree`. `pytrends` was rejected deliberately: it is an
  unofficial client for a private API, and it breaks and rate-limits often.
* **It is honest about what it knows.** The feed carries an approximate traffic
  figure and a timestamp, and nothing else. Fields it does not supply - growth
  rate above all - stay `None` here rather than being invented.

The feed is per-country, so there is no worldwide edition. `GLOBAL` is composed
by the service layer from several country feeds; this provider only ever speaks
to one country at a time.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from xml.etree import ElementTree

import httpx

from app.models.schemas import TrendItem
from app.providers.base import BaseTrendProvider, TrendProviderError

log = logging.getLogger(__name__)

TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"

# The feed's own namespace, where every non-standard field lives.
_HT_NAMESPACE = {"ht": "https://trends.google.com/trending/rss"}

# "20,000+" -> 20000. Anything without digits has no usable figure.
_DIGITS = re.compile(r"[\d,]+")

# Traffic figures are the only volume signal the feed gives. This is the value
# a topic's score is normalised against, so a 200k-search topic scores 1.0 and
# everything below scales under it.
_VOLUME_CEILING = 200_000.0


def _parse_traffic(raw: Optional[str]) -> Optional[int]:
    """Turn '20,000+' into 20000. Returns None when there is no figure."""
    if not raw:
        return None
    match = _DIGITS.search(raw)
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_pubdate(raw: Optional[str]) -> Optional[datetime]:
    """RFC 822 date from the feed, normalised to UTC."""
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _score_from_volume(volume: Optional[int]) -> float:
    """A 0..1 signal derived from the feed's own traffic figure.

    Not a popularity metric of our own invention: with no figure the score is
    0.0, which is what "the source told us nothing" should look like.
    """
    if not volume or volume <= 0:
        return 0.0
    return round(min(volume / _VOLUME_CEILING, 1.0), 4)


def _text(element: Any, path: str) -> Optional[str]:
    found = element.find(path, _HT_NAMESPACE)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


class GoogleTrendsProvider(BaseTrendProvider):
    """Daily trending searches for one country."""

    name = "google_trends"

    def __init__(self, timeout_seconds: float = 15.0, url_template: str = TRENDS_RSS_URL):
        self.timeout_seconds = timeout_seconds
        self.url_template = url_template

    async def fetch_trends(self, region: str, limit: int) -> list[TrendItem]:
        """Fetch and normalise one country's trending searches."""
        geo = region.upper()
        url = self.url_template.format(geo=geo)

        log.info("google_trends: fetching region=%s limit=%d", geo, limit)
        xml = await self._get(url, geo)
        trends = self._parse(xml, geo, limit)

        log.info("google_trends: region=%s returned %d trends", geo, len(trends))
        return trends

    # -- transport ----------------------------------------------------------
    async def _get(self, url: str, geo: str) -> str:
        """One HTTP GET. Every transport failure becomes TrendProviderError."""
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": "startup-intelligence-newsletter/0.1"},
            ) as client:
                response = await client.get(url)
        except httpx.TimeoutException as exc:
            raise TrendProviderError(
                f"Google Trends timed out after {self.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise TrendProviderError(f"Google Trends unreachable: {exc}") from exc

        if response.status_code == 429:
            raise TrendProviderError("Google Trends rate-limited this client (HTTP 429)")
        if response.status_code >= 400:
            # A bad geo code is answered with an error status, not an empty feed.
            raise TrendProviderError(
                f"Google Trends returned HTTP {response.status_code} for region {geo}"
            )
        if not response.text.strip():
            raise TrendProviderError(f"Google Trends returned an empty body for {geo}")
        return response.text

    # -- parsing ------------------------------------------------------------
    def _parse(self, xml: str, geo: str, limit: int) -> list[TrendItem]:
        """RSS -> TrendItem. A malformed document is a provider error."""
        try:
            root = ElementTree.fromstring(xml)
        except ElementTree.ParseError as exc:
            raise TrendProviderError(f"Google Trends returned unparseable XML: {exc}") from exc

        collected_at = datetime.now(timezone.utc)
        trends: list[TrendItem] = []
        seen: set[str] = set()

        for item in root.iter("item"):
            topic = _text(item, "title")
            if not topic:
                continue

            key = topic.lower()
            if key in seen:
                continue
            seen.add(key)

            volume = _parse_traffic(_text(item, "ht:approx_traffic"))
            trends.append(
                TrendItem(
                    topic=topic,
                    region=geo,
                    source=self.name,
                    trend_score=_score_from_volume(volume),
                    search_volume=volume,
                    # The feed has no growth figure. None is the honest answer.
                    growth=None,
                    collected_at=_parse_pubdate(_text(item, "pubDate")) or collected_at,
                )
            )
            if len(trends) >= limit:
                break

        return trends
