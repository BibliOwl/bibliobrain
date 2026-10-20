"""Provider cascade of the article discovery, stage by stage."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

import src.article_discovery as discovery


def _run(**overrides: Any) -> discovery._ProviderCollection:
    values: dict[str, Any] = {
        "client": cast(Any, None),
        "query_payload": {"search_query": "HTS AC losses", "keywords": ["HTS", "AC losses"]},
        "abort_signal": None,
        "llm_available": False,
        "result_limit": 5,
        "candidate_limit": 10,
        "provider_stats": discovery._init_provider_stats(),
        "warnings": [],
    }
    values.update(overrides)
    return discovery._ProviderCollection(**values)


async def _no_rows(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
    return []


async def _unavailable(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
    raise RuntimeError("service down")


def _collect(run: discovery._ProviderCollection) -> None:
    asyncio.run(
        discovery._collect_provider_rows(
            run,
            notebook_mode=False,
            seed_dois=[],
            max_results=5,
            openalex_api_key_configured=True,
            crossref_mailto="",
        )
    )


def test_specialized_error_is_reported_as_fallback_before_any_row() -> None:
    run = _run()

    discovery._adopt_specialized_rows(run, "arxiv", "arXiv", [], RuntimeError("timeout"))

    assert run.warnings == ["Fallback arXiv indisponible: timeout"]
    assert run.provider_stats["arxiv"]["errors"]
    assert run.rows == []


def test_specialized_rows_become_primary_only_without_a_better_provider() -> None:
    run = _run()
    discovery._adopt_specialized_rows(run, "hal", "HAL", [{"title": "A"}], None)

    assert run.primary_provider == run.provider_used == "hal"
    assert run.warnings == ["Fallback HAL utilisé pour la recherche d'articles."]

    complemented = _run()
    complemented.set_primary("semanticscholar")
    discovery._adopt_specialized_rows(complemented, "arxiv", "arXiv", [{"title": "B"}], None)

    assert complemented.primary_provider == "semanticscholar"
    assert complemented.fallback_provider == "arxiv"
    assert complemented.warnings == []
    assert complemented.rows == [("arxiv", {"title": "B"})]


def test_cascade_falls_back_to_hal_when_primary_providers_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    crossref_calls: list[str] = []

    async def hal_rows(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [{"title": "Pertes AC dans les supraconducteurs HTS"}]

    async def crossref(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        crossref_calls.append("called")
        return []

    monkeypatch.setattr(discovery, "_semantic_search_with_retries", _no_rows)
    monkeypatch.setattr(discovery, "_openalex_search", _unavailable)
    monkeypatch.setattr(discovery, "_arxiv_search", _unavailable)
    monkeypatch.setattr(discovery, "_hal_search", hal_rows)
    monkeypatch.setattr(discovery, "_crossref_search", crossref)
    run = _run()

    _collect(run)

    assert run.primary_provider == "hal"
    assert run.providers_used == {"HAL"}
    assert "Fallback OpenAlex indisponible: service down" in run.warnings
    assert "Fallback arXiv indisponible: service down" in run.warnings
    assert "Fallback HAL utilisé pour la recherche d'articles." in run.warnings
    assert crossref_calls == []


def test_cascade_uses_crossref_only_as_last_resort(monkeypatch: pytest.MonkeyPatch) -> None:
    async def crossref_rows(*_args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["mailto"] == ""
        return [{"title": "HTS AC losses"}]

    for name in ("_semantic_search_with_retries", "_openalex_search", "_arxiv_search", "_hal_search"):
        monkeypatch.setattr(discovery, name, _no_rows)
    monkeypatch.setattr(discovery, "_crossref_search", crossref_rows)
    run = _run()

    _collect(run)

    assert run.primary_provider == run.provider_used == "crossref"
    assert run.providers_used == {"Crossref"}
    assert run.rows == [("crossref", {"title": "HTS AC losses"})]
    assert any(warning.startswith("Fallback Crossref utilisé") for warning in run.warnings)


def test_snowballing_adds_recommended_papers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def recommendations(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "paperId": "snow-1",
                "title": "HTS AC losses in coated conductors",
                "abstract": "AC losses of HTS coated conductors.",
                "externalIds": {"DOI": "10.1000/snow-1"},
                "year": 2023,
            }
        ]

    async def no_enrichment(*_args: Any, **_kwargs: Any) -> int:
        return 0

    monkeypatch.setattr(discovery, "_strong_snowball_seed_dois", lambda papers: ["10.1000/seed"])
    monkeypatch.setattr(discovery, "_semantic_recommendations_for_dois", recommendations)
    monkeypatch.setattr(discovery, "_enrich_missing_abstracts", no_enrichment)
    run = _run()
    seed = {"title": "Seed paper", "doi": "10.1000/seed", "paper_id": "seed"}

    papers, added = asyncio.run(
        discovery._snowball_discovery_papers(run, [seed], mailto="", enrich_cap=10)
    )

    assert added == 1
    assert any(paper.get("doi") == "10.1000/snow-1" for paper in papers)
    assert run.providers_used == {"Semantic Scholar"}
    assert run.warnings == ["1 article(s) ajoutés via graphe de citations (snowballing)."]


def test_snowballing_rate_limit_is_recorded_without_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def rate_limited(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(discovery, "_strong_snowball_seed_dois", lambda papers: ["10.1000/seed"])
    monkeypatch.setattr(discovery, "_semantic_recommendations_for_dois", rate_limited)
    run = _run()
    seed = {"title": "Seed paper", "doi": "10.1000/seed"}

    papers, added = asyncio.run(
        discovery._snowball_discovery_papers(run, [seed], mailto="", enrich_cap=10)
    )

    assert (papers, added) == ([seed], 0)
    assert run.rate_limited is True
