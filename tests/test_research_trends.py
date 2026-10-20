from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from src import research_trends
from src.api_models import ResearchTrendsRequest, ResearchTrendTopic


def test_research_trends_uses_grouped_counts_and_normalizes_comparisons(monkeypatch) -> None:
    current_year = datetime.now(UTC).year
    start = current_year - 2
    calls: list[dict] = []

    async def fake_get(_client, _url, *, params, api_name):
        calls.append(dict(params))
        assert api_name == "openalex"
        query = params.get("search", "")
        if query == "alpha":
            counts = [(start, 10), (start + 1, 20), (current_year, 5)]
        elif query == "beta":
            counts = [(start, 5), (start + 1, 40), (current_year, 1)]
        else:
            counts = [(start, 1_000), (start + 1, 1_000), (current_year, 500)]
        return {
            "meta": {"count": sum(count for _year, count in counts)},
            "group_by": [
                {"key": str(year), "key_display_name": str(year), "count": count}
                for year, count in counts
            ],
        }

    monkeypatch.setattr(research_trends, "_openalex_api_key", lambda: "test-key")
    monkeypatch.setattr(research_trends, "get_cached_response", lambda _key: None)
    monkeypatch.setattr(research_trends, "set_cached_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(research_trends, "_get_json_with_retry", fake_get)

    payload = asyncio.run(
        research_trends.build_research_trends(
            topics=[
                {"label": "Alpha", "query": "alpha"},
                {"label": "Beta", "query": "beta"},
            ],
            from_year=start,
            to_year=current_year,
            work_types=["article", "review"],
        )
    )

    assert payload["ok"] is True
    assert len(calls) == 3
    assert all(call["group_by"] == "publication_year" for call in calls)
    assert all("type:article|review" in call["filter"] for call in calls)
    alpha_points = payload["series"][0]["points"]
    beta_points = payload["series"][1]["points"]
    assert alpha_points[1]["share_per_million"] == 20_000
    assert alpha_points[1]["index"] == 50
    assert beta_points[1]["index"] == 100
    assert alpha_points[-1]["incomplete"] is True
    assert payload["series"][1]["stats"]["peak_year"] == start + 1
    assert payload["meta"]["current_year_incomplete"] is True


def test_research_trends_zero_fills_missing_years(monkeypatch) -> None:
    current_year = datetime.now(UTC).year

    async def fake_get(_client, _url, *, params, api_name):
        del api_name
        if params.get("search"):
            return {"meta": {"count": 4}, "group_by": [{"key": str(current_year), "count": 4}]}
        return {"meta": {"count": 100}, "group_by": [{"key": str(current_year), "count": 100}]}

    monkeypatch.setattr(research_trends, "_openalex_api_key", lambda: "test-key")
    monkeypatch.setattr(research_trends, "get_cached_response", lambda _key: None)
    monkeypatch.setattr(research_trends, "set_cached_response", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(research_trends, "_get_json_with_retry", fake_get)

    payload = asyncio.run(
        research_trends.build_research_trends(
            topics=[{"label": "Topic", "query": "topic"}],
            from_year=current_year - 2,
            to_year=current_year,
            work_types=["article"],
        )
    )

    assert [point["count"] for point in payload["series"][0]["points"]] == [0, 0, 4]


def test_research_trends_marks_cached_payload(monkeypatch) -> None:
    cached: dict[str, Any] = {"ok": True, "series": [], "meta": {"cached": False}}
    monkeypatch.setattr(research_trends, "_openalex_api_key", lambda: "test-key")
    monkeypatch.setattr(research_trends, "get_cached_response", lambda _key: cached)

    payload = asyncio.run(
        research_trends.build_research_trends(
            topics=[{"label": "Topic", "query": "topic"}],
            from_year=2020,
            to_year=2021,
            work_types=["article"],
        )
    )

    assert payload["meta"]["cached"] is True
    assert cached["meta"]["cached"] is False


def test_research_trends_requires_openalex_key(monkeypatch) -> None:
    monkeypatch.setattr(research_trends, "_openalex_api_key", lambda: "")
    with pytest.raises(research_trends.OpenAlexConfigurationError):
        asyncio.run(
            research_trends.build_research_trends(
                topics=[{"label": "Topic", "query": "topic"}],
                from_year=2020,
                to_year=2021,
                work_types=["article"],
            )
        )


def test_research_trends_request_rejects_reversed_years() -> None:
    with pytest.raises(ValueError):
        ResearchTrendsRequest(
            topics=[ResearchTrendTopic(label="Topic", query="topic")],
            from_year=2022,
            to_year=2020,
            privacy_ack=True,
        )
