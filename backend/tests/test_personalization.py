"""Tests for the personalisation stack: users, ingestion, intelligence, feed.

Everything runs offline. Google News is a fixture string parsed by the real
provider, and the LLM is a fake implementing the one method each service calls,
so the suite needs no API key, costs nothing, and still exercises the real
parsing, scoring and selection code.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.models.db import Article, User
from app.models.schemas import InterestIn, UserCreate, slugify_topic
from app.providers.news_base import NewsProviderError
from app.providers.google_news import GoogleNewsProvider, is_google_redirect
from app.services import user_service
from app.services.ingestion_service import (
    IngestionResult,
    IngestionService,
    canonical_url,
    is_valid_url,
    normalize_amounts,
    title_similarity,
    url_hash,
)
from app.services.intelligence_service import (
    AnalysisResult,
    IntelligenceService,
    parse_analysis,
    rule_importance,
    rule_topics,
)
from app.services.recommendation_service import RecommendationService

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>Acme raises $10M Series A - TechCrunch</title>
    <link>https://example.com/acme?utm_source=twitter</link>
    <description>&lt;a href="x"&gt;Acme&lt;/a&gt; raised ten million.</description>
    <pubDate>Wed, 10 Sep 2026 09:00:00 GMT</pubDate>
    <source url="https://techcrunch.com">TechCrunch</source>
  </item>
  <item>
    <title>Acme Raises $10 Million In Series A Round - Reuters</title>
    <link>https://other.example.com/acme-round</link>
    <pubDate>Wed, 10 Sep 2026 10:00:00 GMT</pubDate>
    <source url="https://reuters.com">Reuters</source>
  </item>
  <item>
    <title>India tightens fintech rules - Mint</title>
    <link>https://mint.example.com/fintech-rules</link>
    <pubDate>Wed, 10 Sep 2026 08:00:00 GMT</pubDate>
    <source url="https://mint.com">Mint</source>
  </item>
  <item>
    <title>Broken link story</title>
    <link>javascript:alert(1)</link>
    <pubDate>Wed, 10 Sep 2026 08:00:00 GMT</pubDate>
  </item>
</channel></rss>"""


class FakeOpenRouter:
    def __init__(self, payload=None, error=None, enabled=True):
        self.payload = payload if payload is not None else {}
        self.error = error
        self.enabled = enabled
        self.calls = 0

    async def generate_structured_output(self, system_prompt, user_prompt, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.payload


class StubNewsProvider:
    name = "stub"
    is_metered = False

    def __init__(self, articles=None, error=None):
        self.articles = articles or []
        self.error = error

    async def fetch(self, query, region, limit):
        if self.error:
            raise self.error
        return self.articles[:limit]

    async def safe_fetch(self, query, region, limit):
        try:
            return await self.fetch(query, region, limit)
        except NewsProviderError:
            return []


def _settings(**overrides) -> Settings:
    base = {"OPENROUTER_API_KEY": "", "OPENROUTER_MODEL": "test/model"}
    base.update(overrides)
    return Settings(**base)


def _article(session, title, *, topics=(), importance=50.0, hours_ago=1, source="Src", url=None):
    url = url or f"https://example.com/{abs(hash(title))}"
    row = Article(
        url=url, url_hash=url_hash(url), title=title, source=source,
        importance_score=importance, analyzed_at=datetime.now(timezone.utc),
        published_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )
    session.add(row)
    session.flush()
    for slug in topics:
        topic = user_service.get_or_create_topic(session, slug)
        from app.models.db import ArticleTopic
        row.topic_links.append(ArticleTopic(topic_id=topic.id))
    session.flush()
    return row


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------
def test_canonical_url_strips_tracking_and_fragments():
    assert canonical_url("https://WWW.Example.com/a/?utm_source=x&id=7#frag") == \
        "https://example.com/a?id=7"


def test_canonical_url_collapses_equivalent_forms():
    assert url_hash("https://example.com/a") == url_hash("https://www.example.com/a/?fbclid=z")


@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a", True),
    ("http://example.com", True),
    ("javascript:alert(1)", False),
    ("ftp://example.com/a", False),
    ("", False),
    ("not a url", False),
])
def test_url_validation(url, ok):
    assert is_valid_url(url) is ok


def test_amounts_are_normalised_across_notations():
    assert normalize_amounts("$10 million") == "$10m"
    assert normalize_amounts("$10M") == "$10m"
    assert normalize_amounts("1.50 billion") == "1.5b"
    assert normalize_amounts("56 Crore") == "56cr"


def test_amount_normalisation_keeps_different_numbers_different():
    """The bug this guards: rstrip('0') turning 50 into 5."""
    assert normalize_amounts("$50M") != normalize_amounts("$5M")


def test_reworded_headline_is_a_duplicate():
    assert title_similarity(
        "Acme raises $10M in Series A", "Acme Raises $10 Million Series A Round"
    ) >= 0.72


@pytest.mark.parametrize("a,b", [
    ("AI startup raises $5M seed", "AI startup raises $50M seed"),
    ("Zomato raises $10M", "Swiggy raises $10M"),
    ("Acme raises $10M", "Cricket World Cup final result"),
])
def test_different_stories_are_never_merged(a, b):
    """A false merge loses a story for good, so this is the strict direction."""
    assert title_similarity(a, b) < 0.72


# ---------------------------------------------------------------------------
# Google News provider
# ---------------------------------------------------------------------------
def test_provider_parses_the_feed():
    articles = GoogleNewsProvider()._parse(FEED, "funding", "IN", limit=10)
    assert len(articles) == 4
    assert all(a.provider == "google_news" and a.region == "IN" for a in articles)


def test_provider_strips_the_outlet_suffix_from_titles():
    articles = GoogleNewsProvider()._parse(FEED, "funding", "IN", limit=10)
    assert articles[0].title == "Acme raises $10M Series A"
    assert articles[0].source == "TechCrunch"


def test_provider_strips_html_from_descriptions():
    articles = GoogleNewsProvider()._parse(FEED, "funding", "IN", limit=10)
    assert "<a" not in articles[0].description
    assert "Acme raised ten million." in articles[0].description


def test_provider_rejects_malformed_xml():
    with pytest.raises(NewsProviderError):
        GoogleNewsProvider()._parse("<rss><channel>", "q", "IN", 5)


def test_provider_respects_the_limit():
    assert len(GoogleNewsProvider()._parse(FEED, "q", "IN", limit=2)) == 2


def test_google_redirect_detection():
    assert is_google_redirect("https://news.google.com/rss/articles/CBMi") is True
    assert is_google_redirect("https://techcrunch.com/x") is False


# ---------------------------------------------------------------------------
# Ingestion pipeline
# ---------------------------------------------------------------------------
def test_ingestion_drops_invalid_urls_and_duplicates(db_session):
    articles = GoogleNewsProvider()._parse(FEED, "funding", "IN", limit=10)
    service = IngestionService(providers=[StubNewsProvider(articles)])
    result = IngestionResult()

    kept = service.deduplicate(articles, result)

    assert result.invalid_urls == 1                # javascript: link
    assert result.duplicates_in_batch == 1         # the reworded Acme story
    assert len(kept) == 2


def test_ingestion_is_idempotent(db_session):
    articles = GoogleNewsProvider()._parse(FEED, "funding", "IN", limit=10)
    service = IngestionService(providers=[StubNewsProvider(articles)])

    first, rows = asyncio.run(service.ingest(db_session, ["funding"], "IN", 10))
    assert first.stored == len(rows) > 0

    second, _ = asyncio.run(service.ingest(db_session, ["funding"], "IN", 10))
    assert second.stored == 0
    assert second.already_known == first.stored


def test_ingestion_with_no_queries_does_nothing(db_session):
    service = IngestionService(providers=[StubNewsProvider([])])
    result, rows = asyncio.run(service.ingest(db_session, [], "IN", 10))
    assert result.collected == 0 and rows == []


def test_a_failing_provider_does_not_end_the_run(db_session):
    service = IngestionService(providers=[StubNewsProvider(error=NewsProviderError("down"))])
    result, rows = asyncio.run(service.ingest(db_session, ["funding"], "IN", 5))
    assert rows == []
    assert "stub" in result.providers_failed


# ---------------------------------------------------------------------------
# Intelligence engine
# ---------------------------------------------------------------------------
def test_parse_analysis_reads_a_well_formed_reply(db_session):
    article = _article(db_session, "Acme raises $10M")
    payload = {"results": [{
        "id": article.id, "topics": ["AI", "Funding"],
        "entities": ["Acme"], "importance": 88, "summary": "Acme raised money.",
    }]}
    parsed = parse_analysis(payload, [article])
    assert parsed[article.id]["topics"] == ["ai", "funding"]
    assert parsed[article.id]["importance"] == 88.0


def test_parse_analysis_clamps_scores(db_session):
    article = _article(db_session, "Clamp me")
    parsed = parse_analysis({"results": [{"id": article.id, "importance": 500}]}, [article])
    assert parsed[article.id]["importance"] == 100.0


def test_parse_analysis_survives_a_useless_reply(db_session):
    article = _article(db_session, "Nonsense reply")
    assert parse_analysis({"nope": 1}, [article]) == {}


def test_analysis_falls_back_to_rules_when_the_model_fails(db_session):
    from app.services.openrouter_service import OpenRouterUnavailableError

    article = _article(db_session, "Acme acquires Beta in $2B deal", importance=0.0)
    service = IntelligenceService(
        openrouter=FakeOpenRouter(error=OpenRouterUnavailableError("boom"))
    )
    result = asyncio.run(service.analyze(db_session, [article]))

    assert result.ai_used is False
    assert result.analyzed == 1
    assert article.analyzed_at is not None
    assert article.importance_score > 0        # rules produced something


def test_disabled_llm_is_never_called(db_session):
    fake = FakeOpenRouter(enabled=False)
    article = _article(db_session, "Quiet day")
    asyncio.run(IntelligenceService(openrouter=fake).analyze(db_session, [article]))
    assert fake.calls == 0


def test_topics_are_visible_in_memory_after_analysis(db_session):
    """The bug this guards: bare inserts leaving article.topics empty."""
    article = _article(db_session, "OpenAI raises a round", importance=0.0)
    payload = {"results": [{"id": article.id, "topics": ["ai", "funding"], "importance": 70}]}
    service = IntelligenceService(openrouter=FakeOpenRouter(payload=payload))
    asyncio.run(service.analyze(db_session, [article]))

    assert sorted(article.topics) == ["ai", "funding"]


def test_rule_topics_never_returns_empty(db_session):
    assert rule_topics(_article(db_session, "Something entirely unclassifiable")) == ["general"]


def test_rule_importance_rewards_event_words_and_freshness(db_session):
    big = _article(db_session, "Acme acquires Beta after IPO launch", hours_ago=1)
    small = _article(db_session, "A quiet afternoon in the office", hours_ago=300)
    assert rule_importance(big) > rule_importance(small)


def test_event_clustering_groups_coverage_of_one_story(db_session):
    a = _article(db_session, "Acme raises $10M Series A")
    b = _article(db_session, "Acme raises $10 million in Series A round")
    c = _article(db_session, "India tightens fintech rules")

    result = AnalysisResult()
    IntelligenceService(openrouter=FakeOpenRouter()).cluster_events(db_session, [a, b, c], result)

    assert a.event_id == b.event_id
    assert c.event_id not in (None, a.event_id)


# ---------------------------------------------------------------------------
# Recommendation engine
# ---------------------------------------------------------------------------
def test_interest_matching_prefers_the_strongest_topic():
    service = RecommendationService(_settings())
    score, matched = service.interest_score(["ai", "cricket"], {"ai": 80.0, "cricket": 30.0})
    assert (score, matched) == (80.0, "ai")


def test_unmatched_topics_get_a_baseline_not_zero():
    """A feed that can only show stated interests never widens."""
    service = RecommendationService(_settings())
    score, matched = service.interest_score(["curling"], {"ai": 80.0})
    assert score == service.settings.rec_baseline_interest > 0
    assert matched == ""


def test_recency_halves_at_the_configured_halflife():
    service = RecommendationService(_settings(REC_RECENCY_HALFLIFE_HOURS=24))
    now = datetime.now(timezone.utc)
    fresh = service.recency_score(now, now)
    day_old = service.recency_score(now - timedelta(hours=24), now)
    assert fresh == pytest.approx(100.0)
    assert day_old == pytest.approx(50.0, abs=0.5)


def test_undated_articles_are_not_treated_as_brand_new():
    service = RecommendationService(_settings())
    now = datetime.now(timezone.utc)
    assert service.recency_score(None, now) < service.recency_score(now, now)


def test_behavior_moves_the_score_both_ways():
    service = RecommendationService(_settings(REC_BEHAVIOR_INFLUENCE=20))
    assert service.behavior_score(["ai"], {"ai": 1.0}) == pytest.approx(20.0)
    assert service.behavior_score(["ai"], {"ai": -1.0}) == pytest.approx(-20.0)
    assert service.behavior_score(["ai"], {}) == 0.0


def test_selection_penalises_repeated_topics(db_session):
    service = RecommendationService(_settings(REC_DIVERSITY_PENALTY=30))
    weights = {"ai": 90.0, "fintech": 70.0}

    articles = [
        _article(db_session, f"AI story {i}", topics=["ai"], importance=80) for i in range(3)
    ] + [_article(db_session, "Fintech story", topics=["fintech"], importance=80)]

    scored = service.score_all(articles, weights, {})
    chosen = service.select(scored, limit=3)
    topics = [c.article.topics[0] for c in chosen]

    assert "fintech" in topics, "a diverse feed must not be all one topic"


def test_selection_shows_one_article_per_event(db_session):
    service = RecommendationService(_settings())
    a = _article(db_session, "Event story one", topics=["ai"], importance=90)
    b = _article(db_session, "Event story two", topics=["ai"], importance=88)
    a.event_id = b.event_id = 4242
    db_session.flush()

    chosen = service.select(service.score_all([a, b], {"ai": 90.0}, {}), limit=5)
    assert len(chosen) == 1


def test_feed_records_why_each_article_was_chosen(db_session):
    service = RecommendationService(_settings())
    article = _article(db_session, "AI funding news", topics=["ai"], importance=90)
    scored = service.score_all([article], {"ai": 85.0}, {})
    assert "ai" in scored[0].reason
    assert scored[0].interest == 85.0


# ---------------------------------------------------------------------------
# Users and interests through the API
# ---------------------------------------------------------------------------
def test_topic_slugs_are_canonical():
    assert slugify_topic("Artificial Intelligence") == "artificial-intelligence"
    assert slugify_topic("  AI  ") == "ai"
    assert slugify_topic("E-commerce") == "e-commerce"
    with pytest.raises(ValueError):
        slugify_topic("   ")


def test_the_same_topic_is_never_created_twice(db_session):
    a = user_service.get_or_create_topic(db_session, "AI")
    b = user_service.get_or_create_topic(db_session, "  ai ")
    assert a.id == b.id


def test_user_lifecycle_through_the_api():
    created = client.post("/api/users", json={
        "email": "lifecycle@example.com",
        "interests": [{"topic": "AI", "weight": 80}, {"topic": "Cricket", "weight": 90}],
    })
    assert created.status_code == 201
    user_id = created.json()["id"]

    # Strongest interest first.
    assert [i["topic"] for i in created.json()["interests"]] == ["cricket", "ai"]

    fetched = client.get(f"/api/users/{user_id}")
    assert fetched.status_code == 200
    assert len(fetched.json()["interests"]) == 2


def test_duplicate_email_is_a_conflict():
    client.post("/api/users", json={"email": "dupe@example.com"})
    assert client.post("/api/users", json={"email": "dupe@example.com"}).status_code == 409


def test_invalid_email_is_rejected():
    assert client.post("/api/users", json={"email": "no-at-sign"}).status_code == 422


def test_unknown_user_is_a_404():
    assert client.get("/api/users/999999").status_code == 404
    assert client.get("/api/users/999999/feed").status_code == 404


def test_setting_interests_replaces_rather_than_merges():
    """Without replace semantics, removing a topic would be impossible."""
    user_id = client.post("/api/users", json={
        "email": "replace@example.com",
        "interests": [{"topic": "AI", "weight": 80}, {"topic": "Cricket", "weight": 90}],
    }).json()["id"]

    updated = client.put(f"/api/users/{user_id}/interests",
                         json={"interests": [{"topic": "Fintech", "weight": 60}]})
    assert updated.status_code == 200
    assert [i["topic"] for i in updated.json()["interests"]] == ["fintech"]


def test_interest_weight_is_bounded():
    assert client.post("/api/users", json={
        "email": "bounds@example.com", "interests": [{"topic": "AI", "weight": 500}],
    }).status_code == 422


def test_behavior_can_be_recorded():
    user_id = client.post("/api/users", json={"email": "behave@example.com"}).json()["id"]
    response = client.post(f"/api/users/{user_id}/behavior",
                           json={"article_id": 1, "action": "open"})
    assert response.status_code == 204


def test_unknown_behavior_action_is_rejected():
    user_id = client.post("/api/users", json={"email": "badaction@example.com"}).json()["id"]
    assert client.post(f"/api/users/{user_id}/behavior",
                       json={"article_id": 1, "action": "teleport"}).status_code == 422


# ---------------------------------------------------------------------------
# Feed and newsletter endpoints
# ---------------------------------------------------------------------------
def test_feed_is_empty_not_an_error_before_ingestion():
    user_id = client.post("/api/users", json={"email": "emptyfeed@example.com"}).json()["id"]
    response = client.get(f"/api/users/{user_id}/feed")
    assert response.status_code == 200
    assert response.json()["total"] >= 0


def test_ingest_needs_queries_or_a_user_with_interests():
    assert client.post("/api/news/ingest", json={"queries": []}).status_code == 422


def test_ingest_with_an_unknown_user_is_a_404():
    assert client.post("/api/news/ingest", json={"user_id": 999999}).status_code == 404


def test_newsletter_for_an_unknown_user_is_a_404():
    assert client.post("/api/newsletter/generate", json={"user_id": 999999}).status_code == 404


def test_new_routes_are_in_the_openapi_schema():
    paths = client.get("/openapi.json").json()["paths"]
    for path in ("/api/users", "/api/users/{user_id}", "/api/users/{user_id}/feed",
                 "/api/news/ingest"):
        assert path in paths, path


def test_day_1_and_day_2_endpoints_still_work():
    assert client.get("/health").status_code == 200
    assert client.get("/api").status_code == 200
    assert client.get("/api/trends/health").status_code == 200
    assert client.get("/api/newsletter/health").status_code == 200
    assert client.get("/docs").status_code == 200


def test_no_secret_reaches_any_response():
    for path in ("/api", "/health", "/api/users"):
        assert "sk-or-" not in client.get(path).text, path
