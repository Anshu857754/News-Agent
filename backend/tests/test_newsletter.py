"""Tests for the Day 1 foundation.

Every test runs offline: no test makes a real OpenRouter call, so the suite
needs no API key and costs nothing. The service is exercised directly and the
API through `TestClient`.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.models.schemas import (
    GenerationStatus,
    NewsArticle,
    NewsletterRequest,
    NewsletterType,
    TimeRange,
)
from app.services.newsletter_service import NewsletterService
from app.services.openrouter_service import (
    OpenRouterResponseError,
    OpenRouterService,
    OpenRouterUnavailableError,
    extract_json,
)

client = TestClient(app)


def _settings(**overrides) -> Settings:
    """Settings built from explicit values, ignoring whatever is in .env.

    Apify is left out of the defaults on purpose: `apify_api_token` accepts two
    env names, and passing one of them here would always win over the other,
    making the alias untestable.
    """
    base = {
        "OPENROUTER_API_KEY": "",
        "OPENROUTER_MODEL": "google/gemini-2.5-flash",
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def test_settings_disabled_without_a_key():
    settings = _settings()
    assert settings.openrouter_enabled is False
    assert settings.describe()["openrouter_key_present"] is False


def test_settings_enabled_with_a_key():
    assert _settings(OPENROUTER_API_KEY="sk-or-test").openrouter_enabled is True


def test_describe_never_leaks_the_key():
    settings = _settings(OPENROUTER_API_KEY="sk-or-secret-value")
    assert "sk-or-secret-value" not in str(settings.describe())


def test_apify_token_accepts_the_legacy_key_name():
    """Some .env files already use APIFY_API_KEY; both must work."""
    assert _settings(APIFY_API_KEY="apify_abc").apify_api_token == "apify_abc"
    assert _settings(APIFY_API_TOKEN="apify_xyz").apify_api_token == "apify_xyz"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
def test_request_defaults_match_the_documented_example():
    request = NewsletterRequest()
    assert request.region == "Global"
    assert request.category == "Startups"
    assert request.time_range is TimeRange.LAST_24H
    assert request.newsletter_type is NewsletterType.DAILY


def test_time_range_exposes_a_window_in_hours():
    assert TimeRange.LAST_24H.hours == 24
    assert TimeRange.LAST_7D.hours == 168
    assert TimeRange.LAST_30D.hours == 720


def test_blank_region_is_rejected():
    with pytest.raises(ValueError):
        NewsletterRequest(region="   ")


def test_region_whitespace_is_collapsed():
    assert NewsletterRequest(region="  United  States ").region == "United States"


def test_news_article_normalises_naive_timestamps_to_utc():
    from datetime import datetime

    article = NewsArticle(
        title="  Acme   raises  $10M ",
        url="https://example.com/a",
        published_at=datetime(2026, 9, 8, 12, 0),
    )
    assert article.title == "Acme raises $10M"
    assert article.published_at.tzinfo is not None


def test_news_article_rejects_an_out_of_range_score():
    with pytest.raises(ValueError):
        NewsArticle(title="t", url="https://example.com", relevance_score=1.5)


# ---------------------------------------------------------------------------
# OpenRouter service
# ---------------------------------------------------------------------------
def test_extract_json_handles_a_bare_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_handles_code_fences():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_handles_leading_prose():
    assert extract_json('Sure! Here you go: {"a": 1}') == {"a": 1}


def test_extract_json_rejects_an_empty_reply():
    with pytest.raises(OpenRouterResponseError):
        extract_json("   ")


def test_extract_json_rejects_a_non_object():
    with pytest.raises(OpenRouterResponseError):
        extract_json("[1, 2, 3]")


def test_service_describe_never_leaks_the_key():
    service = OpenRouterService(_settings(OPENROUTER_API_KEY="sk-or-secret-value"))
    assert "sk-or-secret-value" not in str(service.describe())


def test_unconfigured_service_raises_rather_than_calling_out():
    """No key must fail fast and locally, not as a network error."""
    service = OpenRouterService(_settings())
    with pytest.raises(OpenRouterUnavailableError):
        asyncio.run(service.generate_completion("hello"))


def test_analyze_content_rejects_empty_input():
    service = OpenRouterService(_settings(OPENROUTER_API_KEY="sk-or-test"))
    with pytest.raises(OpenRouterResponseError):
        asyncio.run(service.analyze_content("   ", "summarise this"))


# ---------------------------------------------------------------------------
# Newsletter service
# ---------------------------------------------------------------------------
def test_service_reports_not_implemented_without_faking_a_newsletter():
    service = NewsletterService(OpenRouterService(_settings()))
    result = asyncio.run(service.generate(NewsletterRequest()))

    assert result.status is GenerationStatus.NOT_IMPLEMENTED
    assert result.newsletter is None
    assert result.request is not None
    assert result.request.region == "Global"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def test_newsletter_health_endpoint():
    response = client.get("/api/newsletter/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "newsletter"}


def test_generate_endpoint_returns_the_placeholder():
    response = client.post(
        "/api/newsletter/generate",
        json={
            "region": "Global",
            "category": "Startups",
            "time_range": "24h",
            "newsletter_type": "daily",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "not_implemented"
    assert body["message"] == "Newsletter generation pipeline foundation is ready"
    assert body["newsletter"] is None
    assert body["request"]["time_range"] == "24h"


def test_generate_accepts_an_empty_body_and_uses_defaults():
    response = client.post("/api/newsletter/generate", json={})
    assert response.status_code == 200
    assert response.json()["request"]["region"] == "Global"


def test_generate_rejects_an_unknown_time_range():
    response = client.post("/api/newsletter/generate", json={"time_range": "48h"})
    assert response.status_code == 422


def test_generate_rejects_an_unknown_newsletter_type():
    response = client.post(
        "/api/newsletter/generate", json={"newsletter_type": "hourly"}
    )
    assert response.status_code == 422


def test_application_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "openrouter_model" in body["config"]


def test_root_lists_the_endpoints():
    response = client.get("/")
    assert response.status_code == 200
    assert "/api/newsletter/generate" in response.json()["endpoints"]


def test_no_api_key_appears_in_any_response():
    """The one regression that must never ship."""
    for path in ("/", "/health", "/api/newsletter/health"):
        assert "sk-or-" not in client.get(path).text
    assert "sk-or-" not in client.post("/api/newsletter/generate", json={}).text


def test_routes_are_registered_in_the_openapi_schema():
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/newsletter/health" in paths
    assert "/api/newsletter/generate" in paths
