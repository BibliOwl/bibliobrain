from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import src.config as config
from src.zotero_db import ZoteroItem


def _load_module(storage_dir: Path, data_dir: Path) -> Any:
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    config.STORAGE_DIR = str(storage_dir)
    config.DATA_PATH = str(data_dir)
    return importlib.reload(importlib.import_module("src.library_taxonomy"))


def test_article_taxonomy_uses_collection_metadata_without_llm(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    data_dir = workspace_tmp_dir / "data"
    storage_dir = workspace_tmp_dir / "storage"
    module = _load_module(storage_dir, data_dir)
    pdf = data_dir / "papers" / "p1.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")

    item = ZoteroItem(
        item_key="zot-p1",
        title="AC losses in HTS tapes",
        authors=["Alice Example"],
        year="2024",
        tags=["HTS"],
        collections=["Physics / HTS / AC losses"],
    )
    monkeypatch.setattr(module, "find_pdfs", lambda: [("papers/p1.pdf", str(pdf))])
    monkeypatch.setattr(module, "get_indexed_rel_paths", lambda: {"papers/p1.pdf"})
    monkeypatch.setattr(module, "_lookup_zotero", lambda _rel_path: item)

    def fail_classifier(_article: dict[str, Any]) -> dict[str, Any] | None:
        raise AssertionError("metadata-rich articles should not need LLM classification")

    payload = module.build_article_taxonomy_payload(classifier=fail_classifier)

    assert payload["engine"] == "rag_article_taxonomy"
    assert payload["graph_type"] == "article_taxonomy"
    assert payload["stats"]["articles"] == 1
    labels = {node["label"] for node in payload["nodes"]}
    assert {"Physics", "HTS", "AC losses"}.issubset(labels)
    assert any(
        article["path"] == "papers/p1.pdf"
        for articles in payload["articles_by_concept"].values()
        for article in articles
    )


def test_article_taxonomy_llm_fallback_is_cached(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    data_dir = workspace_tmp_dir / "data"
    storage_dir = workspace_tmp_dir / "storage"
    module = _load_module(storage_dir, data_dir)
    pdf = data_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    item = ZoteroItem(
        item_key="zot-p2",
        title="A numerical model for trapped fields",
        abstract="Pulsed field magnetization of high-temperature superconducting bulks.",
    )
    monkeypatch.setattr(module, "find_pdfs", lambda: [("paper.pdf", str(pdf))])
    monkeypatch.setattr(module, "get_indexed_rel_paths", lambda: {"paper.pdf"})
    monkeypatch.setattr(module, "_lookup_zotero", lambda _rel_path: item)

    calls = {"count": 0}

    def classifier(article: dict[str, Any]) -> dict[str, Any]:
        calls["count"] += 1
        return {
            "article_id": article["article_id"],
            "path": ["Physical sciences", "Applied superconductivity", "Trapped field magnetization"],
            "confidence": 0.81,
            "rationale_short": "HTS trapped field paper.",
        }

    first = module.build_article_taxonomy_payload(classifier=classifier, model_name="test-model")
    second = module.build_article_taxonomy_payload(
        classifier=lambda _article: (_ for _ in ()).throw(AssertionError("cache miss")),
        model_name="test-model",
    )

    assert calls["count"] == 1
    assert first["stats"]["llm_classified"] == 1
    assert second["stats"]["cache_hit"] is True
    assert second["root_ids"] == first["root_ids"]


def test_article_taxonomy_removes_redundant_parent_categories(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    data_dir = workspace_tmp_dir / "data"
    storage_dir = workspace_tmp_dir / "storage"
    module = _load_module(storage_dir, data_dir)
    pdf = data_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    item = ZoteroItem(
        item_key="zot-p3",
        title="Electrical engineering model",
        collections=["Engineering / Electrical engineering / Applied superconductivity"],
    )
    monkeypatch.setattr(module, "find_pdfs", lambda: [("paper.pdf", str(pdf))])
    monkeypatch.setattr(module, "get_indexed_rel_paths", lambda: {"paper.pdf"})
    monkeypatch.setattr(module, "_lookup_zotero", lambda _rel_path: item)

    payload = module.build_article_taxonomy_payload()
    labels = [node["label"] for node in payload["nodes"]]

    assert "Electrical engineering" in labels
    assert "Engineering" not in labels
    electrical = next(node for node in payload["nodes"] if node["label"] == "Electrical engineering")
    assert electrical["depth"] == 0


def test_article_taxonomy_merges_orphan_label_with_deeper_instance(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    data_dir = workspace_tmp_dir / "data"
    storage_dir = workspace_tmp_dir / "storage"
    module = _load_module(storage_dir, data_dir)
    pdf_a = data_dir / "papers" / "a.pdf"
    pdf_b = data_dir / "papers" / "b.pdf"
    pdf_a.parent.mkdir(parents=True)
    pdf_a.write_bytes(b"%PDF-1.4\n")
    pdf_b.write_bytes(b"%PDF-1.4\n")
    items = {
        "papers/a.pdf": ZoteroItem(
            item_key="zot-orphan",
            title="Standalone electrical engineering paper",
            collections=["Electrical engineering"],
        ),
        "papers/b.pdf": ZoteroItem(
            item_key="zot-deeper",
            title="Sub-classified electrical engineering paper",
            collections=["Computer science / Electrical engineering"],
        ),
    }
    monkeypatch.setattr(
        module,
        "find_pdfs",
        lambda: [("papers/a.pdf", str(pdf_a)), ("papers/b.pdf", str(pdf_b))],
    )
    monkeypatch.setattr(
        module,
        "get_indexed_rel_paths",
        lambda: {"papers/a.pdf", "papers/b.pdf"},
    )
    monkeypatch.setattr(module, "_lookup_zotero", lambda rel: items.get(rel))

    payload = module.build_article_taxonomy_payload()

    electrical_nodes = [n for n in payload["nodes"] if n["label"].lower() == "electrical engineering"]
    assert len(electrical_nodes) == 1, electrical_nodes
    canonical = electrical_nodes[0]
    assert canonical["depth"] >= 1
    assert canonical["parent_id"] is not None
    parent = next(n for n in payload["nodes"] if n["id"] == canonical["parent_id"])
    assert parent["label"].lower() == "computer science"
    articles = payload["articles_by_concept"].get(canonical["id"]) or []
    article_keys = {a["article_id"] for a in articles}
    assert {"zot-orphan", "zot-deeper"}.issubset(article_keys)


def test_article_taxonomy_drops_generic_engineering_root(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    data_dir = workspace_tmp_dir / "data"
    storage_dir = workspace_tmp_dir / "storage"
    module = _load_module(storage_dir, data_dir)
    pdf = data_dir / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    item = ZoteroItem(
        item_key="zot-p4",
        title="High energy engineering application",
        collections=["Engineering / High-energy physics / Accelerator systems"],
    )
    monkeypatch.setattr(module, "find_pdfs", lambda: [("paper.pdf", str(pdf))])
    monkeypatch.setattr(module, "get_indexed_rel_paths", lambda: {"paper.pdf"})
    monkeypatch.setattr(module, "_lookup_zotero", lambda _rel_path: item)

    payload = module.build_article_taxonomy_payload()
    labels = [node["label"] for node in payload["nodes"]]

    assert "Engineering" not in labels
    high_energy = next(node for node in payload["nodes"] if node["label"] == "High-energy physics")
    assert high_energy["depth"] == 0


def test_article_taxonomy_uses_content_clusters_when_zotero_hierarchy_is_poor(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    data_dir = workspace_tmp_dir / "data"
    storage_dir = workspace_tmp_dir / "storage"
    module = _load_module(storage_dir, data_dir)
    definitions = [
        (
            "super-1",
            "AC losses in superconducting HTS tapes",
            "Superconducting HTS tape AC losses and electromagnetic fields.",
            "Inbox",
        ),
        (
            "super-2",
            "Critical current of superconducting tapes",
            "Critical current measurements in HTS superconducting conductors.",
            "To read",
        ),
        (
            "super-3",
            "Quench propagation in HTS coils",
            "Quench and electromagnetic behavior of superconducting HTS coils.",
            "Project X",
        ),
        (
            "climate-1",
            "Climate warming effects on forest ecosystems",
            "Climate change modifies forest ecosystem carbon dynamics.",
            "Inbox",
        ),
        (
            "climate-2",
            "Soil carbon under climate change",
            "Soil carbon and ecosystem response to climate warming.",
            "To read",
        ),
        (
            "climate-3",
            "Hydrology response to climate warming",
            "Hydrology and environmental ecosystem response to climate change.",
            "Project Y",
        ),
    ]
    items: dict[str, ZoteroItem] = {}
    pdf_rows: list[tuple[str, str]] = []
    for item_key, title, abstract, collection in definitions:
        rel_path = f"papers/{item_key}.pdf"
        pdf = data_dir / rel_path
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4\n")
        pdf_rows.append((rel_path, str(pdf)))
        items[rel_path] = ZoteroItem(
            item_key=item_key,
            title=title,
            abstract=abstract,
            collections=[collection],
        )

    monkeypatch.setattr(module, "find_pdfs", lambda: pdf_rows)
    monkeypatch.setattr(module, "get_indexed_rel_paths", lambda: set(items))
    monkeypatch.setattr(module, "_lookup_zotero", lambda rel: items.get(rel))

    payload = module.build_article_taxonomy_payload()

    assert payload["stats"]["taxonomy_strategy"] == "semantic_content_clusters"
    assert payload["stats"]["semantic_clustered"] == 6
    assert payload["stats"]["semantic_clusters"] >= 2
    labels = {str(node["label"]) for node in payload["nodes"]}
    assert not {"Inbox", "To read", "Project X", "Project Y"}.intersection(labels)

    article_groups = [
        {str(article["article_id"]) for article in articles}
        for articles in payload["articles_by_concept"].values()
    ]
    assert any({"super-1", "super-2"}.issubset(group) and "climate-1" not in group for group in article_groups)
    assert any({"climate-1", "climate-2"}.issubset(group) and "super-1" not in group for group in article_groups)
