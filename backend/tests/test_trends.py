"""Tests for the Day 2 trend discovery pipeline.

No test touches the network. The Google Trends feed is a fixture string parsed
by the real provider, and the LLM is a fake object implementing the one method
`TrendRelevanceAnalyzer` calls. That keeps the suite free, fast and
deterministic, and still exercises the real parsing and scoring code.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.cache import TTLCache
from app.core.config import Settings
from app.main import app
from app.models.schemas import (
    RelevantTrend,
    TrendItem,
    TrendPriority,
    TrendRequest,
    normalize_region,
)
from app.providers.base import TrendProviderError
from app.providers.google_trends import GoogleTrendsProvider
from app.services.trend_filter import classify, prefilter
from app.services.trend_relevance import (
    TrendRelevanceAnalyzer,
    parse_results,
    rule_based_result,
)
from app.services.trend_service import TrendService

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
FEED = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0">
  <channel>
    <item>
      <title>AI Agents</title>
      <ht:approx_traffic>50,000+</ht:approx_traffic>
      <pubDate>Mon, 7 Sep 2026 22:50:00 -0700</pubDate>
    </item>
    <item>
      <title>Bigg Boss</title>
      <ht:approx_traffic>200,000+</ht:approx_traffic>
      <pubDate>Mon, 7 Sep 2026 21:00:00 -0700</pubDate>
    </item>
    <item>
      <title>OpenAI funding round</title>
      <pubDate>Mon, 7 Sep 2026 20:00:00 -0700</pubDate>
    </item>
    <item>
      <title>Some Cricketer</title>
      <ht:approx_traffic>10,000+</ht:approx_traffic>
    </item>
  </channel>
</rss>"""


class FakeOpenRouter:
    """Stands in for OpenRouterService. Records what it was asked."""

    def __init__(self, payload=None, error: Exception | None = None, enabled: bool = True):
        self.payload = payload if payload is not None else {"results": []}
        self.error = error
        self.enabled = enabled
        self.calls = 0

    async def generate_structured_output(self, system_prompt, user_prompt, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


class StubProvider:
    """A provider that returns fixed items, or fails."""

    name = "stub"

    def __init__(self, items=None, error: Exception | None = None):
        self.items = items or []
        self.error = error

    async def fetch_trends(self, region, limit):
        if self.error is not None:
            raise self.error
        return [i for i in self.items if i.region == region][:limit] or self.items[:limit]

    async def safe_fetch_trends(self, region, limit):
        try:
            return await self.fetch_trends(region, limit)
        except TrendProviderError:
            return []


def _settings(**overrides) -> Settings:
    base = {"OPENROUTER_API_KEY": "", "TREND_CACHE_TTL_SECONDS": 0}
    base.update(overrides)
    return Settings(**base)


def _item(topic, *, region="IN", score=0.0, volume=None, minutes_ago=0) -> TrendItem:
    return TrendItem(
        topic=topic,
        region=region,
        source="stub",
        trend_score=score,
        search_volume=volume,
        collected_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )


# ---------------------------------------------------------------------------
# 1. Provider returns normalised data
# ---------------------------------------------------------------------------
def test_provider_parses_the_feed_into_trend_items():
    provider = GoogleTrendsProvider()
    trends = provider._parse(FEED, "IN", limit=20)

    assert [t.topic for t in trends] == [
        "AI Agents", "Bigg Boss", "OpenAI funding round", "Some Cricketer",
    ]
    assert all(t.region == "IN" and t.source == "google_trends" for t in trends)


def test_provider_parses_traffic_and_leaves_growth_null():
    trends = GoogleTrendsProvider()._parse(FEED, "IN", limit=20)
    by_topic = {t.topic: t for t in trends}

    assert by_topic["AI Agents"].search_volume == 50000
    assert by_topic["Bigg Boss"].trend_score == 1.0        # at the ceiling
    # No traffic element at all -> no invented figure.
    assert by_topic["OpenAI funding round"].search_volume is None
    assert by_topic["OpenAI funding round"].trend_score == 0.0
    # The feed has no growth field for any item, ever.
    assert all(t.growth is None for t in trends)


def test_provider_respects_the_limit():
    assert len(GoogleTrendsProvider()._parse(FEED, "IN", limit=2)) == 2


def test_provider_rejects_malformed_xml():
    with pytest.raises(TrendProviderError):
        GoogleTrendsProvider()._parse("<rss><channel>", "IN", limit=5)


def test_provider_handles_an_empty_feed():
    empty = '<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
    assert GoogleTrendsProvider()._parse(empty, "IN", limit=5) == []


# ---------------------------------------------------------------------------
# 2. Region handling
# ---------------------------------------------------------------------------
def test_region_is_normalised_to_uppercase():
    assert normalize_region("in") == "IN"
    assert normalize_region(" global ") == "GLOBAL"


@pytest.mark.parametrize("bad", ["XYZ", "1", "", "INDIA", "I"])
def test_invalid_region_is_rejected(bad):
    with pytest.raises(ValueError):
        normalize_region(bad)


def test_invalid_region_is_a_422_from_the_api():
    assert client.get("/api/trends", params={"region": "INDIA"}).status_code == 422
    assert client.post("/api/trends/discover", json={"region": "XX1"}).status_code == 422


# ---------------------------------------------------------------------------
# 3-6. Rule-based pre-filter
# ---------------------------------------------------------------------------
def test_high_priority_classification():
    for topic in ("AI Agents", "OpenAI", "Series A funding", "fintech startup"):
        priority, category = classify(topic)
        assert priority is TrendPriority.HIGH_PRIORITY, topic
        assert category


def test_possible_classification_for_ambiguous_topics():
    """An unknown name must not be discarded - the model decides."""
    for topic in ("Virat Kohli", "Zomato", "some random person"):
        assert classify(topic)[0] is TrendPriority.POSSIBLE, topic


def test_low_priority_classification():
    for topic in ("Bigg Boss", "box office collection", "horoscope today"):
        assert classify(topic)[0] is TrendPriority.LOW_PRIORITY, topic


def test_startup_keyword_beats_a_noise_keyword():
    """'Nvidia vs AMD' contains 'vs' but is plainly a technology story."""
    assert classify("Nvidia vs AMD")[0] is TrendPriority.HIGH_PRIORITY


def test_keyword_matching_respects_word_boundaries():
    """'ai' must not match inside another word."""
    assert classify("Dubai")[0] is not TrendPriority.HIGH_PRIORITY


def test_prefilter_classifies_every_topic():
    verdicts = prefilter(["AI Agents", "Bigg Boss", "Virat Kohli"])
    assert set(verdicts) == {"AI Agents", "Bigg Boss", "Virat Kohli"}


def test_low_priority_trends_never_reach_the_ai():
    fake = FakeOpenRouter(payload={"results": []})
    service = TrendService(
        settings=_settings(),
        provider=StubProvider([_item("AI Agents"), _item("Bigg Boss")]),
        analyzer=TrendRelevanceAnalyzer(openrouter=fake),
        cache=TTLCache(0),
    )
    asyncio.run(service.discover("IN", 10))

    # One call, and "Bigg Boss" was not in it.
    assert fake.calls == 1


# ---------------------------------------------------------------------------
# 7-8. AI response parsing
# ---------------------------------------------------------------------------
def test_parse_results_reads_a_well_formed_reply():
    payload = {
        "results": [
            {"topic": "AI Agents", "relevant": True, "category": "AI",
             "relevance_score": 95, "reason": "Core AI topic."}
        ]
    }
    parsed = parse_results(payload, ["AI Agents"])
    assert parsed["AI Agents"].relevance_score == 95
    assert parsed["AI Agents"].relevant is True


def test_parse_results_clamps_an_out_of_range_score():
    payload = {"results": [{"topic": "X", "relevance_score": 500}]}
    assert parse_results(payload, ["X"])["X"].relevance_score == 100.0


def test_parse_results_tolerates_a_differently_named_key():
    payload = {"trends": [{"topic": "X", "relevance_score": 80}]}
    assert parse_results(payload, ["X"])["X"].relevance_score == 80


def test_parse_results_matches_case_insensitively():
    payload = {"results": [{"topic": "ai agents", "relevance_score": 90}]}
    assert "AI Agents" in parse_results(payload, ["AI Agents"])


def test_parse_results_falls_back_to_position_when_the_topic_is_rewritten():
    payload = {"results": [{"topic": "AI-Agents!", "relevance_score": 90}]}
    assert parse_results(payload, ["AI Agents"])["AI Agents"].relevance_score == 90


def test_parse_results_survives_a_reply_with_no_list():
    assert parse_results({"nonsense": True}, ["X"]) == {}


def test_parse_results_skips_non_dict_rows():
    payload = {"results": ["oops", 42, {"topic": "X", "relevance_score": 75}]}
    assert parse_results(payload, ["X"])["X"].relevance_score == 75


def test_invalid_ai_json_does_not_crash_the_pipeline():
    """A reply the parser cannot use degrades to rule-based scores."""
    fake = FakeOpenRouter(payload={"garbage": "yes"})
    analyzer = TrendRelevanceAnalyzer(openrouter=fake)
    verdicts, ai_used = asyncio.run(
        analyzer.classify_batch({"AI Agents": (TrendPriority.HIGH_PRIORITY, "AI")})
    )
    assert ai_used is False
    assert verdicts["AI Agents"].relevance_score == 75.0    # the rule fallback


def test_openrouter_failure_degrades_to_rules():
    from app.services.openrouter_service import OpenRouterUnavailableError

    fake = FakeOpenRouter(error=OpenRouterUnavailableError("boom"))
    analyzer = TrendRelevanceAnalyzer(openrouter=fake)
    verdicts, ai_used = asyncio.run(
        analyzer.classify_batch({
            "AI Agents": (TrendPriority.HIGH_PRIORITY, "AI"),
            "Virat Kohli": (TrendPriority.POSSIBLE, None),
        })
    )
    assert ai_used is False
    assert verdicts["AI Agents"].relevance_score == 75.0
    assert verdicts["Virat Kohli"].relevance_score == 40.0  # stays below threshold


def test_disabled_openrouter_is_not_called_at_all():
    fake = FakeOpenRouter(enabled=False)
    analyzer = TrendRelevanceAnalyzer(openrouter=fake)
    _, ai_used = asyncio.run(
        analyzer.classify_batch({"AI Agents": (TrendPriority.HIGH_PRIORITY, "AI")})
    )
    assert fake.calls == 0
    assert ai_used is False


def test_batching_sends_one_request_for_many_topics():
    fake = FakeOpenRouter(payload={"results": []})
    analyzer = TrendRelevanceAnalyzer(openrouter=fake, batch_size=25)
    candidates = {f"topic {i}": (TrendPriority.POSSIBLE, None) for i in range(20)}
    asyncio.run(analyzer.classify_batch(candidates))
    assert fake.calls == 1


def test_batching_chunks_when_over_the_batch_size():
    fake = FakeOpenRouter(payload={"results": []})
    analyzer = TrendRelevanceAnalyzer(openrouter=fake, batch_size=10)
    candidates = {f"topic {i}": (TrendPriority.POSSIBLE, None) for i in range(25)}
    asyncio.run(analyzer.classify_batch(candidates))
    assert fake.calls == 3


def test_rule_based_result_names_a_category():
    result = rule_based_result("OpenAI", TrendPriority.HIGH_PRIORITY)
    assert result.category == "Artificial Intelligence"


# ---------------------------------------------------------------------------
# 9. Threshold
# ---------------------------------------------------------------------------
def _service_with_scores(scores: dict[str, int], **settings_overrides) -> TrendService:
    payload = {
        "results": [
            {"topic": topic, "relevant": score >= 50, "category": "Technology",
             "relevance_score": score, "reason": "test"}
            for topic, score in scores.items()
        ]
    }
    return TrendService(
        settings=_settings(**settings_overrides),
        provider=StubProvider([_item(t) for t in scores]),
        analyzer=TrendRelevanceAnalyzer(openrouter=FakeOpenRouter(payload=payload)),
        cache=TTLCache(0),
    )


def test_threshold_excludes_low_scores():
    service = _service_with_scores({"AI Agents": 95, "Zomato": 40})
    result = asyncio.run(service.discover("IN", 10))

    assert result.total_trends_collected == 2
    assert result.total_relevant_trends == 1
    assert result.trends[0].topic == "AI Agents"


def test_threshold_is_configurable():
    service = _service_with_scores(
        {"AI Agents": 95, "Zomato": 40}, TREND_RELEVANCE_THRESHOLD=30
    )
    assert asyncio.run(service.discover("IN", 10)).total_relevant_trends == 2


def test_threshold_boundary_is_inclusive():
    service = _service_with_scores({"Exactly": 70})
    assert asyncio.run(service.discover("IN", 10)).total_relevant_trends == 1


# ---------------------------------------------------------------------------
# 10. Ranking
# ---------------------------------------------------------------------------
def test_ranking_orders_by_relevance_then_trend_score_then_recency():
    now = datetime.now(timezone.utc)

    def trend(topic, relevance, trend_score, minutes_ago):
        return RelevantTrend(
            topic=topic, category="T", relevance_score=relevance, reason="r",
            region="IN", source="stub", trend_score=trend_score,
            collected_at=now - timedelta(minutes=minutes_ago),
        )

    ranked = TrendService.rank([
        trend("low", 70, 0.9, 0),
        trend("tie-older", 90, 0.5, 60),
        trend("tie-newer", 90, 0.5, 1),
        trend("tie-stronger", 90, 0.8, 60),
    ])
    assert [t.topic for t in ranked] == [
        "tie-stronger", "tie-newer", "tie-older", "low",
    ]


# ---------------------------------------------------------------------------
# Collection, GLOBAL and caching
# ---------------------------------------------------------------------------
def test_global_merges_regions_and_deduplicates():
    provider = StubProvider([
        _item("AI Agents", region="US"),
        _item("Shared Topic", region="US"),
        _item("Shared Topic", region="IN"),
        _item("Fintech", region="IN"),
    ])
    service = TrendService(
        settings=_settings(TREND_GLOBAL_REGIONS="US,IN"),
        provider=provider, cache=TTLCache(0),
    )
    trends = asyncio.run(service.collect_trends("GLOBAL", 10))
    topics = [t.topic for t in trends]

    assert len(topics) == len(set(topics)), "GLOBAL must not repeat a topic"


def test_global_fails_only_when_every_region_fails():
    service = TrendService(
        settings=_settings(TREND_GLOBAL_REGIONS="US,IN"),
        provider=StubProvider(error=TrendProviderError("down")),
        cache=TTLCache(0),
    )
    with pytest.raises(TrendProviderError):
        asyncio.run(service.collect_trends("GLOBAL", 10))


def test_provider_failure_surfaces_as_503():
    service = TrendService(
        settings=_settings(),
        provider=StubProvider(error=TrendProviderError("Google Trends unreachable")),
        cache=TTLCache(0),
    )
    app.dependency_overrides[get_trend_service_dep()] = lambda: service
    try:
        response = client.get("/api/trends", params={"region": "IN"})
        assert response.status_code == 503
        assert "sk-or-" not in response.text
        assert "Traceback" not in response.text
    finally:
        app.dependency_overrides.clear()


def get_trend_service_dep():
    from app.services.trend_service import get_trend_service

    return get_trend_service


def test_cache_returns_the_same_object_within_ttl():
    provider = StubProvider([_item("AI Agents")])
    service = TrendService(
        settings=_settings(), provider=provider, cache=TTLCache(900)
    )
    first = asyncio.run(service.collect_trends("IN", 10))
    provider.items = [_item("Different")]
    second = asyncio.run(service.collect_trends("IN", 10))

    assert [t.topic for t in second] == [t.topic for t in first]


def test_cache_expires():
    clock = {"now": 0.0}
    cache = TTLCache(60, clock=lambda: clock["now"])
    cache.set("k", "v")
    assert cache.get("k") == "v"
    clock["now"] = 61.0
    assert cache.get("k") is None


def test_zero_ttl_disables_the_cache():
    cache = TTLCache(0)
    cache.set("k", "v")
    assert cache.get("k") is None
    assert cache.enabled is False


def test_empty_trend_list_is_a_200_not_an_error():
    service = TrendService(
        settings=_settings(), provider=StubProvider([]), cache=TTLCache(0)
    )
    result = asyncio.run(service.discover("IN", 10))
    assert result.total_trends_collected == 0
    assert result.trends == []


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------
def test_trends_health_endpoint():
    response = client.get("/api/trends/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "trend-discovery"}


def test_discover_request_defaults():
    request = TrendRequest()
    assert request.region == "IN"
    assert request.limit == 20


def test_discover_rejects_an_out_of_range_limit():
    assert client.post("/api/trends/discover", json={"limit": 0}).status_code == 422
    assert client.post("/api/trends/discover", json={"limit": 500}).status_code == 422


def test_trend_routes_are_in_the_openapi_schema():
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/api/trends", "/api/trends/discover", "/api/trends/health"):
        assert path in paths


# ---------------------------------------------------------------------------
# 11. Day 1 functionality is unaffected
# ---------------------------------------------------------------------------
def test_day_1_endpoints_still_work():
    assert client.get("/api/newsletter/health").status_code == 200
    assert client.post("/api/newsletter/generate", json={}).status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200


def test_service_info_lists_the_new_trend_endpoints():
    endpoints = client.get("/api").json()["endpoints"]
    assert "/api/trends/discover" in endpoints
