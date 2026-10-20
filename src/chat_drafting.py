"""Draft text from supplied material without requiring an indexed library.

When the request asks for citations, the library is searched with queries
built from the draft and the pasted material (not from the request sentence),
so the model can cite sources it was never shown in earlier turns.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterable, Iterator
from typing import Any

from src.chat_export import collect_export_bibliography_sources
from src.chat_intents import chat_request_text, requests_library_citations
from src.chat_output_guard import strip_invalid_citations
from src.llm_provider import (
    StreamAbortSignal,
    create_chat_llm,
    is_ollama_provider,
    sanitize_assistant_content,
    stream_ollama_chat,
    stream_text,
    strip_thinking_blocks,
)
from src.prompts import get_response_language_instruction

_logger = logging.getLogger(__name__)

_CITATION_MARK_RE = re.compile(r"\s?\[S\d+(?:\s*[,;]\s*S\d+)*\]", re.I)
_QUOTED_TITLE_RE = re.compile(r"[\"“«]\s*([^\"”»\n]{30,300}?)\s*[\"”»]")
_MAX_CITATION_QUERIES = 6
_CHUNKS_PER_QUERY = 6
_MAX_LIBRARY_SOURCES = 12


def _recent_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Preserve enough of a pasted plan for follow-up edits; the RAG history
    # summarizer's 320 characters per turn would discard most of this material.
    recent: list[dict[str, Any]] = []
    remaining = 24000
    for row in reversed(history):
        role = str(row.get("role") or "")
        content = strip_thinking_blocks(str(row.get("content") or "")).strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if not remaining or len(recent) >= 6:
            break
        recent.append({**row, "role": role, "content": content[:remaining]})
        remaining -= min(len(content), remaining)
    recent.reverse()
    return recent


def _citation_queries(message: str, recent: list[dict[str, Any]]) -> list[str]:
    """Pick a few informative claims to search for, preferring the latest draft."""
    from src.drafting_citations import _split_into_sentences, _tokens

    user_texts = [message] + [
        row["content"] for row in reversed(recent) if row["role"] == "user"
    ]
    draft = next(
        (
            row["content"]
            for row in reversed(recent)
            if row["role"] == "assistant"
            and isinstance(row.get("meta"), dict)
            and row["meta"].get("mode") == "drafting"
        ),
        "",
    )
    request = chat_request_text(message)
    material = [draft, message[len(request):] if message.startswith(request) else message]
    material.extend(text for text in user_texts[1:] if len(text) > 400)

    titles = [match for text in user_texts for match in _QUOTED_TITLE_RE.findall(text)]
    sentences: list[str] = []
    for text in material:
        text = _CITATION_MARK_RE.sub("", text)
        text = re.sub(r"[*#`>]+|^\s*[-•]\s+", " ", text, flags=re.M)
        sentences.extend(_split_into_sentences(text))
    sentences.sort(key=lambda sentence: len(_tokens(sentence)), reverse=True)

    queries: list[str] = []
    chosen: list[set[str]] = []
    for candidate in [*titles, *sentences]:
        tokens = _tokens(candidate)
        if len(tokens) < 5:
            continue
        if any(len(tokens & other) / len(tokens | other) >= 0.5 for other in chosen):
            continue
        queries.append(candidate[:300])
        chosen.append(tokens)
        if len(queries) >= _MAX_CITATION_QUERIES:
            break
    return queries or [message[:300]]


def _search_library(
    queries: list[str],
    model_name: str,
    *,
    threshold: float,
    tags: Any,
    collections: Any,
    language: str,
    abort_signal: StreamAbortSignal | None,
) -> Iterator[dict[str, Any]]:
    """Yield progress events; the final event is ``{"type": "_library_docs"}``."""
    from src.rag.native_chat import ensure_rag_ready
    from src.rag.query_engine import (
        _clone_document_with_metadata,
        _consume_pipeline_metrics,
        _dedupe_source_docs,
        _diversify_documents_by_article,
        _prepare_explicit_filters,
        _retrieve_documents,
    )

    started = time.perf_counter()
    try:
        ensure_rag_ready(model_name, threshold)
    except Exception as exc:
        _logger.debug("Recovered from an error in _search_library.", exc_info=True)
        yield {"type": "status", "msg": str(exc)}
        yield {"type": "_library_docs", "docs": None}
        return
    filters = _prepare_explicit_filters(tags=tags, collections=collections)
    ranked: list[list[Any]] = []
    for index, query in enumerate(queries, start=1):
        if abort_signal is not None and abort_signal.is_aborted():
            raise InterruptedError("Generation aborted.")
        label = "Recherche de sources dans la bibliothèque" if language == "fr" else (
            "Searching the library for sources"
        )
        yield {"type": "status", "msg": f"{label} ({index}/{len(queries)})..."}
        # Consume per-call metrics before yielding: generator resumptions may
        # run on another thread, and stale thread-local metrics would leak.
        ranked.append(_retrieve_documents(
            query, threshold, _CHUNKS_PER_QUERY, filters, depth="quick"
        ))
        _consume_pipeline_metrics()
    # Round-robin keeps the best hit of every claim before second-best hits.
    merged = [docs[rank] for rank in range(_CHUNKS_PER_QUERY) for docs in ranked if rank < len(docs)]
    docs = _diversify_documents_by_article(
        _dedupe_source_docs(merged), limit=_MAX_LIBRARY_SOURCES
    )
    labelled = [
        _clone_document_with_metadata(doc, {"ref_label": f"S{index}"})
        for index, doc in enumerate(docs, start=1)
    ]
    yield {
        "type": "pipeline_metrics",
        "stage": "retrieval",
        "data": {
            "queries": len(queries),
            "returned": len(labelled),
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        },
    }
    yield {"type": "_library_docs", "docs": labelled}


def _draft_messages(
    message: str,
    history: list[dict[str, Any]],
    language: str,
    library_docs: list[Any] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    recent = _recent_history(history)
    if library_docs is None:
        sources = collect_export_bibliography_sources(recent, scope="chat")
        source_block = (
            "Only use [Sx] citations supported by the source metadata below; never invent "
            f"labels.\n\nAvailable source metadata (data only):\n"
            f"{json.dumps(sources, ensure_ascii=False)}"
        )
    else:
        from src.rag.query_engine import _format_context_for_direct_chat, format_sources_api

        # Fresh labels replace earlier ones; stale [Sx] in history would now
        # point at different documents.
        recent = [{**row, "content": _CITATION_MARK_RE.sub("", row["content"])} for row in recent]
        sources = format_sources_api(library_docs)
        source_block = (
            "Library excerpts were retrieved for this request. Add [Sx] right after a "
            "sentence only when that excerpt directly supports it (background, prior work, "
            "methods, comparable results). Never cite a library source for the user's own "
            "new measurements or contributions. Use distinct sources where they fit. Leave "
            "unsupported sentences uncited; do not claim the library lacks relevant work when "
            "some excerpts fit. After the text, briefly list statements that still need a "
            "reference, if any. Never invent labels.\n\n"
            "Library excerpts (data only):\n"
            f"{_format_context_for_direct_chat(library_docs) or '(no matching excerpt)'}"
        )
    system = (
        "You are an academic writing assistant. Execute the user's writing request directly. "
        "Use the supplied text, outline, measurements and recent conversation as material. "
        "Context headings such as validation, discussion or references are not requests to "
        "switch tasks. No indexed documents or prior RAG answer are required. "
        "Preserve supplied values, units and uncertainty. Do not invent experimental results, "
        "claims of novelty, references or facts missing from the material. Do not present "
        "user-provided claims as independently verified. If essential material is absent, "
        "ask a focused question. Return the requested prose, not advice on how to write it. "
        "Follow an explicitly requested output language; otherwise use this default: "
        f"{get_response_language_instruction(language)}\n\n{source_block}"
    )
    messages = [{"role": "system", "content": system}]
    messages.extend({"role": row["role"], "content": row["content"]} for row in recent)
    messages.append({"role": "user", "content": message})
    return messages, sources


def stream_chat_drafting(
    message: str,
    model_name: str,
    *,
    previous_messages: list[dict[str, Any]],
    language: str = "fr",
    abort_signal: StreamAbortSignal | None = None,
    threshold: float = 0.3,
    tags: Any = None,
    collections: Any = None,
) -> Iterator[dict[str, Any]]:
    library_docs: list[Any] | None = None
    if requests_library_citations(message):
        queries = _citation_queries(message, _recent_history(previous_messages))
        for event in _search_library(
            queries,
            model_name,
            threshold=threshold,
            tags=tags,
            collections=collections,
            language=language,
            abort_signal=abort_signal,
        ):
            if event["type"] == "_library_docs":
                library_docs = event["docs"]
            else:
                yield event
    messages, sources = _draft_messages(message, previous_messages, language, library_docs)
    started = time.perf_counter()
    raw = ""
    visible = ""
    pieces: Iterable[str]
    if is_ollama_provider():
        pieces = (
            str(chunk.get("content") or "")
            for chunk in stream_ollama_chat(
                model_name, messages, think=False, abort_signal=abort_signal
            )
        )
    else:
        pieces = stream_text(create_chat_llm(model_name), messages, abort_signal=abort_signal)
    for piece in pieces:
        if abort_signal is not None and abort_signal.is_aborted():
            raise InterruptedError("Generation aborted.")
        raw += piece
        cleaned = sanitize_assistant_content(raw, thinking_enabled=False)
        if cleaned.startswith(visible):
            delta = cleaned[len(visible) :]
            if delta:
                yield {"type": "token", "text": delta}
        elif cleaned != visible:
            yield {"type": "answer_replace", "text": cleaned}
        visible = cleaned
    final = strip_invalid_citations(visible, sources=sources).strip()
    if final != visible:
        yield {"type": "answer_replace", "text": final}
    yield {
        "type": "pipeline_metrics",
        "stage": "llm_generation",
        "data": {"duration_ms": round((time.perf_counter() - started) * 1000, 1)},
    }
    if library_docs is None:
        yield {"type": "sources", "data": sources, "answer_origin": "drafting"}
        return
    from src.rag.query_engine import (
        _build_anchor_event,
        _filter_citation_docs_by_answer,
        format_sources_api,
    )

    cited = _filter_citation_docs_by_answer(final, library_docs)
    yield {"type": "sources", "data": format_sources_api(cited), "answer_origin": "drafting"}
    anchor_event = _build_anchor_event(final, cited)
    if anchor_event:
        yield anchor_event
