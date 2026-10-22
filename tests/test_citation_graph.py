from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

import src.config as config
from src.zotero_db import ZoteroItem


def _load_modules(storage_dir: Path, data_dir: Path) -> tuple[Any, Any]:
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    config.STORAGE_DIR = str(storage_dir)
    config.DATA_PATH = str(data_dir)
    notebooks = importlib.reload(importlib.import_module("src.notebooks"))
    citation_graph = importlib.reload(importlib.import_module("src.citation_graph"))
    return notebooks, citation_graph


def _fake_oa_work(oa_id: str, doi: str, refs: list[str], **kwargs: Any) -> dict[str, Any]:
    return {
        "id": f"https://openalex.org/{oa_id}",
        "display_name": kwargs.get("title") or f"Work {oa_id}",
        "doi": f"https://doi.org/{doi}",
        "publication_year": kwargs.get("year") or 2024,
        "authorships": [{"author": {"display_name": kwargs.get("author") or "Doe, J."}}],
        "cited_by_count": kwargs.get("cited_by_count") or 0,
        "referenced_works": [f"https://openalex.org/{r}" for r in refs],
    }


async def _empty_crossref_references(
    client: Any, doi: str, *, center_id: str
) -> list[dict[str, Any]]:
    return []


def _fake_crossref_references(citation_graph: Any) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for ordinal in range(1, 17):
        raw: dict[str, Any] = {
            "article-title": f"Reference title {ordinal}",
            "author": f"Author {ordinal}",
            "year": "2024",
        }
        if ordinal <= 4:
            raw["DOI"] = f"10.1/ref{ordinal}"
        references.append(
            citation_graph._normalize_crossref_reference(
                raw,
                center_id="Wcenter",
                ordinal=ordinal,
            )
        )
    return references


def test_citation_graph_builds_edges_and_missing_links(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    notebooks, citation_graph = _load_modules(storage_dir, data_dir)

    nb = notebooks.create_notebook("Test")
    notebooks.add_source(nb["id"], "Paper A", "...", source_path="papers/a.pdf")
    notebooks.add_source(nb["id"], "Paper B", "...", source_path="papers/b.pdf")
    notebooks.add_source(nb["id"], "Paper C", "...", source_path="papers/c.pdf")
    notebooks.add_source(nb["id"], "No DOI", "...", source_path="papers/d.pdf")

    items = {
        "papers/a.pdf": ZoteroItem(item_key="A", title="A", doi="10.1/a"),
        "papers/b.pdf": ZoteroItem(item_key="B", title="B", doi="10.1/b"),
        "papers/c.pdf": ZoteroItem(item_key="C", title="C", doi="10.1/c"),
        "papers/d.pdf": ZoteroItem(item_key="D", title="D", doi=""),
    }
    monkeypatch.setattr(citation_graph, "_lookup_zotero", lambda rel: items.get(rel))

    # Mock OpenAlex helpers
    async def fake_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        works = {
            "10.1/a": _fake_oa_work("W1", "10.1/a", refs=["W2", "Wmissing", "Wother"]),
            "10.1/b": _fake_oa_work("W2", "10.1/b", refs=["Wmissing", "Wother"]),
            "10.1/c": _fake_oa_work("W3", "10.1/c", refs=["W1"]),
        }
        return [citation_graph._normalize_oa_work(works[doi]) for doi in dois if doi in works]

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        meta = {
            "Wmissing": _fake_oa_work("Wmissing", "10.1/missing", refs=[], title="Missing landmark"),
            "Wother": _fake_oa_work("Wother", "10.1/other", refs=[], title="Other landmark"),
        }
        return [citation_graph._normalize_oa_work(meta[oa]) for oa in oa_ids if oa in meta]

    async def fake_top_citers(client: Any, oa_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", fake_doi_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)

    import asyncio
    payload = asyncio.run(citation_graph.build_notebook_citation_graph(nb["id"]))

    assert payload["ok"] is True
    node_ids = {n["id"] for n in payload["nodes"]}
    assert node_ids == {"W1", "W2", "W3"}
    edge_pairs = {(e["source"], e["target"]) for e in payload["edges"]}
    assert ("W1", "W2") in edge_pairs
    assert ("W3", "W1") in edge_pairs
    assert ("W1", "Wmissing") not in edge_pairs

    prior_ids = {p["id"] for p in payload["prior_works"]}
    assert prior_ids == {"Wmissing", "Wother"}
    prior_counts = {p["id"]: p["citers_in_notebook"] for p in payload["prior_works"]}
    assert prior_counts["Wmissing"] == 2
    assert prior_counts["Wother"] == 2

    stats = payload["stats"]
    assert stats["papers_with_doi"] == 3
    assert stats["papers_without_doi"] == 1
    assert stats["edges_count"] == 2
    assert stats["prior_works_count"] == 2


def test_citation_graph_handles_empty_notebook(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    notebooks, citation_graph = _load_modules(storage_dir, data_dir)
    nb = notebooks.create_notebook("Empty")

    monkeypatch.setattr(citation_graph, "_lookup_zotero", lambda _rel: None)

    import asyncio
    payload = asyncio.run(citation_graph.build_notebook_citation_graph(nb["id"]))

    assert payload["ok"] is True
    assert payload["nodes"] == []
    assert payload["edges"] == []
    assert payload["missing_links"] == []


def test_library_internal_citation_graph_keeps_only_library_articles(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    _, citation_graph = _load_modules(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    paths = ["papers/a.pdf", "papers/b.pdf", "papers/c.pdf", "papers/no-doi.pdf"]
    rows = [["A"], ["B"], ["C"], ["Sans DOI"]]
    items = {
        "papers/a.pdf": ZoteroItem(item_key="A", title="A", doi="10.1/a"),
        "papers/b.pdf": ZoteroItem(item_key="B", title="B", doi="10.1/b"),
        "papers/c.pdf": ZoteroItem(item_key="C", title="C", doi="10.1/c"),
        "papers/no-doi.pdf": ZoteroItem(item_key="D", title="Sans DOI", doi=""),
    }
    monkeypatch.setattr(citation_graph, "list_library", lambda indexed_only=True: (rows, paths))
    monkeypatch.setattr(citation_graph, "_lookup_zotero", lambda rel: items.get(rel))

    fetch_calls = 0

    async def fake_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        nonlocal fetch_calls
        fetch_calls += 1
        works = {
            "10.1/a": _fake_oa_work("W1", "10.1/a", refs=["W2", "Wexternal"]),
            "10.1/b": _fake_oa_work("W2", "10.1/b", refs=[]),
            "10.1/c": _fake_oa_work("W3", "10.1/c", refs=["W1"]),
        }
        return [citation_graph._normalize_oa_work(works[doi]) for doi in dois]

    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", fake_doi_fetch)

    import asyncio

    payload = asyncio.run(citation_graph.build_library_internal_citation_graph())
    cached_payload = asyncio.run(citation_graph.build_library_internal_citation_graph())

    assert payload["graph_type"] == "library_citations"
    assert {node["id"] for node in payload["nodes"]} == {"W1", "W2", "W3"}
    assert {(edge["source"], edge["target"]) for edge in payload["edges"]} == {
        ("W1", "W2"),
        ("W3", "W1"),
    }
    assert all("Wexternal" not in (edge["source"], edge["target"]) for edge in payload["edges"])
    assert payload["stats"]["connected_articles"] == 3
    assert payload["stats"]["connected_components"] == 1
    assert payload["stats"]["largest_component"] == 3
    assert payload["stats"]["edges_count"] == 2
    assert payload["stats"]["unresolved_articles"] == 1
    assert payload["skipped_sources"][0]["reason"] == "Pas de DOI dans Zotero"
    assert {node["component_id"] for node in payload["nodes"]} == {0}
    assert {node["component_size"] for node in payload["nodes"]} == {3}
    assert payload["stats"]["communities_count"] == 1
    assert payload["stats"]["largest_community"] == 3
    assert payload["communities"][0]["node_ids"] == ["W1", "W2", "W3"]
    assert {node["community_id"] for node in payload["nodes"]} == {0}
    assert fetch_calls == 1
    assert cached_payload["stats"]["cached"] is True


def test_library_citation_graph_lazily_restores_zotero_metadata(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    _, citation_graph = _load_modules(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    item = ZoteroItem(item_key="A", title="Paper A", doi="10.1/a")
    loaded = False
    init_calls: list[str] = []

    monkeypatch.setattr(
        citation_graph,
        "list_library",
        lambda indexed_only=True: ([["Paper A"]], ["papers/a.pdf"]),
    )
    monkeypatch.setattr(
        citation_graph,
        "_lookup_zotero",
        lambda rel_path: item if loaded and rel_path == "papers/a.pdf" else None,
    )
    monkeypatch.setattr(citation_graph.config, "ZOTERO_DB_PATH", "zotero.sqlite")
    monkeypatch.setattr(citation_graph.library_module, "_zotero_library", {})

    def fake_init_zotero(path: str = "") -> str:
        nonlocal loaded
        loaded = True
        init_calls.append(path)
        return "Zotero OK"

    monkeypatch.setattr(citation_graph.library_module, "init_zotero", fake_init_zotero)

    resolved, skipped = citation_graph._resolve_indexed_library_sources()

    assert init_calls == ["zotero.sqlite"]
    assert [record["doi"] for record in resolved] == ["10.1/a"]
    assert skipped == []


def test_connected_components_keep_isolates_neutral_and_split_groups(
    workspace_tmp_dir: Path,
) -> None:
    _, citation_graph = _load_modules(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    component_by_node, sizes = citation_graph._connected_component_metadata(
        {"A", "B", "C", "D", "E", "F"},
        {("A", "B"), ("B", "C"), ("D", "E")},
    )

    assert component_by_node["A"] == component_by_node["B"] == component_by_node["C"] == 0
    assert component_by_node["D"] == component_by_node["E"] == 1
    assert component_by_node["F"] == -1
    assert sizes == {0: 3, 1: 2, -1: 1}


def test_citation_communities_split_a_large_connected_component(
    workspace_tmp_dir: Path,
) -> None:
    _, citation_graph = _load_modules(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    first = {f"A{i}" for i in range(6)}
    second = {f"B{i}" for i in range(6)}
    edge_pairs = {
        (source, target)
        for group in (sorted(first), sorted(second))
        for index, source in enumerate(group)
        for target in group[index + 1 :]
    }
    edge_pairs.add(("A0", "B0"))
    node_ids = first | second | {"ISO"}
    works = {
        node_id: {
            "title": ("Magnet superconducting coil" if node_id.startswith("A") else "Neural citation retrieval"),
            "year": 2020,
        }
        for node_id in node_ids
    }
    degrees = {node_id: 1 for node_id in node_ids}

    communities, community_by_node, aggregate_edges = citation_graph._build_citation_communities(
        node_ids,
        edge_pairs,
        works,
        degrees,
    )

    assert len(communities) == 2
    assert {community["size"] for community in communities} == {6}
    assert community_by_node["A0"] != community_by_node["B0"]
    assert "ISO" not in community_by_node
    assert sum(edge["count"] for edge in aggregate_edges) == 1


def test_citation_graph_emits_similarity_edges_from_shared_refs(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    notebooks, citation_graph = _load_modules(storage_dir, data_dir)

    nb = notebooks.create_notebook("Sim")
    notebooks.add_source(nb["id"], "A", "...", source_path="papers/a.pdf")
    notebooks.add_source(nb["id"], "B", "...", source_path="papers/b.pdf")
    notebooks.add_source(nb["id"], "C", "...", source_path="papers/c.pdf")

    items = {
        "papers/a.pdf": ZoteroItem(item_key="A", title="A", doi="10.1/a"),
        "papers/b.pdf": ZoteroItem(item_key="B", title="B", doi="10.1/b"),
        "papers/c.pdf": ZoteroItem(item_key="C", title="C", doi="10.1/c"),
    }
    monkeypatch.setattr(citation_graph, "_lookup_zotero", lambda rel: items.get(rel))

    async def fake_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        # A and B share 3 of 4 refs (high jaccard); C shares 0
        works = {
            "10.1/a": _fake_oa_work("W1", "10.1/a", refs=["X1", "X2", "X3", "X4"]),
            "10.1/b": _fake_oa_work("W2", "10.1/b", refs=["X1", "X2", "X3", "X9"]),
            "10.1/c": _fake_oa_work("W3", "10.1/c", refs=["Y1", "Y2"]),
        }
        return [citation_graph._normalize_oa_work(works[doi]) for doi in dois if doi in works]

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        return []

    async def fake_top_citers(client: Any, oa_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", fake_doi_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)

    import asyncio
    payload = asyncio.run(citation_graph.build_notebook_citation_graph(nb["id"]))
    sim = payload["similarity_edges"]
    assert any(
        {e["a"], e["b"]} == {"W1", "W2"} and e["score"] > 0.5 for e in sim
    ), sim
    assert not any(
        "W3" in {e["a"], e["b"]} for e in sim
    ), sim


def test_citation_graph_aggregates_derivative_works(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    notebooks, citation_graph = _load_modules(storage_dir, data_dir)

    nb = notebooks.create_notebook("Deriv")
    notebooks.add_source(nb["id"], "A", "...", source_path="papers/a.pdf")
    notebooks.add_source(nb["id"], "B", "...", source_path="papers/b.pdf")
    items = {
        "papers/a.pdf": ZoteroItem(item_key="A", title="A", doi="10.1/a"),
        "papers/b.pdf": ZoteroItem(item_key="B", title="B", doi="10.1/b"),
    }
    monkeypatch.setattr(citation_graph, "_lookup_zotero", lambda rel: items.get(rel))

    async def fake_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        works = {
            "10.1/a": _fake_oa_work("W1", "10.1/a", refs=[]),
            "10.1/b": _fake_oa_work("W2", "10.1/b", refs=[]),
        }
        return [citation_graph._normalize_oa_work(works[doi]) for doi in dois if doi in works]

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        return []

    async def fake_top_citers(client: Any, oa_id: str, limit: int = 30) -> list[dict[str, Any]]:
        # Wcommon cites both A and B, Wonly cites only A
        if oa_id == "W1":
            return [
                citation_graph._normalize_oa_work(_fake_oa_work("Wcommon", "10.1/c", refs=[], title="Common citer")),
                citation_graph._normalize_oa_work(_fake_oa_work("Wonly", "10.1/o", refs=[], title="Only A citer")),
            ]
        if oa_id == "W2":
            return [
                citation_graph._normalize_oa_work(_fake_oa_work("Wcommon", "10.1/c", refs=[], title="Common citer")),
            ]
        return []

    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", fake_doi_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)

    import asyncio
    payload = asyncio.run(citation_graph.build_notebook_citation_graph(nb["id"]))

    deriv = payload["derivative_works"]
    by_id = {d["id"]: d for d in deriv}
    assert by_id["Wcommon"]["cites_in_notebook"] == 2
    assert by_id["Wonly"]["cites_in_notebook"] == 1
    # Sorted by cites_in_notebook desc → Wcommon first
    assert deriv[0]["id"] == "Wcommon"


def test_decode_abstract_inverted_index() -> None:
    from src.citation_graph import _decode_abstract

    inverted = {"Hello": [0, 4], "world": [1], "of": [2], "tests": [3]}
    assert _decode_abstract(inverted) == "Hello world of tests Hello"
    assert _decode_abstract({}) == ""
    assert _decode_abstract(None) == ""


def test_citation_graph_unknown_notebook_returns_error(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _, citation_graph = _load_modules(storage_dir, data_dir)

    import asyncio
    payload = asyncio.run(citation_graph.build_notebook_citation_graph("does-not-exist"))

    assert payload["ok"] is False
    assert payload["nodes"] == []


def test_single_library_paper_graph_reuses_full_graph_pipeline(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _, citation_graph = _load_modules(storage_dir, data_dir)

    item = ZoteroItem(item_key="A", title="Source paper", doi="10.1/source")
    monkeypatch.setattr(citation_graph, "_lookup_zotero", lambda rel: item if rel == "papers/a.pdf" else None)

    async def fake_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        assert dois == ["10.1/source"]
        return [
            citation_graph._normalize_oa_work(
                _fake_oa_work("Wsource", "10.1/source", refs=["Wprior"], title="Source paper")
            )
        ]

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        assert oa_ids == ["Wprior"]
        return [
            citation_graph._normalize_oa_work(
                _fake_oa_work("Wprior", "10.1/prior", refs=[], title="Prior work")
            )
        ]

    async def fake_top_citers(client: Any, oa_id: str, limit: int = 30) -> list[dict[str, Any]]:
        assert oa_id == "Wsource"
        return []

    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", fake_doi_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)
    monkeypatch.setattr(citation_graph, "_fetch_crossref_references", _empty_crossref_references)

    import asyncio

    payload = asyncio.run(citation_graph.build_paper_citation_graph("papers/a.pdf"))

    assert payload["ok"] is True
    assert [node["id"] for node in payload["nodes"]] == ["Wsource"]
    assert [node["id"] for node in payload["prior_works"]] == ["Wprior"]
    assert payload["prior_works"][0]["cited_by_notebook_ids"] == ["Wsource"]
    assert payload["stats"]["papers_in_notebook"] == 1


def test_title_lookup_accepts_a_truncated_zotero_filename_title(monkeypatch: Any) -> None:
    from src import citation_graph

    async def fake_get_json(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["params"]["search"] == (
            "Computation of Losses in HTS Under the Action of V"
        )
        return {
            "results": [
                _fake_oa_work(
                    "Wwrong",
                    "10.1/wrong",
                    refs=[],
                    title="Unrelated computation paper",
                    year=2014,
                ),
                _fake_oa_work(
                    "Wsource",
                    "10.1/source",
                    refs=[],
                    title=(
                        "Computation of Losses in HTS Under the Action of "
                        "Varying Magnetic Fields and Currents"
                    ),
                    year=2014,
                    author="Grilli, F.",
                ),
            ]
        }

    monkeypatch.setattr(citation_graph, "_get_json_with_retry", fake_get_json)

    import asyncio

    work = asyncio.run(
        citation_graph._fetch_work_by_title(
            cast(Any, None),
            "Computation of Losses in HTS Under the Action of V",
            year="2014",
            author="Grilli et al.",
        )
    )

    assert work is not None
    assert work["openalex_id"] == "Wsource"


def test_single_library_paper_without_zotero_metadata_resolves_from_filename(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _, citation_graph = _load_modules(storage_dir, data_dir)
    filename = (
        "papers/Grilli et al. - 2014 - Computation of Losses in HTS "
        "Under the Action of V.pdf"
    )
    source_work = citation_graph._normalize_oa_work(
        _fake_oa_work(
            "Wsource",
            "10.1/source",
            refs=["Wprior"],
            title="Computation of Losses in HTS Under the Action of Varying Fields",
            year=2014,
            author="Grilli, F.",
        )
    )

    monkeypatch.setattr(citation_graph, "_lookup_zotero", lambda _rel: None)

    async def fake_title_fetch(
        client: Any, title: str, *, year: str = "", author: str = ""
    ) -> dict[str, Any]:
        assert title == "Computation of Losses in HTS Under the Action of V"
        assert year == "2014"
        assert author == "Grilli et al."
        return source_work

    async def fake_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        assert dois == []
        return []

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        assert oa_ids == ["Wprior"]
        return [
            citation_graph._normalize_oa_work(
                _fake_oa_work("Wprior", "10.1/prior", refs=[], title="Prior work")
            )
        ]

    async def fake_top_citers(
        client: Any, oa_id: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        assert oa_id == "Wsource"
        return []

    monkeypatch.setattr(citation_graph, "_fetch_work_by_title", fake_title_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", fake_doi_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)
    monkeypatch.setattr(citation_graph, "_fetch_crossref_references", _empty_crossref_references)

    import asyncio

    payload = asyncio.run(citation_graph.build_paper_citation_graph(filename))

    assert payload["ok"] is True
    assert [node["id"] for node in payload["nodes"]] == ["Wsource"]
    assert payload["nodes"][0]["source_path"] == filename
    assert payload["stats"]["papers_without_doi"] == 0


def test_expand_citation_graph_work_returns_one_directed_layer(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _, citation_graph = _load_modules(storage_dir, data_dir)

    async def fake_full_fetch(client: Any, oa_id: str) -> dict[str, Any]:
        assert oa_id == "Wcenter"
        return citation_graph._normalize_oa_work(
            _fake_oa_work(
                "Wcenter",
                "10.1/center",
                refs=["Wprior1", "Wprior2"],
                title="Center work",
            )
        )

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        works = {
            "Wprior1": _fake_oa_work("Wprior1", "10.1/p1", refs=[], cited_by_count=4),
            "Wprior2": _fake_oa_work("Wprior2", "10.1/p2", refs=[], cited_by_count=20),
        }
        return [citation_graph._normalize_oa_work(works[oa_id]) for oa_id in oa_ids]

    async def fake_top_citers(
        client: Any, oa_id: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        assert oa_id == "Wcenter"
        assert limit == 2
        return [
            citation_graph._normalize_oa_work(
                _fake_oa_work("Wciter", "10.1/citer", refs=[], title="Citing work")
            )
        ]

    monkeypatch.setattr(citation_graph, "_fetch_full_work_by_oa_id", fake_full_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)
    monkeypatch.setattr(citation_graph, "_fetch_crossref_references", _empty_crossref_references)

    import asyncio

    payload = asyncio.run(citation_graph.expand_citation_graph_work("Wcenter", limit=2))

    assert payload["ok"] is True
    assert payload["center"]["id"] == "Wcenter"
    assert [work["id"] for work in payload["prior_works"]] == ["Wprior2", "Wprior1"]
    assert [work["id"] for work in payload["derivative_works"]] == ["Wciter"]
    assert {(edge["source"], edge["target"]) for edge in payload["edges"]} == {
        ("Wcenter", "Wprior1"),
        ("Wcenter", "Wprior2"),
        ("Wciter", "Wcenter"),
    }


def test_expansion_completes_missing_openalex_references_from_crossref(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    _, citation_graph = _load_modules(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")

    async def fake_full_fetch(client: Any, oa_id: str) -> dict[str, Any]:
        assert oa_id == "Wcenter"
        return citation_graph._normalize_oa_work(
            _fake_oa_work(
                "Wcenter",
                "10.1/center",
                refs=[f"Wprior{index}" for index in range(1, 12)],
                title="Center work",
            )
        )

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        assert oa_ids == [f"Wprior{index}" for index in range(1, 12)]
        return [
            citation_graph._normalize_oa_work(
                _fake_oa_work(
                    f"Wprior{index}",
                    f"10.1/ref{index}",
                    refs=[],
                    title=f"Reference title {index}",
                )
            )
            for index in range(1, 6)
        ]

    async def fake_top_citers(client: Any, oa_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return []

    async def fake_crossref_fetch(client: Any, doi: str, *, center_id: str) -> list[dict[str, Any]]:
        assert doi == "10.1/center"
        assert center_id == "Wcenter"
        return _fake_crossref_references(citation_graph)

    async def unexpected_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        raise AssertionError(f"All Crossref DOIs should already be resolved: {dois}")

    monkeypatch.setattr(citation_graph, "_fetch_full_work_by_oa_id", fake_full_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)
    monkeypatch.setattr(citation_graph, "_fetch_crossref_references", fake_crossref_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", unexpected_doi_fetch)

    import asyncio

    payload = asyncio.run(citation_graph.expand_citation_graph_work("Wcenter", limit=18))

    assert payload["ok"] is True
    assert len(payload["prior_works"]) == 16
    assert [work["id"] for work in payload["prior_works"][:5]] == [
        "Wprior1",
        "Wprior2",
        "Wprior3",
        "Wprior4",
        "Wprior5",
    ]
    assert payload["prior_works"][5]["id"] == "crossref-ref:Wcenter:6"
    assert payload["prior_works"][5]["openalex_id"] == ""
    assert len(payload["edges"]) == 16
    assert payload["stats"]["references_available"] == 16
    assert payload["stats"]["fallback_references_count"] == 11


def test_single_paper_graph_uses_crossref_bibliography_when_openalex_is_incomplete(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    _, citation_graph = _load_modules(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    item = ZoteroItem(item_key="A", title="Center work", doi="10.1/center")
    monkeypatch.setattr(citation_graph, "_lookup_zotero", lambda rel: item)

    async def fake_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        assert dois == ["10.1/center"]
        return [
            citation_graph._normalize_oa_work(
                _fake_oa_work(
                    "Wcenter",
                    "10.1/center",
                    refs=[f"Wprior{index}" for index in range(1, 12)],
                    title="Center work",
                )
            )
        ]

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        return [
            citation_graph._normalize_oa_work(
                _fake_oa_work(
                    f"Wprior{index}",
                    f"10.1/ref{index}",
                    refs=[],
                    title=f"Reference title {index}",
                )
            )
            for index in range(1, 6)
        ]

    async def fake_top_citers(client: Any, oa_id: str, limit: int = 30) -> list[dict[str, Any]]:
        return []

    async def fake_crossref_fetch(client: Any, doi: str, *, center_id: str) -> list[dict[str, Any]]:
        return _fake_crossref_references(citation_graph)

    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", fake_doi_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)
    monkeypatch.setattr(citation_graph, "_fetch_crossref_references", fake_crossref_fetch)

    import asyncio

    payload = asyncio.run(citation_graph.build_paper_citation_graph("papers/a.pdf"))

    assert payload["ok"] is True
    assert len(payload["prior_works"]) == 16
    assert payload["prior_works"][4]["id"] == "Wprior5"
    assert payload["prior_works"][5]["openalex_id"] == ""
    assert all(work["cited_by_notebook_ids"] == ["Wcenter"] for work in payload["prior_works"])
    assert payload["stats"]["crossref_references_count"] == 16
    assert payload["stats"]["fallback_references_count"] == 11


def test_search_research_entities_normalizes_author_identity(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    _, citation_graph = _load_modules(
        workspace_tmp_dir / "storage", workspace_tmp_dir / "data"
    )
    captured: dict[str, Any] = {}

    async def fake_get_json(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "",
        retries: int = 2,
    ) -> dict[str, Any]:
        captured.update(url=url, params=params, api_name=api_name)
        return {
            "results": [
                {
                    "id": "https://openalex.org/A42",
                    "display_name": "Ada Example",
                    "orcid": "https://orcid.org/0000-0001-0000-0001",
                    "works_count": 12,
                    "cited_by_count": 345,
                    "last_known_institutions": [
                        {"display_name": "Example Laboratory"}
                    ],
                }
            ]
        }

    monkeypatch.setattr(citation_graph, "_get_json_with_retry", fake_get_json)

    import asyncio

    payload = asyncio.run(
        citation_graph.search_research_entities("author", "Ada Example", limit=5)
    )

    assert captured["url"].endswith("/authors")
    assert captured["params"]["search"] == "Ada Example"
    assert captured["api_name"] == "openalex"
    assert payload["entities"] == [
        {
            "id": "A42",
            "openalex_id": "A42",
            "entity_type": "author",
            "label": "Ada Example",
            "subtitle": "Example Laboratory",
            "external_id": "https://orcid.org/0000-0001-0000-0001",
            "country_code": "",
            "works_count": 12,
            "cited_by_count": 345,
            "openalex_url": "https://openalex.org/A42",
        }
    ]


def test_research_scope_graph_builds_balanced_author_corpus_without_doi(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    _, citation_graph = _load_modules(
        workspace_tmp_dir / "storage", workspace_tmp_dir / "data"
    )
    citation_graph._research_scope_cache.clear()
    work_requests: list[dict[str, Any]] = []

    def raw_work(index: int, *, recent: bool = False) -> dict[str, Any]:
        raw = _fake_oa_work(
            f"W{index}",
            f"10.1/{index}",
            refs=["W2"] if index == 1 else [],
            title=f"Work {index}",
            year=2026 - index if recent else 2000 + index,
            cited_by_count=200 - index,
        )
        if index == 15:
            raw["doi"] = None
        return raw

    cited_rows = [raw_work(index) for index in range(1, 11)]
    recent_rows = [raw_work(index, recent=True) for index in range(15, 5, -1)]

    async def fake_get_json(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "",
        retries: int = 2,
    ) -> dict[str, Any]:
        if url.endswith("/authors/A42"):
            return {
                "id": "https://openalex.org/A42",
                "display_name": "Ada Example",
                "works_count": 15,
                "cited_by_count": 500,
                "last_known_institutions": [],
            }
        assert url.endswith("/works")
        assert params is not None
        work_requests.append(dict(params))
        rows = recent_rows if params.get("sort") == "publication_date:desc" else cited_rows
        return {"meta": {"count": 15}, "results": rows}

    async def fake_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        assert dois == []
        return []

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        return []

    async def fake_top_citers(
        client: Any, oa_id: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(citation_graph, "_get_json_with_retry", fake_get_json)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", fake_doi_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)

    import asyncio

    payload = asyncio.run(
        citation_graph.build_research_scope_citation_graph(
            "author",
            "A42",
            from_year=1995,
            to_year=2026,
            max_works=10,
            strategy="balanced",
        )
    )

    assert payload["ok"] is True
    assert payload["graph_type"] == "research_entity_citations"
    assert payload["scope"]["label"] == "Ada Example"
    assert payload["scope"]["selected_works"] == 10
    assert payload["stats"]["papers_in_scope"] == 10
    assert payload["data_handling"]["storage"] == "volatile_memory_cache"
    assert payload["data_handling"]["cache_ttl_seconds"] == 900
    assert payload["data_handling"]["automated_decision_making"] is False
    assert {request["sort"] for request in work_requests} == {
        "cited_by_count:desc",
        "publication_date:desc",
    }
    assert all(
        "authorships.author.id:A42" in request["filter"]
        and "publication_year:1995-2026" in request["filter"]
        for request in work_requests
    )
    assert "W15" in {node["id"] for node in payload["nodes"]}
    assert ("W1", "W2") in {
        (edge["source"], edge["target"]) for edge in payload["edges"]
    }
    assert payload["warnings"][0] == "Corpus limité à 10 publications sur 15 disponibles."


def test_research_scope_rejects_entity_id_with_wrong_prefix(
    workspace_tmp_dir: Path
) -> None:
    _, citation_graph = _load_modules(
        workspace_tmp_dir / "storage", workspace_tmp_dir / "data"
    )

    import asyncio

    import pytest

    with pytest.raises(ValueError, match="correspond pas"):
        asyncio.run(
            citation_graph.build_research_scope_citation_graph(
                "institution", "A42"
            )
        )


def test_bibliographic_search_builds_a_publication_centric_graph(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    _, citation_graph = _load_modules(
        workspace_tmp_dir / "storage", workspace_tmp_dir / "data"
    )
    citation_graph._research_scope_cache.clear()
    requested_endpoints: list[str] = []

    direct_work = _fake_oa_work(
        "W1", "10.1/direct", refs=["W2"], title="Direct match", cited_by_count=30
    )
    author_work = _fake_oa_work(
        "W2", "10.1/author", refs=[], title="Author work", cited_by_count=20
    )

    async def fake_get_json(
        client: Any,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        api_name: str = "",
        retries: int = 2,
    ) -> dict[str, Any]:
        requested_endpoints.append(url)
        if url.endswith("/authors"):
            return {
                "results": [{
                    "id": "https://openalex.org/A42",
                    "display_name": "Ada Example",
                    "works_count": 1,
                    "cited_by_count": 20,
                    "last_known_institutions": [],
                }]
            }
        if url.endswith("/institutions"):
            return {"results": []}
        assert url.endswith("/works")
        assert params is not None
        if "authorships.author.id:A42" in str(params.get("filter") or ""):
            return {"meta": {"count": 1}, "results": [author_work]}
        assert params.get("search") == "Ada Example"
        return {"meta": {"count": 1}, "results": [direct_work]}

    async def fake_doi_fetch(client: Any, dois: list[str]) -> list[dict[str, Any]]:
        assert dois == []
        return []

    async def fake_oaid_fetch(client: Any, oa_ids: list[str]) -> list[dict[str, Any]]:
        return []

    async def fake_top_citers(
        client: Any, oa_id: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(citation_graph, "_get_json_with_retry", fake_get_json)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_doi", fake_doi_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_works_by_oa_ids", fake_oaid_fetch)
    monkeypatch.setattr(citation_graph, "_fetch_top_citers", fake_top_citers)

    import asyncio

    payload = asyncio.run(
        citation_graph.build_bibliographic_search_citation_graph(
            "Ada Example", max_works=10, strategy="balanced"
        )
    )

    assert payload["ok"] is True
    assert payload["graph_type"] == "bibliographic_search_citations"
    assert payload["scope"]["kind"] == "bibliographic_search"
    assert payload["scope"]["query"] == "Ada Example"
    assert payload["scope"]["matches"] == [
        {"kind": "author", "label": "Ada Example"}
    ]
    assert {node["id"] for node in payload["nodes"]} == {"W1", "W2"}
    assert payload["data_handling"] == {
        "source": "OpenAlex",
        "processing": "on_device",
        "storage": "volatile_memory_cache",
        "cache_ttl_seconds": 900,
    }
    assert any(url.endswith("/authors") for url in requested_endpoints)
    assert any(url.endswith("/institutions") for url in requested_endpoints)
    assert citation_graph.clear_bibliographic_search_cache() == 1
    assert citation_graph.clear_bibliographic_search_cache() == 0


def test_crossref_reference_reads_author_and_year_from_raw_text(
    workspace_tmp_dir: Path,
) -> None:
    _, citation_graph = _load_modules(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")

    def normalize(unstructured: str) -> tuple[list[str], int | None]:
        work = citation_graph._normalize_crossref_reference(
            {"unstructured": unstructured}, center_id="Wcenter", ordinal=1
        )
        return work["authors"], work["year"]

    assert normalize("C. P. Bean, Rev. Mod. Phys. 36, 31 (1964).") == (["Bean"], 1964)
    assert normalize("Tomita M, Murakami M. Bulk magnets. Nature 2003;421:517") == (["Tomita"], 2003)
    assert normalize("Jiang Z 2004 Supercond. Sci. Technol. 17 1311") == (["Jiang"], 2004)
    # A title or a journal first is not taken for an author.
    assert normalize("Magnetization of hard superconductors, Phys Rev Lett 1962") == ([], 1962)
    assert normalize("Nature 421, 517 (2003)") == ([], 2003)
    # Structured fields win over the raw text.
    work = citation_graph._normalize_crossref_reference(
        {"author": "Brandt", "year": "1996", "unstructured": "C. P. Bean (1964)"},
        center_id="Wcenter",
        ordinal=2,
    )
    assert (work["authors"], work["year"]) == (["Brandt"], 1996)


def test_paper_lookup_hints_split_zotero_filenames_in_linear_time() -> None:
    import time

    from src.citation_graph import _paper_lookup_hints

    assert _paper_lookup_hints("Smith  -  2020 - Un titre - suite.pdf", None) == (
        "Un titre - suite",
        "2020",
        "Smith",
    )
    started = time.monotonic()
    _paper_lookup_hints("a" + " " * 100_000 + "b.pdf", None)
    assert time.monotonic() - started < 2.0
