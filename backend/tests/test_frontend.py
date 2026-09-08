"""Tests for the trend discovery dashboard.

The page is plain HTML/CSS/JS served by FastAPI, so these tests check what can
be checked without a browser: that the assets are served, that the document
contains the structure the spec asks for, and - the one that actually catches
regressions - that no mock trend data was left behind in the JavaScript.

Browser behaviour (rendering, responsive layout, the states firing in order)
is not covered here; that needs a real browser and is verified by hand.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _page() -> str:
    return client.get("/").text


def _script() -> str:
    return client.get("/static/app.js").text


# ---------------------------------------------------------------------------
# The page is served
# ---------------------------------------------------------------------------
def test_dashboard_is_served_at_root():
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_dashboard_is_also_served_at_trends():
    """The spec's suggested route. Same document, meaningful URL."""
    assert client.get("/trends").status_code == 200
    assert client.get("/trends").text == client.get("/").text


def test_static_assets_are_served():
    for path, expected in (
        ("/static/styles.css", "text/css"),
        ("/static/app.js", "javascript"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert expected in response.headers["content-type"], path


# ---------------------------------------------------------------------------
# Page structure
# ---------------------------------------------------------------------------
def test_page_has_the_specified_header_text():
    page = _page()
    assert "Startup Trend Intelligence" in page
    assert (
        "Discover emerging technology, startup, and business signals "
        "from trending search activity." in page
    )


def test_navigation_contains_every_section():
    page = _page()
    for label in ("Dashboard", "Trends", "News", "Newsletter"):
        assert ">" + label + "<" in page, label


def test_unbuilt_nav_sections_are_disabled_not_fake_links():
    """News and Newsletter have no page yet; they must not pretend otherwise."""
    page = _page()
    assert page.count('aria-disabled="true"') >= 3
    assert 'href="/news"' not in page
    assert 'href="/newsletter"' not in page


def test_region_control_offers_india_and_global():
    page = _page()
    assert '<option value="IN" selected>India</option>' in page
    assert '<option value="GLOBAL">Global</option>' in page


def test_limit_control_offers_the_three_sizes():
    page = _page()
    for value in ("10", "20", "30"):
        assert 'value="' + value + '"' in page


def test_refresh_button_is_present():
    assert "Refresh Trends" in _page()


def test_all_four_metrics_are_present():
    page = _page()
    for label in (
        "Total trends analyzed",
        "Startup relevant",
        "Average relevance",
        "Last updated",
    ):
        assert label in page, label


# ---------------------------------------------------------------------------
# Accessibility
# ---------------------------------------------------------------------------
def test_selects_have_labels():
    page = _page()
    assert 'for="region"' in page and 'id="region"' in page
    assert 'for="limit"' in page and 'id="limit"' in page


def test_page_uses_semantic_landmarks():
    page = _page()
    for tag in ("<header", "<nav", "<main", "<footer", "<h1"):
        assert tag in page, tag


def test_page_declares_a_language_and_viewport():
    page = _page()
    assert '<html lang="en">' in page
    assert 'name="viewport"' in page


def test_results_region_announces_updates():
    assert 'aria-live="polite"' in _page()


def test_focus_styles_are_not_removed():
    css = client.get("/static/styles.css").text
    assert ":focus-visible" in css
    assert "outline: none" not in css.replace(" ", " ")


def test_relevance_is_not_communicated_by_colour_alone():
    """A numeric score and a word, not just a coloured bar."""
    script = _script()
    assert "scoreBand" in script
    assert "/ 100" in script
    assert "aria-label" in script


# ---------------------------------------------------------------------------
# Design system
# ---------------------------------------------------------------------------
def test_stylesheet_uses_the_projects_palette():
    css = client.get("/static/styles.css").text
    for colour in ("#0B0F14", "#111827", "#1F2937", "#F9FAFB", "#9CA3AF", "#3B82F6"):
        assert colour in css, colour


def test_stylesheet_avoids_decorative_gradients():
    """One shimmer sweep for the skeleton is allowed; nothing else."""
    css = client.get("/static/styles.css").text
    assert css.count("gradient(") <= 1


def test_stylesheet_is_responsive():
    css = client.get("/static/styles.css").text
    assert css.count("@media") >= 2
    assert "max-width: 620px" in css


# ---------------------------------------------------------------------------
# API integration, and the no-mock-data rule
# ---------------------------------------------------------------------------
def test_script_calls_every_trend_endpoint():
    script = _script()
    assert "/api/trends/health" in script
    assert "/api/trends/discover" in script
    assert re.search(r'"/api/trends\?"', script)


def test_script_posts_to_discover():
    script = _script()
    assert 'method: "POST"' in script
    assert "JSON.stringify" in script


def test_script_handles_every_state():
    script = _script()
    for component in (
        "TrendSkeleton", "TrendEmptyState", "TrendErrorState",
        "TrendList", "TrendItem", "renderMetrics",
    ):
        assert component in script, component


def test_loading_and_empty_and_error_copy_match_the_spec():
    script = _script()
    assert "Analyzing market signals" in script
    assert "Collecting trending topics and identifying startup relevance." in script
    assert "No relevant startup trends detected." in script
    assert "Try another region or refresh the latest trend data." in script
    assert "Unable to analyze trend data." in script


def test_no_mock_trend_data_is_left_in_the_ui():
    """The rule that matters: every number must come from the backend."""
    script = _script()
    for smell in ("AI Agents", "Startup Funding", "mockTrends", "sampleData", "FAKE_"):
        assert smell not in script, f"mock data left in app.js: {smell}"
    assert "AI Agents" not in _page()


def test_backend_errors_are_not_shown_raw():
    """The UI maps status codes to its own copy instead of echoing detail."""
    script = _script()
    assert "messageForStatus" in script
    # The parsed `detail` is kept for the console, never written into the DOM.
    assert "state.errorMessage" in script


def test_script_escapes_interpolated_values():
    """Topics and reasons are model output; they get escaped before render."""
    script = _script()
    assert "function escapeHtml" in script
    assert "escapeHtml(trend.topic)" in script
    assert "escapeHtml(trend.reason)" in script


# ---------------------------------------------------------------------------
# The API is unaffected
# ---------------------------------------------------------------------------
def test_serving_the_ui_did_not_break_the_api():
    assert client.get("/api").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/api/trends/health").status_code == 200
    assert client.get("/api/newsletter/health").status_code == 200
    assert client.post("/api/newsletter/generate", json={}).status_code == 200


def test_docs_still_work():
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_no_api_key_reaches_the_browser():
    for path in ("/", "/static/app.js", "/static/styles.css"):
        assert "sk-or-" not in client.get(path).text, path
