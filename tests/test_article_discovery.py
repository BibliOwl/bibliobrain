from __future__ import annotations

import asyncio
import importlib
import time
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import src.config as config


@pytest.fixture(autouse=True)
def _offline_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep these tests off the internet: a request no test faked gets a 404 at once.

    Without it, abstract enrichment looked up the tests' made-up DOIs on Semantic
    Scholar, OpenAlex and Crossref.
    """

    async def offline_send(
        self: httpx.AsyncClient, request: httpx.Request, **_kwargs: Any
    ) -> httpx.Response:
        return httpx.Response(404, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", offline_send)


def test_pdf_download_rejects_loopback_target() -> None:
    from src.article_discovery import _validate_public_download_url

    with pytest.raises(ValueError, match="locales ou privees"):
        _validate_public_download_url("http://127.0.0.1/private.pdf")


def test_pdf_download_connects_to_the_validated_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second DNS answer (DNS rebinding) must not change where the download goes."""
    import socket

    import src.article_discovery as discovery

    answers = iter(["93.184.216.34", "127.0.0.1"])

    def rebinding_getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        address = next(answers, "127.0.0.1")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(discovery.socket, "getaddrinfo", rebinding_getaddrinfo)
    seen: list[httpx.Request] = []

    async def recording_send(
        self: httpx.AsyncClient, request: httpx.Request, **_kwargs: Any
    ) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, request=request, content=b"%PDF-1.7\n")

    monkeypatch.setattr(httpx.AsyncClient, "send", recording_send)

    content = asyncio.run(discovery._download_binary("https://papers.example.org/a.pdf?x=1"))

    assert content == b"%PDF-1.7\n"
    assert len(seen) == 1
    assert str(seen[0].url) == "https://93.184.216.34/a.pdf?x=1"
    assert seen[0].headers["host"] == "papers.example.org"
    assert seen[0].extensions["sni_hostname"] == "papers.example.org"


def test_pdf_download_revalidates_each_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    import src.article_discovery as discovery

    def getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        address = "10.0.0.5" if host == "intranet.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(discovery.socket, "getaddrinfo", getaddrinfo)

    async def redirecting_send(
        self: httpx.AsyncClient, request: httpx.Request, **_kwargs: Any
    ) -> httpx.Response:
        return httpx.Response(
            302, request=request, headers={"location": "http://intranet.example/secret.pdf"}
        )

    monkeypatch.setattr(httpx.AsyncClient, "send", redirecting_send)

    with pytest.raises(ValueError, match="locales ou privees"):
        asyncio.run(discovery._download_binary("https://papers.example.org/a.pdf"))


def test_pinned_download_request_keeps_port_and_brackets_ipv6() -> None:
    from src.article_discovery import _pinned_download_request

    url, headers, extensions = _pinned_download_request(
        "http://papers.example.org:8080/a.pdf", "2606:2800:220:1::1"
    )

    assert url == "http://[2606:2800:220:1::1]:8080/a.pdf"
    assert headers == {"Host": "papers.example.org:8080"}
    assert extensions == {}


def test_pdf_download_skips_zotero_connector_when_disabled(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    async def fake_download(_url: str) -> bytes:
        return b"%PDF-1.7\n" + (b"x" * 200)

    async def unexpected_zotero_add(**_kwargs: Any) -> tuple[bool, str]:
        raise AssertionError("Zotero connector must not be called")

    discovery._download_binary = fake_download
    discovery._attempt_local_zotero_connector_add = unexpected_zotero_add

    payload = asyncio.run(
        discovery.download_and_store_paper_pdf(
            title="Article sans Zotero",
            doi="10.1000/test",
            pdf_url="https://example.org/article.pdf",
            add_to_zotero=False,
        )
    )

    assert payload["ok"] is True
    assert payload["added_to_zotero"] is False
    assert payload["zotero_msg"] == ""
    assert payload["msg"] == "PDF telecharge localement."


async def _fake_discovery_rerank(
    papers: list[dict[str, Any]],
    *,
    input_text: str,
    query_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    return papers, {"applied": False, "candidates": len(papers)}


async def _fake_discovery_dense(
    papers: list[dict[str, Any]],
    *,
    input_text: str,
    query_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    # Hermetic default: never reaches a real embedding backend so existing
    # ``discover_articles`` tests keep their legacy lexical ordering.
    return papers, {"applied": False, "candidates": len(papers)}


async def _fake_recommendations(
    client: Any,
    seed_dois: list[str],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    # Hermetic default: snowballing makes no network calls in tests unless a
    # test opts in by reassigning ``_semantic_recommendations_for_dois``.
    return []


def _load_article_discovery_module(workspace_tmp_dir: Path) -> Any:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    config.STORAGE_DIR = str(storage_dir)
    config.DATA_PATH = str(data_dir)

    for name in ["src.analysis_cache_db", "src.library", "src.article_discovery"]:
        module = cast(Any, importlib.import_module(name))
        cast(Any, importlib.reload(module))

    discovery = cast(Any, importlib.import_module("src.article_discovery"))
    # Stash the real implementations (reload re-creates them) so unit tests can
    # exercise them directly, then install hermetic fakes for the heavy paths.
    discovery._real_dense_rank_discovery_papers = discovery._dense_rank_discovery_papers
    discovery._rerank_discovery_papers = _fake_discovery_rerank
    discovery._dense_rank_discovery_papers = _fake_discovery_dense
    discovery._semantic_recommendations_for_dois = _fake_recommendations
    # DOI lookups for missing abstracts retry each 404 with a backoff (~3 s per
    # DOI); the enrichment tests call the real one through this name.
    discovery._real_enrich_missing_abstracts = discovery._enrich_missing_abstracts
    discovery._enrich_missing_abstracts = _fake_no_abstract_enrichment
    return discovery


async def _fake_no_abstract_enrichment(*args: Any, **kwargs: Any) -> int:
    return 0


async def _fake_empty_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return []


def test_notebook_discovery_profile_uses_notes_sources_and_existing_articles(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    discovery.get_notebook = lambda _nb_id: {
        "id": "nb-1",
        "title": "HTS magnets",
        "notes": "Comparer les formulations H et T-A pour les pertes AC.",
        "sources": [
            {
                "title": "H-formulation for HTS coils",
                "source_path": "papers/h-formulation.pdf",
                "content": "This paper studies AC losses in superconducting coils. DOI 10.1000/hts-1",
            },
            {
                "title": "T-A formulation benchmark",
                "source_path": "",
                "content": "A COMSOL benchmark for the T-A formulation.",
            },
        ],
    }
    discovery.get_item_citations = lambda _path: {"doi": "10.1000/hts-1"}

    profile = discovery._build_notebook_discovery_profile("nb-1")

    assert profile["id"] == "nb-1"
    assert profile["source_count"] == 2
    assert profile["seed_dois"] == ["10.1000/hts-1"]
    assert "Notes et axes de travail" in profile["context_text"]
    assert "H-formulation for HTS coils" in profile["context_text"]
    assert "T-A formulation benchmark" in profile["context_text"]
    assert "doi:10.1000/hts-1" in profile["existing_keys"]
    assert "title:h-formulation for hts coils" in profile["existing_keys"]


def test_notebook_discovery_merges_topic_search_and_doi_graph_then_excludes_existing(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    seen_inputs: list[str] = []

    discovery._build_notebook_discovery_profile = lambda _nb_id: {
        "id": "nb-1",
        "title": "HTS magnets",
        "summary": "AC-loss modelling in HTS magnets.",
        "source_count": 1,
        "profiled_source_count": 1,
        "seed_dois": ["10.1000/existing"],
        "existing_keys": ["doi:10.1000/existing"],
        "context_text": "Notebook corpus about HTS magnet AC-loss modelling.",
    }

    async def fake_query(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        seen_inputs.append(input_text)
        return {
            "search_query": "HTS magnet AC loss modelling",
            "search_queries": ["HTS magnet AC loss modelling"],
            "keywords": ["HTS", "magnet", "AC loss"],
            "topic_summary": "A shared focus on AC-loss models for HTS magnets.",
            "generator": "llm_local",
        }

    def raw_paper(index: int, doi: str) -> dict[str, Any]:
        return {
            "paperId": f"p-{index}",
            "title": f"HTS magnet AC loss modelling study {index}",
            "abstract": "A detailed experimental and numerical study of AC losses in HTS magnets.",
            "authors": [{"name": "Ada"}],
            "year": 2024,
            "externalIds": {"DOI": doi},
            "openAccessPdf": {"url": ""},
            "url": f"https://example.org/{index}",
        }

    async def fake_search(client: Any, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        return [raw_paper(0, "10.1000/existing")] + [
            raw_paper(index, f"10.2000/new-{index}") for index in range(1, 12)
        ]

    async def fake_recommendations(
        client: Any, seed_dois: list[str], *, limit: int = 12
    ) -> list[dict[str, Any]]:
        return [raw_paper(20, "10.3000/graph-neighbour")]

    async def fake_validation(*args: Any, **kwargs: Any) -> None:
        for paper in args[0]:
            paper["llm_relevance_score"] = 90
            paper["llm_rejected"] = False
            paper["validated_score"] = 90

    discovery._generate_query_payload_with_llm = fake_query
    discovery._semantic_search = fake_search
    discovery._semantic_recommendations_for_dois = fake_recommendations
    discovery._arxiv_search = _fake_empty_search
    discovery._crossref_search = _fake_empty_search
    discovery._hal_search = _fake_empty_search
    discovery._attach_llm_relevance_validation = fake_validation

    response = asyncio.run(
        discovery.discover_articles(
            mode="notebook",
            input_text="prioriser COMSOL",
            model_name="dummy-model",
            notebook_id="nb-1",
            max_results=12,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    dois = {str(result.get("doi") or "") for result in response["results"]}
    assert response["mode"] == "notebook"
    assert "Notebook corpus" in seen_inputs[0]
    assert "10.1000/existing" not in dois
    assert "10.3000/graph-neighbour" in dois
    assert response["meta"]["notebook_profile"]["excluded_result_count"] == 1
    assert response["meta"]["notebook_profile"]["summary"].startswith("A shared focus")


def test_openalex_abstract_is_reconstructed_from_inverted_index(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    raw = {
        "id": "https://openalex.org/W123",
        "display_name": "AC losses in HTS",
        "doi": "https://doi.org/10.1000/example",
        "publication_year": 2024,
        "authorships": [],
        "primary_location": {},
        "best_oa_location": {},
        "abstract_inverted_index": {
            "losses": [1],
            "AC": [0],
            "HTS": [3],
            "in": [2],
        },
        "cited_by_count": 12,
    }

    paper = discovery._normalize_openalex_work(raw)

    assert paper["abstract"] == "AC losses in HTS"


def test_openalex_title_strips_inline_markup(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    paper = discovery._normalize_openalex_work(
        {
            "id": "https://openalex.org/W-title",
            "display_name": "Overview of <i>H</i> - Formulation",
            "authorships": [],
            "primary_location": {},
            "best_oa_location": {},
        }
    )

    assert paper["title"] == "Overview of H-Formulation"


def test_canonicalize_doi_strips_provider_prefix_and_version(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    assert (
        discovery._canonicalize_doi("https://doi.org/10.36227/techrxiv.11635320.v1")
        == "10.36227/techrxiv.11635320"
    )


def test_missing_abstract_is_made_explicit_for_prompt_and_preview(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    paper = {"title": "No abstract paper", "abstract": ""}

    assert (
        discovery._paper_abstract_for_prompt(paper, limit=100)
        == "[Abstract unavailable in source metadata]"
    )
    assert discovery._paper_abstract_preview(paper) == discovery._MISSING_ABSTRACT_PREVIEW


def test_enrich_missing_abstracts_uses_doi_lookup(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    paper: dict[str, Any] = {
        "title": "HTS AC losses in coils",
        "abstract": "",
        "doi": "10.1000/example",
        "authors": [],
        "provider_sources": ["OpenAlex"],
        "provider_support_count": 1,
        "citation_count": 0,
        "influential_citation_count": 0,
    }

    async def fake_semantic_paper_by_doi(client: Any, doi: str) -> dict[str, Any] | None:
        return {
            "paperId": "p1",
            "title": "HTS AC losses in coils",
            "abstract": "This paper compares H-formulation and finite-element strategies for AC losses in HTS coils.",
            "authors": [{"name": "Ada"}],
            "year": 2024,
            "externalIds": {"DOI": doi},
            "openAccessPdf": {"url": ""},
            "venue": "Journal A",
            "url": "https://example.org/p1",
            "citationCount": 5,
            "influentialCitationCount": 1,
        }

    async def fake_crossref_work_by_doi(
        client: Any,
        doi: str,
        *,
        mailto: str = "",
    ) -> dict[str, Any] | None:
        return None

    discovery._semantic_paper_by_doi = fake_semantic_paper_by_doi
    discovery._crossref_work_by_doi = fake_crossref_work_by_doi

    enriched = asyncio.run(
        discovery._real_enrich_missing_abstracts(
            [paper],
            object(),
            mailto="",
            max_papers=3,
        )
    )

    assert enriched == 1
    assert "H-formulation" in paper["abstract"]
    assert paper["provider_support_count"] == 2


def test_scoring_still_anchors_on_title_when_abstract_is_missing(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    paper = {
        "title": "HTS AC losses in rotating machines",
        "abstract": "",
        "year": "2024",
        "citation_count": 20,
        "influential_citation_count": 3,
        "open_access_pdf": "",
    }
    query_payload = {"keywords": ["HTS", "rotating machines"]}

    scored = discovery._score_discovery_paper(paper, query_payload)

    assert scored["has_query_anchor"] is True
    assert "hts" in [str(item).lower() for item in scored["matched_keywords"]]
    assert scored["discovery_score"] > 0


def test_scoring_rewards_abstract_rich_matches(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    query_payload = discovery._build_heuristic_query_payload(
        "Formulations to model AC losses in HTS magnets with COMSOL"
    )
    sparse = {
        "title": "HTS magnets overview",
        "abstract": "",
        "year": "2024",
        "citation_count": 5,
        "influential_citation_count": 1,
        "open_access_pdf": "",
    }
    rich = {
        "title": "HTS magnets overview",
        "abstract": "This work compares formulations for modeling AC losses in HTS magnets with COMSOL finite-element simulations.",
        "year": "2024",
        "citation_count": 5,
        "influential_citation_count": 1,
        "open_access_pdf": "",
    }

    sparse_scored = discovery._score_discovery_paper(sparse, query_payload)
    rich_scored = discovery._score_discovery_paper(rich, query_payload)

    assert rich_scored["abstract_coverage"] >= 2
    assert rich_scored["discovery_score"] > sparse_scored["discovery_score"]


def test_query_planner_payload_adds_facet_queries(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    payload = discovery._build_query_payload_from_plan(
        "How to reduce AC losses in HTS rotating machines?",
        {
            "intent": "method search",
            "answer_hypothesis": "AC losses can be reduced through conductor design and operating constraints.",
            "keywords": ["HTS", "AC losses", "rotating machines"],
            "dois": [],
            "search_query": "HTS AC losses rotating machines",
            "search_queries": ["superconducting rotating machine AC loss"],
            "facets": [
                {
                    "label": "design",
                    "query": "filamentation HTS AC loss",
                    "keywords": ["filamentation"],
                },
                {
                    "label": "application",
                    "query": "HTS transformer AC loss",
                    "keywords": ["transformer"],
                },
            ],
        },
    )

    assert payload["generator"] == "llm_query_planner"
    assert payload["search_query"] == "HTS AC losses rotating machines"
    assert "filamentation HTS AC loss" in payload["search_queries"]
    assert "HTS transformer AC loss" in payload["search_queries"]
    assert payload["query_plan"]["intent"] == "method search"


def test_heuristic_query_payload_normalizes_french_technical_prompt(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    payload = discovery._build_heuristic_query_payload(
        "je cherche les différentes formulations pour modéliser les pertes AC dans les aimants HTS, en particulier sur COMSOL"
    )

    assert payload["generator"] == "heuristic"
    assert "cherche" not in payload["keywords"]
    assert "liser" not in payload["keywords"]
    assert "comsol" in [str(item).lower() for item in payload["keywords"]]
    assert any("COMSOL" in query for query in payload["search_queries"])
    assert any("modeling" in query.lower() for query in payload["search_queries"])
    assert any("HTS" in query for query in payload["search_queries"])


def test_heuristic_query_payload_generalizes_beyond_hts_terms(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    payload = discovery._build_heuristic_query_payload(
        "Je cherche des formulations de simulation thermique pour batteries lithium-ion sous COMSOL"
    )

    queries = [str(query) for query in payload["search_queries"]]
    joined = " | ".join(queries).lower()
    assert payload["generator"] == "heuristic"
    assert "cherche" not in payload["keywords"]
    assert "comsol" in joined
    assert "simulation" in joined or "modeling" in joined
    assert "batter" in joined


def test_heuristic_query_payload_builds_query_facets_and_variants(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    payload = discovery._build_heuristic_query_payload(
        "Comparer des formulations numeriques pour simuler des bobines supraconductrices sur COMSOL"
    )

    assert payload["query_facets"]
    assert payload["facet_keywords"]["topic"]
    assert payload["facet_keywords"]["method"]
    assert payload["facet_keywords"]["tool"] == ["COMSOL"]
    assert len(payload["search_queries"]) >= 2


def test_heuristic_query_variants_prioritize_distinct_facets(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    payload = discovery._build_heuristic_query_payload(
        "Formulations pour modeliser les pertes AC dans les aimants HTS avec COMSOL"
    )
    queries = [str(query) for query in payload["search_queries"]]

    assert len(queries) == 4
    assert len({query.lower() for query in queries}) == 4
    assert any(query.startswith("formulations modeling") for query in queries)
    assert any(query.startswith("COMSOL") for query in queries)
    assert all("AC HTS AC" not in query for query in queries)


def test_heuristic_query_payload_drops_question_fillers_and_keeps_domain_terms(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    payload = discovery._build_heuristic_query_payload(
        "je cherche comment modeliser tous les types de pertes dans les aimants HTS"
    )

    lowered_keywords = [str(value).lower() for value in payload["keywords"]]
    assert "comment" not in lowered_keywords
    assert "types" not in lowered_keywords
    assert "tout" not in lowered_keywords
    assert "aimants" in lowered_keywords
    assert "hts" in lowered_keywords
    assert payload["domain_anchor_terms"] == ["HTS", "magnets"]


def test_strict_query_anchor_mode_drops_openalex_like_noise(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    papers = [
        {
            "title": "High-citation paper about unrelated catalysts",
            "abstract": "",
            "year": "2025",
            "citation_count": 250,
            "influential_citation_count": 0,
            "open_access_pdf": "",
        },
        {
            "title": "Another unrelated review",
            "abstract": "General overview about catalysts and battery materials.",
            "year": "2024",
            "citation_count": 120,
            "influential_citation_count": 0,
            "open_access_pdf": "",
        },
    ]

    ranked, dropped = discovery._rank_discovery_papers(
        papers,
        {"keywords": ["HTS", "AC losses", "rotating machines"]},
        require_query_anchor=True,
    )

    assert ranked == []
    assert dropped == 2


def test_crossref_strict_mode_rejects_isolated_word_matches(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    query_payload = discovery._build_heuristic_query_payload(
        "xyzzqv nonexistent quasiparticle banana 918273645"
    )
    papers = [
        {
            "title": "Nonexistent Objects",
            "abstract": "A philosophical analysis of nonexistent objects.",
            "year": "2024",
            "citation_count": 120,
            "influential_citation_count": 4,
            "open_access_pdf": "",
        },
        {
            "title": "Nonexistent quasiparticles in a synthetic banana lattice",
            "abstract": "We study quasiparticle signatures in a banana-shaped synthetic lattice.",
            "year": "2025",
            "citation_count": 1,
            "influential_citation_count": 0,
            "open_access_pdf": "",
        },
    ]

    ranked, dropped = discovery._rank_discovery_papers(
        papers,
        query_payload,
        require_query_anchor=True,
        minimum_domain_anchor_coverage=2,
    )

    assert dropped == 1
    assert [paper["title"] for paper in ranked] == [
        "Nonexistent quasiparticles in a synthetic banana lattice"
    ]
    assert ranked[0]["domain_anchor_coverage"] >= 2


def test_crossref_strict_mode_preserves_short_technical_queries(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    query_payload = discovery._build_heuristic_query_payload("REBCO AC losses")
    papers = [
        {
            "title": "AC losses in REBCO superconducting tapes",
            "abstract": "Measurements and models of alternating-current losses in coated conductors.",
            "year": "2025",
            "citation_count": 8,
            "influential_citation_count": 1,
            "open_access_pdf": "",
        }
    ]

    ranked, dropped = discovery._rank_discovery_papers(
        papers,
        query_payload,
        require_query_anchor=True,
    )

    assert dropped == 0
    assert [paper["title"] for paper in ranked] == ["AC losses in REBCO superconducting tapes"]


def test_scoring_penalizes_document_noise_and_missing_topic_facets(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    query_payload = discovery._build_heuristic_query_payload(
        "Formulations de modelisation pour pertes AC dans des aimants HTS avec COMSOL"
    )

    scored = discovery._score_discovery_paper(
        {
            "title": "Editorial: COMSOL workflow for generic simulations",
            "abstract": "A broad overview of simulation workflows and editorial perspectives.",
            "year": "2025",
            "citation_count": 40,
            "influential_citation_count": 0,
            "open_access_pdf": "",
        },
        query_payload,
    )

    assert scored["has_query_anchor"] is False
    assert scored["negative_penalty"] >= 20
    assert scored["discovery_score"] < 0


def test_scoring_rejects_off_topic_fallback_noise_for_hts_magnets_query(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    query_payload = discovery._build_heuristic_query_payload(
        "je cherche comment modeliser tous les types de pertes dans les aimants HTS"
    )

    scored = discovery._score_discovery_paper(
        {
            "title": "The sustainable role of agricultural residues in future bioeconomy strategies",
            "abstract": (
                "This article examines agricultural residues, soil carbon returns, "
                "and bioeconomy pathways for arable land."
            ),
            "year": "2023",
            "citation_count": 0,
            "influential_citation_count": 0,
            "open_access_pdf": "https://example.org/paper.pdf",
        },
        query_payload,
    )

    assert scored["domain_anchor_required"] is True
    assert scored["matched_domain_keywords"] == []
    assert scored["has_query_anchor"] is False
    assert scored["negative_penalty"] >= 30
    assert scored["discovery_score"] < 0


def test_rank_discovery_papers_prefers_hts_domain_anchor_over_generic_loss_matches(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    query_payload = discovery._build_heuristic_query_payload(
        "je cherche comment modeliser tous les types de pertes dans les aimants HTS"
    )
    papers = [
        {
            "title": "Modeling AC losses in high-temperature superconducting magnets",
            "abstract": (
                "This paper compares formulations for AC losses in HTS magnets "
                "with finite-element simulations."
            ),
            "year": "2024",
            "citation_count": 12,
            "influential_citation_count": 1,
            "open_access_pdf": "",
        },
        {
            "title": "Does Gamification Work? A Literature Review of Empirical Studies",
            "abstract": "A review of empirical studies on gamification and motivation.",
            "year": "2014",
            "citation_count": 4759,
            "influential_citation_count": 0,
            "open_access_pdf": "",
        },
    ]

    ranked, dropped = discovery._rank_discovery_papers(papers, query_payload, require_query_anchor=True)

    assert dropped == 1
    assert len(ranked) == 1
    assert ranked[0]["title"] == "Modeling AC losses in high-temperature superconducting magnets"
    assert ranked[0]["matched_domain_keywords"] == ["HTS", "magnets"]


def test_select_diverse_top_papers_avoids_near_duplicates(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    papers = [
        {
            "title": "HTS AC losses in rotating machines",
            "venue": "Journal A",
            "validated_score": 92,
        },
        {
            "title": "HTS AC losses in rotating machine design",
            "venue": "Journal A",
            "validated_score": 90,
        },
        {
            "title": "Finite-element formulation for superconducting coils",
            "venue": "Journal B",
            "validated_score": 86,
        },
    ]

    selected = discovery._select_diverse_top_papers(papers, limit=2)

    assert len(selected) == 2
    titles = [paper["title"] for paper in selected]
    assert "Finite-element formulation for superconducting coils" in titles


def test_select_diverse_top_papers_prioritizes_llm_validated_candidates(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    papers = [
        {
            "title": "Validated direct match",
            "venue": "Journal A",
            "llm_relevance_score": 90,
            "validated_score": 90,
            "discovery_score": 70,
        },
        {
            "title": "Unvalidated lexical match",
            "venue": "Journal B",
            "llm_relevance_score": None,
            "discovery_score": 500,
        },
    ]

    selected = discovery._select_diverse_top_papers(papers, limit=1)

    assert selected[0]["title"] == "Validated direct match"


def test_merge_paper_records_tracks_provider_support(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    left = discovery._normalize_paper(
        {
            "paperId": "p1",
            "title": "HTS AC losses",
            "abstract": "",
            "authors": [{"name": "Ada"}],
            "year": 2024,
            "externalIds": {"DOI": "10.1000/example"},
            "openAccessPdf": {"url": ""},
            "venue": "Journal A",
            "url": "https://example.org/a",
            "citationCount": 7,
            "influentialCitationCount": 1,
        }
    )
    right = discovery._normalize_openalex_work(
        {
            "id": "https://openalex.org/W1",
            "display_name": "HTS AC losses",
            "doi": "https://doi.org/10.1000/example",
            "publication_year": 2024,
            "authorships": [{"author": {"display_name": "Bob"}}],
            "primary_location": {"source": {"display_name": "Journal B"}},
            "best_oa_location": {"pdf_url": "https://example.org/a.pdf"},
            "abstract_inverted_index": {"HTS": [0], "AC": [1], "losses": [2]},
            "cited_by_count": 12,
        }
    )

    merged = discovery._merge_paper_records(left, right)

    assert merged["provider_support_count"] == 2
    assert "Semantic Scholar" in merged["provider_sources"]
    assert "OpenAlex" in merged["provider_sources"]
    assert merged["open_access_pdf"] == "https://example.org/a.pdf"
    assert merged["citation_count"] == 12


def test_semantic_search_builds_expected_request(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {}

    async def fake_get_json_with_retry(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "semanticscholar",
        retries: int = 2,
    ) -> dict[str, Any]:
        captured["client"] = client
        captured["url"] = url
        captured["params"] = params
        captured["api_name"] = api_name
        captured["retries"] = retries
        return {"data": [{"paperId": "p1", "title": "HTS paper"}]}

    discovery._get_json_with_retry = fake_get_json_with_retry
    client = object()

    rows = asyncio.run(discovery._semantic_search(client, "HTS AC losses", limit=7))

    assert rows == [{"paperId": "p1", "title": "HTS paper"}]
    assert captured["client"] is client
    assert captured["url"] == f"{discovery._S2_BASE}/graph/v1/paper/search"
    assert captured["api_name"] == "semanticscholar"
    assert captured["params"]["query"] == "HTS AC losses"
    assert captured["params"]["limit"] == 7
    assert "citationCount" in captured["params"]["fields"]


def test_openalex_search_builds_expected_request(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {}

    async def fake_get_json_with_retry(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "semanticscholar",
        retries: int = 2,
    ) -> dict[str, Any]:
        captured["client"] = client
        captured["url"] = url
        captured["params"] = params
        captured["api_name"] = api_name
        return {"results": [{"id": "https://openalex.org/W1", "display_name": "OA paper"}]}

    discovery._get_json_with_retry = fake_get_json_with_retry
    client = object()

    rows = asyncio.run(discovery._openalex_search(client, "HTS AC losses", limit=9))

    assert rows == [{"id": "https://openalex.org/W1", "display_name": "OA paper"}]
    assert captured["client"] is client
    assert captured["url"] == f"{discovery._OPENALEX_BASE}/works"
    assert captured["api_name"] == "openalex"
    assert captured["params"]["search"] == "HTS AC losses"
    assert captured["params"]["per-page"] == 9
    assert "abstract_inverted_index" in captured["params"]["select"]


def test_openalex_search_uses_configured_api_key_header(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    """La clé OpenAlex part en header Authorization, jamais dans l'URL."""
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {}

    async def fake_get_json_with_retry(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "semanticscholar",
        retries: int = 2,
    ) -> dict[str, Any]:
        captured["params"] = params
        captured["api_name"] = api_name
        return {"results": []}

    monkeypatch.setenv("OPENALEX_API_KEY", "oa-test-key")
    discovery._get_json_with_retry = fake_get_json_with_retry

    rows = asyncio.run(discovery._openalex_search(object(), "HTS AC losses", limit=3))

    assert rows == []
    assert "api_key" not in captured["params"]
    assert captured["api_name"] == "openalex"
    assert discovery._api_request_headers("openalex") == {
        "Authorization": "Bearer oa-test-key"
    }


def test_parse_arxiv_atom_and_normalize_pdf(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    atom = """<?xml version="1.0" encoding="utf-8"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
      <entry>
        <id>http://arxiv.org/abs/2401.12345v2</id>
        <published>2024-01-03T00:00:00Z</published>
        <updated>2024-01-04T00:00:00Z</updated>
        <title>Finite-element modeling of HTS AC losses</title>
        <summary>We compare formulations for AC losses in HTS coils.</summary>
        <author><name>Ada Lovelace</name></author>
        <arxiv:doi>10.48550/arXiv.2401.12345</arxiv:doi>
        <arxiv:primary_category term="physics.app-ph" />
        <category term="physics.app-ph" />
        <link href="http://arxiv.org/abs/2401.12345v2" rel="alternate" type="text/html"/>
        <link title="pdf" href="http://arxiv.org/pdf/2401.12345v2" rel="related" type="application/pdf"/>
      </entry>
    </feed>"""

    rows = discovery._parse_arxiv_atom(atom)
    paper = discovery._normalize_arxiv_work(rows[0])

    assert paper["paper_id"] == "arxiv:2401.12345"
    assert paper["title"] == "Finite-element modeling of HTS AC losses"
    assert paper["year"] == "2024"
    assert paper["doi"] == "10.48550/arxiv.2401.12345"
    assert paper["authors"] == ["Ada Lovelace"]
    assert paper["open_access_pdf"] == "http://arxiv.org/pdf/2401.12345v2"
    assert paper["external_source_label"] == "arXiv"


def test_arxiv_search_builds_expected_request(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {}

    async def fake_get_text_with_retry(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "arxiv",
        retries: int = 2,
    ) -> str:
        captured["client"] = client
        captured["url"] = url
        captured["params"] = params
        captured["api_name"] = api_name
        return """<feed xmlns="http://www.w3.org/2005/Atom"></feed>"""

    discovery._get_text_with_retry = fake_get_text_with_retry
    client = object()

    rows = asyncio.run(discovery._arxiv_search(client, "HTS AC losses COMSOL", limit=6))

    assert rows == []
    assert captured["client"] is client
    assert captured["url"] == discovery._ARXIV_BASE
    assert captured["api_name"] == "arxiv"
    assert captured["params"]["max_results"] == 6
    assert "all:HTS" in captured["params"]["search_query"]
    assert "all:COMSOL" in captured["params"]["search_query"]


def test_hal_search_builds_expected_request(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {}

    async def fake_get_json_with_retry(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "semanticscholar",
        retries: int = 2,
    ) -> dict[str, Any]:
        captured["client"] = client
        captured["url"] = url
        captured["params"] = params
        captured["api_name"] = api_name
        return {"response": {"docs": [{"halId_s": "hal-1", "title_s": ["HTS paper"]}]}}

    discovery._get_json_with_retry = fake_get_json_with_retry
    client = object()

    rows = asyncio.run(discovery._hal_search(client, "HTS AC losses COMSOL", limit=8))

    assert rows == [{"halId_s": "hal-1", "title_s": ["HTS paper"]}]
    assert captured["client"] is client
    assert captured["url"] == discovery._HAL_BASE
    assert captured["api_name"] == "hal"
    assert captured["params"]["rows"] == 8
    assert captured["params"]["wt"] == "json"
    assert "docType_s" in captured["params"]["fq"]
    assert "fileMain_s" in captured["params"]["fl"]


def test_hal_normalization_prefers_open_pdf_and_doi(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    paper = discovery._normalize_hal_work(
        {
            "halId_s": "hal-047",
            "uri_s": "https://hal.science/hal-047",
            "title_s": ["Modelisation des pertes AC dans les aimants HTS"],
            "en_abstract_s": ["COMSOL finite-element formulations are compared."],
            "publicationDateY_i": 2024,
            "authFullName_s": ["Ada Lovelace", "Bob Stone"],
            "journalTitle_s": "Superconductivity Journal",
            "doiId_s": "https://doi.org/10.1000/hal-example",
            "fileMain_s": "https://hal.science/hal-047/document",
        }
    )

    assert paper["paper_id"] == "hal:hal-047"
    assert paper["doi"] == "10.1000/hal-example"
    assert paper["year"] == "2024"
    assert paper["authors"] == ["Ada Lovelace", "Bob Stone"]
    assert paper["open_access_pdf"] == "https://hal.science/hal-047/document"
    assert paper["provider_sources"] == ["HAL"]


def test_direct_archive_query_budgets_are_capped(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    queries = ["q1", "q2", "q3"]

    assert discovery._provider_query_budget("arxiv", queries, llm_available=True) == ["q1", "q2"]
    assert discovery._provider_query_budget("hal", queries, llm_available=True) == ["q1", "q2"]
    assert discovery._provider_query_budget("arxiv", queries, llm_available=False) == ["q1"]
    assert discovery._provider_query_budget("hal", queries, llm_available=False) == ["q1"]


def test_discover_articles_rejects_openalex_noise_in_strict_fallback(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "HTS AC losses rotating machines",
            "search_queries": ["HTS AC losses rotating machines"],
            "keywords": ["HTS", "AC losses", "rotating machines"],
            "generator": "llm_local",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return []

    async def fake_crossref_search(
        client: Any,
        query: str,
        *,
        limit: int = 10,
        mailto: str = "",
    ) -> list[dict[str, Any]]:
        return []

    async def fake_openalex_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "https://openalex.org/W-noise-1",
                "display_name": "Battery catalyst review",
                "doi": "https://doi.org/10.1000/noise1",
                "publication_year": 2025,
                "authorships": [],
                "primary_location": {},
                "best_oa_location": {},
                "abstract_inverted_index": {
                    "battery": [0],
                    "catalyst": [1],
                    "review": [2],
                },
                "cited_by_count": 300,
            },
            {
                "id": "https://openalex.org/W-noise-2",
                "display_name": "Materials overview",
                "doi": "https://doi.org/10.1000/noise2",
                "publication_year": 2024,
                "authorships": [],
                "primary_location": {},
                "best_oa_location": {},
                "abstract_inverted_index": {
                    "general": [0],
                    "materials": [1],
                    "overview": [2],
                },
                "cited_by_count": 120,
            },
        ]

    async def fake_attach_llm_relevance_validation(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        *,
        validation_limit: int | None = None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        return None

    async def fake_attach_local_short_summaries(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        return None

    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._arxiv_search = _fake_empty_search
    discovery._crossref_search = fake_crossref_search
    discovery._hal_search = _fake_empty_search
    discovery._openalex_search = fake_openalex_search
    discovery._attach_llm_relevance_validation = fake_attach_llm_relevance_validation
    discovery._attach_local_short_summaries = fake_attach_local_short_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="How to reduce AC losses in HTS rotating machines?",
            model_name=None,
            notebook_id=None,
            max_results=5,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert response["results"] == []
    assert response["meta"]["provider_used"] == "openalex"
    assert "OpenAlex" in response["meta"]["providers_used"]
    assert any("Fallback OpenAlex utilisé" in warning for warning in response["warnings"])
    assert any("trop peu fiable" in warning for warning in response["warnings"])


def test_discover_articles_expands_candidate_pool_for_llm_reranking(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {"semantic_limits": [], "validation_calls": []}

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "graph neural networks molecules",
            "search_queries": ["graph neural networks molecules"],
            "keywords": ["graph", "neural networks", "molecules"],
            "generator": "llm_local",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        captured["semantic_limits"].append(limit)
        rows = []
        for idx in range(limit):
            rows.append(
                {
                    "paperId": f"p{idx}",
                    "title": f"Graph neural networks for molecules {idx}",
                    "abstract": "Graph neural networks for molecular property prediction.",
                    "authors": [{"name": "Ada"}],
                    "year": 2024,
                    "externalIds": {"DOI": f"10.1000/example{idx}"},
                    "openAccessPdf": {"url": ""},
                    "venue": "Journal",
                    "url": f"https://example.org/{idx}",
                    "citationCount": 10 + idx,
                    "influentialCitationCount": 2,
                }
            )
        return rows

    async def fake_crossref_search(
        client: Any,
        query: str,
        *,
        limit: int = 10,
        mailto: str = "",
    ) -> list[dict[str, Any]]:
        return []

    async def fake_attach_llm_relevance_validation(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        *,
        validation_limit: int | None = None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        captured["validation_calls"].append(
            {"paper_count": len(papers), "validation_limit": validation_limit}
        )
        for idx, paper in enumerate(papers):
            paper["llm_relevance_score"] = 100 - idx
            paper["llm_rationale"] = "Pertinent"
            paper["llm_rejected"] = False
            paper["validated_score"] = 100 - idx

    async def fake_attach_local_short_summaries(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        return None

    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._arxiv_search = _fake_empty_search
    discovery._crossref_search = fake_crossref_search
    discovery._hal_search = _fake_empty_search
    discovery._attach_llm_relevance_validation = fake_attach_llm_relevance_validation
    discovery._attach_local_short_summaries = fake_attach_local_short_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="Find the best graph neural network papers for molecules",
            model_name="dummy-model",
            notebook_id=None,
            max_results=5,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert captured["semantic_limits"] == [20]
    assert captured["validation_calls"] == [{"paper_count": 15, "validation_limit": 15}]
    assert response["meta"]["candidate_limit"] == 20
    assert response["meta"]["validation_limit"] == 15
    assert response["meta"]["llm_reranking_enabled"] is True
    assert len(response["results"]) == 5
    assert all(result["abstract_available"] is True for result in response["results"])


def test_weighted_llm_relevance_uses_explicit_dimensions(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    query_payload = discovery._build_heuristic_query_payload(
        "Different formulations to model AC losses in HTS magnets with COMSOL"
    )

    relevance, dimensions = discovery._weighted_llm_relevance(
        {
            "topic_score": 95,
            "method_score": 88,
            "tool_score": 70,
            "directness_score": 90,
        },
        query_payload,
    )

    assert dimensions["topic"] == 95
    assert dimensions["method"] == 88
    assert dimensions["tool"] == 70
    assert relevance >= 85


def test_discover_articles_uses_configured_discovery_multipliers(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {"semantic_limits": [], "validation_calls": []}

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "urban heat island roofs",
            "search_queries": ["urban heat island roofs"],
            "keywords": ["urban", "heat island", "roofs"],
            "generator": "llm_local",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        captured["semantic_limits"].append(limit)
        rows = []
        for idx in range(limit):
            rows.append(
                {
                    "paperId": f"cfg{idx}",
                    "title": f"Urban heat island roofs {idx}",
                    "abstract": "Urban heat island mitigation with roofs.",
                    "authors": [{"name": "Ada"}],
                    "year": 2024,
                    "externalIds": {"DOI": f"10.1000/cfg{idx}"},
                    "openAccessPdf": {"url": ""},
                    "venue": "Journal",
                    "url": f"https://example.org/cfg/{idx}",
                    "citationCount": 8 + idx,
                    "influentialCitationCount": 1,
                }
            )
        return rows

    async def fake_crossref_search(
        client: Any,
        query: str,
        *,
        limit: int = 10,
        mailto: str = "",
    ) -> list[dict[str, Any]]:
        return []

    async def fake_attach_llm_relevance_validation(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        *,
        validation_limit: int | None = None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        captured["validation_calls"].append(
            {"paper_count": len(papers), "validation_limit": validation_limit}
        )

    async def fake_attach_local_short_summaries(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        return None

    discovery.get_app_settings = lambda: {
        "discovery_candidate_multiplier": 6,
        "discovery_llm_validation_multiplier": 4,
    }
    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._arxiv_search = _fake_empty_search
    discovery._crossref_search = fake_crossref_search
    discovery._hal_search = _fake_empty_search
    discovery._attach_llm_relevance_validation = fake_attach_llm_relevance_validation
    discovery._attach_local_short_summaries = fake_attach_local_short_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="Urban heat island roofs",
            model_name="dummy-model",
            notebook_id=None,
            max_results=5,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert captured["semantic_limits"] == [30]
    assert captured["validation_calls"] == [{"paper_count": 20, "validation_limit": 20}]
    assert response["meta"]["candidate_limit"] == 30
    assert response["meta"]["validation_limit"] == 20


def test_discover_articles_queries_openalex_alongside_semantic(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {"semantic_calls": 0, "openalex_calls": 0}

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "hts ac losses",
            "search_queries": ["hts ac losses", "hts modeling"],
            "keywords": ["hts", "ac losses"],
            "generator": "llm_local",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        captured["semantic_calls"] += 1
        return [
            {
                "paperId": "s1",
                "title": "HTS AC losses in coils",
                "abstract": "AC losses in superconducting coils.",
                "authors": [{"name": "Ada"}],
                "year": 2024,
                "externalIds": {"DOI": "10.1000/semantic"},
                "openAccessPdf": {"url": ""},
                "venue": "Journal A",
                "url": "https://example.org/s1",
                "citationCount": 5,
                "influentialCitationCount": 1,
            }
        ]

    async def fake_crossref_search(
        client: Any,
        query: str,
        *,
        limit: int = 10,
        mailto: str = "",
    ) -> list[dict[str, Any]]:
        return []

    async def fake_openalex_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        captured["openalex_calls"] += 1
        return [
            {
                "id": "https://openalex.org/W1",
                "display_name": "Finite-element formulation for HTS magnets",
                "doi": "https://doi.org/10.1000/openalex",
                "publication_year": 2023,
                "authorships": [{"author": {"display_name": "Bob"}}],
                "primary_location": {"source": {"display_name": "Journal B"}},
                "best_oa_location": {},
                "abstract_inverted_index": {
                    "Finite-element": [0],
                    "formulation": [1],
                    "HTS": [2],
                    "magnets": [3],
                },
                "cited_by_count": 8,
            }
        ]

    async def fake_attach_llm_relevance_validation(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        *,
        validation_limit: int | None = None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        return None

    async def fake_attach_local_short_summaries(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        return None

    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._arxiv_search = _fake_empty_search
    discovery._crossref_search = fake_crossref_search
    discovery._hal_search = _fake_empty_search
    discovery._openalex_search = fake_openalex_search
    discovery._attach_llm_relevance_validation = fake_attach_llm_relevance_validation
    discovery._attach_local_short_summaries = fake_attach_local_short_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="HTS AC losses",
            model_name="dummy-model",
            notebook_id=None,
            max_results=5,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert captured["semantic_calls"] >= 1
    assert captured["openalex_calls"] >= 1
    assert "Semantic Scholar" in response["meta"]["providers_used"]
    assert "OpenAlex" in response["meta"]["providers_used"]
    assert len(response["results"]) == 2


def test_discover_articles_adds_openalex_complement_for_multifacet_search(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {"openalex_calls": 0}

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return discovery._build_heuristic_query_payload(input_text)

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [
            {
                "paperId": f"s{idx}",
                "title": f"HTS AC losses in magnets {idx}",
                "abstract": "Modeling AC losses in HTS magnets.",
                "authors": [],
                "year": 2024,
                "externalIds": {"DOI": f"10.1000/semantic-complement-{idx}"},
                "openAccessPdf": {"url": ""},
                "venue": "Journal",
                "url": f"https://example.org/s{idx}",
            }
            for idx in range(2)
        ]

    async def fake_openalex_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        captured["openalex_calls"] += 1
        return [
            {
                "id": "https://openalex.org/W-complement",
                "display_name": "Overview of H-formulation for HTS applications",
                "doi": "https://doi.org/10.1000/openalex-complement",
                "publication_year": 2020,
                "authorships": [],
                "primary_location": {},
                "best_oa_location": {},
                "abstract_inverted_index": {
                    "HTS": [0],
                    "formulation": [1],
                    "modeling": [2],
                    "COMSOL": [3],
                },
            }
        ]

    async def fake_attach(*args: Any, **kwargs: Any) -> None:
        return None

    discovery._openalex_api_key = lambda: "configured"
    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._openalex_search = fake_openalex_search
    discovery._arxiv_search = _fake_empty_search
    discovery._hal_search = _fake_empty_search
    discovery._crossref_search = _fake_empty_search
    discovery._attach_llm_relevance_validation = fake_attach
    discovery._attach_local_short_summaries = fake_attach

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="Formulations pour les pertes AC dans les aimants HTS avec COMSOL",
            model_name="dummy-model",
            notebook_id=None,
            max_results=2,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert captured["openalex_calls"] >= 1
    assert "OpenAlex" in response["meta"]["providers_used"]
    assert any("complément méthodologique" in warning for warning in response["warnings"])


def test_discover_articles_retries_semantic_before_openalex_complement(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {"semantic_calls": 0, "openalex_calls": 0}

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "HTS AC losses COMSOL",
            "search_queries": ["HTS AC losses COMSOL"],
            "keywords": ["HTS", "AC losses", "COMSOL"],
            "generator": "heuristic",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        captured["semantic_calls"] += 1
        if captured["semantic_calls"] < 3:
            raise RuntimeError("Rate limit")
        return [
            {
                "paperId": "s-good",
                "title": "COMSOL modeling of HTS AC losses",
                "abstract": "Finite-element modeling of AC losses in HTS magnets with COMSOL.",
                "authors": [{"name": "Ada"}],
                "year": 2024,
                "externalIds": {"DOI": "10.1000/semantic-good"},
                "openAccessPdf": {"url": ""},
                "venue": "Journal A",
                "url": "https://example.org/s-good",
                "citationCount": 5,
                "influentialCitationCount": 1,
            }
        ]

    async def fake_openalex_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        captured["openalex_calls"] += 1
        return []

    async def fake_attach_summaries(*args: Any, **kwargs: Any) -> None:
        return None

    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._semantic_retry_delay_seconds = lambda attempt_index, exc: 0.0
    discovery._openalex_search = fake_openalex_search
    # The OpenAlex complement needs a configured key (never the developer's own).
    discovery._openalex_api_key = lambda: 'test-openalex-key'
    discovery._arxiv_search = _fake_empty_search
    discovery._hal_search = _fake_empty_search
    discovery._crossref_search = _fake_empty_search
    discovery._attach_local_short_summaries = fake_attach_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="HTS AC losses COMSOL",
            model_name=None,
            notebook_id=None,
            max_results=1,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert captured["semantic_calls"] == 3
    assert captured["openalex_calls"] == 1
    assert response["meta"]["provider_used"] == "semanticscholar"
    assert response["results"][0]["title"] == "COMSOL modeling of HTS AC losses"


def test_discover_articles_merges_mixed_provider_duplicates(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "hts ac losses",
            "search_queries": ["hts ac losses", "hts modeling"],
            "keywords": ["hts", "ac losses"],
            "generator": "llm_local",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [
            {
                "paperId": "p1",
                "title": "HTS AC losses in coils",
                "abstract": "",
                "authors": [{"name": "Ada"}],
                "year": 2024,
                "externalIds": {"DOI": "10.36227/techrxiv.11635320.v1"},
                "openAccessPdf": {"url": ""},
                "venue": "Journal A",
                "url": "https://example.org/p1",
                "citationCount": 4,
                "influentialCitationCount": 1,
            }
        ]

    async def fake_crossref_search(
        client: Any,
        query: str,
        *,
        limit: int = 10,
        mailto: str = "",
    ) -> list[dict[str, Any]]:
        return []

    async def fake_openalex_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "https://openalex.org/W1",
                "display_name": "HTS AC losses in coils",
                "doi": "https://doi.org/10.36227/techrxiv.11635320",
                "publication_year": 2024,
                "authorships": [{"author": {"display_name": "Bob"}}],
                "primary_location": {"source": {"display_name": "Journal B"}},
                "best_oa_location": {"pdf_url": "https://example.org/p1.pdf"},
                "abstract_inverted_index": {"HTS": [0], "AC": [1], "losses": [2], "coils": [3]},
                "cited_by_count": 9,
            }
        ]

    async def fake_attach_llm_relevance_validation(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        *,
        validation_limit: int | None = None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        return None

    async def fake_attach_local_short_summaries(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        return None

    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._arxiv_search = _fake_empty_search
    discovery._crossref_search = fake_crossref_search
    discovery._hal_search = _fake_empty_search
    discovery._openalex_search = fake_openalex_search
    # Without an OpenAlex key, no catalog complement: only the fallback providers.
    discovery._openalex_api_key = lambda: ''
    discovery._attach_llm_relevance_validation = fake_attach_llm_relevance_validation
    discovery._attach_local_short_summaries = fake_attach_local_short_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="HTS AC losses",
            model_name="dummy-model",
            notebook_id=None,
            max_results=5,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert len(response["results"]) == 1
    result = response["results"][0]
    assert result["doi"] == "10.36227/techrxiv.11635320"
    assert result["provider_support_count"] == 2
    assert result["open_access_pdf"] == "https://example.org/p1.pdf"


def test_discover_articles_does_not_mix_fallbacks_when_semantic_is_sufficient(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    calls = {"openalex": 0}

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "HTS AC losses",
            "search_queries": ["HTS AC losses"],
            "keywords": ["HTS", "AC losses"],
            "generator": "heuristic",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [
            {
                "paperId": f"s{idx}",
                "title": f"HTS AC losses paper {idx}",
                "abstract": "This article studies AC losses in HTS conductors and magnets.",
                "authors": [{"name": "Ada"}],
                "year": 2024,
                "externalIds": {"DOI": f"10.1000/sufficient{idx}"},
                "openAccessPdf": {"url": ""},
                "venue": "Journal",
                "url": f"https://example.org/s{idx}",
                "citationCount": 10,
                "influentialCitationCount": 1,
            }
            for idx in range(2)
        ]

    async def fake_openalex_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        calls["openalex"] += 1
        return []

    async def fake_attach_validation(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_attach_summaries(*args: Any, **kwargs: Any) -> None:
        return None

    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._openalex_search = fake_openalex_search
    discovery._arxiv_search = _fake_empty_search
    discovery._hal_search = _fake_empty_search
    discovery._crossref_search = _fake_empty_search
    discovery._attach_llm_relevance_validation = fake_attach_validation
    discovery._attach_local_short_summaries = fake_attach_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="HTS AC losses",
            model_name="dummy-model",
            notebook_id=None,
            max_results=2,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert calls["openalex"] == 0
    assert response["meta"]["primary_provider"] == "semanticscholar"
    assert response["meta"]["fallback_provider"] == ""
    assert "OpenAlex" not in response["meta"]["providers_used"]


def test_discover_articles_uses_openalex_as_primary_fallback_after_semantic_rate_limit(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    calls = {"crossref": 0}

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "HTS AC losses COMSOL",
            "search_queries": ["HTS AC losses COMSOL"],
            "keywords": ["HTS", "AC losses", "COMSOL"],
            "generator": "heuristic",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        raise RuntimeError("Rate limit")

    async def fake_openalex_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "https://openalex.org/W-good",
                "display_name": "COMSOL modeling of HTS AC losses",
                "doi": "https://doi.org/10.1000/openalex-good",
                "publication_year": 2024,
                "authorships": [{"author": {"display_name": "Ada"}}],
                "primary_location": {"source": {"display_name": "Journal B"}},
                "best_oa_location": {},
                "abstract_inverted_index": {
                    "COMSOL": [0],
                    "models": [1],
                    "AC": [2],
                    "losses": [3],
                    "in": [4],
                    "HTS": [5],
                    "magnets": [6],
                    "with": [7],
                    "finite": [8],
                    "element": [9],
                    "simulations": [10],
                },
                "cited_by_count": 8,
            }
        ]

    async def fake_crossref_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        calls["crossref"] += 1
        return []

    async def fake_attach_summaries(*args: Any, **kwargs: Any) -> None:
        return None

    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._semantic_retry_delay_seconds = lambda attempt_index, exc: 0.0
    discovery._openalex_api_key = lambda: ""
    discovery._openalex_search = fake_openalex_search
    discovery._arxiv_search = _fake_empty_search
    discovery._hal_search = _fake_empty_search
    discovery._crossref_search = fake_crossref_search
    discovery._attach_local_short_summaries = fake_attach_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="HTS AC losses COMSOL",
            model_name=None,
            notebook_id=None,
            max_results=1,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert response["meta"]["provider_used"] == "openalex"
    assert response["meta"]["primary_provider"] == "openalex"
    assert response["meta"]["openalex_api_key_configured"] is False
    assert response["meta"]["abstracts_enriched"] == 0
    assert calls["crossref"] == 0
    assert any("OPENALEX_API_KEY" in warning for warning in response["warnings"])
    assert any("grandement améliorer les résultats" in warning for warning in response["warnings"])
    assert response["results"][0]["title"] == "COMSOL modeling of HTS AC losses"


def test_discover_articles_reranks_before_llm_validation(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {}

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "HTS AC losses",
            "search_queries": ["HTS AC losses"],
            "keywords": ["HTS", "AC losses"],
            "generator": "heuristic",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        return [
            {
                "paperId": f"p{idx}",
                "title": title,
                "abstract": "This article studies AC losses in HTS magnets.",
                "authors": [{"name": "Ada"}],
                "year": 2024,
                "externalIds": {"DOI": f"10.1000/rerank{idx}"},
                "openAccessPdf": {"url": ""},
                "venue": "Journal",
                "url": f"https://example.org/r{idx}",
                "citationCount": 10,
                "influentialCitationCount": 1,
            }
            for idx, title in enumerate(["Paper A", "Paper B", "Paper C"])
        ]

    async def fake_rerank(
        papers: list[dict[str, Any]],
        *,
        input_text: str,
        query_payload: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ordered = list(reversed(papers))
        for idx, paper in enumerate(ordered, start=1):
            paper["reranker_rank"] = idx
            paper["reranker_score"] = float(len(ordered) - idx + 1)
        return ordered, {"applied": True, "candidates": len(papers), "top_k": len(papers)}

    async def fake_attach_validation(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        *,
        validation_limit: int | None = None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        captured["titles"] = [paper["title"] for paper in papers]
        for idx, paper in enumerate(papers):
            paper["llm_relevance_score"] = 100 - idx
            paper["validated_score"] = 100 - idx
            paper["llm_rejected"] = False

    async def fake_attach_summaries(*args: Any, **kwargs: Any) -> None:
        return None

    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._openalex_search = _fake_empty_search
    discovery._arxiv_search = _fake_empty_search
    discovery._hal_search = _fake_empty_search
    discovery._crossref_search = _fake_empty_search
    discovery._rerank_discovery_papers = fake_rerank
    discovery._attach_llm_relevance_validation = fake_attach_validation
    discovery._attach_local_short_summaries = fake_attach_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="HTS AC losses",
            model_name="dummy-model",
            notebook_id=None,
            max_results=2,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert captured["titles"] == ["Paper C", "Paper B", "Paper A"]
    assert response["meta"]["reranker_applied"] is True
    assert response["meta"]["reranker_candidates"] == 3


def test_enrich_missing_abstracts_uses_openalex_after_semantic_miss(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    calls = {"crossref": 0}
    paper: dict[str, Any] = {
        "title": "HTS AC losses in coils",
        "abstract": "",
        "doi": "10.1000/openalex-abstract",
        "authors": [],
        "provider_sources": ["Crossref"],
        "provider_support_count": 1,
        "citation_count": 0,
        "influential_citation_count": 0,
    }

    async def fake_semantic_paper_by_doi(client: Any, doi: str) -> dict[str, Any] | None:
        return None

    async def fake_openalex_work_by_doi(client: Any, doi: str) -> dict[str, Any] | None:
        return {
            "id": "https://openalex.org/W-abstract",
            "display_name": "HTS AC losses in coils",
            "doi": "https://doi.org/10.1000/openalex-abstract",
            "publication_year": 2024,
            "authorships": [],
            "primary_location": {"source": {"display_name": "Journal"}},
            "best_oa_location": {},
            "abstract_inverted_index": {
                "This": [0],
                "paper": [1],
                "compares": [2],
                "COMSOL": [3],
                "formulations": [4],
                "for": [5],
                "HTS": [6],
                "AC": [7],
                "losses": [8],
                "in": [9],
                "coils": [10],
            },
            "cited_by_count": 5,
        }

    async def fake_crossref_work_by_doi(
        client: Any,
        doi: str,
        *,
        mailto: str = "",
    ) -> dict[str, Any] | None:
        calls["crossref"] += 1
        return None

    discovery._semantic_paper_by_doi = fake_semantic_paper_by_doi
    discovery._openalex_work_by_doi = fake_openalex_work_by_doi
    discovery._crossref_work_by_doi = fake_crossref_work_by_doi

    enriched = asyncio.run(
        discovery._real_enrich_missing_abstracts(
            [paper],
            object(),
            mailto="",
            max_papers=3,
        )
    )

    assert enriched == 1
    assert "COMSOL formulations" in paper["abstract"]
    assert "OpenAlex" in paper["provider_sources"]
    assert calls["crossref"] == 0


def test_cosine_similarity_basic(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    assert discovery._cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert discovery._cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert discovery._cosine([], [1.0]) == 0.0
    assert discovery._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    # mismatched lengths fall back to the shared prefix
    assert discovery._cosine([1.0, 0.0, 5.0], [1.0, 0.0]) == 1.0


def test_query_plan_payload_includes_ideal_abstract(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    payload = discovery._build_query_payload_from_plan(
        "tests experimentaux bobines HTS MI NI conclusions",
        {
            "intent": "veille",
            "answer_hypothesis": "NI coils tolerate quench",
            "ideal_abstract": "Experimental tests of NI and MI HTS coils report quench behaviour.",
            "keywords": ["HTS", "no-insulation"],
            "search_query": "no-insulation HTS coil experimental test",
        },
    )
    assert payload["query_plan"]["ideal_abstract"].startswith("Experimental tests")
    # HyDE text used for dense retrieval must embed the ideal abstract.
    dense_text = discovery._dense_query_text("user question", payload)
    assert "Experimental tests of NI and MI HTS coils" in dense_text


def test_dense_rank_orders_by_cosine_and_soft_fails(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    import src.llm_provider as llm_provider

    class _FakeEmbeddings:
        def embed_query(self, text: str) -> list[float]:
            return [1.0, 0.0]

        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            # idx1 ("aligned") is closest to the query direction [1, 0]
            return [[0.2, 1.0], [1.0, 0.1], [0.6, 0.6]]

    monkeypatch.setattr(llm_provider, "required_embedding_model_name", lambda: "fake-embed")
    monkeypatch.setattr(llm_provider, "create_embeddings", lambda name: _FakeEmbeddings())

    papers = [
        {"title": "orthogonal", "abstract": "a"},
        {"title": "aligned", "abstract": "b"},
        {"title": "middle", "abstract": "c"},
    ]
    ranked, meta = asyncio.run(
        discovery._real_dense_rank_discovery_papers(
            papers,
            input_text="question",
            query_payload={"search_query": "question"},
        )
    )
    assert meta["applied"] is True
    assert ranked[0]["title"] == "aligned"
    assert ranked[0]["dense_rank"] == 1
    assert ranked[0]["dense_score"] >= ranked[1]["dense_score"]

    # Soft-fail: a raising backend leaves the papers untouched.
    def _boom(name: str) -> Any:
        raise RuntimeError("embedding backend down")

    monkeypatch.setattr(llm_provider, "create_embeddings", _boom)
    same, fail_meta = asyncio.run(
        discovery._real_dense_rank_discovery_papers(
            papers,
            input_text="question",
            query_payload={"search_query": "question"},
        )
    )
    assert fail_meta["applied"] is False
    assert "error" in fail_meta


def test_dense_rank_soft_fails_without_embedding_model(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    import src.llm_provider as llm_provider

    monkeypatch.setattr(llm_provider, "required_embedding_model_name", lambda: "")
    papers = [{"title": "a"}, {"title": "b"}]
    ranked, meta = asyncio.run(
        discovery._real_dense_rank_discovery_papers(
            papers,
            input_text="q",
            query_payload={"search_query": "q"},
        )
    )
    assert meta["applied"] is False
    assert ranked == papers


def test_semantic_rank_score_demotes_cited_offtopic_paper(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    on_topic: dict[str, Any] = {
        "title": "Experimental NI HTS coil tests",
        "discovery_score": 18,
        "citation_count": 3,
        "dense_score": 0.82,
        "reranker_score": 4.5,
        "llm_relevance_score": 88,
        "llm_rejected": False,
    }
    off_topic_popular: dict[str, Any] = {
        "title": "HTS flux pump",
        "discovery_score": 40,
        "citation_count": 800,
        "dense_score": 0.21,
        "reranker_score": -3.0,
        "llm_relevance_score": 30,
        "llm_rejected": True,
    }
    papers = [off_topic_popular, on_topic]
    discovery._assign_semantic_rank_scores(papers)

    assert on_topic["semantic_rank_score"] > off_topic_popular["semantic_rank_score"]
    ordered = sorted(papers, key=discovery._paper_selection_score, reverse=True)
    assert ordered[0]["title"] == "Experimental NI HTS coil tests"


def test_semantic_rank_score_ranks_judged_above_unjudged(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    judged = {
        "title": "judged",
        "discovery_score": 10,
        "dense_score": 0.4,
        "llm_relevance_score": 70,
        "llm_rejected": False,
    }
    unjudged_strong_lexical = {
        "title": "unjudged",
        "discovery_score": 500,
        "dense_score": 0.9,
        "llm_relevance_score": None,
    }
    papers = [unjudged_strong_lexical, judged]
    discovery._assign_semantic_rank_scores(papers)
    ordered = sorted(papers, key=discovery._paper_selection_score, reverse=True)
    assert ordered[0]["title"] == "judged"


def test_short_acronyms_survive_query_building(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    text = "tests experimentaux bobines HTS MI ou NI conclusions"
    keywords = discovery._keyword_fallback(text, limit=8)
    assert "mi" in keywords
    assert "ni" in keywords

    terms = [term.lower() for term in discovery._provider_query_terms("HTS NI MI coil tests")]
    assert "ni" in terms
    assert "mi" in terms

    anchors = [a.lower() for a in discovery._build_domain_anchor_terms(text, keywords)]
    assert "ni" in anchors or "mi" in anchors


def test_native_search_queries_preserve_french(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    payload = discovery._build_heuristic_query_payload(
        "bobines supraconductrices et pertes AC"
    )
    native = payload["native_search_queries"]
    assert native
    joined = " ".join(native).lower()
    # French wording is kept (not translated to coil/superconducting)
    assert "bobines" in joined
    assert "supraconductrices" in joined


def test_dense_query_text_is_english_primary(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    payload = {
        "query_plan": {"ideal_abstract": "Experimental NI HTS coil quench tests."},
        "search_query": "no-insulation HTS coil experimental tests",
        "search_queries": ["no-insulation HTS coil experimental tests"],
        "facet_keywords": {"topic": ["bobine"], "method": [], "tool": []},
    }
    text = discovery._dense_query_text("question en francais", payload)
    assert text.startswith("Experimental NI HTS coil quench tests.")
    # French facet term is canonicalized to English for the embedder
    assert "coil" in text.lower()
    # the exact user phrasing is still appended last
    assert "question en francais" in text


def test_merge_paper_lists_dedupes_and_counts(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    existing = [
        discovery._normalize_paper(
            {"paperId": "p1", "title": "A", "externalIds": {"DOI": "10.1/x"}, "authors": []}
        )
    ]
    incoming = [
        discovery._normalize_paper(
            {"paperId": "p2", "title": "A dup", "externalIds": {"DOI": "10.1/x"}, "authors": []}
        ),
        discovery._normalize_paper(
            {"paperId": "p3", "title": "B", "externalIds": {"DOI": "10.1/y"}, "authors": []}
        ),
    ]
    merged, added = discovery._merge_paper_lists(existing, incoming)
    assert added == 1
    assert len(merged) == 2
    # the duplicate was merged into the existing record (provider support grows)
    assert merged[0]["provider_support_count"] >= 1


def test_discover_articles_snowballs_citation_neighbours(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    async def fake_generate_query_payload_with_llm(
        input_text: str,
        model_name: str | None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> dict[str, Any]:
        return {
            "search_query": "no-insulation HTS coil experimental tests",
            "search_queries": ["no-insulation HTS coil experimental tests"],
            "keywords": ["HTS", "no-insulation", "coil"],
            "generator": "llm_local",
        }

    async def fake_semantic_search(
        client: Any, query: str, *, limit: int = 10
    ) -> list[dict[str, Any]]:
        rows = []
        for idx in range(12):
            rows.append(
                {
                    "paperId": f"p{idx}",
                    "title": f"No-insulation HTS coil experimental test {idx}",
                    "abstract": "Experimental quench tests of NI HTS coils with conclusions.",
                    "authors": [{"name": "Ada"}],
                    "year": 2024,
                    "externalIds": {"DOI": f"10.2000/seed{idx}"},
                    "openAccessPdf": {"url": ""},
                    "venue": "IEEE TAS",
                    "url": f"https://example.org/seed{idx}",
                    "citationCount": 5,
                    "influentialCitationCount": 1,
                }
            )
        return rows

    async def fake_recommendations(
        client: Any, seed_dois: list[str], *, limit: int = 12
    ) -> list[dict[str, Any]]:
        # one genuinely new neighbour + one duplicate of an existing seed DOI
        return [
            {
                "paperId": "rec1",
                "title": "Metal-insulation HTS coil charging test",
                "abstract": "MI HTS coil experimental characterization and conclusions.",
                "authors": [{"name": "Bo"}],
                "year": 2023,
                "externalIds": {"DOI": "10.3000/neighbour1"},
                "openAccessPdf": {"url": ""},
                "venue": "SUST",
                "url": "https://example.org/neighbour1",
                "citationCount": 9,
                "influentialCitationCount": 2,
            },
            {
                "paperId": "p0-dup",
                "title": "No-insulation HTS coil experimental test 0",
                "abstract": "Duplicate of an existing seed.",
                "authors": [{"name": "Ada"}],
                "year": 2024,
                "externalIds": {"DOI": "10.2000/seed0"},
                "openAccessPdf": {"url": ""},
                "venue": "IEEE TAS",
                "url": "https://example.org/seed0",
                "citationCount": 5,
                "influentialCitationCount": 1,
            },
        ]

    async def fake_attach_llm_relevance_validation(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        *,
        validation_limit: int | None = None,
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        for idx, paper in enumerate(papers):
            paper["llm_relevance_score"] = 90
            paper["llm_rejected"] = False
            paper["validated_score"] = 90

    async def fake_attach_local_short_summaries(
        papers: list[dict[str, Any]],
        model_name: str | None,
        query_payload: dict[str, Any],
        debug_events: list[dict[str, Any]] | None = None,
        abort_signal: Any | None = None,
    ) -> None:
        return None

    discovery._generate_query_payload_with_llm = fake_generate_query_payload_with_llm
    discovery._semantic_search = fake_semantic_search
    discovery._semantic_recommendations_for_dois = fake_recommendations
    discovery._arxiv_search = _fake_empty_search
    discovery._crossref_search = _fake_empty_search
    discovery._hal_search = _fake_empty_search
    discovery._attach_llm_relevance_validation = fake_attach_llm_relevance_validation
    discovery._attach_local_short_summaries = fake_attach_local_short_summaries

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="tests experimentaux bobines HTS MI ou NI conclusions",
            model_name="dummy-model",
            notebook_id=None,
            max_results=8,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    # one new neighbour merged, the duplicate folded into the existing seed
    assert response["meta"]["snowball_added"] == 1
    dois = {str(result.get("doi") or "") for result in response["results"]}
    assert "10.3000/neighbour1" in dois


def test_semantic_scholar_api_key_header_injection(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    assert discovery._api_request_headers("semanticscholar") is None

    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "abc123")
    assert discovery._api_request_headers("semanticscholar") == {"x-api-key": "abc123"}
    # the Semantic Scholar key is never reused for other providers
    assert discovery._api_request_headers("openalex") is None
    assert discovery._api_request_headers("arxiv") is None

    monkeypatch.setenv("OPENALEX_API_KEY", "oa-key")
    assert discovery._api_request_headers("openalex") == {"Authorization": "Bearer oa-key"}


def test_semantic_batch_resolves_many_dois_in_one_request(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    captured: dict[str, Any] = {}

    async def fake_post_json_with_retry(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        api_name: str = "semanticscholar",
        retries: int = 2,
    ) -> list[dict[str, Any]]:
        captured.update(url=url, params=params, json_body=json_body, api_name=api_name)
        return [{"paperId": "p1"}, {"paperId": "p2"}]

    discovery._post_json_with_retry = fake_post_json_with_retry
    rows = asyncio.run(
        discovery._semantic_papers_batch(
            object(),
            ["DOI:10.1000/a", "DOI:10.1000/b"],
            fields="paperId,title",
        )
    )

    assert [row["paperId"] for row in rows] == ["p1", "p2"]
    assert captured["url"].endswith("/graph/v1/paper/batch")
    assert captured["json_body"] == {"ids": ["DOI:10.1000/a", "DOI:10.1000/b"]}
    assert captured["params"] == {"fields": "paperId,title"}


def test_semantic_circuit_breaker_stops_retry_storm(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    calls = {"count": 0}

    class FakeResponse:
        status_code = 429
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {}

    class FakeClient:
        async def get(self, *args: Any, **kwargs: Any) -> FakeResponse:
            calls["count"] += 1
            return FakeResponse()

    async def no_wait(*args: Any, **kwargs: Any) -> None:
        return None

    discovery._semantic_circuit.update(
        {"consecutive_rate_limits": 0, "open_until": 0.0, "last_error": ""}
    )
    discovery._throttle = no_wait
    discovery._budgeted_sleep = no_wait

    try:
        asyncio.run(
            discovery._get_json_with_retry(
                FakeClient(),
                "https://api.semanticscholar.org/graph/v1/paper/search?unique=circuit-test",
                retries=3,
            )
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("The circuit breaker should stop the request")

    assert calls["count"] == 2
    assert discovery._semantic_circuit_status()["open"] is True


def test_discovery_wrapper_enforces_hard_timeout(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    async def slow_impl(**kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(2.0)
        return {"ok": True, "results": [], "meta": {}}

    discovery._discover_articles_impl = slow_impl
    discovery._discovery_total_budget = lambda: 0.01
    discovery._DISCOVERY_HARD_TIMEOUT_MARGIN_SECONDS = 0.5
    started = time.monotonic()
    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="HTS losses",
            model_name=None,
            notebook_id=None,
            max_results=5,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert time.monotonic() - started < 1.5
    assert response["ok"] is False
    assert response["meta"]["timed_out"] is True


def _validation_papers(count: int) -> list[dict[str, Any]]:
    return [
        {
            "title": f"Screening currents in REBCO coil {index}",
            "abstract": "Abstract Screening currents in REBCO coils degrade field quality.",
            "discovery_score": 50,
            "score_reasons": [],
        }
        for index in range(count)
    ]


def test_llm_validation_runs_in_batches_and_reports_partial_timeout(monkeypatch) -> None:
    from src import article_discovery as discovery

    calls: list[int] = []

    async def fake_llm(*, prompt: str, model_name: str | None, task: str = "query", abort_signal=None) -> str:
        calls.append(prompt.split("Articles: ", 1)[1].count('"idx"'))
        if len(calls) > 1:
            raise TimeoutError()
        rows = [
            f'{{"idx": {i}, "topic_score": 90, "method_score": 80, "tool_score": 70, '
            f'"directness_score": 85, "reject": false, "rationale": "Lien direct {i}."}}'
            for i in range(calls[-1])
        ]
        return '{"evaluations": [' + ", ".join(rows) + "]}"

    monkeypatch.setattr(discovery, "_invoke_discovery_llm_text", fake_llm)
    monkeypatch.setattr(discovery, "_has_discovery_budget", lambda *a, **k: True)
    papers = _validation_papers(14)
    events: list[dict[str, Any]] = []

    outcome = asyncio.run(
        discovery._attach_llm_relevance_validation(
            papers, "ornith", {"keywords": ["screening"]}, validation_limit=14, debug_events=events
        )
    )

    assert calls == [discovery._DISCOVERY_VALIDATION_BATCH_SIZE, discovery._DISCOVERY_VALIDATION_BATCH_SIZE]
    assert outcome["status"] == "partial"
    assert outcome["evaluated"] == discovery._DISCOVERY_VALIDATION_BATCH_SIZE
    assert "TimeoutError" in outcome["error"]
    assert papers[0]["llm_relevance_score"] is not None
    assert papers[-1]["llm_relevance_score"] is None
    # The debug panel now says why a step failed instead of showing nothing.
    assert any("délai LLM dépassé" in event["error"] for event in events)


def test_llm_invented_dois_are_dropped_unless_written_by_the_user() -> None:
    from src import article_discovery as discovery

    grounded = discovery._grounded_dois(
        ["10.1016/j.cryogenics.2023.103456", "10.1063/5.0027489"],
        "Compare with doi:10.1063/5.0027489 on no-insulation coils.",
    )

    assert grounded == ["10.1063/5.0027489"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Abstract REBCO tapes are promising.", "REBCO tapes are promising."),
        ("ABSTRACT: The implementation of HTS inserts.", "The implementation of HTS inserts."),
        ("Summary statistics show a trend.", "Summary statistics show a trend."),
        ("Abstraction layers help.", "Abstraction layers help."),
    ],
)
def test_publisher_abstract_label_is_removed(raw: str, expected: str) -> None:
    from src import article_discovery as discovery

    assert discovery._paper_abstract_text({"abstract": raw}) == expected


def test_source_report_tells_rate_limits_empty_answers_and_unused_sources_apart() -> None:
    from src import article_discovery as discovery

    stats = discovery._init_provider_stats()
    discovery._record_provider_fetch(
        stats, "semanticscholar", phase="primary",
        error="Semantic Scholar circuit open (rate limit); retry after 57.0s",
    )
    discovery._record_provider_fetch(stats, "openalex", rows=[{}] * 12, query_count=2)
    discovery._record_provider_fetch(stats, "arxiv", rows=[], query_count=1)
    discovery._record_provider_fetch(stats, "hal", error="ConnectError: connection refused")

    report = {item["provider"]: item for item in discovery._build_source_report(stats)}

    assert report["semanticscholar"]["status"] == "rate_limited"
    assert "57 s" in report["semanticscholar"]["detail"]
    assert report["openalex"] == {**report["openalex"], "status": "ok", "count": 12}
    assert report["arxiv"]["status"] == "empty"
    assert report["hal"]["status"] == "unavailable"
    assert report["crossref"]["status"] == "not_queried"


def test_empty_result_explains_whether_sources_failed_or_filters_removed_everything() -> None:
    from src import article_discovery as discovery

    failed = [
        {"label": "Semantic Scholar", "status": "rate_limited", "detail": "limite de requêtes atteinte", "count": 0},
        {"label": "OpenAlex", "status": "unavailable", "detail": "pas de réponse dans le délai", "count": 0},
    ]
    message = discovery._empty_result_explanation(failed, discovery._new_result_funnel())
    assert message.startswith("Aucune source externe n'a fourni de résultat")
    assert "Semantic Scholar" in message and "OpenAlex" in message

    answered = [{"label": "OpenAlex", "status": "ok", "detail": "30 résultat(s)", "count": 30}]
    funnel = {**discovery._new_result_funnel(), "found": 30, "dropped_anchor": 22, "rejected_llm": 8}
    message = discovery._empty_result_explanation(answered, funnel)
    assert "30 article(s) trouvés mais écartés" in message
    assert "22 sans lien assez net" in message
    assert "8 jugés hors sujet" in message

    timed_out = discovery._empty_result_explanation([], discovery._new_result_funnel(), timed_out=True)
    assert "limite de temps" in timed_out


def test_hard_timeout_reports_sources_and_leaves_room_over_the_budget(monkeypatch) -> None:
    from src import article_discovery as discovery

    monkeypatch.setattr(discovery, "_discovery_total_budget", lambda: 0.05)
    monkeypatch.setattr(discovery, "_DISCOVERY_HARD_TIMEOUT_MARGIN_SECONDS", 0.05)

    async def slow_impl(**_kwargs):
        report = discovery._discovery_run_report_var.get()
        assert report is not None
        stats = discovery._init_provider_stats()
        discovery._record_provider_fetch(stats, "openalex", error="ReadTimeout")
        report["provider_stats"] = stats
        await asyncio.sleep(1)

    monkeypatch.setattr(discovery, "_discover_articles_impl", slow_impl)
    response = asyncio.run(discovery.discover_articles(
        mode="search", input_text="REBCO screening currents", model_name=None,
        notebook_id=None, max_results=5, include_unpaywall=False, unpaywall_email=None,
    ))

    assert response["ok"] is False
    assert response["meta"]["timed_out"] is True
    sources = {item["provider"]: item["status"] for item in response["meta"]["source_report"]}
    assert sources["openalex"] == "unavailable"
    assert "limite de temps" in response["msg"]


@pytest.mark.parametrize(
    ("text", "keyword"),
    [
        ("Numerical Analysis of Magnetization Loss in No-Insulation Coils", "magnetization losses"),
        ("Epoxy-impregnated YBCO insert coils", "REBCO"),
        ("Charging delay of non-insulated pancake coils", "no-insulation"),
        ("Shielding currents induced in coated conductors", "screening currents"),
        ("Quench protection of HTS inserts", "quench-protection"),
    ],
)
def test_keyword_matching_accepts_variants_and_domain_aliases(text: str, keyword: str) -> None:
    from src import article_discovery as discovery

    assert discovery._text_has_keyword(text, keyword)


def test_short_common_words_are_not_turned_into_acronym_keywords() -> None:
    from src import article_discovery as discovery

    assert [discovery._canonical_query_term(term) for term in ("high", "coil", "loss")] == ["high", "coil", "loss"]
    assert [discovery._canonical_query_term(term) for term in ("rebco", "ni", "hts")] == ["REBCO", "NI", "HTS"]
    assert "high" not in discovery._keyword_fallback("High temperature superconducting magnets at high field")


@pytest.mark.parametrize(
    ("text", "name", "topic_keywords"),
    [
        ("articles de l'auteur camilla moreau", "camilla moreau", []),
        ("Articles de l’auteure Marie Curie ?", "Marie Curie", []),
        ("author: Frédéric Sirois", "Frédéric Sirois", []),
        ("papers by F. Sirois", "F. Sirois", []),
        ("publications écrites par Jean-Pierre Dupont", "Jean-Pierre Dupont", []),
        ("trouve les publications récentes de l'auteur Dupont", "Dupont", []),
        ("articles de l'auteur Sirois sur les pertes AC", "Sirois", ["losses", "AC"]),
    ],
)
def test_author_request_is_recognised(text: str, name: str, topic_keywords: list[str]) -> None:
    from src import article_discovery as discovery

    request = discovery._extract_author_request(text)

    assert request is not None
    assert request["name"] == name
    assert sorted(request["topic_keywords"]) == sorted(topic_keywords)


@pytest.mark.parametrize(
    "text",
    [
        "How to compute AC losses in HTS magnets ?",
        "how do authors measure AC losses in REBCO tapes",
        "papers by the author of the T-A formulation",
        "articles sur les pertes AC dans les aimants HTS",
        "author contribution statements in superconductivity journals and their evolution",
        "auteur: 10.1000/xyz",
    ],
)
def test_topical_request_is_not_taken_for_an_author_request(text: str) -> None:
    from src import article_discovery as discovery

    assert discovery._extract_author_request(text) is None


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        ("Frédéric Sirois", True),
        ("Sirois, Frederic", True),
        ("F. Sirois", True),
        ("Frédéric Dupont", False),
        ("G. Sirois", False),
        ("F. S.", False),
    ],
)
def test_author_name_matching_allows_initials_but_not_homonyms(
    candidate: str, expected: bool
) -> None:
    from src import article_discovery as discovery

    wanted = discovery._author_name_tokens("frederic sirois")

    assert discovery._author_name_matches(candidate, wanted) is expected


def test_hal_author_query_is_a_phrase_in_both_orders() -> None:
    from src import article_discovery as discovery

    assert (
        discovery._hal_author_query("Frédéric Sirois")
        == 'authFullName_t:("frederic sirois" OR "sirois frederic")'
    )


def test_author_request_lists_the_author_publications_newest_first(
    workspace_tmp_dir: Path,
) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)
    requests: list[tuple[str, dict[str, Any]]] = []

    async def fake_get_json_with_retry(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "semanticscholar",
        retries: int = 2,
    ) -> dict[str, Any]:
        requests.append((url, dict(params or {})))
        if url.endswith("/authors"):
            return {
                "results": [
                    {
                        "id": "https://openalex.org/A1",
                        "display_name": "Frédéric Sirois",
                        "display_name_alternatives": ["F. Sirois"],
                    },
                    {"id": "https://openalex.org/A2", "display_name": "Frédéric Siroisson"},
                ]
            }
        if url.endswith("/works"):
            return {
                "results": [
                    {
                        "id": "https://openalex.org/W-old",
                        "display_name": "Older paper",
                        "doi": "https://doi.org/10.1000/old",
                        "publication_year": 2015,
                        "authorships": [{"author": {"display_name": "Frédéric Sirois"}}],
                    },
                    {
                        "id": "https://openalex.org/W-new",
                        "display_name": "Newer paper",
                        "doi": "https://doi.org/10.1000/new",
                        "publication_year": 2024,
                        "authorships": [{"author": {"display_name": "Frédéric Sirois"}}],
                    },
                ]
            }
        if url == discovery._HAL_BASE:
            return {
                "response": {
                    "docs": [
                        {
                            "halId_s": "hal-1",
                            "title_s": ["Newer paper"],
                            "producedDateY_i": 2024,
                            "authFullName_s": ["Frédéric Sirois"],
                        }
                    ]
                }
            }
        if url.endswith("/author/search"):
            raise RuntimeError("Rate limit")
        raise AssertionError(f"unexpected request: {url}")

    async def fail_query_generation(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("an author request must not be turned into topic keywords")

    discovery._get_json_with_retry = fake_get_json_with_retry
    discovery._generate_query_payload_with_llm = fail_query_generation
    discovery._openalex_api_key = lambda: "key"

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="articles de l'auteur frederic sirois",
            model_name="dummy-model",
            notebook_id=None,
            max_results=10,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert [paper["title"] for paper in response["results"]] == ["Newer paper", "Older paper"]
    assert response["results"][0]["provider_sources"] == ["OpenAlex", "HAL"]
    assert response["meta"]["query_generator"] == "author_search"
    assert response["meta"]["providers_used"] == ["HAL", "OpenAlex"]
    works_params = next(params for url, params in requests if url.endswith("/works"))
    assert works_params["filter"] == "authorships.author.id:A1"
    assert works_params["sort"] == "publication_date:desc"
    statuses = {item["provider"]: item["status"] for item in response["meta"]["source_report"]}
    assert statuses["semanticscholar"] == "rate_limited"
    assert statuses["crossref"] == "not_queried"


def test_unknown_author_gets_an_explicit_empty_answer(workspace_tmp_dir: Path) -> None:
    discovery = _load_article_discovery_module(workspace_tmp_dir)

    async def fake_get_json_with_retry(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "semanticscholar",
        retries: int = 2,
    ) -> dict[str, Any]:
        if api_name == "crossref":
            return {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1000/homonym",
                            "title": ["Someone else's paper"],
                            "author": [{"given": "Camille", "family": "Moreau"}],
                        }
                    ]
                }
            }
        return {}

    discovery._get_json_with_retry = fake_get_json_with_retry

    response = asyncio.run(
        discovery.discover_articles(
            mode="search",
            input_text="articles de l'auteur camilla moreau",
            model_name=None,
            notebook_id=None,
            max_results=10,
            include_unpaywall=False,
            unpaywall_email=None,
        )
    )

    assert response["ok"] is True
    assert response["results"] == []
    assert "camilla moreau" in response["meta"]["empty_reason"]
