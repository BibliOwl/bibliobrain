"""Original-document evidence and retrieval adapters shared by drafting tools."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

_logger = logging.getLogger(__name__)


@dataclass
class EvidenceChunk:
    """An original excerpt with its article identity and retrieval score."""

    chunk_uid: str
    text: str
    paper_id: str
    rel_path: str = ""
    page: str = ""
    score: float = 0.0
    _extra: dict[str, Any] = field(default_factory=dict)


def _doc_to_chunk(doc: Any, *, score: float = 0.0) -> EvidenceChunk:
    metadata = dict(getattr(doc, "metadata", {}) or {})
    chunk_uid = (
        str(metadata.get("chunk_uid", "")).strip()
        or f"{metadata.get('article_id', '')}::{metadata.get('chunk_id', '')}"
    )
    return EvidenceChunk(
        chunk_uid=chunk_uid,
        text=str(getattr(doc, "page_content", "") or ""),
        paper_id=str(metadata.get("article_id", "") or "").strip(),
        rel_path=str(metadata.get("rel_path", "") or metadata.get("source", "") or "").strip(),
        page=str(metadata.get("page", "") or "").strip(),
        score=float(score),
        _extra={"metadata": metadata},
    )


def build_query_engine_retriever(
    *,
    threshold: float = 0.0,
    filters: dict[str, list[str]] | None = None,
    use_reranker: bool = True,
    depth: str = "deep",
) -> Callable[[str, int], list[EvidenceChunk]]:
    """Return a ``Retriever`` backed by the existing query_engine retrieval.

    ``threshold`` and ``filters`` are forwarded verbatim. The retriever
    fetches up to ``k`` documents before evidence selection.
    """
    effective_filters = filters or {}

    def _retrieve(query: str, k: int) -> list[EvidenceChunk]:
        # Keep server startup light; Chroma and the query engine are only
        # needed when a retrieval is actually requested.
        from src.rag.query_engine import _retrieve_documents

        try:
            depth_kwargs = {"depth": depth} if depth != "deep" else {}
            try:
                docs = _retrieve_documents(
                    query,
                    threshold=threshold,
                    max_chunks=int(k),
                    filters=effective_filters,
                    use_reranker=use_reranker,
                    **depth_kwargs,
                )
            except TypeError as exc:
                if "use_reranker" not in str(exc):
                    raise
                docs = _retrieve_documents(
                    query,
                    threshold=threshold,
                    max_chunks=int(k),
                    filters=effective_filters,
                )
        except Exception as exc:
            _logger.warning("RAG retrieval failed: %s", exc, exc_info=True)
            return []
        chunks: list[EvidenceChunk] = []
        # Documents come back already ranked; we assign decreasing scores
        # so EvidenceChunk.score remains a useful relative signal.
        for index, doc in enumerate(docs):
            chunks.append(_doc_to_chunk(doc, score=max(0.0, 1.0 - 0.02 * index)))
        return chunks

    return _retrieve


RerankFn = Callable[[str, Sequence[EvidenceChunk]], list[float]]

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]{2,}")


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text or "")}


def _overlap_bonus(query_tokens: set[str], chunk_text: str) -> float:
    if not query_tokens:
        return 0.0
    chunk_tokens = _tokens(chunk_text)
    if not chunk_tokens:
        return 0.0
    inter = len(query_tokens & chunk_tokens)
    return min(0.4, 0.05 * inter)


def select_evidence_chunks(
    chunks: Sequence[EvidenceChunk],
    *,
    original_query: str,
    max_chunks: int = 8,
    rerank_fn: RerankFn | None = None,
) -> list[EvidenceChunk]:
    """Return the top ``max_chunks`` evidence chunks for grounding.

    Always preserves the chunks' identity (no text trimming): the answer
    generator quotes from these.
    """
    if not chunks:
        return []

    if rerank_fn is not None:
        try:
            scores = rerank_fn(original_query, list(chunks))
        except Exception:
            _logger.debug("Recovered from an error in select_evidence_chunks.", exc_info=True)
            scores = []
        if isinstance(scores, list) and len(scores) == len(chunks):
            ranked = sorted(zip(scores, chunks, strict=True), key=lambda kv: -kv[0])
            return [chunk for _, chunk in ranked[:max_chunks]]

    qt = _tokens(original_query)
    ranked_local: list[tuple[float, EvidenceChunk]] = []
    for chunk in chunks:
        ranked_local.append((chunk.score + _overlap_bonus(qt, chunk.text), chunk))
    ranked_local.sort(key=lambda kv: -kv[0])
    return [chunk for _, chunk in ranked_local[:max_chunks]]

