"""Papers excluded from the RAG: stored per library, skipped at search time."""

from __future__ import annotations

import pytest
from langchain_core.documents import Document

from src import config
from src.rag import exclusions


@pytest.fixture(autouse=True)
def _isolated_storage(tmp_path, monkeypatch: pytest.MonkeyPatch):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(config, "STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setattr(config, "DATA_PATH", str(library))
    monkeypatch.setattr(exclusions, "_cache", None)
    return library


def _doc(text: str, rel_path: str) -> Document:
    return Document(
        page_content=text,
        metadata={"rel_path": rel_path, "source": rel_path, "page": "1", "content_type": "article"},
    )


def test_exclusions_round_trip_and_stay_per_library(tmp_path, monkeypatch) -> None:
    result = exclusions.set_rel_paths_excluded(["A\\paper.pdf", "b.pdf", "b.pdf"], True)
    assert result == {"changed": 2, "excluded_count": 2}
    assert exclusions.excluded_path_keys() == {"a/paper.pdf", "b.pdf"}

    # Same paper written with other slashes or case is recognised for removal.
    result = exclusions.set_rel_paths_excluded(["a/PAPER.pdf"], False)
    assert result == {"changed": 1, "excluded_count": 1}
    assert exclusions.excluded_rel_paths() == ["b.pdf"]

    other_library = tmp_path / "other"
    other_library.mkdir()
    monkeypatch.setattr(config, "DATA_PATH", str(other_library))
    assert exclusions.excluded_rel_paths() == []


def test_collect_scored_docs_filters_excluded_paths_inside_chroma(monkeypatch) -> None:
    from src.rag import query_engine as qe
    from src.rag import state

    calls: list[dict] = []

    class FakeVectorstore:
        def similarity_search_with_relevance_scores(self, query, k, **kwargs):
            calls.append(kwargs)
            return [(_doc("kept", "kept.pdf"), 0.9)]

    monkeypatch.setattr(state, "vectorstore", FakeVectorstore())

    result = qe._collect_scored_docs("query", k=5, excluded_paths=["out.pdf"])

    assert [doc.metadata["rel_path"] for doc, _ in result] == ["kept.pdf"]
    assert calls == [{"filter": {"rel_path": {"$nin": ["out.pdf"]}}}]


def _patch_retrieval(monkeypatch, scored, lexical):
    from src.rag import query_engine as qe

    seen_kwargs: dict = {}

    def fake_collect(query, k, **kwargs):
        seen_kwargs.update(kwargs)
        return list(scored)

    monkeypatch.setattr(qe, "_collect_scored_docs", fake_collect)
    monkeypatch.setattr(qe, "_collect_source_scored_docs", lambda query, k, source_paths: list(scored))
    monkeypatch.setattr(
        qe,
        "_collect_lexical_metadata_docs",
        lambda *args, excluded_keys=frozenset(), **kwargs: [
            (doc, score)
            for doc, score in lexical
            if not exclusions.is_metadata_excluded(doc.metadata, excluded_keys)
        ],
    )
    monkeypatch.setattr(qe, "_english_retrieval_query", lambda query: "")
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)
    return qe, seen_kwargs


def test_retrieval_skips_excluded_papers(monkeypatch) -> None:
    exclusions.set_rel_paths_excluded(["sub\\excluded.pdf"], True)
    excluded = _doc("Screening currents in coils.", "sub\\excluded.pdf")
    kept = _doc("Screening currents measured in tapes.", "kept.pdf")
    qe, seen_kwargs = _patch_retrieval(
        monkeypatch,
        scored=[(excluded, 0.95), (kept, 0.8)],
        lexical=[(excluded, 0.5)],
    )

    result = qe._retrieve_documents("screening currents", threshold=0.0, max_chunks=4, filters={})

    assert [doc.metadata["rel_path"] for doc in result] == ["kept.pdf"]
    assert seen_kwargs == {"excluded_paths": ["sub\\excluded.pdf"]}


def test_explicitly_scoped_question_still_reads_an_excluded_paper(monkeypatch) -> None:
    exclusions.set_rel_paths_excluded(["excluded.pdf"], True)
    excluded = _doc("Screening currents in coils.", "excluded.pdf")
    qe, _ = _patch_retrieval(monkeypatch, scored=[(excluded, 0.9)], lexical=[])

    result = qe._retrieve_documents(
        "screening currents",
        threshold=0.0,
        max_chunks=4,
        filters={"sources": ["excluded.pdf"]},
    )

    assert [doc.metadata["rel_path"] for doc in result] == ["excluded.pdf"]


def test_rag_exclusion_route_updates_the_list() -> None:
    from src.api.library import api_library_rag_exclusion
    from src.api_models import RagExclusionRequest

    response = api_library_rag_exclusion(RagExclusionRequest(paths=["a.pdf", "b.pdf"]))
    assert response == {"ok": True, "changed": 2, "excluded_count": 2}

    response = api_library_rag_exclusion(RagExclusionRequest(paths=["a.pdf"], excluded=False))
    assert response == {"ok": True, "changed": 1, "excluded_count": 1}
    assert exclusions.excluded_rel_paths() == ["b.pdf"]
