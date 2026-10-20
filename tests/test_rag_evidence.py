from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from src.rag.evidence import EvidenceChunk, select_evidence_chunks


def _doc(text: str, **metadata: Any) -> Any:
    """Mimic a langchain ``Document`` shape with the attrs the binding uses."""

    class _D:
        def __init__(self) -> None:
            self.page_content = text
            self.metadata = dict(metadata)

    return _D()


# ---------------------------------------------------------------------------
# build_query_engine_retriever
# ---------------------------------------------------------------------------


def test_retriever_adapter_maps_documents_to_evidence_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_retrieve(query: str, threshold, max_chunks, filters):
        captured.update(
            query=query, threshold=threshold, max_chunks=max_chunks, filters=filters
        )
        return [
            _doc(
                "first chunk",
                chunk_uid="p1:0:abc",
                article_id="p1",
                rel_path="p1.pdf",
                page="3",
            ),
            _doc(
                "second chunk",
                chunk_uid="p1:1:def",
                article_id="p1",
                rel_path="p1.pdf",
                page="4",
            ),
        ]

    import src.rag.query_engine as qe  # noqa: PLC0415

    monkeypatch.setattr(qe, "_retrieve_documents", fake_retrieve)

    from src.rag.evidence import build_query_engine_retriever  # noqa: PLC0415

    retriever = build_query_engine_retriever(threshold=0.4)
    chunks = retriever("dense retrieval", 8)
    assert captured["query"] == "dense retrieval"
    assert captured["threshold"] == 0.4
    assert captured["max_chunks"] == 8
    assert [c.chunk_uid for c in chunks] == ["p1:0:abc", "p1:1:def"]
    assert chunks[0].paper_id == "p1"
    assert chunks[0].page == "3"
    # Decreasing scores so the evidence selector keeps a ranking signal.
    assert chunks[0].score > chunks[1].score


def test_retriever_adapter_returns_empty_list_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_a, **_k):
        raise RuntimeError("Chroma is down")

    import src.rag.query_engine as qe  # noqa: PLC0415

    monkeypatch.setattr(qe, "_retrieve_documents", boom)

    from src.rag.evidence import build_query_engine_retriever  # noqa: PLC0415

    retriever = build_query_engine_retriever()
    assert retriever("anything", 5) == []


def test_retriever_adapter_falls_back_to_synthetic_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documents missing ``chunk_uid`` get a synthetic uid from article+chunk_id."""

    def fake_retrieve(*_a, **_k):
        return [_doc("noisy", article_id="p9", chunk_id=42, rel_path="x.pdf")]

    import src.rag.query_engine as qe  # noqa: PLC0415

    monkeypatch.setattr(qe, "_retrieve_documents", fake_retrieve)

    from src.rag.evidence import build_query_engine_retriever  # noqa: PLC0415

    chunks = build_query_engine_retriever()("q", 1)
    assert chunks[0].chunk_uid == "p9::42"


def test_select_evidence_uses_overlap_bonus_without_reranker() -> None:
    chunks = _stub_chunks()
    selected = select_evidence_chunks(
        chunks, original_query="PubMedQA dense retrieval", max_chunks=2
    )
    # The chunk that mentions both PubMedQA AND dense retrieval should win.
    assert selected[0].chunk_uid == "c1"


def test_select_evidence_uses_rerank_fn_when_provided() -> None:
    chunks = _stub_chunks()

    def rerank(_q: str, items: Sequence[EvidenceChunk]) -> list[float]:
        return [-c.score for c in items]  # invert preference

    selected = select_evidence_chunks(
        chunks, original_query="anything", max_chunks=2, rerank_fn=rerank
    )
    # Lowest base-score chunk now ranks first.
    assert selected[0].chunk_uid == "c3"


def _stub_chunks() -> list[EvidenceChunk]:
    return [
        EvidenceChunk(
            chunk_uid="c1",
            text="Dense retrieval reaches 78.4% on PubMedQA.",
            paper_id="p1",
            rel_path="p1.pdf",
            page="3",
            score=0.92,
        ),
        EvidenceChunk(
            chunk_uid="c2",
            text="DPR uses BioBERT embeddings.",
            paper_id="p1",
            rel_path="p1.pdf",
            page="4",
            score=0.71,
        ),
        EvidenceChunk(
            chunk_uid="c3",
            text="BM25 is the strongest sparse baseline.",
            paper_id="p2",
            rel_path="p2.pdf",
            page="1",
            score=0.58,
        ),
    ]
