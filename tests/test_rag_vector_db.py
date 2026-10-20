from __future__ import annotations

import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor as RealThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain_core.documents import Document

import src.config as config

_RAG_MODULE_NAMES = [
    "src.analysis_cache_db",
    "src.chunk_cache",
    "src.library",
    "src.rag.state",
    "src.rag_settings",
    "src.rag.zotero_integration",
    "src.rag.document_loader",
    "src.rag.vector_store",
    "src.rag.query_engine",
]


@pytest.fixture(scope="module", autouse=True)
def _restore_rag_module_globals():
    original_paths = (config.DATA_PATH, config.STORAGE_DIR, config.CHROMA_PATH)
    yield
    config.DATA_PATH, config.STORAGE_DIR, config.CHROMA_PATH = original_paths
    for name in _RAG_MODULE_NAMES:
        module = importlib.import_module(name)
        importlib.reload(module)


def _load_rag_modules(workspace_tmp_dir: Path) -> Any:
    data_dir = workspace_tmp_dir / "data"
    storage_dir = workspace_tmp_dir / "storage"
    chroma_dir = workspace_tmp_dir / "chroma"
    data_dir.mkdir(parents=True, exist_ok=True)
    storage_dir.mkdir(parents=True, exist_ok=True)

    config.DATA_PATH = str(data_dir)
    config.STORAGE_DIR = str(storage_dir)
    config.CHROMA_PATH = str(chroma_dir)

    modules: dict[str, Any] = {}
    for name in _RAG_MODULE_NAMES:
        module = cast(Any, importlib.import_module(name))
        modules[name] = cast(Any, importlib.reload(module))

    return SimpleNamespace(
        data_dir=data_dir,
        storage_dir=storage_dir,
        chroma_dir=chroma_dir,
        state=modules["src.rag.state"],
        chunk_cache=modules["src.chunk_cache"],
        zotero_integration=modules["src.rag.zotero_integration"],
        document_loader=modules["src.rag.document_loader"],
        vector_store=modules["src.rag.vector_store"],
        query_engine=modules["src.rag.query_engine"],
    )


class FakeChroma:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.deleted_filters: list[dict[str, Any]] = []
        self.added_batches: list[list[Document]] = []

    def delete(self, where: dict[str, Any]) -> None:
        self.deleted_filters.append(dict(where))

    def add_documents(self, docs: list[Document]) -> None:
        self.added_batches.append(list(docs))


def test_chroma_telemetry_is_explicitly_disabled(workspace_tmp_dir: Path) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    settings = rag.vector_store._chroma_client_settings()

    assert settings.anonymized_telemetry is False


def test_file_signature_ignores_mtime_changes(workspace_tmp_dir: Path) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    pdf_path = rag.data_dir / "papers" / "a.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 stable content")

    first = rag.zotero_integration._file_signature(str(pdf_path))
    os.utime(pdf_path, (1_700_000_000, 1_700_000_000))
    second = rag.zotero_integration._file_signature(str(pdf_path))

    assert first == second
    assert ":sha256:" in first


def test_zotero_signature_ignores_the_order_of_tags_differing_by_case(
    workspace_tmp_dir: Path,
) -> None:
    # Tags come out of a set; with key=str.lower alone, "HTS" and "hts" tied
    # and kept the set order, which changes per process: every restart then
    # reported documents as modified.
    from src.zotero_db import ZoteroItem

    rag = _load_rag_modules(workspace_tmp_dir)
    signature = rag.zotero_integration._zotero_signature
    first = ZoteroItem(title="T", tags=["hts", "HTS", "Magnet"], collections=["b", "B"])
    second = ZoteroItem(title="T", tags=["Magnet", "HTS", "hts"], collections=["B", "b"])

    assert signature(first) == signature(second)


def test_legacy_manifest_signature_is_migrated_without_reindex(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    pdf_path = rag.data_dir / "papers" / "a.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 same content")

    current = {
        "papers/a.pdf": {
            "abs_path": str(pdf_path),
            "file_sig": rag.zotero_integration._file_signature(str(pdf_path)),
            "legacy_file_sig": rag.zotero_integration._legacy_file_signature(str(pdf_path)),
            "zotero_sig": "meta-a",
        }
    }
    previous = {
        "papers/a.pdf": {
            "file_sig": current["papers/a.pdf"]["legacy_file_sig"],
            "zotero_sig": "meta-a",
            "chunk_count": 2,
        }
    }
    monkeypatch.setattr(
        rag.vector_store,
        "_build_chunks_from_pdfs",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No reindex expected")),
    )

    entries, resumed, indexed, *_ = rag.vector_store._index_pdf_groups_with_manifest(
        target_vectorstore=FakeChroma(),
        pdf_list=[("papers/a.pdf", str(pdf_path))],
        current_entries=current,
        manifest_entries=previous,
        chunk_size=1000,
        progress=lambda frac, desc="": None,
        progress_start=0.0,
        progress_end=1.0,
        read_label="Lecture",
        index_label="Vectorisation",
    )

    assert resumed == 1
    assert indexed == 0
    assert entries["papers/a.pdf"]["file_sig"] == current["papers/a.pdf"]["file_sig"]


def test_stable_manifest_detects_only_new_or_changed_files(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    captured_groups: list[list[tuple[str, str]]] = []
    current_entries = {
        "papers/a.pdf": {"abs_path": "X:/a.pdf", "file_sig": "stable-a", "zotero_sig": "meta-a"},
        "papers/b.pdf": {"abs_path": "X:/b.pdf", "file_sig": "stable-b", "zotero_sig": "meta-b"},
        "papers/c.pdf": {"abs_path": "X:/c.pdf", "file_sig": "stable-c", "zotero_sig": "meta-c"},
    }
    manifest_entries = {
        "papers/a.pdf": {
            "file_sig": "stable-a",
            "zotero_sig": "meta-a",
            "chunk_count": 1,
        },
        "papers/b.pdf": {"file_sig": "old-b", "zotero_sig": "meta-b"},
    }

    def fake_build_chunks_from_pdfs(**kwargs: Any) -> tuple[list[Document], list[Document], int]:
        captured_groups.append(list(kwargs["pdf_list"]))
        return (
            [],
            [
                Document(page_content="chunk-b", metadata={"rel_path": "papers/b.pdf"}),
                Document(page_content="chunk-c", metadata={"rel_path": "papers/c.pdf"}),
            ],
            1,
        )

    monkeypatch.setattr(rag.vector_store, "_build_chunks_from_pdfs", fake_build_chunks_from_pdfs)

    _entries, resumed, indexed, *_ = rag.vector_store._index_pdf_groups_with_manifest(
        target_vectorstore=FakeChroma(),
        pdf_list=[
            ("papers/a.pdf", "X:/a.pdf"),
            ("papers/b.pdf", "X:/b.pdf"),
            ("papers/c.pdf", "X:/c.pdf"),
        ],
        current_entries=current_entries,
        manifest_entries=manifest_entries,
        chunk_size=1000,
        progress=lambda frac, desc="": None,
        progress_start=0.0,
        progress_end=1.0,
        read_label="Lecture",
        index_label="Vectorisation",
    )

    assert resumed == 1
    assert indexed == 2
    assert captured_groups == [[("papers/b.pdf", "X:/b.pdf"), ("papers/c.pdf", "X:/c.pdf")]]


def test_prepared_article_cache_accepts_legacy_mtime_key(
    workspace_tmp_dir: Path,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    pdf_path = rag.data_dir / "a.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 cached article")
    rel_path = "a.pdf"
    file_sig = rag.zotero_integration._file_signature(str(pdf_path))
    legacy_file_sig = rag.zotero_integration._legacy_file_signature(str(pdf_path))
    legacy_key = rag.chunk_cache.compute_prepared_article_cache_key(
        rel_path=rel_path,
        file_sig=legacy_file_sig,
        zotero_sig="",
        chunk_size=1000,
    )
    article = {
        "article_id": "a",
        "title": "A",
        "rel_path": rel_path,
        "page_count": 1,
        "pdf_chunks": [
            {"text": "cached text", "metadata": {"rel_path": rel_path, "page": 1}},
        ],
        "note_chunks": [],
    }
    rag.chunk_cache.save_cached_prepared_article(
        article,
        cache_key=legacy_key,
        file_sig=legacy_file_sig,
        zotero_sig="",
        chunk_size=1000,
    )
    os.utime(pdf_path, (1_700_000_000, 1_700_000_000))

    note_chunks, pdf_chunks, page_count = rag.document_loader._load_or_prepare_single_pdf(
        rel_path,
        str(pdf_path),
        1000,
    )

    assert note_chunks == []
    assert page_count == 1
    assert [doc.page_content for doc in pdf_chunks] == ["cached text"]
    assert rag.chunk_cache.load_cached_prepared_article(
        "a",
        rag.chunk_cache.compute_prepared_article_cache_key(
            rel_path=rel_path,
            file_sig=file_sig,
            zotero_sig="",
            chunk_size=1000,
        ),
    )


def test_reset_vector_database_removes_files_and_clears_runtime_state(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    chroma_dir = rag.chroma_dir
    chroma_dir.mkdir(parents=True, exist_ok=True)
    (chroma_dir / "chroma.sqlite3").write_text("db", encoding="utf-8")

    rag.state.vectorstore = object()
    rag.state.document_chain = object()
    rag.state.last_sources = [Document(page_content="x")]
    rag.state._zotero_library = {"k": object()}

    closed: list[bool] = []
    monkeypatch.setattr(rag.vector_store, "_close_chroma_store", lambda store: closed.append(True))

    result = rag.vector_store.reset_vector_database()

    assert result["removed"] is True
    assert result["path"] == str(chroma_dir)
    assert closed == [True]
    assert rag.state.vectorstore is None
    assert rag.state.document_chain is None
    assert rag.state.last_sources == []
    assert rag.state._zotero_library == {}


def test_format_sources_api_exposes_chunk_uid_for_wiki_citation_links(
    workspace_tmp_dir: Path,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    docs = [
        Document(
            page_content="extrait",
            metadata={
                "source": "papers/demo.pdf",
                "page": 3,
                "chunk_uid": "ABC123:7",
                "article_id": "ABC123",
                "chunk_id": 7,
                "zotero_title": "Demo paper",
                "rag_retrieval_score": 0.87,
            },
        )
    ]

    payload = rag.query_engine.format_sources_api(docs)

    assert len(payload) == 1
    assert payload[0]["chunk_uid"] == "ABC123:7"
    assert payload[0]["ref_label"] in {"S1", "ABC123:7"}
    assert payload[0]["retrieval_score"] == 0.87


def test_format_sources_api_hides_leading_pdf_formula_glyph_noise(
    workspace_tmp_dir: Path,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    noisy = (
        "2 0\uf066\uf0d1= JT= \uf0d1\uf0b4 HT \uf066= -\uf0d1 "
        "( )J\uf072\uf072= A-V 2 AA Vt\uf06d\uf073 "
        "302 The option of a formulation is in principle arbitrary, however, "
        "in certain cases one formulation is more convenient than another."
    )
    docs = [
        Document(
            page_content=noisy,
            metadata={"source": "papers/demo.pdf", "page": 9, "chunk_uid": "ABC123:9"},
        )
    ]

    payload = rag.query_engine.format_sources_api(docs)

    assert len(payload) == 1
    assert payload[0]["snippet"].startswith("The option of a formulation")
    assert "\uf066" not in payload[0]["snippet"]
    assert "JT=" not in payload[0]["snippet"]


def test_auto_visual_router_uses_structured_llm_decision(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    docs = [
        Document(
            page_content="Figure 2 compares measured and simulated losses.",
            metadata={"rel_path": "papers/demo.pdf", "zotero_title": "Demo"},
        )
    ]
    candidates = rag.query_engine._build_auto_visual_candidates(docs)
    captured: dict[str, Any] = {}

    def fake_structured(model: str, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        captured.update(model=model, messages=messages, response_model=kwargs.get("response_model"))
        return rag.query_engine.AutoVisualAnalysisDecision(
            analyze=True,
            source_index=1,
            reason="La question demande les valeurs de la courbe.",
        )

    monkeypatch.setattr(rag.query_engine, "invoke_structured_chat", fake_structured)
    decision = rag.query_engine._decide_auto_visual_analysis(
        "Quelles valeurs montre la courbe ?",
        "text-model:4b",
        candidates,
    )

    assert decision.analyze is True
    assert decision.source_index == 1
    assert captured["model"] == "text-model:4b"
    assert captured["response_model"] is rag.query_engine.AutoVisualAnalysisDecision
    assert "USER QUESTION" in captured["messages"][1]["content"]


def test_auto_visual_enrichment_adds_bounded_visual_sources(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    data_dir = workspace_tmp_dir / "data"
    pdf_path = data_dir / "papers" / "demo.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(rag.query_engine.config, "DATA_PATH", str(data_dir))
    monkeypatch.setattr(
        rag.query_engine,
        "get_app_settings",
        lambda: {
            "visual_analysis_mode": "on_demand",
            "vision_model": "vision:2b",
            "visual_description_model": "vision:9b",
            "visual_analysis_auto_chat": True,
        },
    )
    monkeypatch.setattr(
        rag.query_engine,
        "_decide_auto_visual_analysis",
        lambda *_args, **_kwargs: rag.query_engine.AutoVisualAnalysisDecision(
            analyze=True,
            source_index=1,
            reason="graph needed",
        ),
    )
    captured: dict[str, Any] = {}

    def fake_visual(
        path: str,
        *,
        model: str,
        analysis_model: str,
        max_pages: int,
        question: str = "",
    ) -> dict[str, Any]:
        captured.update(
            path=path,
            model=model,
            analysis_model=analysis_model,
            max_pages=max_pages,
            question=question,
        )
        return {
            "results": [
                {
                    "page": 4,
                    "text": "La série rouge atteint 8 A.",
                    "visual_source": "embedded_image",
                    "figure_label": "Figure 2",
                    "caption": "Figure 2. Courant mesuré.",
                }
            ]
        }

    monkeypatch.setattr(rag.query_engine, "analyze_pdf_visuals", fake_visual)
    monkeypatch.setattr(rag.query_engine, "is_remote_provider", lambda: True)
    docs = [
        Document(
            page_content="See Figure 2 for the measured current.",
            metadata={
                "source": str(pdf_path),
                "rel_path": "papers/demo.pdf",
                "article_id": "DEMO",
                "zotero_title": "Demo paper",
                "content_type": "pdf_page",
            },
        )
    ]

    generator = rag.query_engine._stream_auto_visual_enrichment(
        "What value does the red series reach?",
        "text-model:4b",
        docs,
    )
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            enriched = stop.value
            break

    assert captured == {
        "path": str(pdf_path),
        "model": "vision:2b",
        "analysis_model": "vision:9b",
        "max_pages": 3,
        "question": "What value does the red series reach?",
    }
    assert len(enriched) == 2
    assert enriched[1].metadata["content_type"] == "figure_ocr"
    assert enriched[1].metadata["auto_visual_analysis"] is True
    assert enriched[1].metadata["page"] == 4
    assert enriched[1].metadata["visual_source"] == "embedded_image"
    assert enriched[1].metadata["ocr_model"] == "vision:2b"
    assert enriched[1].metadata["visual_analysis_model"] == "vision:9b"
    assert enriched[1].metadata["figure_label"] == "Figure 2"
    assert enriched[1].metadata["figure_caption"] == "Figure 2. Courant mesuré."
    assert any("3 pages maximum" in event.get("msg", "") for event in events)


def test_forced_visual_enrichment_bypasses_auto_router(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    data_dir = workspace_tmp_dir / "data"
    pdf_path = data_dir / "papers" / "demo.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(rag.query_engine.config, "DATA_PATH", str(data_dir))
    monkeypatch.setattr(
        rag.query_engine,
        "get_app_settings",
        lambda: {
            "visual_analysis_mode": "on_demand",
            "vision_model": "vision:2b",
            "visual_analysis_auto_chat": False,
        },
    )
    monkeypatch.setattr(
        rag.query_engine,
        "_decide_auto_visual_analysis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("the automatic router must be bypassed")
        ),
    )
    captured: dict[str, Any] = {}

    def fake_analysis(candidate: dict[str, Any], **kwargs: Any) -> list[Document]:
        captured.update(candidate=candidate, **kwargs)
        return [
            Document(
                page_content="La figure 8 atteint 12 T.",
                metadata={"content_type": "figure_ocr", "forced_visual_analysis": True},
            )
        ]

    monkeypatch.setattr(rag.query_engine, "_analyze_auto_visual_candidate", fake_analysis)
    monkeypatch.setattr(rag.query_engine, "is_remote_provider", lambda: True)
    docs = [
        Document(
            page_content="See Figure 8 for the result.",
            metadata={
                "rel_path": "papers/demo.pdf",
                "zotero_title": "Demo paper",
                "content_type": "pdf_page",
            },
        )
    ]

    generator = rag.query_engine._stream_auto_visual_enrichment(
        "Que montre la figure 8 ?",
        "text-model:4b",
        docs,
        force_visual_analysis=True,
    )
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            enriched = stop.value
            break

    assert len(enriched) == 2
    assert captured["vision_model"] == "vision:2b"
    assert captured["question"] == "Que montre la figure 8 ?"
    assert captured["forced"] is True
    assert any("Lecture forcee" in event.get("msg", "") for event in events)


def test_auto_visual_failure_keeps_text_sources(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    monkeypatch.setattr(
        rag.query_engine,
        "get_app_settings",
        lambda: {
            "visual_analysis_mode": "on_demand",
            "vision_model": "vision:2b",
            "visual_analysis_auto_chat": True,
        },
    )
    monkeypatch.setattr(
        rag.query_engine,
        "_decide_auto_visual_analysis",
        lambda *_args, **_kwargs: rag.query_engine.AutoVisualAnalysisDecision(
            analyze=True,
            source_index=1,
        ),
    )
    monkeypatch.setattr(rag.query_engine, "is_remote_provider", lambda: True)
    monkeypatch.setattr(
        rag.query_engine,
        "_analyze_auto_visual_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("vision offline")),
    )
    docs = [
        Document(
            page_content="Figure 1 reports the result.",
            metadata={"rel_path": "papers/demo.pdf", "content_type": "pdf_page"},
        )
    ]

    generator = rag.query_engine._stream_auto_visual_enrichment("Lire la figure", "text:4b", docs)
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            enriched = stop.value
            break

    assert enriched == docs
    assert any("indisponible" in event.get("msg", "") for event in events)


def test_vector_database_status_reports_missing_current_profile_when_other_profile_exists(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    rag.vector_store._save_manifest_entries(
        {"papers/demo.pdf": {"file_sig": "sig", "zotero_sig": "meta"}},
        1000,
        "embed-old",
    )
    monkeypatch.setattr(rag.vector_store, "_current_embedding_profile", lambda: "embed-new")

    status = rag.vector_store.get_vector_database_status()

    assert status["status"] == "missing"
    assert status["embedding_model_mismatch"] is False
    assert status["current_embedding_model"] == "embed-new"
    assert status["available_profile_count"] >= 1
    assert "Recharger PDF" in status["warning"]
    assert "autres index sont conserves" in status["warning"]


def test_manifest_save_uses_atomic_json_writer(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    writes: list[tuple[Path, dict[str, Any]]] = []
    monkeypatch.setattr(
        rag.vector_store,
        "atomic_write_json",
        lambda path, payload: writes.append((Path(path), dict(payload))),
    )

    rag.vector_store._save_manifest_entries(
        {
            "papers/demo.pdf": {
                "file_sig": "sig",
                "zotero_sig": "meta",
                "chunk_count": 3,
            }
        },
        1000,
        "embed-demo",
    )

    assert len(writes) == 1
    path, payload = writes[0]
    assert path.name == "index_manifest.json"
    assert payload["version"] == 3
    assert payload["entries"]["papers/demo.pdf"]["file_sig"] == "sig"
    assert payload["entries"]["papers/demo.pdf"]["chunk_count"] == 3


def test_vector_database_status_explains_how_to_resume_an_incomplete_profile(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    monkeypatch.setattr(rag.vector_store, "_current_embedding_profile", lambda: "embed-current")
    profile = rag.vector_store.build_vector_index_profile(
        chunk_size=1000,
        embedding_model="embed-current",
    )
    profile_path = Path(profile["path"])
    profile_path.mkdir(parents=True, exist_ok=True)
    (profile_path / "chroma.sqlite3").write_bytes(b"")
    rag.vector_store._mark_index_incomplete(db_path=str(profile_path))

    status = rag.vector_store.get_vector_database_status()

    assert status["status"] == "incomplete"
    assert "interrompue" in status["warning"]
    assert "Recharger PDF" in status["warning"]


def test_index_pdf_groups_with_manifest_only_reindexes_changed_files(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    captured_groups: list[list[tuple[str, str]]] = []

    def fake_build_chunks_from_pdfs(
        pdf_list: list[tuple[str, str]],
        chunk_size: int,
        progress: Any,
        progress_start: float,
        progress_end: float,
        status_label: str = "Lecture PDF",
        textless_paths: set[str] | None = None,
    ) -> tuple[list[Document], list[Document], int]:
        captured_groups.append(list(pdf_list))
        return (
            [Document(page_content="note", metadata={"rel_path": "papers/b.pdf"})],
            [Document(page_content="pdf", metadata={"rel_path": "papers/b.pdf"})],
            7,
        )

    monkeypatch.setattr(rag.vector_store, "_build_chunks_from_pdfs", fake_build_chunks_from_pdfs)

    vectorstore = FakeChroma()
    current_entries = {
        "papers/a.pdf": {"file_sig": "same-a", "zotero_sig": "meta-a"},
        "papers/b.pdf": {"file_sig": "new-b", "zotero_sig": "meta-b"},
    }
    manifest_entries = {
        "papers/a.pdf": {
            "file_sig": "same-a",
            "zotero_sig": "meta-a",
            "chunk_count": 2,
        },
        "papers/b.pdf": {"file_sig": "old-b", "zotero_sig": "meta-b"},
    }

    entries, resumed, indexed, pages, note_chunks, pdf_chunks = (
        rag.vector_store._index_pdf_groups_with_manifest(
            target_vectorstore=vectorstore,
            pdf_list=[
                ("papers/a.pdf", "X:/papers/a.pdf"),
                ("papers/b.pdf", "X:/papers/b.pdf"),
            ],
            current_entries=current_entries,
            manifest_entries=manifest_entries,
            chunk_size=1000,
            progress=lambda frac, desc="": None,
            progress_start=0.0,
            progress_end=1.0,
            read_label="Lecture",
            index_label="Vectorisation",
        )
    )

    assert resumed == 1
    assert indexed == 1
    assert pages == 7
    assert note_chunks == 1
    assert pdf_chunks == 1
    assert captured_groups == [[("papers/b.pdf", "X:/papers/b.pdf")]]
    assert vectorstore.deleted_filters == [{"rel_path": "papers/b.pdf"}]
    assert entries["papers/a.pdf"]["file_sig"] == "same-a"
    assert entries["papers/b.pdf"]["file_sig"] == "new-b"
    assert entries["papers/b.pdf"]["chunk_count"] == 2


def test_index_pdf_groups_does_not_manifest_or_resume_zero_chunk_pdf(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    captured_groups: list[list[tuple[str, str]]] = []

    def fake_build_chunks_from_pdfs(**kwargs: Any) -> tuple[list[Document], list[Document], int]:
        group = list(kwargs["pdf_list"])
        captured_groups.append(group)
        docs = []
        if any(rel_path == "papers/readable.pdf" for rel_path, _ in group):
            docs.append(
                Document(
                    page_content="readable",
                    metadata={"rel_path": "papers/readable.pdf"},
                )
            )
        return [], docs, 1

    monkeypatch.setattr(rag.vector_store, "_build_chunks_from_pdfs", fake_build_chunks_from_pdfs)
    current_entries = {
        "papers/readable.pdf": {"file_sig": "sig-readable", "zotero_sig": ""},
        "papers/unreadable.pdf": {"file_sig": "sig-unreadable", "zotero_sig": ""},
    }
    vectorstore = FakeChroma()

    entries, resumed, indexed, *_ = rag.vector_store._index_pdf_groups_with_manifest(
        target_vectorstore=vectorstore,
        pdf_list=[
            ("papers/readable.pdf", "X:/papers/readable.pdf"),
            ("papers/unreadable.pdf", "X:/papers/unreadable.pdf"),
        ],
        current_entries=current_entries,
        manifest_entries={},
        chunk_size=1000,
        progress=lambda frac, desc="": None,
        progress_start=0.0,
        progress_end=1.0,
        read_label="Lecture",
        index_label="Vectorisation",
    )

    assert resumed == 0
    assert indexed == 1
    assert entries["papers/readable.pdf"]["chunk_count"] == 1
    assert "papers/unreadable.pdf" not in entries

    second_entries, resumed, indexed, *_ = rag.vector_store._index_pdf_groups_with_manifest(
        target_vectorstore=vectorstore,
        pdf_list=[
            ("papers/readable.pdf", "X:/papers/readable.pdf"),
            ("papers/unreadable.pdf", "X:/papers/unreadable.pdf"),
        ],
        current_entries=current_entries,
        manifest_entries=entries,
        chunk_size=1000,
        progress=lambda frac, desc="": None,
        progress_start=0.0,
        progress_end=1.0,
        read_label="Lecture",
        index_label="Vectorisation",
    )

    assert resumed == 1
    assert indexed == 0
    assert second_entries == entries
    assert captured_groups[-1] == [("papers/unreadable.pdf", "X:/papers/unreadable.pdf")]


def test_index_pdf_groups_records_a_pdf_read_without_text_and_stops_offering_it(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    # A scan with no text layer used to be dropped from the manifest, so every
    # reload preview listed it as new and every reload deleted its OCR pages.
    rag = _load_rag_modules(workspace_tmp_dir)
    captured_groups: list[list[tuple[str, str]]] = []

    def fake_build_chunks_from_pdfs(**kwargs: Any) -> tuple[list[Document], list[Document], int]:
        group = list(kwargs["pdf_list"])
        captured_groups.append(group)
        for rel_path, _ in group:
            if rel_path == "papers/scan.pdf":
                kwargs["textless_paths"].add(rel_path)
        return [], [], 0

    monkeypatch.setattr(rag.vector_store, "_build_chunks_from_pdfs", fake_build_chunks_from_pdfs)
    current_entries = {
        "papers/scan.pdf": {"file_sig": "sig-scan", "zotero_sig": "z", "visual_sig": "off"},
        "papers/broken.pdf": {"file_sig": "sig-broken", "zotero_sig": "", "visual_sig": "off"},
    }
    pdf_list = [
        ("papers/scan.pdf", "X:/papers/scan.pdf"),
        ("papers/broken.pdf", "X:/papers/broken.pdf"),
    ]
    run = dict(
        target_vectorstore=FakeChroma(),
        pdf_list=pdf_list,
        current_entries=current_entries,
        chunk_size=1000,
        progress=lambda frac, desc="": None,
        progress_start=0.0,
        progress_end=1.0,
        read_label="Lecture",
        index_label="Vectorisation",
    )

    entries, _resumed, indexed, *_ = rag.vector_store._index_pdf_groups_with_manifest(
        manifest_entries={}, **run
    )

    assert indexed == 0
    assert entries["papers/scan.pdf"]["chunk_count"] == 0
    assert entries["papers/scan.pdf"]["no_text"] is True
    # A file whose read failed is still retried.
    assert "papers/broken.pdf" not in entries

    db_path = workspace_tmp_dir / "textless-index"
    rag.vector_store._save_manifest_entries(entries, 1000, "embed", db_path=str(db_path))
    reloaded = rag.vector_store._load_manifest_entries(db_path=str(db_path))
    assert reloaded["papers/scan.pdf"]["no_text"] is True

    changes = rag.vector_store._preview_manifest_changes(
        current_entries=current_entries, previous_entries=reloaded
    )
    assert changes["new_paths"] == ["papers/broken.pdf"]
    assert changes["unchanged_paths"] == ["papers/scan.pdf"]

    _entries, resumed, *_ = rag.vector_store._index_pdf_groups_with_manifest(
        manifest_entries=reloaded, **run
    )
    assert resumed == 1
    assert captured_groups[-1] == [("papers/broken.pdf", "X:/papers/broken.pdf")]

    # Once the file changes, it is read again like any other document.
    changed = dict(current_entries, **{"papers/scan.pdf": {"file_sig": "sig-new", "zotero_sig": "z"}})
    assert not rag.vector_store._manifest_entry_matches(
        reloaded["papers/scan.pdf"], changed["papers/scan.pdf"]
    )


def test_single_pdf_load_reports_textless_files_but_not_failed_reads(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    loader = rag.document_loader
    pdf_path = rag.data_dir / "papers" / "scan.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 scan")
    monkeypatch.setattr(loader, "visual_analysis_model_settings", lambda: ("off", "", ""))

    textless: set[str] = set()
    monkeypatch.setattr(loader, "load_document_units", lambda *_args, **_kwargs: [])
    assert loader._load_and_chunk_single_pdf(
        "papers/scan.pdf", str(pdf_path), 1000, textless_paths=textless
    ) == ([], [], 0)
    assert textless == {"papers/scan.pdf"}

    def unreadable(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise OSError("locked")

    textless.clear()
    monkeypatch.setattr(loader, "load_document_units", unreadable)
    loader._load_and_chunk_single_pdf("papers/scan.pdf", str(pdf_path), 1000, textless_paths=textless)
    assert textless == set()


def test_build_chunks_from_pdfs_uses_real_threadpool_worker_count(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    monkeypatch.setenv("RAG_PDF_WORKERS", "3")

    captured: dict[str, Any] = {}

    class CapturingExecutor:
        def __init__(self, max_workers: int) -> None:
            captured["max_workers"] = max_workers
            self._inner = RealThreadPoolExecutor(max_workers=max_workers)

        def submit(self, *args: Any, **kwargs: Any) -> Any:
            return self._inner.submit(*args, **kwargs)

        def shutdown(self, *args: Any, **kwargs: Any) -> None:
            self._inner.shutdown(*args, **kwargs)

    monkeypatch.setattr(rag.document_loader, "ThreadPoolExecutor", CapturingExecutor)
    monkeypatch.setattr(
        rag.document_loader,
        "_load_or_prepare_single_pdf",
        lambda rel_path, full_path, chunk_size: (
            [],
            [Document(page_content=f"chunk:{rel_path}", metadata={"rel_path": rel_path})],
            1,
        ),
    )

    progress_messages: list[str] = []
    note_chunks, pdf_chunks, page_count = rag.document_loader._build_chunks_from_pdfs(
        pdf_list=[
            ("papers/a.pdf", "X:/papers/a.pdf"),
            ("papers/b.pdf", "X:/papers/b.pdf"),
        ],
        chunk_size=1000,
        progress=lambda frac, desc="": progress_messages.append(desc),
        progress_start=0.0,
        progress_end=1.0,
        status_label="Lecture PDF",
    )

    assert captured["max_workers"] == 3
    assert note_chunks == []
    assert len(pdf_chunks) == 2
    assert page_count == 2
    assert any("demarrage parallel (3 workers)" in message for message in progress_messages)


def test_init_rag_initial_build_creates_vector_index_and_reports_workers(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    saved_manifest: list[tuple[dict[str, Any], int, str, str]] = []

    monkeypatch.setattr(rag.query_engine.state, "find_pdfs", lambda: [("papers/a.pdf", "X:/a.pdf")])
    monkeypatch.setattr(rag.query_engine, "_refresh_zotero_library_cache", lambda: None)
    monkeypatch.setattr(rag.query_engine, "_current_embedding_model", lambda: "embed-a")
    monkeypatch.setattr(
        rag.query_engine, "current_embedding_profile_label", lambda model: "embed-a"
    )
    monkeypatch.setattr(rag.query_engine, "get_embedding_provider_mode", lambda: "remote")
    monkeypatch.setattr(rag.query_engine, "requires_local_ollama_embeddings", lambda: False)
    monkeypatch.setattr(rag.query_engine, "create_embeddings", lambda model: object())
    monkeypatch.setattr(rag.query_engine, "create_chat_llm", lambda model_name: object())
    monkeypatch.setattr(
        rag.query_engine,
        "create_stuff_documents_chain",
        lambda llm, prompt: {"chain": "ok"},
    )
    monkeypatch.setattr(rag.query_engine, "provider_runtime_label", lambda embedding_model: "local")
    monkeypatch.setattr(rag.query_engine, "Chroma", FakeChroma)
    monkeypatch.setattr(
        rag.query_engine,
        "_index_pdf_groups_with_manifest",
        lambda **kwargs: (
            {"papers/a.pdf": {"file_sig": "sig-a", "zotero_sig": ""}},
            0,
            1,
            3,
            1,
            4,
        ),
    )
    monkeypatch.setattr(
        rag.query_engine,
        "_save_manifest_entries",
        lambda entries, chunk_size, embedding_model, **kwargs: saved_manifest.append(
            (dict(entries), int(chunk_size), str(embedding_model), str(kwargs.get("db_path") or ""))
        ),
    )

    ok, msg = rag.query_engine.init_rag("chat-model", 0.3, force_reload=False)

    assert ok is True
    assert "Indexation initiale (reprenable)" in msg
    assert "workers:" in msg
    assert saved_manifest
    assert saved_manifest[0][0] == {"papers/a.pdf": {"file_sig": "sig-a", "zotero_sig": ""}}
    assert saved_manifest[0][1] == 1000
    assert saved_manifest[0][2] == "embed-a"
    assert "indexes" in saved_manifest[0][3]
    assert isinstance(rag.query_engine.state.vectorstore, FakeChroma)


def test_set_active_vector_index_profile_persists_selected_profile(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    monkeypatch.setattr(rag.vector_store, "_current_embedding_model", lambda: "embed-b")

    payload = rag.vector_store.set_active_vector_index_profile(
        chunk_size=1536,
        embedding_model="embed-b",
        exists=False,
    )
    stored = rag.vector_store._load_active_vector_index_profile()

    assert payload["profile_id"] == stored["profile_id"]
    assert stored["embedding_model"] == "embed-b"
    assert stored["chunk_size"] == 1536
    assert stored["exists"] is False


def test_set_active_vector_index_profile_reuses_an_existing_profile(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    monkeypatch.setattr(rag.vector_store, "_current_embedding_model", lambda: "embed-local")
    profile = rag.vector_store.build_vector_index_profile(
        chunk_size=1000,
        embedding_model="embed-local",
    )
    profile_path = Path(profile["path"])
    profile_path.mkdir(parents=True, exist_ok=True)
    (profile_path / "chroma.sqlite3").write_bytes(b"ready")

    payload = rag.vector_store.set_active_vector_index_profile(
        chunk_size=1000,
        embedding_model="embed-local",
        exists=False,
    )

    assert payload["path"] == str(profile_path)
    assert payload["exists"] is True
    assert payload["ready"] is True
    assert payload["reused"] is True
    assert payload["source"] == "profile"


def test_init_rag_force_reload_stays_incremental_when_embedding_is_unchanged(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    saved_manifests: list[dict[str, Any]] = []

    monkeypatch.setattr(rag.query_engine.state, "find_pdfs", lambda: [("papers/a.pdf", "X:/a.pdf")])
    monkeypatch.setattr(rag.query_engine, "_refresh_zotero_library_cache", lambda: None)
    monkeypatch.setattr(rag.query_engine, "_current_embedding_model", lambda: "embed-a")
    monkeypatch.setattr(
        rag.query_engine, "current_embedding_profile_label", lambda model: "embed-a"
    )
    monkeypatch.setattr(rag.query_engine, "get_embedding_provider_mode", lambda: "remote")
    monkeypatch.setattr(rag.query_engine, "requires_local_ollama_embeddings", lambda: False)
    monkeypatch.setattr(rag.query_engine, "create_embeddings", lambda model: object())
    monkeypatch.setattr(rag.query_engine, "create_chat_llm", lambda model_name: object())
    monkeypatch.setattr(
        rag.query_engine,
        "create_stuff_documents_chain",
        lambda llm, prompt: {"chain": "ok"},
    )
    monkeypatch.setattr(rag.query_engine, "provider_runtime_label", lambda embedding_model: "local")
    monkeypatch.setattr(rag.query_engine, "Chroma", FakeChroma)
    monkeypatch.setattr(
        rag.query_engine,
        "_compute_index_entries",
        lambda pdf_list: {
            "papers/a.pdf": {"abs_path": "X:/a.pdf", "file_sig": "sig", "zotero_sig": ""}
        },
    )
    profile = rag.query_engine.resolve_vector_index_profile(
        chunk_size=1000, embedding_model="embed-a"
    )
    profile_path = Path(str(profile["path"]))
    profile_path.mkdir(parents=True, exist_ok=True)
    (profile_path / "placeholder.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        rag.query_engine,
        "_load_manifest_entries",
        lambda **kwargs: {"papers/a.pdf": {"file_sig": "sig", "zotero_sig": "", "chunk_count": 1}},
    )
    monkeypatch.setattr(rag.query_engine, "_load_manifest_chunk_size", lambda **kwargs: 1000)
    monkeypatch.setattr(
        rag.query_engine, "_load_manifest_embedding_model", lambda **kwargs: "embed-a"
    )
    monkeypatch.setattr(
        rag.query_engine,
        "_index_pdf_groups_with_manifest",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("No PDF reindex expected")),
    )
    monkeypatch.setattr(
        rag.query_engine,
        "_save_manifest_entries",
        lambda entries, *_args, **_kwargs: saved_manifests.append(dict(entries)),
    )
    monkeypatch.setattr(
        rag.query_engine.shutil,
        "rmtree",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Full rebuild not expected")),
    )

    ok, msg = rag.query_engine.init_rag("chat-model", 0.3, force_reload=True)

    assert saved_manifests == [
        {
            "papers/a.pdf": {
                "file_sig": "sig",
                "zotero_sig": "",
                "visual_sig": "off",
                "chunk_count": 1,
            }
        }
    ]

    assert ok is True
    assert "Mise a jour incrementale (reprenable)" in msg
    assert "reindexes: 0 documents" in msg


def test_init_rag_force_reload_invalidates_document_scan_cache(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    events: list[str] = []

    def find_pdfs() -> list[tuple[str, str]]:
        events.append("scanned")
        return []

    monkeypatch.setattr(
        rag.query_engine.state,
        "clear_document_scan_cache",
        lambda: events.append("cache-cleared"),
    )
    monkeypatch.setattr(
        rag.query_engine.state,
        "find_pdfs",
        find_pdfs,
    )
    monkeypatch.setattr(rag.query_engine, "_refresh_zotero_library_cache", lambda: None)
    monkeypatch.setattr(rag.query_engine, "_current_embedding_model", lambda: "embed-a")
    monkeypatch.setattr(
        rag.query_engine, "current_embedding_profile_label", lambda model: "embed-a"
    )
    monkeypatch.setattr(rag.query_engine, "get_embedding_provider_mode", lambda: "remote")
    monkeypatch.setattr(rag.query_engine, "requires_local_ollama_embeddings", lambda: False)
    monkeypatch.setattr(rag.query_engine, "create_embeddings", lambda model: object())

    ok, msg = rag.query_engine.init_rag("chat-model", 0.3, force_reload=True)

    assert ok is False
    assert "Aucun document" in msg
    assert events == ["cache-cleared", "scanned"]


def test_init_rag_force_reload_removes_last_document_from_existing_index(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    saved_manifests: list[dict[str, Any]] = []
    deleted_lexical_paths: list[list[str]] = []

    monkeypatch.setattr(rag.query_engine.state, "clear_document_scan_cache", lambda: None)
    monkeypatch.setattr(rag.query_engine.state, "find_pdfs", lambda: [])
    monkeypatch.setattr(rag.query_engine, "_refresh_zotero_library_cache", lambda: None)
    monkeypatch.setattr(rag.query_engine, "_current_embedding_model", lambda: "embed-a")
    monkeypatch.setattr(
        rag.query_engine, "current_embedding_profile_label", lambda model: "embed-a"
    )
    monkeypatch.setattr(rag.query_engine, "get_embedding_provider_mode", lambda: "remote")
    monkeypatch.setattr(rag.query_engine, "requires_local_ollama_embeddings", lambda: False)
    monkeypatch.setattr(rag.query_engine, "create_embeddings", lambda model: object())
    monkeypatch.setattr(rag.query_engine, "create_chat_llm", lambda model_name: object())
    monkeypatch.setattr(
        rag.query_engine,
        "create_stuff_documents_chain",
        lambda llm, prompt: {"chain": "ok"},
    )
    monkeypatch.setattr(rag.query_engine, "provider_runtime_label", lambda model: "local")
    monkeypatch.setattr(rag.query_engine, "Chroma", FakeChroma)
    monkeypatch.setattr(rag.query_engine, "ensure_lexical_index", lambda *args: True)
    monkeypatch.setattr(
        rag.query_engine,
        "delete_lexical_paths",
        lambda _db_path, paths: deleted_lexical_paths.append(list(paths)),
    )
    profile = rag.query_engine.resolve_vector_index_profile(
        chunk_size=1000, embedding_model="embed-a"
    )
    profile_path = Path(str(profile["path"]))
    profile_path.mkdir(parents=True, exist_ok=True)
    (profile_path / "placeholder.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        rag.query_engine,
        "_load_manifest_entries",
        lambda **kwargs: {"papers/last.pdf": {"file_sig": "sig", "zotero_sig": ""}},
    )
    monkeypatch.setattr(rag.query_engine, "_load_manifest_chunk_size", lambda **kwargs: 1000)
    monkeypatch.setattr(
        rag.query_engine, "_load_manifest_embedding_model", lambda **kwargs: "embed-a"
    )
    monkeypatch.setattr(
        rag.query_engine,
        "_save_manifest_entries",
        lambda entries, *_args, **_kwargs: saved_manifests.append(dict(entries)),
    )

    ok, msg = rag.query_engine.init_rag("chat-model", 0.3, force_reload=True)

    assert ok is True
    assert "supprimes: 1 document" in msg
    assert "total: 0 documents" in msg
    assert rag.query_engine.state.vectorstore.deleted_filters == [{"rel_path": "papers/last.pdf"}]
    assert deleted_lexical_paths == [["papers/last.pdf"]]
    assert saved_manifests == [{}]


def test_init_rag_validates_lmstudio_embedding_before_build(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    monkeypatch.setattr(rag.query_engine, "_current_embedding_model", lambda: "gemma-4-26b-a4b-it")
    monkeypatch.setattr(
        rag.query_engine,
        "current_embedding_profile_label",
        lambda model: f"lmstudio:{model}",
    )
    monkeypatch.setattr(rag.query_engine, "get_embedding_provider_mode", lambda: "lmstudio")
    monkeypatch.setattr(
        rag.query_engine,
        "validate_local_embedding_model",
        lambda model: (
            False,
            str(model),
            "Aucun modele d'embedding LM Studio charge. Chargez-en un puis reessayez.",
        ),
    )
    monkeypatch.setattr(
        rag.query_engine,
        "create_embeddings",
        lambda model: (_ for _ in ()).throw(
            AssertionError("create_embeddings ne doit pas etre appele")
        ),
    )

    ok, msg = rag.query_engine.init_rag("chat-model", 0.3, force_reload=False)

    assert ok is False
    assert "LM Studio" in msg


def test_response_style_guide_includes_model_profile_note(workspace_tmp_dir: Path) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    guide = rag.query_engine._build_response_style_guide(
        "je cherche des formulations HTS sur COMSOL",
        model_name="gemma4:e2b",
    )

    assert "Consignes specifiques au modele:" in guide
    assert "redige toute la reponse visible en francais" in guide
    assert "Do not use mandatory wrapper sections" in guide
    assert "Never emit Gemma channel markers" in guide


def test_response_style_guide_follows_ui_language_setting(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    monkeypatch.setattr(rag.query_engine, "get_app_settings", lambda: {"ui_language": "en"})

    guide = rag.query_engine._build_response_style_guide(
        "je cherche des formulations HTS sur COMSOL",
        model_name="llama3.2:3b",
    )
    system_prompt = rag.query_engine._language_bound_system_prompt("context")

    assert "write the entire visible answer in English" in guide
    assert "write the entire visible answer in English" in system_prompt
    assert "redige toute la reponse visible en francais" not in guide


def test_response_style_guide_treats_library_themes_as_supplied_context(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    monkeypatch.setattr(rag.query_engine, "get_app_settings", lambda: {"ui_language": "en"})

    guide = rag.query_engine._build_response_style_guide(
        "What are the main themes in my library?",
        model_name="lfm2.5:8b",
    )

    assert "Library-overview task" in guide
    assert "do not claim that no library context was supplied" in guide
    assert "indexed evidence" in guide


def test_library_overview_document_selection_prefers_distinct_articles(
    workspace_tmp_dir: Path,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    docs = [
        Document(page_content="A1", metadata={"article_id": "A", "source": "a.pdf"}),
        Document(page_content="A2", metadata={"article_id": "A", "source": "a.pdf"}),
        Document(page_content="B1", metadata={"article_id": "B", "source": "b.pdf"}),
        Document(page_content="C1", metadata={"article_id": "C", "source": "c.pdf"}),
    ]

    selected = rag.query_engine._diversify_documents_by_article(docs, limit=3)

    assert [doc.metadata["article_id"] for doc in selected] == ["A", "B", "C"]


def test_library_overview_retrieval_widens_pool_and_returns_distinct_articles(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    requested_k: list[int] = []
    scored = [
        (
            Document(
                page_content=text,
                metadata={"article_id": article_id, "source": f"{article_id}.pdf"},
            ),
            score,
        )
        for article_id, text, score in [
            ("A", "A first", 0.99),
            ("A", "A second", 0.98),
            ("B", "B first", 0.97),
            ("C", "C first", 0.96),
        ]
    ]

    def fake_collect(_query: str, k: int) -> list[tuple[Document, float]]:
        requested_k.append(k)
        return scored

    monkeypatch.setattr(rag.query_engine, "_collect_scored_docs", fake_collect)
    monkeypatch.setattr(
        rag.query_engine,
        "_collect_lexical_metadata_docs",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(rag.query_engine, "_is_reranker_enabled", lambda: False)

    selected = rag.query_engine._retrieve_documents(
        "What are the main themes in my library?",
        threshold=0.0,
        max_chunks=3,
        filters={},
        depth="quick",
    )

    assert requested_k == [64]
    assert [doc.metadata["article_id"] for doc in selected] == ["A", "B", "C"]


def test_named_query_anchors_ignore_french_imperative_clitics(
    workspace_tmp_dir: Path,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    query = (
        "D'apres les articles de ma bibliotheque, quelles limites des approches "
        "analytiques sont rapportees pour modeliser les pertes AC dans les aimants "
        "supraconducteurs ? Reponds uniquement avec ce qui est etaye par les sources "
        "et cite-les."
    )

    assert rag.query_engine._named_query_anchor_groups(query) == []
    assert rag.query_engine._named_query_anchor_groups(
        "Quelles valeurs utilise le modele Kim-Anderson ?"
    ) == [("kim", "anderson")]


def test_response_style_guide_expands_detailed_article_analysis(workspace_tmp_dir: Path) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    guide = rag.query_engine._build_response_style_guide(
        "Fais une analyse détaillée de cet article",
        model_name="demo-model",
    )

    assert "pas d'un resume allonge" in guide
    assert "700 a 1 200 mots" in guide
    assert "chaine methodologique" in guide
    assert "Prefere 1 ou 2 courts paragraphes" not in guide


def test_history_context_preserves_assistant_source_labels(workspace_tmp_dir: Path) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    context = rag.query_engine._build_history_context(
        [
            {"role": "user", "content": "Quel article traite des courants d'ecran ?"},
            {
                "role": "assistant",
                "content": "Le document principal est [S1].",
                "meta": {
                    "sources": [
                        {
                            "ref_label": "S1",
                            "title": (
                                "Experimental and numerical study of screening currents in "
                                "REBCO high temperature superconducting magnets"
                            ),
                        }
                    ]
                },
            },
        ]
    )

    assert "historique immuable" in context
    assert "[S1] Experimental and numerical study of screening currents" in context


def test_stream_chat_api_resolves_recent_s1_title_without_new_retrieval(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()
    source: dict[str, Any] = {
        "index": 1,
        "ref_label": "S1",
        "title": (
            "Experimental and numerical study of screening currents in REBCO high "
            "temperature superconducting magnets"
        ),
        "file": "screening-currents.pdf",
    }
    history = [
        {"role": "user", "content": "Quel document est pertinent ?"},
        {
            "role": "assistant",
            "content": "Le document [S1] est pertinent.",
            "meta": {"sources": [source]},
        },
    ]
    monkeypatch.setattr(
        rag.query_engine,
        "_retrieve_documents",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("retrieval not expected")),
    )

    events = list(
        rag.query_engine.stream_chat_api(
            "Quel est le titre exact du document que tu viens de citer comme S1 ?",
            "demo-model",
            0.2,
            conversation_history=history,
        )
    )

    answer = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    source_events = [event for event in events if event.get("type") == "sources"]
    assert source["title"] in answer
    assert source_events == [
        {
            "type": "sources",
            "data": [source],
            "answer_origin": "conversation_history",
        }
    ]


def test_historical_s1_resolution_uses_latest_source_bearing_answer(
    workspace_tmp_dir: Path,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    history = [
        {
            "role": "assistant",
            "content": "Ancienne reponse [S1].",
            "meta": {"sources": [{"ref_label": "S1", "title": "Ancien titre"}]},
        },
        {
            "role": "assistant",
            "content": "Nouvelle reponse [S2].",
            "meta": {"sources": [{"ref_label": "S2", "title": "Nouveau titre"}]},
        },
    ]

    resolved = rag.query_engine._resolve_historical_bibliographic_reference(
        "Quel document correspond a S1 ?",
        history,
    )

    assert resolved is None


def test_stream_chat_api_asks_for_clarification_before_ambiguous_retrieval(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()
    monkeypatch.setattr(
        rag.query_engine,
        "_retrieve_documents",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("retrieval not expected")),
    )

    events = list(
        rag.query_engine.stream_chat_api(
            "Compare-les et dis-moi lequel est le meilleur.",
            "demo-model",
            0.2,
            conversation_history=[],
        )
    )

    answer = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "pas assez de contexte" in answer
    assert "savoir à quoi" in answer
    assert "fait référence" in answer
    assert "documents, méthodes ou éléments" in answer
    assert "voulez-vous comparer" in answer
    assert [event for event in events if event.get("type") == "sources"] == [
        {"type": "sources", "data": [], "answer_origin": "clarification"}
    ]
    assert not any(event.get("type") == "status" for event in events)


def test_stream_chat_api_explains_comparison_scoped_to_one_article(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()
    monkeypatch.setattr(
        rag.query_engine,
        "_retrieve_documents",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("retrieval not expected")),
    )

    events = list(
        rag.query_engine.stream_chat_api(
            "compare ces articles",
            "demo-model",
            0.2,
            conversation_history=[],
            source_paths=["eddy/weak-strong.pdf"],
        )
    )

    answer = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "un seul article" in answer
    assert "Questionner dans le chat" in answer
    assert [event for event in events if event.get("type") == "sources"] == [
        {"type": "sources", "data": [], "answer_origin": "clarification"}
    ]


def test_stream_chat_api_keeps_comparative_follow_up_when_history_exists(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()
    doc = Document(page_content="La methode A est plus precise.", metadata={"source": "a.pdf"})
    retrieval_queries: list[str] = []

    def fake_retrieve(query: str, *args: Any, **kwargs: Any) -> list[Document]:
        retrieval_queries.append(query)
        return [doc]

    monkeypatch.setattr(rag.query_engine, "_retrieve_documents", fake_retrieve)
    monkeypatch.setattr(rag.query_engine, "_pin_source_ref_labels", lambda docs: docs)
    monkeypatch.setattr(rag.query_engine, "format_sources_api", lambda docs: [])
    monkeypatch.setattr(rag.query_engine, "_build_source_citation_guide", lambda docs: "")
    monkeypatch.setattr(rag.query_engine, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(rag.query_engine, "resolve_chat_think_mode", lambda *args: False)
    monkeypatch.setattr(
        rag.query_engine,
        "stream_ollama_chat",
        lambda *args, **kwargs: iter([{"content": "La methode A est la meilleure [S1]."}]),
    )

    events = list(
        rag.query_engine.stream_chat_api(
            "Compare-les et dis-moi lequel est le meilleur.",
            "demo-model",
            0.2,
            conversation_history=[
                {"role": "user", "content": "Compare la methode A et la methode B."},
                {"role": "assistant", "content": "Voici leurs differences."},
            ],
        )
    )

    assert retrieval_queries
    assert any(event.get("type") == "status" for event in events)
    assert not any(event.get("answer_origin") == "clarification" for event in events)


def test_stream_chat_api_appends_language_constraint_last(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()
    doc = Document(page_content="Un resultat scientifique.", metadata={"source": "demo.pdf"})
    captured_messages: list[list[dict[str, str]]] = []

    def fake_stream(_model: str, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        captured_messages.append(messages)
        return iter([{"content": "Une reponse [S1]."}])

    monkeypatch.setattr(rag.query_engine, "_retrieve_documents", lambda *args, **kwargs: [doc])
    monkeypatch.setattr(rag.query_engine, "_pin_source_ref_labels", lambda docs: docs)
    monkeypatch.setattr(rag.query_engine, "format_sources_api", lambda docs: [])
    monkeypatch.setattr(rag.query_engine, "_build_source_citation_guide", lambda docs: "")
    monkeypatch.setattr(rag.query_engine, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(rag.query_engine, "resolve_chat_think_mode", lambda *args: False)
    monkeypatch.setattr(rag.query_engine, "stream_ollama_chat", fake_stream)

    list(rag.query_engine.stream_chat_api("Que conclut ce document ?", "demo-model", 0.2))

    user_prompt = captured_messages[0][-1]["content"]
    assert user_prompt.endswith(rag.query_engine._final_response_language_constraint())
    assert "toute la reponse visible en francais" in user_prompt


def test_stream_chat_api_sanitizes_remote_chain_output(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()

    monkeypatch.setattr(
        rag.query_engine,
        "_retrieve_documents",
        lambda *args, **kwargs: [Document(page_content="ctx", metadata={"source": "demo"})],
    )
    monkeypatch.setattr(rag.query_engine, "_pin_source_ref_labels", lambda docs: docs)
    monkeypatch.setattr(rag.query_engine, "format_sources_api", lambda docs: [])
    monkeypatch.setattr(rag.query_engine, "_build_source_citation_guide", lambda docs: "")
    monkeypatch.setattr(rag.query_engine, "_build_retrieval_query", lambda msg, hist: msg)
    monkeypatch.setattr(rag.query_engine, "_build_history_context", lambda hist: "")
    monkeypatch.setattr(rag.query_engine, "is_remote_provider", lambda: False)
    monkeypatch.setattr(rag.query_engine, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(
        rag.query_engine,
        "stream_ollama_chat",
        lambda *args, **kwargs: iter(
            [
                {"content": "<think>raisonnement interne"},
                {"content": " cache</think>\n<|channel|>thought bruit cache<channel|>"},
                {
                    "content": (
                        "\nAnswer: sortie propre [S1]\n\n"
                        "Explanation: ok [S1]\n\nResponse note: stable."
                    )
                },
            ]
        ),
    )

    events = list(
        rag.query_engine.stream_chat_api(
            "question",
            "gemma4:e2b",
            0.2,
        )
    )

    tokens = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "<think>" not in tokens
    assert "<|channel|>" not in tokens
    assert "raisonnement interne" not in tokens
    assert tokens.startswith("Answer: sortie propre [S1]")


def test_stream_chat_api_accepts_unstructured_local_output_before_streaming(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()

    doc = Document(
        page_content="Les pertes AC incluent des pertes hysteretiques.",
        metadata={"source": "demo.pdf"},
    )

    monkeypatch.setattr(rag.query_engine, "_retrieve_documents", lambda *args, **kwargs: [doc])
    monkeypatch.setattr(rag.query_engine, "_pin_source_ref_labels", lambda docs: docs)
    monkeypatch.setattr(rag.query_engine, "format_sources_api", lambda docs: [])
    monkeypatch.setattr(rag.query_engine, "_build_source_citation_guide", lambda docs: "")
    monkeypatch.setattr(rag.query_engine, "_build_retrieval_query", lambda msg, hist: msg)
    monkeypatch.setattr(rag.query_engine, "_build_history_context", lambda hist: "")
    monkeypatch.setattr(
        rag.query_engine, "_build_response_style_guide", lambda msg, model_name=None: ""
    )
    monkeypatch.setattr(rag.query_engine, "is_remote_provider", lambda: False)
    monkeypatch.setattr(rag.query_engine, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(rag.query_engine, "resolve_chat_think_mode", lambda *args: False)
    monkeypatch.setattr(
        rag.query_engine,
        "stream_ollama_chat",
        lambda *args, **kwargs: iter(
            [
                {
                    "content": (
                        "Les pertes dans les aimants HTS sont essentiellement des pertes AC [S1]."
                    )
                }
            ]
        ),
    )
    monkeypatch.setattr(
        rag.query_engine,
        "invoke_structured_chat",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("repair not expected")),
    )

    events = list(
        rag.query_engine.stream_chat_api(
            "quelles pertes dans les aimants HTS ?",
            "gpt-oss:20b",
            0.2,
        )
    )

    tokens = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    statuses = [event.get("msg", "") for event in events if event.get("type") == "status"]

    assert tokens == "Les pertes dans les aimants HTS sont essentiellement des pertes AC [S1]."
    assert "Answer:" not in tokens
    assert "Response note:" not in tokens
    assert not any("Reponse fragile detectee" in status for status in statuses)


def test_stream_chat_api_streams_content_when_requested_thinking_is_not_supported(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()

    doc = Document(
        page_content="La formulation H est robuste pour representer le champ magnetique.",
        metadata={"source": "demo.pdf"},
    )
    calls: list[Any] = []

    def fake_stream(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.get("think"))
        return iter([{"content": "La formulation H est robuste [S1]."}])

    monkeypatch.setattr(rag.query_engine, "_retrieve_documents", lambda *args, **kwargs: [doc])
    monkeypatch.setattr(rag.query_engine, "_pin_source_ref_labels", lambda docs: docs)
    monkeypatch.setattr(rag.query_engine, "format_sources_api", lambda docs: [])
    monkeypatch.setattr(rag.query_engine, "_build_source_citation_guide", lambda docs: "")
    monkeypatch.setattr(rag.query_engine, "_build_retrieval_query", lambda msg, hist: msg)
    monkeypatch.setattr(rag.query_engine, "_build_history_context", lambda hist: "")
    monkeypatch.setattr(
        rag.query_engine, "_build_response_style_guide", lambda msg, model_name=None: ""
    )
    monkeypatch.setattr(rag.query_engine, "is_remote_provider", lambda: False)
    monkeypatch.setattr(rag.query_engine, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(rag.query_engine, "stream_ollama_chat", fake_stream)

    events = list(
        rag.query_engine.stream_chat_api(
            "Quelle formulation est robuste ?",
            "qwen3.5:4b",
            0.2,
            thinking_enabled=True,
        )
    )

    tokens = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert calls == [False]
    assert "<think>" not in tokens
    assert tokens == "La formulation H est robuste [S1]."


def test_stream_chat_api_strips_drafting_from_final_answer_when_thinking_enabled(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()

    doc = Document(
        page_content="La formulation H est robuste pour representer le champ magnetique.",
        metadata={"source": "demo.pdf"},
    )

    monkeypatch.setattr(rag.query_engine, "_retrieve_documents", lambda *args, **kwargs: [doc])
    monkeypatch.setattr(rag.query_engine, "_pin_source_ref_labels", lambda docs: docs)
    monkeypatch.setattr(rag.query_engine, "format_sources_api", lambda docs: [])
    monkeypatch.setattr(rag.query_engine, "_build_source_citation_guide", lambda docs: "")
    monkeypatch.setattr(rag.query_engine, "_build_retrieval_query", lambda msg, hist: msg)
    monkeypatch.setattr(rag.query_engine, "_build_history_context", lambda hist: "")
    monkeypatch.setattr(
        rag.query_engine, "_build_response_style_guide", lambda msg, model_name=None: ""
    )
    monkeypatch.setattr(rag.query_engine, "is_remote_provider", lambda: False)
    monkeypatch.setattr(rag.query_engine, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(rag.query_engine, "resolve_chat_think_mode", lambda *args: True)
    monkeypatch.setattr(
        rag.query_engine,
        "stream_ollama_chat",
        lambda *args, **kwargs: iter(
            [
                {"thinking": "Je verifie les sources."},
                {
                    "content": (
                        "La formulation H est robuste [S1].\n\n"
                        "Wait, check S3 text: Numerical methods are required."
                    )
                },
            ]
        ),
    )

    events = list(
        rag.query_engine.stream_chat_api(
            "Quelle formulation est robuste ?",
            "gemma4:e4b",
            0.2,
            thinking_enabled=True,
        )
    )

    tokens = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "<think>Je verifie les sources.</think>" in tokens
    assert "Wait, check" not in tokens
    assert tokens.endswith("La formulation H est robuste [S1].")


def test_stream_chat_api_repairs_guard_error_before_streaming(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()

    doc = Document(
        page_content="La formulation H est robuste pour representer le champ magnetique.",
        metadata={"source": "demo.pdf"},
    )

    calls: list[str] = []

    def fake_structured_chat(*args: Any, **kwargs: Any) -> Any:
        calls.append("call")
        return rag.query_engine.ChatAnswer(
            answer="La formulation H est robuste [S1].",
            explanation="Le passage source le dit [S1].",
            response_note="Aucune note utile.",
        )

    monkeypatch.setattr(rag.query_engine, "_retrieve_documents", lambda *args, **kwargs: [doc])
    monkeypatch.setattr(rag.query_engine, "format_sources_api", lambda docs: [])
    monkeypatch.setattr(rag.query_engine, "_build_retrieval_query", lambda msg, hist: msg)
    monkeypatch.setattr(rag.query_engine, "_build_history_context", lambda hist: "")
    monkeypatch.setattr(
        rag.query_engine, "_build_response_style_guide", lambda msg, model_name=None: ""
    )
    monkeypatch.setattr(rag.query_engine, "is_ollama_provider", lambda: False)
    monkeypatch.setattr(rag.query_engine, "create_chat_llm", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        rag.query_engine,
        "stream_text",
        lambda *_args, **_kwargs: iter(["La formulation H est robuste [S9]."]),
    )
    monkeypatch.setattr(rag.query_engine, "invoke_structured_chat", fake_structured_chat)

    events = list(
        rag.query_engine.stream_chat_api(
            "Quelle formulation est robuste ?",
            "chat-model",
            0.2,
        )
    )

    tokens = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    replacements = [
        event.get("text", "") for event in events if event.get("type") == "answer_replace"
    ]
    statuses = [event.get("msg", "") for event in events if event.get("type") == "status"]

    assert len(calls) == 0
    assert "[S9]" in tokens
    assert replacements == ["La formulation H est robuste."]
    assert not any("Reponse fragile detectee" in status for status in statuses)


class _FakeArtifactCollection:
    def __init__(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self.rows = rows

    def get(self, include: Any = None, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        page = self.rows[offset : offset + limit]
        return {"documents": [text for text, _ in page], "metadatas": [meta for _, meta in page]}


def test_text_artifact_scan_finds_documents_indexed_with_broken_latex_text(
    workspace_tmp_dir: Path,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    rows = [
        ("Les REBCO sont des c´ eramiques", {"rel_path": "a/thesis.pdf", "zotero_title": "Thesis"}),
        ("the ﬁeld of the magnet", {"rel_path": "a/thesis.pdf", "zotero_title": "Thesis"}),
        ("Clean text about coils.", {"rel_path": "b/clean.pdf"}),
        ("mod´ elisation", {"rel_path": "c/other.pdf"}),
    ]
    with rag.state._state_lock:
        rag.state.vectorstore = SimpleNamespace(_collection=_FakeArtifactCollection(rows))

    found = rag.vector_store.find_documents_with_text_artifacts(batch_size=2)

    assert [(item["rel_path"], item["affected_chunks"]) for item in found] == [
        ("a/thesis.pdf", 2),
        ("c/other.pdf", 1),
    ]
    assert found[0]["title"] == "Thesis"


def test_marking_for_reindex_only_invalidates_the_file_signature(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    saved: dict[str, Any] = {}
    entries = {
        "a/thesis.pdf": {"file_sig": "sig-a", "zotero_sig": "z-a", "chunk_count": 12},
        "b/clean.pdf": {"file_sig": "sig-b", "zotero_sig": "z-b", "chunk_count": 3},
    }
    monkeypatch.setattr(rag.vector_store, "_load_manifest_entries", lambda **_: entries)
    monkeypatch.setattr(rag.vector_store, "_load_manifest_chunk_size", lambda **_: 1000)
    monkeypatch.setattr(rag.vector_store, "_load_manifest_embedding_model", lambda **_: "embed")
    monkeypatch.setattr(
        rag.vector_store,
        "_save_manifest_entries",
        lambda entries, chunk_size, model, **_: saved.update(entries=entries, chunk_size=chunk_size, model=model),
    )

    assert rag.vector_store.mark_documents_for_reindex(["a/thesis.pdf"]) == 1

    assert saved["entries"]["a/thesis.pdf"] == {"file_sig": "", "zotero_sig": "z-a", "chunk_count": 12}
    assert saved["entries"]["b/clean.pdf"]["file_sig"] == "sig-b"
    assert (saved["chunk_size"], saved["model"]) == (1000, "embed")
    # A changed entry is reindexed by the incremental reload, not skipped.
    current = {"file_sig": "sig-a", "zotero_sig": "z-a"}
    assert not rag.vector_store._manifest_entry_matches(saved["entries"]["a/thesis.pdf"], current)


def test_french_question_also_searches_with_its_english_translation(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    qe = rag.query_engine
    queries: list[str] = []

    def single(
        query, threshold, max_chunks, filters, *, use_reranker=True, depth="deep", pool_only=False
    ):
        queries.append(query)
        if query.startswith("What"):
            return [Document(page_content="Screening currents reduce the central field.", metadata={"source": "en.pdf"})]
        return [
            Document(page_content="Les mesures d'écrantage magnétique.", metadata={"source": "fr-copy1.pdf"}),
            # Same passage from a second copy of the PDF, with broken accents.
            Document(page_content="Les mesures d’´ ecrantage magn´ etique.", metadata={"source": "fr-copy2.pdf"}),
            Document(page_content="Les mesures d'écrantage magnétique.", metadata={"source": "fr-copy2.pdf"}),
        ]

    monkeypatch.setattr(qe, "_retrieve_documents_single", single)
    monkeypatch.setattr(qe, "_english_retrieval_query", lambda q: "What are screening currents?")
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    docs = qe._retrieve_documents("Qu'est-ce que l'écrantage ?", 0.3, 6, {}, depth="quick")

    assert queries == ["Qu'est-ce que l'écrantage ?", "What are screening currents?"]
    contents = [doc.page_content for doc in docs]
    assert contents[0] == "Screening currents reduce the central field."
    assert contents.count("Les mesures d'écrantage magnétique.") == 1


def test_english_question_is_searched_once(workspace_tmp_dir: Path, monkeypatch: Any) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    qe = rag.query_engine
    calls: list[str] = []

    def retrieve_single(query: str, *args: Any, **kwargs: Any) -> list[Document]:
        calls.append(query)
        return [Document(page_content="x", metadata={})]

    monkeypatch.setattr(qe, "_retrieve_documents_single", retrieve_single)
    monkeypatch.setattr(qe, "_english_retrieval_query", lambda q: "")

    qe._retrieve_documents("How do NI coils charge?", 0.3, 6, {})

    assert calls == ["How do NI coils charge?"]


def test_chat_answer_asks_ollama_for_a_window_that_fits_the_prompt(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    qe = rag.query_engine
    seen: list[int] = []

    def resolve_context_length(model: str, required_tokens: int = 0) -> int:
        seen.append(required_tokens)
        return 16384

    monkeypatch.setattr(qe, "resolve_ollama_context_length", resolve_context_length)

    options = qe._chat_context_options("ornith", "s" * 30000, "question")

    assert options == {"num_ctx": 16384}
    assert seen[0] >= 10000 + qe._CHAT_OUTPUT_TOKENS_RESERVE


def test_legacy_manifest_entries_get_their_chunk_count_from_the_lexical_index(
    workspace_tmp_dir: Path,
) -> None:
    import json as _json
    import sqlite3

    rag = _load_rag_modules(workspace_tmp_dir)
    index_dir = workspace_tmp_dir / "legacy-index"
    index_dir.mkdir()
    with sqlite3.connect(index_dir / "lexical_fts.sqlite3") as connection:
        connection.execute("CREATE TABLE lexical_chunks (search_text, content, metadata_json, rel_path)")
        connection.executemany(
            "INSERT INTO lexical_chunks VALUES ('t', 'c', '{}', ?)",
            [("papers/a.pdf",), ("papers/a.pdf",), ("papers/b.pdf",)],
        )
    (index_dir / rag.state._MANIFEST_FILE).write_text(
        _json.dumps(
            {
                "version": 2,
                "entries": {
                    "papers/a.pdf": {"file_sig": "sa", "zotero_sig": "za"},
                    "papers/b.pdf": {"file_sig": "sb", "zotero_sig": "zb"},
                    "papers/unreadable.pdf": {"file_sig": "su", "zotero_sig": "zu"},
                },
            }
        ),
        encoding="utf-8",
    )

    entries = rag.vector_store._load_manifest_entries(db_path=str(index_dir))

    assert entries["papers/a.pdf"]["chunk_count"] == 2
    assert entries["papers/b.pdf"]["chunk_count"] == 1
    # A file that never produced a chunk is still retried, as v3 intends.
    assert entries["papers/unreadable.pdf"]["chunk_count"] == 0
    assert rag.vector_store._manifest_entry_matches(
        entries["papers/a.pdf"], {"file_sig": "sa", "zotero_sig": "za"}
    )


def test_init_rag_resumes_an_interrupted_build_instead_of_starting_over(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    monkeypatch.setenv("RAG_PDF_GROUP_SIZE", "1")
    pdfs = [(f"papers/{name}.pdf", f"X:/{name}.pdf") for name in ("a", "b", "c")]
    built: list[str] = []
    paused: list[bool] = []

    def fake_build_chunks_from_pdfs(**kwargs: Any) -> tuple[list[Document], list[Document], int]:
        rel_path = kwargs["pdf_list"][0][0]
        if rel_path == "papers/c.pdf" and not paused:
            # The user pauses (or closes the app) while the third document is read.
            paused.append(True)
            rag.state.pause_vector_loading()
            rag.state._raise_if_vector_load_paused()
        built.append(rel_path)
        return [], [Document(page_content=rel_path, metadata={"rel_path": rel_path})], 1

    monkeypatch.setattr(rag.query_engine.state, "find_pdfs", lambda: list(pdfs))
    monkeypatch.setattr(rag.query_engine, "_current_embedding_model", lambda: "embed-a")
    monkeypatch.setattr(rag.query_engine, "current_embedding_profile_label", lambda model: "embed-a")
    monkeypatch.setattr(rag.query_engine, "get_embedding_provider_mode", lambda: "remote")
    monkeypatch.setattr(rag.query_engine, "requires_local_ollama_embeddings", lambda: False)
    monkeypatch.setattr(rag.query_engine, "create_embeddings", lambda model: object())
    monkeypatch.setattr(rag.query_engine, "create_chat_llm", lambda model_name: object())
    monkeypatch.setattr(
        rag.query_engine, "create_stuff_documents_chain", lambda llm, prompt: {"chain": "ok"}
    )
    monkeypatch.setattr(rag.query_engine, "provider_runtime_label", lambda embedding_model: "local")
    monkeypatch.setattr(rag.query_engine, "Chroma", FakeChroma)
    monkeypatch.setattr(rag.query_engine, "ensure_lexical_index", lambda *args: False)
    monkeypatch.setattr(rag.vector_store, "replace_lexical_paths", lambda *args: None)
    monkeypatch.setattr(rag.vector_store, "_build_chunks_from_pdfs", fake_build_chunks_from_pdfs)
    def compute_index_entries(pdf_list: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
        return {
            rel_path: {"abs_path": abs_path, "file_sig": f"sig-{rel_path}", "zotero_sig": ""}
            for rel_path, abs_path in pdf_list
        }

    for module in (rag.query_engine, rag.vector_store):
        monkeypatch.setattr(module, "_compute_index_entries", compute_index_entries)
        monkeypatch.setattr(module, "_refresh_zotero_library_cache", lambda: None)
    monkeypatch.setattr(rag.vector_store, "_current_embedding_profile", lambda: "embed-a")

    ok, _msg = rag.query_engine.init_rag("chat-model", 0.3, force_reload=False)

    assert ok is False
    db_path = str(
        rag.vector_store.resolve_vector_index_profile(chunk_size=1000, embedding_model="embed-a")[
            "path"
        ]
    )
    assert rag.vector_store._has_incomplete_index_marker(db_path=db_path)
    assert set(rag.vector_store._load_manifest_entries(db_path=db_path)) == {
        "papers/a.pdf",
        "papers/b.pdf",
    }
    assert rag.vector_store.get_vector_reload_preview(chunk_size=1000)["mode"] == "incremental"

    monkeypatch.setattr(
        rag.query_engine.shutil,
        "rmtree",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Full rebuild not expected")),
    )
    ok, msg = rag.query_engine.init_rag("chat-model", 0.3, force_reload=True)

    assert ok is True, msg
    assert built == ["papers/a.pdf", "papers/b.pdf", "papers/c.pdf"]
    assert set(rag.vector_store._load_manifest_entries(db_path=db_path)) == {
        "papers/a.pdf",
        "papers/b.pdf",
        "papers/c.pdf",
    }
    assert not rag.vector_store._has_incomplete_index_marker(db_path=db_path)


def test_save_manifest_keeps_the_recorded_embedding_prompts(workspace_tmp_dir: Path) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    db_path = str(workspace_tmp_dir / "index")
    entries = {"papers/a.pdf": {"file_sig": "sig", "zotero_sig": "", "chunk_count": 2}}

    rag.vector_store._save_manifest_entries(
        entries, 1000, "embeddinggemma", db_path=db_path, embedding_prompts="embeddinggemma"
    )
    # Later saves (reindex marks, checkpoints) that do not name a scheme keep it.
    rag.vector_store._save_manifest_entries(entries, 1000, "embeddinggemma", db_path=db_path)

    assert rag.vector_store._load_manifest_embedding_prompts(db_path=db_path) == "embeddinggemma"


def test_index_manifest_without_prompts_reads_as_raw_text(workspace_tmp_dir: Path) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    db_path = str(workspace_tmp_dir / "index")
    entries = {"papers/a.pdf": {"file_sig": "sig", "zotero_sig": "", "chunk_count": 2}}

    rag.vector_store._save_manifest_entries(entries, 1000, "embeddinggemma", db_path=db_path)

    manifest = json.loads((Path(db_path) / "index_manifest.json").read_text(encoding="utf-8"))
    assert "embedding_prompts" not in manifest
    assert rag.vector_store._load_manifest_embedding_prompts(db_path=db_path) == ""


def _patch_init_rag_for_prompts(rag: Any, monkeypatch: Any, saved: list[dict[str, Any]]) -> None:
    monkeypatch.setattr(rag.query_engine.state, "find_pdfs", lambda: [("papers/a.pdf", "X:/a.pdf")])
    monkeypatch.setattr(rag.query_engine, "_refresh_zotero_library_cache", lambda: None)
    monkeypatch.setattr(rag.query_engine, "_current_embedding_model", lambda: "embeddinggemma")
    monkeypatch.setattr(
        rag.query_engine, "current_embedding_profile_label", lambda model: "embeddinggemma"
    )
    monkeypatch.setattr(rag.query_engine, "get_embedding_provider_mode", lambda: "remote")
    monkeypatch.setattr(rag.query_engine, "requires_local_ollama_embeddings", lambda: False)
    monkeypatch.setattr(rag.query_engine, "create_embeddings", lambda model: object())
    monkeypatch.setattr(rag.query_engine, "create_chat_llm", lambda model_name: object())
    monkeypatch.setattr(
        rag.query_engine,
        "create_stuff_documents_chain",
        lambda llm, prompt: {"chain": "ok"},
    )
    monkeypatch.setattr(rag.query_engine, "provider_runtime_label", lambda embedding_model: "local")
    monkeypatch.setattr(rag.query_engine, "Chroma", FakeChroma)
    monkeypatch.setattr(rag.query_engine, "ensure_lexical_index", lambda *args: None)
    monkeypatch.setattr(
        rag.query_engine,
        "_save_manifest_entries",
        lambda entries, *_args, **kwargs: saved.append(dict(kwargs)),
    )


def test_init_rag_builds_a_new_embeddinggemma_index_with_its_prompts(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    saved: list[dict[str, Any]] = []
    _patch_init_rag_for_prompts(rag, monkeypatch, saved)
    monkeypatch.setattr(
        rag.query_engine,
        "_index_pdf_groups_with_manifest",
        lambda **kwargs: ({"papers/a.pdf": {"file_sig": "sig-a", "zotero_sig": ""}}, 0, 1, 3, 1, 4),
    )

    ok, _msg = rag.query_engine.init_rag("chat-model", 0.3, force_reload=False)

    assert ok is True
    embedder = rag.query_engine.state.vectorstore.kwargs["embedding_function"]
    assert getattr(embedder, "scheme", "") == "embeddinggemma"
    assert saved and all(call["embedding_prompts"] == "embeddinggemma" for call in saved)


def test_init_rag_keeps_an_existing_raw_index_without_prompts(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    saved: list[dict[str, Any]] = []
    _patch_init_rag_for_prompts(rag, monkeypatch, saved)
    raw_embedder = object()
    monkeypatch.setattr(rag.query_engine, "create_embeddings", lambda model: raw_embedder)
    profile = rag.query_engine.resolve_vector_index_profile(
        chunk_size=1000, embedding_model="embeddinggemma"
    )
    profile_path = Path(str(profile["path"]))
    profile_path.mkdir(parents=True, exist_ok=True)
    (profile_path / "placeholder.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(rag.query_engine, "_load_manifest_embedding_prompts", lambda **kwargs: "")
    monkeypatch.setattr(
        rag.query_engine,
        "_load_manifest_entries",
        lambda **kwargs: {"papers/a.pdf": {"file_sig": "sig", "zotero_sig": "", "chunk_count": 1}},
    )
    monkeypatch.setattr(rag.query_engine, "_load_manifest_chunk_size", lambda **kwargs: 1000)
    monkeypatch.setattr(
        rag.query_engine, "_load_manifest_embedding_model", lambda **kwargs: "embeddinggemma"
    )
    monkeypatch.setattr(
        rag.query_engine,
        "_compute_index_entries",
        lambda pdf_list: {
            "papers/a.pdf": {"abs_path": "X:/a.pdf", "file_sig": "sig", "zotero_sig": ""}
        },
    )
    monkeypatch.setattr(
        rag.query_engine,
        "_index_pdf_groups_with_manifest",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("No PDF reindex expected")),
    )

    ok, _msg = rag.query_engine.init_rag("chat-model", 0.3, force_reload=True)

    assert ok is True
    assert rag.query_engine.state.vectorstore.kwargs["embedding_function"] is raw_embedder
    assert saved and all(call["embedding_prompts"] == "" for call in saved)


def _stream_with_fake_answer(
    rag: Any, monkeypatch: Any, docs: list[Document], answer: str, question: str
) -> tuple[list[dict[str, Any]], list[list[dict[str, str]]]]:
    rag.query_engine.state.document_chain = object()
    rag.query_engine.state.vectorstore = object()
    captured: list[list[dict[str, str]]] = []

    def fake_stream(_model: str, messages: list[dict[str, str]], **kwargs: Any) -> Any:
        captured.append(messages)
        return iter([{"content": answer}])

    monkeypatch.setattr(rag.query_engine, "_retrieve_documents", lambda *args, **kwargs: docs)
    monkeypatch.setattr(rag.query_engine, "format_sources_api", lambda docs: [])
    monkeypatch.setattr(rag.query_engine, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(rag.query_engine, "resolve_chat_think_mode", lambda *args: False)
    monkeypatch.setattr(rag.query_engine, "stream_ollama_chat", fake_stream)
    events = list(rag.query_engine.stream_chat_api(question, "demo-model", 0.2))
    return events, captured


def _final_answer(events: list[dict[str, Any]]) -> str:
    replaced = [event["text"] for event in events if event.get("type") == "answer_replace"]
    if replaced:
        return str(replaced[-1])
    return "".join(str(event["text"]) for event in events if event.get("type") == "token")


_SCIF_DOCS = [
    Document(
        page_content="At 4.2 K the SCIF after the first ramp was -0.42 T.",
        metadata={"source": "a.pdf", "page": 4, "zotero_title": "Insert", "zotero_year": "2021"},
    ),
    Document(
        page_content="At 20 K the SCIF after the first ramp was -0.19 T.",
        metadata={"source": "a.pdf", "page": 6, "zotero_title": "Insert", "zotero_year": "2021"},
    ),
]


def test_stream_chat_api_drops_piled_labels_and_keeps_the_model_citation(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    events, _ = _stream_with_fake_answer(
        rag,
        monkeypatch,
        _SCIF_DOCS,
        "Le SCIF vaut -0,19 T [S1]. Aucune autre mesure n'est rapportee. [S1] [S2]",
        "Quel est le SCIF apres la premiere rampe ?",
    )

    # The figure sits in S2, but the citation stays the model's own.
    assert _final_answer(events) == (
        "Le SCIF vaut -0,19 T [S1]. Aucune autre mesure n'est rapportee."
    )
    guard = [e for e in events if e.get("stage") == "answer_guard"]
    assert guard and guard[0]["data"]["values_to_check"] == 0


def test_stream_chat_api_names_figures_absent_from_the_passages(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)

    events, _ = _stream_with_fake_answer(
        rag,
        monkeypatch,
        _SCIF_DOCS,
        "Le SCIF vaut -0,27 T [S2].",
        "Quel est le SCIF apres la premiere rampe ?",
    )

    answer = _final_answer(events)
    assert answer.startswith("Le SCIF vaut -0,27 T [S2].")
    assert answer.endswith("*Valeurs à vérifier, absentes des passages retrouvés : 0,27.*")


def test_stream_chat_api_does_not_source_a_value_from_a_page_number(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    rag = _load_rag_modules(workspace_tmp_dir)
    docs = [
        Document(page_content=doc.page_content, metadata={**doc.metadata, "page": page})
        for doc, page in zip(_SCIF_DOCS, (48, 64), strict=True)
    ]

    events, _ = _stream_with_fake_answer(
        rag,
        monkeypatch,
        docs,
        "Le SCIF vaut 64 T [S1].",
        "Quel est le SCIF apres la premiere rampe ?",
    )

    answer = _final_answer(events)
    assert answer.startswith("Le SCIF vaut 64 T [S1].")
    assert answer.endswith("*Valeurs à vérifier, absentes des passages retrouvés : 64.*")
