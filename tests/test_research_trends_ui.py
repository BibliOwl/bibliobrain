from __future__ import annotations

from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = read_app_javascript()
CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")


def test_research_trends_is_a_discovery_subview() -> None:
    assert 'id="discovery-workspace-trends"' in HTML
    assert 'id="discovery-trends-view"' in HTML
    assert 'id="research-trends-form"' in HTML
    assert 'id="research-trends-chart"' in HTML
    assert 'id="research-trends-table-wrap"' in HTML


def test_research_trends_frontend_posts_filters_and_supports_csv() -> None:
    assert "fetch('/api/research-trends'" in JS
    assert "work_types: workTypes" in JS
    assert "open_access_only:" in JS
    assert "function exportResearchTrendsCsv()" in JS
    assert "share_per_million" in JS
    assert "current_year_incomplete" in JS


def test_research_trend_api_errors_are_localized_by_stable_error_code() -> None:
    assert "function _researchTrendErrorMessage(payload)" in JS
    assert "openalex_api_key_required: 'trends.apiKeyRequired'" in JS
    assert "research_trends_unavailable: 'trends.loadError'" in JS
    assert "'trends.apiKeyRequired'" in I18N


def test_research_trends_has_accessible_and_responsive_presentation() -> None:
    assert 'role="tabpanel"' in HTML
    assert "#research-trends-empty[hidden]" in CSS
    assert "svg.setAttribute('hidden', '')" in JS
    assert "svg.removeAttribute('hidden')" in JS
    assert 'role="img"' in HTML
    assert '<caption class="visually-hidden">' in JS
    assert ".research-trends-chart-wrap" in CSS
    assert "@media (max-width: 700px)" in CSS
    assert "'trends.currentIncomplete'" in I18N
