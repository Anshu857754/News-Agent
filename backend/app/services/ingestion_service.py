"""The ingestion pipeline: raw feeds in, clean rows out.

    providers -> normalize -> validate -> deduplicate -> persist

Deduplication happens in two passes because there are two different duplicates
to catch, and only one of them is exact:

* **Same link.** The canonical URL decides. Tracking parameters are stripped
  first, or `?utm_source=twitter` would make one article look like five.
* **Same story, different outlet.** Reuters copy runs verbatim under six
  mastheads with slightly reworded headlines, so near-identical titles collapse
  by word overlap. Only headlines are compared - bodies diverge wildly between
  outlets covering one event, which is exactly when you most want them merged.

  This pass is deliberately conservative. A false merge loses a story for good,
  while a missed one is caught later by event clustering, which compares more
  than a headline. So the threshold sits where no realistic pair of *different*
  stories collapses, and some genuine duplicates survive to the next stage.

Order matters: validate before deduplicating, because a junk URL should never
be the copy that wins and survives.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.db import Article
from app.models.schemas import NewsArticle
from app.providers.google_news import GoogleNewsProvider
from app.providers.news_base import BaseNewsProvider

log = logging.getLogger(__name__)

# Parameters that identify a campaign, never a document.
_TRACKING_PREFIXES = ("utm_", "ga_", "mc_")
_TRACKING_KEYS = {
    "fbclid", "gclid", "igshid", "ref", "ref_src", "cmpid", "smid",
    "source", "spm", "at_medium", "at_campaign",
}

# Above this word overlap, two headlines are the same story.
TITLE_SIMILARITY_THRESHOLD = 0.72

_WORD_RE = re.compile(r"[a-z0-9]+")
# Words that carry no identity; two headlines sharing only these share nothing.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "by", "from", "as", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "it", "its", "this", "that", "will", "says", "say",
    "new", "after", "over", "amid", "into", "up", "down", "out",
}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------
def canonical_url(url: str) -> str:
    """Strip a URL down to what identifies the document."""
    raw = (url or "").strip()
    if not raw:
        return ""

    parts = urlparse(raw)
    query = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in _TRACKING_KEYS
        and not k.lower().startswith(_TRACKING_PREFIXES)
    ]
    path = parts.path.rstrip("/") or "/"

    return urlunparse((
        parts.scheme.lower(),
        parts.netloc.lower().removeprefix("www."),
        path,
        "",
        urlencode(query),
        "",                       # fragments never identify a document
    ))


def url_hash(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def is_valid_url(url: str) -> bool:
    """An http(s) URL with a host. Anything else cannot be opened."""
    try:
        parts = urlparse((url or "").strip())
    except ValueError:
        return False
    return parts.scheme in {"http", "https"} and bool(parts.netloc)


# Money is where re-headlined copy diverges most: "$10M", "$10 million" and
# "10M" are one number written three ways. Normalising them is what makes
# overlap comparable - the alternative, lowering the threshold, would merge
# "$5M" with "$50M", which are different stories about different companies.
_SCALES = {
    "k": "k", "thousand": "k",
    "m": "m", "mn": "m", "million": "m",
    "b": "b", "bn": "b", "billion": "b",
    "cr": "cr", "crore": "cr", "crores": "cr",
    "lakh": "lakh", "lakhs": "lakh",
}
_AMOUNT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + "|".join(sorted(_SCALES, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


def _canonical_amount(match: re.Match) -> str:
    """'10 million' -> '10m', '1.50 billion' -> '1.5b'.

    Trailing zeros are only dropped after a decimal point. Stripping them from
    an integer would turn 50 into 5 and quietly merge two different stories.
    """
    number = match.group(1)
    if "." in number:
        number = number.rstrip("0").rstrip(".")
    # The pattern is case-insensitive, so the captured scale may be "M".
    return f"{number}{_SCALES[match.group(2).lower()]}"


def normalize_amounts(text: str) -> str:
    """'$10 million' and '$10M' both become '10m'."""
    return _AMOUNT_RE.sub(_canonical_amount, text)


def title_tokens(title: str) -> set[str]:
    lowered = normalize_amounts((title or "").lower())
    return {w for w in _WORD_RE.findall(lowered) if w not in _STOPWORDS}


def title_similarity(left: str, right: str) -> float:
    """Jaccard overlap of meaningful words. 0 when either side is empty."""
    a, b = title_tokens(left), title_tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------
@dataclass
class IngestionResult:
    """What one run did. Every number is a count of articles."""

    collected: int = 0
    invalid_urls: int = 0
    duplicates_in_batch: int = 0
    already_known: int = 0
    stored: int = 0
    queries: list[str] = field(default_factory=list)
    providers_used: list[str] = field(default_factory=list)
    providers_failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "collected": self.collected,
            "invalid_urls": self.invalid_urls,
            "duplicates_in_batch": self.duplicates_in_batch,
            "already_known": self.already_known,
            "stored": self.stored,
            "queries": self.queries,
            "providers_used": self.providers_used,
            "providers_failed": self.providers_failed,
        }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
class IngestionService:
    """Collects articles for a set of queries and stores the new ones."""

    def __init__(self, providers: Optional[Sequence[BaseNewsProvider]] = None):
        self.providers = list(providers) if providers is not None else [GoogleNewsProvider()]

    # -- stage 1: collect ---------------------------------------------------
    async def collect(
        self, queries: Sequence[str], region: str, limit_per_query: int
    ) -> tuple[list[NewsArticle], list[str], list[str]]:
        """Every provider against every query, concurrently."""
        free = [p for p in self.providers if not p.is_metered]
        metered = [p for p in self.providers if p.is_metered]

        jobs = [
            (provider, query)
            for provider in free
            for query in queries
        ]
        results = await asyncio.gather(
            *(p.safe_fetch(q, region, limit_per_query) for p, q in jobs)
        )

        articles: list[NewsArticle] = []
        used, failed = set(), set()
        for (provider, _), batch in zip(jobs, results):
            if batch:
                used.add(provider.name)
                articles.extend(batch)
            else:
                failed.add(provider.name)

        # A metered provider is a top-up, never a default: it bills per result.
        if metered and not articles:
            log.info("free providers returned nothing; trying %d metered", len(metered))
            extra = await asyncio.gather(
                *(p.safe_fetch(q, region, limit_per_query) for p in metered for q in queries)
            )
            for batch in extra:
                articles.extend(batch)

        # A provider that answered for one query has not failed overall.
        failed -= used
        return articles, sorted(used), sorted(failed)

    # -- stages 2-4: clean --------------------------------------------------
    def deduplicate(
        self, articles: Iterable[NewsArticle], result: IngestionResult
    ) -> list[NewsArticle]:
        """Drop invalid URLs, then exact and near duplicates.

        Articles with a publication date are considered first, so the copy that
        survives is the one carrying the most metadata.
        """
        ordered = sorted(
            articles,
            key=lambda a: (a.published_at is None, -(len(a.description or ""))),
        )

        kept: list[NewsArticle] = []
        seen_hashes: set[str] = set()

        for article in ordered:
            if not is_valid_url(article.url):
                result.invalid_urls += 1
                continue

            digest = url_hash(article.url)
            if digest in seen_hashes:
                result.duplicates_in_batch += 1
                continue

            if any(
                title_similarity(article.title, k.title) >= TITLE_SIMILARITY_THRESHOLD
                for k in kept
            ):
                result.duplicates_in_batch += 1
                continue

            seen_hashes.add(digest)
            kept.append(article)

        return kept

    # -- stage 5: persist ---------------------------------------------------
    def store(
        self, session: Session, articles: Sequence[NewsArticle], result: IngestionResult
    ) -> list[Article]:
        """Insert articles this database has not seen before."""
        if not articles:
            return []

        digests = [url_hash(a.url) for a in articles]
        known = set(
            session.scalars(select(Article.url_hash).where(Article.url_hash.in_(digests)))
        )

        rows: list[Article] = []
        for article, digest in zip(articles, digests):
            if digest in known:
                result.already_known += 1
                continue

            row = Article(
                url=canonical_url(article.url),
                url_hash=digest,
                title=article.title,
                description=article.description or "",
                source=article.source or "unknown",
                provider=article.provider or "",
                region=article.region or "",
                published_at=article.published_at,
                matched_trend=article.topic or "",
            )
            session.add(row)
            rows.append(row)
            known.add(digest)      # guards against duplicates inside this batch

        session.flush()
        result.stored = len(rows)
        return rows

    # -- the entry point ----------------------------------------------------
    async def ingest(
        self,
        session: Session,
        queries: Sequence[str],
        region: str = "IN",
        limit_per_query: int = 10,
    ) -> tuple[IngestionResult, list[Article]]:
        """Run the whole pipeline for a set of queries."""
        cleaned = [" ".join(str(q).split()) for q in queries if str(q).strip()]
        result = IngestionResult(queries=cleaned)
        if not cleaned:
            log.warning("ingestion: no queries given")
            return result, []

        log.info("ingestion started: %d queries region=%s", len(cleaned), region)

        collected, used, failed = await self.collect(cleaned, region, limit_per_query)
        result.collected = len(collected)
        result.providers_used = used
        result.providers_failed = failed

        unique = self.deduplicate(collected, result)
        rows = self.store(session, unique, result)

        log.info(
            "ingestion finished: %d collected, %d invalid, %d duplicates, "
            "%d already known, %d stored",
            result.collected, result.invalid_urls, result.duplicates_in_batch,
            result.already_known, result.stored,
        )
        return result, rows


_default: Optional[IngestionService] = None


def get_ingestion_service() -> IngestionService:
    """Process-wide instance, so provider clients are built once."""
    global _default
    if _default is None:
        _default = IngestionService()
    return _default
