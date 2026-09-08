"""Trend discovery orchestration.

The one file that shows the Day 2 pipeline at a glance:

    1. COLLECT    trending topics from the provider (cached)
    2. NORMALISE  done by the provider - everything below sees TrendItem
    3. PRE-FILTER cheap keyword rules; LOW_PRIORITY is discarded here
    4. CLASSIFY   one batched LLM call over what survived
    5. THRESHOLD  drop anything under the configured relevance score
    6. RANK       relevance, then the provider's signal, then recency

Nothing here knows how trends are fetched: it depends on `BaseTrendProvider`,
so replacing Google Trends is a constructor argument, not a rewrite.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Optional

from app.core.cache import TTLCache
from app.core.config import Settings, get_settings
from app.models.schemas import (
    GLOBAL_REGION,
    RelevantTrend,
    TrendDiscoveryResponse,
    TrendItem,
    TrendPriority,
)
from app.providers.base import BaseTrendProvider, TrendProviderError
from app.providers.google_trends import GoogleTrendsProvider
from app.services.trend_filter import prefilter
from app.services.trend_relevance import TrendRelevanceAnalyzer

log = logging.getLogger(__name__)


class TrendService:
    """Collects, filters, scores and ranks trending topics."""

    def __init__(
        self,
        settings: Optional[Settings] = None,
        provider: Optional[BaseTrendProvider] = None,
        analyzer: Optional[TrendRelevanceAnalyzer] = None,
        cache: Optional[TTLCache] = None,
    ):
        self.settings = settings or get_settings()
        self.provider = provider or GoogleTrendsProvider(
            timeout_seconds=self.settings.trend_provider_timeout_seconds
        )
        self.analyzer = analyzer or TrendRelevanceAnalyzer(
            batch_size=self.settings.trend_ai_batch_size
        )
        self.cache = cache if cache is not None else TTLCache(
            self.settings.trend_cache_ttl_seconds
        )

    # -- stage 1: collection -------------------------------------------------
    async def collect_trends(self, region: str, limit: int) -> list[TrendItem]:
        """Fetch normalised trends, going through the cache.

        GLOBAL has no feed of its own, so it is composed from the configured
        country editions and deduplicated by topic. That composition is
        documented rather than hidden: every returned item still carries the
        country it was actually found in.
        """
        cache_key = f"trends:{region}:{limit}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            log.info("trend collection: cache hit for region=%s limit=%d", region, limit)
            return cached

        log.info("trend collection started: region=%s limit=%d", region, limit)

        if region == GLOBAL_REGION:
            trends = await self._collect_global(limit)
        else:
            trends = await self.provider.fetch_trends(region, limit)

        log.info("trend collection: %d trends collected for region=%s", len(trends), region)
        self.cache.set(cache_key, trends)
        return trends

    async def _collect_global(self, limit: int) -> list[TrendItem]:
        """Merge several country feeds into one list.

        A region that fails costs its own results only - GLOBAL fails as a
        whole just when every region does.
        """
        regions = self.settings.global_regions
        if not regions:
            raise TrendProviderError("GLOBAL is configured with no source regions")

        results = await asyncio.gather(
            *(self.provider.safe_fetch_trends(region, limit) for region in regions)
        )
        if not any(results):
            raise TrendProviderError(
                f"no trends returned from any GLOBAL region ({', '.join(regions)})"
            )

        merged: list[TrendItem] = []
        seen: set[str] = set()
        # Round-robin, so one country cannot fill the whole list.
        for position in range(limit):
            for batch in results:
                if position >= len(batch):
                    continue
                item = batch[position]
                key = item.topic.lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
                if len(merged) >= limit:
                    return merged
        return merged

    # -- the full pipeline ---------------------------------------------------
    async def discover(self, region: str, limit: int) -> TrendDiscoveryResponse:
        """Run every stage and return the ranked, relevant trends."""
        trends = await self.collect_trends(region, limit)
        if not trends:
            log.warning("trend discovery: no trends collected for region=%s", region)
            return TrendDiscoveryResponse(
                total_trends_collected=0, total_relevant_trends=0, region=region
            )

        by_topic = {item.topic: item for item in trends}

        # Stage 3 - the cheap filter. LOW_PRIORITY never reaches the model.
        verdicts = prefilter(list(by_topic))
        candidates = {
            topic: (priority, category)
            for topic, (priority, category) in verdicts.items()
            if priority is not TrendPriority.LOW_PRIORITY
        }
        discarded = len(by_topic) - len(candidates)
        log.info(
            "trend discovery: %d discarded by rules, %d sent to AI",
            discarded, len(candidates),
        )

        # Stage 4 - one batched call.
        results, ai_used = await self.analyzer.classify_batch(candidates)

        # Stage 5 - the threshold, defined once in config.
        threshold = self.settings.trend_relevance_threshold
        relevant: list[RelevantTrend] = []
        for topic, verdict in results.items():
            item = by_topic.get(topic)
            if item is None or verdict.relevance_score < threshold:
                continue
            relevant.append(
                RelevantTrend(
                    topic=item.topic,
                    category=verdict.category,
                    relevance_score=verdict.relevance_score,
                    reason=verdict.reason,
                    region=item.region,
                    source=item.source,
                    trend_score=item.trend_score,
                    search_volume=item.search_volume,
                    collected_at=item.collected_at,
                )
            )

        ranked = self.rank(relevant)
        log.info(
            "trend discovery finished: %d collected, %d relevant (threshold=%s, ai_used=%s)",
            len(trends), len(ranked), threshold, ai_used,
        )

        return TrendDiscoveryResponse(
            total_trends_collected=len(trends),
            total_relevant_trends=len(ranked),
            region=region,
            trends=ranked,
            ai_used=ai_used,
        )

    # -- stage 6: ranking ----------------------------------------------------
    @staticmethod
    def rank(trends: list[RelevantTrend]) -> list[RelevantTrend]:
        """Most relevant first.

        Relevance decides; the provider's own signal breaks ties; recency
        breaks what is left. No popularity metric is invented - `trend_score`
        is 0.0 whenever the source gave nothing, and then recency decides.
        """
        return sorted(
            trends,
            key=lambda t: (t.relevance_score, t.trend_score, t.collected_at),
            reverse=True,
        )


@lru_cache(maxsize=1)
def get_trend_service() -> TrendService:
    """The process-wide instance, so the cache survives between requests."""
    return TrendService()
