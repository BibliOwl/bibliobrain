"""Retrieval, RAG initialization, and chat streaming."""

from __future__ import annotations

import concurrent.futures
import difflib
import functools
import gc
import hashlib
import itertools
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

from langchain_chroma import Chroma
from langchain_classic.chains.combine_documents.stuff import create_stuff_documents_chain
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from src import config
from src.analysis_quality import deep_analysis_guidance, is_deep_article_analysis_request
from src.app_settings import get_app_settings
from src.chat_grounding import check_answer_grounding
from src.chat_intents import (
    is_library_overview_request,
    is_multi_paper_comparison_request,
    refers_to_previous_answer,
)
from src.chat_output_guard import ChatGuardReport, strip_invalid_citations, validate_chat_output
from src.file_access import resolve_pdf_path
from src.llm_provider import (
    StreamAbortSignal,
    activate_fallback,
    create_chat_llm,
    create_embeddings,
    current_embedding_profile_label,
    default_embedding_prompts,
    get_embedding_provider_mode,
    invoke_structured_chat,
    is_ollama_provider,
    is_remote_connection_error,
    is_remote_provider,
    provider_runtime_label,
    requires_local_ollama_embeddings,
    resolve_chat_think_mode,
    resolve_ollama_context_length,
    sanitize_assistant_content,
    stream_ollama_chat,
    stream_text,
    strip_thinking_blocks,
    unload_ollama_model,
    validate_local_embedding_model,
    with_embedding_prompts,
)
from src.model_profiles import get_chat_response_note
from src.native_runtime import native_library_message
from src.prompts import SYSTEM_PROMPT
from src.rag.citation_anchors import extract_citation_anchors
from src.rag.reranker import (
    candidate_multiplier as _reranker_candidate_multiplier,
)
from src.rag.reranker import (
    consume_last_metrics as _reranker_consume_metrics,
)
from src.rag.reranker import (
    english_retrieval_query as _english_retrieval_query,
)
from src.rag.reranker import (
    get_reranker_status as _get_reranker_status,
)
from src.rag.reranker import (
    has_reranker_predict_error as _has_reranker_predict_error,
)
from src.rag.reranker import (
    is_model_loaded as _reranker_is_model_loaded,
)
from src.rag.reranker import (
    is_reranker_enabled as _is_reranker_enabled,
)
from src.rag.reranker import (
    is_reranker_loaded as _is_reranker_loaded,
)
from src.rag.reranker import (
    needs_english_retrieval_query as _needs_english_retrieval_query,
)
from src.rag.reranker import (
    quick_text_chars as _reranker_quick_text_chars,
)
from src.rag.reranker import (
    rerank_documents as _rerank_documents,
)
from src.rag.reranker import (
    resolve_model_name_for_depth as _reranker_model_for_depth,
)
from src.rag.reranker import (
    should_unload_after_use as _reranker_should_unload_after_use,
)
from src.rag.reranker import (
    trigger_load as _reranker_trigger_load,
)
from src.rag_settings import get_persisted_chunk_size
from src.structured_chat import ChatAnswer, normalize_chunk_citation_markup
from src.unicode_utils import sanitize_utf8_text
from src.visual_analysis import VISUAL_CONTENT_TYPE, analyze_pdf_visuals

from . import state
from .document_loader import _sanitize_document
from .embedding_equivalence import adopt_equivalent_vector_index
from .exclusions import excluded_path_keys, excluded_rel_paths, is_metadata_excluded
from .lexical_index import (
    delete_paths as delete_lexical_paths,
)
from .lexical_index import (
    ensure_from_vectorstore as ensure_lexical_index,
)
from .lexical_index import search as search_lexical_index
from .vector_store import (
    _chroma_client_settings,
    _clear_incomplete_index_marker,
    _compute_index_entries,
    _current_embedding_model,
    _has_incomplete_index_marker,
    _index_pdf_groups_with_manifest,
    _load_manifest_chunk_size,
    _load_manifest_embedding_model,
    _load_manifest_embedding_prompts,
    _load_manifest_entries,
    _manifest_chunk_count,
    _manifest_entry_matches,
    _manifest_no_text,
    _mark_index_incomplete,
    _merge_filter_maps,
    _save_manifest_entries,
    _stable_manifest_entry,
    _try_recover_stale_index_marker,
    manifest_model_matches,
    resolve_vector_index_profile,
    set_active_vector_index_profile,
)
from .zotero_integration import (
    _csv_to_list,
    _dedupe_keep_order,
    _filter_active_zotero_pdf_entries,
    _normalize_text,
    _refresh_zotero_library_cache,
    _safe_relpath,
    _split_pipe,
)

_logger = logging.getLogger(__name__)


def _stream_reranker_warmup_status(model_name: str | None = None):
    """Yield status events while pre-warming a reranker model.

    Only triggers when the reranker is enabled but the target model (primary
    quick model when ``model_name`` is None, otherwise the deep-tier model) is
    not yet loaded. Emits a heads-up status, runs the synchronous load in a
    worker thread, and emits progress every ~2s with elapsed time. Does not
    raise — load failures are handled in ``rerank_documents`` (silent fallback).
    """
    if not _is_reranker_enabled():
        return
    if _has_reranker_predict_error():
        return
    if _reranker_should_unload_after_use():
        return

    primary_name = str(_get_reranker_status().get("model_name") or "").strip()
    is_primary = (not model_name) or (model_name == primary_name)
    if is_primary:
        if _is_reranker_loaded():
            return
        target_name = primary_name or "reranker"
    else:
        if _reranker_is_model_loaded(model_name):
            return
        target_name = str(model_name).strip() or "reranker"

    yield {
        "type": "status",
        "msg": _t("reranker_warmup", model=target_name),
    }

    load_done = threading.Event()
    load_outcome: dict[str, Any] = {}

    def _do_load() -> None:
        try:
            load_outcome["status"] = (
                _reranker_trigger_load() if is_primary else _reranker_trigger_load(model_name)
            )
        finally:
            load_done.set()

    worker = threading.Thread(target=_do_load, daemon=True)
    worker.start()
    started_at = time.monotonic()
    while not load_done.wait(timeout=2.5):
        elapsed = int(time.monotonic() - started_at)
        yield {
            "type": "status",
            "msg": _t("reranker_loading", elapsed=elapsed),
        }

    status = load_outcome.get("status") or {}
    if status.get("loaded"):
        total = int(time.monotonic() - started_at)
        yield {
            "type": "status",
            "msg": _t("reranker_ready", total=total),
        }
    elif status.get("load_error"):
        yield {
            "type": "status",
            "msg": _t("reranker_unavailable", error=status["load_error"]),
        }


def _build_reranker_metrics_event() -> dict | None:
    """Pop the last reranker metrics from thread-local and shape it as an event."""
    metrics = _reranker_consume_metrics()
    if not metrics:
        return None
    return {"type": "pipeline_metrics", "stage": "reranker", "data": metrics}


_pipeline_metrics_local = threading.local()


def _duration_ms(started_at: float) -> float:
    return round(max(0.0, (time.perf_counter() - started_at) * 1000.0), 1)


def _build_pipeline_metrics_event(
    stage: str,
    started_at: float,
    data: dict[str, Any] | None = None,
) -> dict:
    payload = dict(data or {})
    payload["duration_ms"] = _duration_ms(started_at)
    return {"type": "pipeline_metrics", "stage": stage, "data": payload}


def _record_pipeline_metric(stage: str, data: dict[str, Any]) -> None:
    metrics = list(getattr(_pipeline_metrics_local, "value", []) or [])
    metrics.append({"type": "pipeline_metrics", "stage": stage, "data": dict(data)})
    _pipeline_metrics_local.value = metrics


def _consume_pipeline_metrics() -> list[dict[str, Any]]:
    metrics = list(getattr(_pipeline_metrics_local, "value", []) or [])
    _pipeline_metrics_local.value = []
    return metrics


def _build_anchor_event(answer_text: str, docs: list) -> dict | None:
    """Build a ``citation_anchors`` event using full chunk text from docs.

    The frontend uses these anchors to highlight the supporting sentence.
    Returns ``None`` when no anchors could be extracted.
    """
    if not answer_text or not docs:
        return None
    try:
        sources_with_full_text = []
        for index, doc in enumerate(docs, start=1):
            metadata = getattr(doc, "metadata", {}) or {}
            sources_with_full_text.append(
                {
                    "index": index,
                    "ref_label": _source_ref_label(doc, index),
                    "chunk_uid": str(metadata.get("chunk_uid", "")).strip(),
                    "content": str(getattr(doc, "page_content", "") or ""),
                }
            )
        anchors = extract_citation_anchors(answer_text, sources_with_full_text)
    except Exception:
        _logger.debug("Recovered from an error in _build_anchor_event.", exc_info=True)
        return None
    if not anchors:
        return None
    return {"type": "citation_anchors", "data": anchors}


_RAG_INIT_LOCK = threading.Lock()


class AutoVisualAnalysisDecision(BaseModel):
    analyze: bool = Field(
        False,
        description=(
            "True only when reading a figure, graph, table, diagram or plotted values is "
            "materially useful to answer the user's exact question."
        ),
    )
    source_index: int = Field(
        0,
        ge=0,
        le=3,
        description="One-based source index to inspect, or 0 when analyze is false.",
    )
    reason: str = Field("", max_length=240, description="Short operational reason.")


_AUTHOR_WORD_RE = r"[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'`-]{1,}"
# "Etienne Rochepault" is one author, not "Etienne" followed by noise.
_AUTHOR_NAME_RE = rf"{_AUTHOR_WORD_RE}(?:\s+{_AUTHOR_WORD_RE}){{0,3}}"
_AUTHOR_LIST_RE = rf"{_AUTHOR_NAME_RE}(?:\s*(?:,|/|&|\+|\bet\b|\band\b)\s*{_AUTHOR_NAME_RE}){{0,5}}"
_CASELESS_AUTHOR_NAME_RE = r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'`-]{1,}"
_CASELESS_AUTHOR_LIST_RE = (
    rf"{_CASELESS_AUTHOR_NAME_RE}"
    rf"(?:\s*(?:,|/|&|\+|\bet\b|\band\b)\s*{_CASELESS_AUTHOR_NAME_RE}){{0,5}}"
)
_AUTHOR_SUFFIX_STOP_RE = re.compile(
    r"\s+\b(?:sur|about|on|concernant|regarding|qui|that|which|avec|with|dans|in|pour|for|"
    r"ecrit|written|auteur|author|articles?|papers?|publications?)\b.*$",
    flags=re.IGNORECASE,
)
_AUTHORSHIP_VERB_RE = (
    r"(?:(?:ecrit|écrit|publie|publié)(?:e|es|s)?\s+par|"
    r"written\s+by|authored\s+by|published\s+by)"
)
_SELF_PRONOUN_RE = re.compile(r"^(?:moi|me|myself|nous|us)$", flags=re.IGNORECASE)
_NATURAL_AUTHOR_PATTERNS = (
    # "publiés par moi (Etienne Rochepault)": the name in brackets is the author.
    re.compile(
        rf"\b(?i:(?:{_AUTHORSHIP_VERB_RE}|par|by|de)\s+(?:moi|me|myself|nous|us))\s*"
        rf"\(\s*(?P<authors>[^()]{{2,80}}?)\s*\)",
    ),
    re.compile(
        rf"\b(?i:{_AUTHORSHIP_VERB_RE})\s+"
        rf"(?P<authors>{_CASELESS_AUTHOR_LIST_RE})",
    ),
    re.compile(
        rf"\b(?i:(?:de\s+l['’]?auteur(?:e|es|s)?|d['’]?auteur(?:e|es|s)?|"
        rf"l['’]?auteur(?:e|es|s)?|auteur(?:e|es|s)?|author)\s+)"
        rf"(?P<authors>{_AUTHOR_LIST_RE})",
    ),
    re.compile(
        rf"\b(?i:(?:{_AUTHORSHIP_VERB_RE}|par))\s+"
        rf"(?P<authors>{_AUTHOR_LIST_RE})",
    ),
    re.compile(
        rf"\b(?i:(?:article|articles|paper|papers|publication|publications)\s+"
        rf"(?:de|d['’]|by))\s+(?P<authors>{_AUTHOR_LIST_RE})",
    ),
    re.compile(
        rf"\b(?P<authors>{_AUTHOR_LIST_RE})\s+"
        rf"(?i:(?:a|à|ont|has|have)\s+(?:travaille|travaillé|travailler|travailles|worked)\s+"
        rf"(?:sur|on|about))\b",
    ),
)

_INLINE_FILTER_PATTERNS = {
    "tags": re.compile(r"\b(?:tag|tags)\s*:\s*([^\n;]+)", flags=re.IGNORECASE),
    "collections": re.compile(
        r"\b(?:collection|collections|dossier|dossiers)\s*:\s*([^\n;]+)", flags=re.IGNORECASE
    ),
    "authors": re.compile(
        rf"\b(?:author|auteur)\s*:\s*({_CASELESS_AUTHOR_LIST_RE})",
        flags=re.IGNORECASE,
    ),
    "years": re.compile(r"\b(?:year|annee)\s*:\s*((?:19|20)\d{2})", flags=re.IGNORECASE),
    "dois": re.compile(r"\bdoi\s*:\s*([^\s,;]+)", flags=re.IGNORECASE),
}


def _normalize_author_filter_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = re.sub(r"['’`]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_author_names(value: str) -> list[str]:
    clean = _AUTHOR_SUFFIX_STOP_RE.sub("", str(value or "")).strip()
    if not clean:
        return []
    parts = re.split(r"\s*(?:,|/|&|\+|\bet\b|\band\b)\s*", clean, flags=re.IGNORECASE)
    names = [part.strip(" .:;!?()[]{}\"'`’") for part in parts]
    return _dedupe_keep_order(
        [name for name in names if name and not _SELF_PRONOUN_RE.fullmatch(name)]
    )


def _drop_partial_author_names(authors: list[str]) -> list[str]:
    """Keep "Etienne Rochepault" rather than also matching every "Etienne"."""
    token_sets = [set(_normalize_author_filter_text(author).split()) for author in authors]
    return [
        author
        for author, tokens in zip(authors, token_sets, strict=True)
        if not any(tokens < other for other in token_sets)
    ]


def _extract_inline_filters(message: str) -> tuple[str, dict[str, list[str]]]:
    filters: dict[str, list[str]] = {
        "tags": [],
        "collections": [],
        "authors": [],
        "years": [],
        "dois": [],
    }
    cleaned = message

    for key, pattern in _INLINE_FILTER_PATTERNS.items():
        for match in pattern.finditer(message):
            filters[key].extend(_csv_to_list(match.group(1)))
            cleaned = cleaned.replace(match.group(0), " ")

    for pattern in _NATURAL_AUTHOR_PATTERNS:
        for match in pattern.finditer(message):
            authors = _extract_author_names(match.group("authors"))
            if not authors:
                continue
            filters["authors"].extend(authors)
            cleaned = cleaned.replace(match.group(0), " ")

    for key in filters:
        filters[key] = _dedupe_keep_order(filters[key])
    filters["authors"] = _drop_partial_author_names(filters["authors"])
    # A shorter pattern may have removed only "par Etienne" and left "Rochepault".
    for author in filters["authors"]:
        for word in (word for word in author.split() if len(word) > 2):
            cleaned = re.sub(rf"\b{re.escape(word)}\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned or message, filters


_STRICT_FILTER_KEYS = ("authors", "years", "dois", "tags", "collections", "sources")


def _normalize_source_filter_path(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    text = re.sub(r"\s*/\s*", "/", text)
    text = re.sub(r"/{2,}", "/", text)
    return text.casefold()


def _source_filter_path_candidates(value: Any) -> list[str]:
    raw = str(value or "").strip()
    normalized = re.sub(r"\s*[\\/]\s*", "/", raw)
    return _dedupe_keep_order(
        [candidate for candidate in (raw, normalized, normalized.replace("/", "\\")) if candidate]
    )


def _prepare_explicit_filters(
    tags: Any = None,
    collections: Any = None,
    source_paths: Any = None,
) -> dict[str, list[str]]:
    return {
        "tags": _dedupe_keep_order(_csv_to_list(tags)),
        "collections": _dedupe_keep_order(_csv_to_list(collections)),
        "sources": _dedupe_keep_order(_csv_to_list(source_paths)),
        "authors": [],
        "years": [],
        "dois": [],
    }


def _filter_summary(filters: dict[str, list[str]]) -> str:
    parts: list[str] = []
    if filters["tags"]:
        parts.append(f"tags={', '.join(filters['tags'])}")
    if filters["collections"]:
        parts.append(f"collections={', '.join(filters['collections'])}")
    if filters["authors"]:
        parts.append(f"authors={', '.join(filters['authors'])}")
    if filters["years"]:
        parts.append(f"years={', '.join(filters['years'])}")
    if filters["dois"]:
        parts.append(f"doi={', '.join(filters['dois'])}")
    if filters.get("sources"):
        labels = [Path(value).name or value for value in filters["sources"]]
        parts.append(f"article={', '.join(labels)}")
    return " | ".join(parts)


def _doc_matches_filters(metadata: dict[str, Any], filters: dict[str, list[str]]) -> bool:
    tags_lc = _split_pipe(str(metadata.get("zotero_tags_lc", "")))
    collections_lc = _split_pipe(str(metadata.get("zotero_collections_lc", "")))
    authors_lc = _normalize_author_filter_text(
        " ".join(
            [
                str(metadata.get("zotero_authors_lc", "")),
                str(metadata.get("zotero_authors", "")),
                str(metadata.get("zotero_first_author_lc", "")),
                str(metadata.get("zotero_first_author", "")),
            ]
        )
    )
    year = str(metadata.get("zotero_year", "")).strip()
    doi_lc = _normalize_text(str(metadata.get("zotero_doi", "")))

    requested_tags = [_normalize_text(value) for value in filters.get("tags", []) if value.strip()]
    if requested_tags and not all(
        any(tag in candidate for candidate in tags_lc) for tag in requested_tags
    ):
        return False

    requested_collections = [
        _normalize_text(value) for value in filters.get("collections", []) if value.strip()
    ]
    if requested_collections and not any(
        any(collection in candidate for candidate in collections_lc)
        for collection in requested_collections
    ):
        return False

    requested_authors = [
        _normalize_author_filter_text(value)
        for value in filters.get("authors", [])
        if value.strip()
    ]
    # Token-wise: "etienne rochepault" must match "Rochepault, Etienne".
    if requested_authors and not any(
        all(token in authors_lc for token in author.split()) for author in requested_authors
    ):
        return False

    requested_years = [value.strip() for value in filters.get("years", []) if value.strip()]
    if requested_years and year not in requested_years:
        return False

    requested_dois = [_normalize_text(value) for value in filters.get("dois", []) if value.strip()]
    if requested_dois and not any(doi in doi_lc for doi in requested_dois):
        return False

    requested_sources = {
        _normalize_source_filter_path(value)
        for value in filters.get("sources", [])
        if str(value or "").strip()
    }
    if requested_sources:
        candidate_sources = {
            _normalize_source_filter_path(metadata.get("rel_path", "")),
            _normalize_source_filter_path(metadata.get("source", "")),
        }
        candidate_sources.discard("")
        if requested_sources.isdisjoint(candidate_sources):
            return False

    return True


_TARGET_STUDY_PATTERN = re.compile(r"([A-Z][A-Za-z'`-]+)\s*\((19|20)\d{2}\)")
_RETRIEVAL_STOPWORDS = {
    "alors",
    "avec",
    "avoir",
    "bien",
    "cette",
    "dans",
    "des",
    "du",
    "elle",
    "elles",
    "est",
    "et",
    "faire",
    "for",
    "from",
    "have",
    "how",
    "ils",
    "les",
    "mais",
    "more",
    "nous",
    "par",
    "pas",
    "plus",
    "pour",
    "quoi",
    "quand",
    "que",
    "quel",
    "quelle",
    "quelles",
    "quels",
    "sais",
    "savoir",
    "ses",
    "sont",
    "sur",
    "the",
    "their",
    "them",
    "they",
    "this",
    "tu",
    "une",
    "what",
    "which",
    "with",
    "your",
}
# Function words were counted as query terms: every title holding "and"
# earned title and lexical bonuses ("Hysteresis and coupling loss..." came up
# for unrelated questions), and French "ont" or "qui" did the same.
_RETRIEVAL_STOPWORDS.update(
    {
        "about", "after", "all", "also", "among", "and", "any", "are", "based", "been",
        "before", "being", "between", "both", "but", "can", "could", "did", "does", "doing",
        "done", "during", "each", "had", "has", "into", "its", "make", "makes", "many", "may",
        "might", "much", "must", "not", "one", "only", "other", "our", "over", "same",
        "should", "some", "such", "than", "then", "there", "these", "those", "three", "two",
        "under", "upon", "use", "used", "using", "very", "via", "was", "were", "when", "where",
        "whether", "while", "who", "whom", "whose", "why", "will", "within", "without", "would",
        "afin", "ainsi", "apres", "après", "aux", "avant", "cela", "celle", "celles", "celui",
        "ces", "ceux", "chez", "comme", "comment", "depuis", "donc", "dont", "encore", "entre",
        "etait", "était", "ete", "été", "etre", "être", "leur", "leurs", "lors", "lorsque",
        "meme", "même", "memes", "mêmes", "ont", "peut", "peuvent", "pourquoi", "puis", "qui",
        "sans", "selon", "sera", "son", "sous", "tous", "tout", "toute", "toutes", "tres",
        "très", "vers",
    }
)
# The full-text query also sees two-letter words, which the term sets skip.
_LEXICAL_SEARCH_STOPWORDS = frozenset(
    _RETRIEVAL_STOPWORDS
    | {
        "an", "as", "at", "be", "by", "if", "in", "is", "it", "of", "on", "or", "to",
        "au", "ce", "de", "en", "il", "la", "le", "ne", "ou", "où", "sa", "se", "un",
    }
)
_COMMON_HYPHENATED_QUERY_TERMS = {
    "current-voltage",
    "finite-element",
    "high-field",
    "high-temperature",
    "low-field",
    "low-temperature",
    "post-mortem",
    "state-of-the-art",
}
_NAMED_QUERY_TERM_RE = re.compile(r"\b([A-Za-zÀ-ÖØ-öø-ÿ]{3,}(?:[-–—][A-Za-zÀ-ÖØ-öø-ÿ]{3,})+)\b")
_NAMED_ANCHOR_CONTEXT_TERMS = {
    "dependence",
    "equation",
    "fit",
    "fitting",
    "formulation",
    "law",
    "model",
    "parameter",
    "parameters",
    "relation",
}
_FRENCH_HYPHENATED_CLITICS = {
    "en",
    "la",
    "le",
    "les",
    "leur",
    "lui",
    "moi",
    "nous",
    "toi",
    "vous",
    "y",
}


def _extract_target_study(query: str) -> tuple[str, str]:
    match = _TARGET_STUDY_PATTERN.search(query)
    if not match:
        return "", ""
    author = _normalize_text(match.group(1))
    year_match = re.search(r"(19|20)\d{2}", match.group(0))
    year = year_match.group(0) if year_match else ""
    return author, year


def _looks_like_named_phrase(query: str, match: re.Match[str]) -> bool:
    """Kim-Anderson or Bean-Livingston name a model; voltage-based does not.

    Anchors are hard filters, so ordinary compounds ("multi-megawatt",
    "oxygen-staged", French "ont-ils") used to reject nearly every passage.
    A capital that only opens the sentence does not make a name.
    """
    parts = [part for part in re.split(r"[-–—]", match.group(1)) if part]
    if any(part[:1].isupper() for part in parts[1:]):
        return True
    if not parts or not parts[0][:1].isupper():
        return False
    before = query[: match.start(1)].rstrip()
    return bool(before) and not before.endswith((".", "!", "?", ":", "\n"))


def _named_query_anchor_groups(query: str) -> list[tuple[str, ...]]:
    groups: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for match in _NAMED_QUERY_TERM_RE.finditer(str(query or "")):
        if not _looks_like_named_phrase(str(query or ""), match):
            continue
        phrase = _normalize_text(match.group(1)).replace("–", "-").replace("—", "-")
        # French imperatives commonly attach an object pronoun with a hyphen
        # ("cite-les", "explique-moi"). They are instructions, not named
        # scientific anchors, and must not filter out unrelated document text.
        phrase_parts = tuple(part for part in phrase.split("-") if part)
        if phrase_parts and phrase_parts[-1] in _FRENCH_HYPHENATED_CLITICS:
            continue
        if phrase in _COMMON_HYPHENATED_QUERY_TERMS:
            continue
        terms = tuple(
            token
            for token in re.split(r"[-\s]+", phrase)
            if len(token) >= 3 and token not in _RETRIEVAL_STOPWORDS
        )
        if not terms or terms in seen:
            continue
        seen.add(terms)
        groups.append(terms)
    return groups


def _matches_named_query_anchors(
    groups: list[tuple[str, ...]],
    *,
    metadata: dict[str, Any],
    content: str,
) -> bool:
    if not groups:
        return True
    haystack = _normalize_text(f"{_retrieval_metadata_text(metadata)}\n{content}")
    normalized_words = re.sub(r"[^a-z0-9]+", " ", haystack)

    for group in groups:
        if not group:
            continue
        ordered_phrase = " ".join(group)
        reverse_phrase = " ".join(reversed(group))
        if ordered_phrase in normalized_words or reverse_phrase in normalized_words:
            continue

        primary = group[0]
        context_pattern = "|".join(re.escape(term) for term in sorted(_NAMED_ANCHOR_CONTEXT_TERMS))
        contextual_pattern = re.compile(
            rf"\b{re.escape(primary)}\b"
            rf"(?:\s+[a-z0-9]{{1,24}}){{0,4}}\s+"
            rf"\b(?:{context_pattern})\b"
        )
        if not contextual_pattern.search(normalized_words):
            return False
    return True


_GREEK_PRIMARY_SYMBOLS: dict[str, str] = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "zeta": "ζ",
    "eta": "η",
    "theta": "θ",
    "iota": "ι",
    "kappa": "κ",
    "lambda": "λ",
    "mu": "μ",
    "nu": "ν",
    "xi": "ξ",
    "omicron": "ο",
    "pi": "π",
    "rho": "ρ",
    "sigma": "σ",
    "tau": "τ",
    "upsilon": "υ",
    "phi": "φ",
    "chi": "χ",
    "psi": "ψ",
    "omega": "ω",
}
_GREEK_SYMBOL_NAMES: dict[str, str] = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "ε": "epsilon",
    "ϵ": "epsilon",
    "ζ": "zeta",
    "η": "eta",
    "θ": "theta",
    "ϑ": "theta",
    "ι": "iota",
    "κ": "kappa",
    "ϰ": "kappa",
    "λ": "lambda",
    "μ": "mu",
    "µ": "mu",
    "ν": "nu",
    "ξ": "xi",
    "ο": "omicron",
    "π": "pi",
    "ϖ": "pi",
    "ρ": "rho",
    "ϱ": "rho",
    "σ": "sigma",
    "ς": "sigma",
    "τ": "tau",
    "υ": "upsilon",
    "φ": "phi",
    "ϕ": "phi",
    "χ": "chi",
    "ψ": "psi",
    "ω": "omega",
}
_GREEK_SYMBOL_ALIASES = str.maketrans(_GREEK_SYMBOL_NAMES)
_GREEK_SYMBOLS_BY_NAME: dict[str, set[str]] = {
    name: {symbol} for name, symbol in _GREEK_PRIMARY_SYMBOLS.items()
}
for _symbol, _name in _GREEK_SYMBOL_NAMES.items():
    _GREEK_SYMBOLS_BY_NAME.setdefault(_name, set()).add(_symbol)
_GREEK_NAME_PATTERN = "|".join(sorted(_GREEK_PRIMARY_SYMBOLS, key=len, reverse=True))
_DASH_CHARS_RE = re.compile(r"[\u2010-\u2015\u2212]+")


def _normalize_symbolic_text(value: str) -> str:
    normalized = _normalize_text(str(value or ""))
    normalized = _DASH_CHARS_RE.sub("-", normalized)
    return normalized.translate(_GREEK_SYMBOL_ALIASES)


def _compact_symbolic_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", _normalize_symbolic_text(value))


def _symbolic_pair_aliases(left: str, greek_name: str, *, left_is_greek: bool) -> set[str]:
    other = str(left or "").strip().lower()
    name = str(greek_name or "").strip().lower()
    if not other or not name:
        return set()
    symbols = _GREEK_SYMBOLS_BY_NAME.get(name, set())
    aliases: set[str] = set()
    if left_is_greek:
        aliases.update({f"{name}-{other}", f"{name} {other}", f"{name}_{other}", f"{name}{other}"})
        for symbol in symbols:
            aliases.update(
                {f"{symbol}-{other}", f"{symbol} {other}", f"{symbol}_{other}", f"{symbol}{other}"}
            )
    else:
        aliases.update({f"{other}-{name}", f"{other} {name}", f"{other}_{name}", f"{other}{name}"})
        for symbol in symbols:
            aliases.update(
                {f"{other}-{symbol}", f"{other} {symbol}", f"{other}_{symbol}", f"{other}{symbol}"}
            )
    return aliases


_GREEK_LOOKALIKE_WORDS = {"meta", "a", "i", "y"}


def _greek_join_is_notation(separator: str, other: str, name: str) -> bool:
    """T-phi, epsilon_r, mu0 and Tphi are notation; multi, pick and meta are words.

    Without a separator, only digits or a single capital letter count: a
    lowercase letter glued to "mu", "pi" or "eta" is far more often an
    ordinary word (mud, pin) than a symbol. Words that merely contain a
    Greek name (Beta = B+eta, Meta, the acronym API = A+PI) are skipped.
    """
    if other.lower() in _GREEK_LOOKALIKE_WORDS:
        return False
    if not separator:
        for joined in (f"{other}{name}", f"{name}{other}"):
            lowered = joined.lower()
            if lowered in _GREEK_PRIMARY_SYMBOLS or lowered in _GREEK_LOOKALIKE_WORDS:
                return False
        if f"{other}{name}".isupper():
            return False
        return other.isdigit() or (len(other) == 1 and other.isupper())
    if separator in {"-", "_"}:
        return True
    return len(other) == 1


def _symbolic_alias_terms_for_query(query: str) -> set[str]:
    raw = str(query or "")
    normalized = _normalize_symbolic_text(raw)
    # Same text with the case kept, to tell "Tphi" (notation) from "tphi".
    cased = _DASH_CHARS_RE.sub("-", raw).translate(_GREEK_SYMBOL_ALIASES)
    aliases: set[str] = set()

    # Whole words only: substrings turned "bibliographie" into phi, "chiffre"
    # into chi and "formule" into mu.
    for name, symbols in _GREEK_SYMBOLS_BY_NAME.items():
        if re.search(rf"\b{name}\b", normalized) or any(symbol in raw for symbol in symbols):
            aliases.add(name)
            aliases.update(symbols)

    for prefix, separator, name in re.findall(
        rf"\b([A-Za-z0-9]{{1,4}})([-_\s]?)({_GREEK_NAME_PATTERN})\b",
        cased,
        flags=re.IGNORECASE,
    ):
        if _greek_join_is_notation(separator, prefix, name):
            aliases.update(_symbolic_pair_aliases(prefix, name.lower(), left_is_greek=False))
    for name, separator, suffix in re.findall(
        rf"\b({_GREEK_NAME_PATTERN})([-_\s]?)([A-Za-z0-9]{{1,4}})\b",
        cased,
        flags=re.IGNORECASE,
    ):
        if _greek_join_is_notation(separator, suffix, name):
            aliases.update(_symbolic_pair_aliases(suffix, name.lower(), left_is_greek=True))

    return {alias for alias in aliases if alias}


def _expand_symbolic_retrieval_query(query: str) -> str:
    aliases = sorted(_symbolic_alias_terms_for_query(query))
    if not aliases:
        return query
    return f"{query}\n\nEquivalent scientific notation terms: {' '.join(aliases[:80])}"


def _contains_symbolic_anchor(value: str, aliases: set[str]) -> bool:
    if not aliases:
        return False
    normalized = _normalize_symbolic_text(value)
    compact = _compact_symbolic_text(value)
    for alias in aliases:
        normalized_alias = _normalize_symbolic_text(alias)
        if normalized_alias and normalized_alias in normalized:
            return True
        compact_alias = _compact_symbolic_text(alias)
        if compact_alias and compact_alias in compact:
            return True
    return False


def _retrieval_query_terms(query: str) -> tuple[set[str], bool]:
    normalized = _normalize_symbolic_text(query)
    base_terms = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9\-_]{2,}", normalized)
        if token not in _RETRIEVAL_STOPWORDS
    }
    expanded = set(base_terms)
    expanded.update(_symbolic_alias_terms_for_query(query))

    domain_query = False
    if "supraconduct" in normalized or "superconduct" in normalized:
        domain_query = True
        expanded.update(
            {
                "superconduct",
                "superconductor",
                "superconducting",
                "supraconduct",
                "supraconducteur",
                "supraconducteurs",
            }
        )
    if (
        "haute temperature" in normalized
        or "high temperature" in normalized
        or re.search(r"\bhts\b", normalized)
    ):
        domain_query = True
        expanded.update(
            {
                "high temperature superconductor",
                "high-temperature superconductor",
                "high temperature superconductivity",
                "hts",
                "rebco",
                "ybco",
                "coated conductor",
            }
        )
    if (
        "temperature critique" in normalized
        or "critical temperature" in normalized
        or re.search(r"\btc\b", normalized)
    ):
        domain_query = True
        expanded.update(
            {
                "critical temperature",
                "transition temperature",
                "temperature critique",
                "tc",
            }
        )

    return expanded, domain_query


def _retrieval_metadata_text(metadata: dict[str, Any]) -> str:
    source_path = str(metadata.get("source", "") or metadata.get("rel_path", "")).strip()
    source_name = Path(source_path).stem if source_path else ""
    parts = [
        str(metadata.get("zotero_title", "")).strip(),
        str(metadata.get("zotero_authors", "")).strip(),
        str(metadata.get("zotero_first_author", "")).strip(),
        str(metadata.get("zotero_year", "")).strip(),
        str(metadata.get("zotero_doi", "")).strip(),
        str(metadata.get("zotero_citation", "")).strip(),
        str(metadata.get("zotero_journal", "")).strip(),
        str(metadata.get("zotero_tags", "")).strip(),
        str(metadata.get("zotero_collections", "")).strip(),
        source_name,
    ]
    return _normalize_text(" ".join(part for part in parts if part))


# The lexical scan scores ~640 candidates per query, a few dozen papers whose
# chunks share title, authors and tags: these pure functions are cached so
# each title and the query are tokenised and fuzzy-matched once. They took
# 0.3 s per search, more than the vector search and the reranker together.
@functools.lru_cache(maxsize=8192)
def _lexical_tokens(value: str) -> frozenset[str]:
    normalized = _normalize_symbolic_text(value)
    return frozenset(
        token
        for token in re.findall(r"[a-z0-9][a-z0-9\-_]{2,}", normalized)
        if token not in _RETRIEVAL_STOPWORDS
    )


_FUZZY_TOKEN_RATIO = 0.82


@functools.lru_cache(maxsize=8192)
def _fuzzy_token_overlap(query_tokens: frozenset[str], candidate_tokens: frozenset[str]) -> int:
    if not query_tokens or not candidate_tokens:
        return 0
    count = 0
    for query_token in query_tokens:
        if query_token in candidate_tokens:
            continue
        if len(query_token) < 5:
            continue
        for candidate in candidate_tokens:
            if abs(len(query_token) - len(candidate)) > 2:
                continue
            matcher = difflib.SequenceMatcher(None, query_token, candidate)
            # Cheap upper bounds first; same verdict as ratio() alone.
            if (
                matcher.real_quick_ratio() >= _FUZZY_TOKEN_RATIO
                and matcher.quick_ratio() >= _FUZZY_TOKEN_RATIO
                and matcher.ratio() >= _FUZZY_TOKEN_RATIO
            ):
                count += 1
                break
    return count


def _generic_lexical_score(query: str, *, metadata: dict[str, Any], content: str) -> float:
    query_tokens = _lexical_tokens(query)
    if not query_tokens:
        return 0.0

    metadata_text = _retrieval_metadata_text(metadata)
    content_text = _normalize_text(content)
    metadata_tokens = _lexical_tokens(metadata_text)
    content_tokens = _lexical_tokens(content_text)

    metadata_hits = len(query_tokens & metadata_tokens)
    content_hits = len(query_tokens & content_tokens)
    fuzzy_metadata_hits = _fuzzy_token_overlap(query_tokens, metadata_tokens)

    score = min(0.72, metadata_hits * 0.16)
    score += min(0.24, content_hits * 0.03)
    score += min(0.24, fuzzy_metadata_hits * 0.12)

    query_phrase = _normalize_text(query)
    if query_phrase and len(query_phrase) >= 8 and query_phrase in metadata_text:
        score += 0.24
    return score


def _document_identity_key(doc: Document) -> tuple[str, str, str, str]:
    metadata = doc.metadata or {}
    return (
        str(metadata.get("source", "")),
        str(metadata.get("page", "")),
        str(metadata.get("content_type", "")),
        doc.page_content[:180],
    )


def _metadata_paper_identity_key(metadata: dict[str, Any]) -> tuple[str, str, str]:
    article_id = str(metadata.get("article_id", "") or metadata.get("zotero_item_key", "")).strip()
    doi = _normalize_text(str(metadata.get("zotero_doi", "") or ""))
    title = _normalize_text(
        str(metadata.get("zotero_title", "") or metadata.get("title", "") or "")
    )
    source = str(metadata.get("source", "") or metadata.get("rel_path", "") or "").strip()
    return (article_id, doi, title or source)


def _diversify_documents_by_article(
    docs: list[Document],
    *,
    limit: int,
) -> list[Document]:
    """Prefer one representative chunk per article before repeating sources."""

    if limit <= 0:
        return []
    first_per_article: list[Document] = []
    repeated: list[Document] = []
    seen_articles: set[tuple[str, str, str]] = set()
    for doc in docs:
        article_key = _metadata_paper_identity_key(doc.metadata or {})
        if article_key in seen_articles:
            repeated.append(doc)
            continue
        seen_articles.add(article_key)
        first_per_article.append(doc)
    return [*first_per_article, *repeated][:limit]


@functools.lru_cache(maxsize=4096)
def _word_start_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9]){re.escape(term)}")


def _term_starts_word(term: str, text: str) -> bool:
    """Term at the start of a word: "magnet" matches "magnetization".

    A plain substring test also matched "and" in "standard", "hts" in
    "rights" and "tc" in "etc", spreading bonuses over unrelated passages.
    """
    return term in text and bool(_word_start_pattern(term).search(text))


def _retrieval_overlap_adjustment(
    query_terms: set[str],
    *,
    metadata: dict[str, Any],
    content: str,
    domain_query: bool,
) -> float:
    if not query_terms:
        return 0.0

    metadata_text = _retrieval_metadata_text(metadata)
    metadata_symbolic = _normalize_symbolic_text(metadata_text)
    content_text = _normalize_text(content)
    content_symbolic = _normalize_symbolic_text(content)
    title_hits = sum(
        1
        for term in query_terms
        if term and (_term_starts_word(term, metadata_text) or _term_starts_word(term, metadata_symbolic))
    )
    content_hits = sum(
        1
        for term in query_terms
        if term and (_term_starts_word(term, content_text) or _term_starts_word(term, content_symbolic))
    )

    adjustment = min(0.48, title_hits * 0.12) + min(0.18, content_hits * 0.03)
    if domain_query and title_hits == 0 and content_hits == 0:
        adjustment -= 0.18
    return adjustment


def _method_anchor_adjustment(query: str, *, metadata: dict[str, Any], content: str) -> float:
    aliases = _symbolic_alias_terms_for_query(query)
    if not aliases:
        return 0.0
    haystack = f"{_retrieval_metadata_text(metadata)}\n{content}"
    if _contains_symbolic_anchor(haystack, aliases):
        return 0.42
    return -0.08


def _collect_scored_docs(
    query: str,
    k: int,
    *,
    excluded_paths: list[str] | None = None,
) -> list[tuple[Document, float]]:
    with state._state_lock:
        vectorstore = state.vectorstore
    if vectorstore is None:
        return []
    if excluded_paths:
        # Filter inside Chroma: dropping excluded chunks after the top-k would
        # leave few passages when an excluded paper dominates the topic.
        try:
            scored = vectorstore.similarity_search_with_relevance_scores(
                query,
                k=k,
                filter={"rel_path": {"$nin": list(excluded_paths)}},
            )
            return [(doc, float(score)) for doc, score in scored]
        except Exception:
            _logger.warning("Excluded-paths filter failed; filtering after search", exc_info=True)
            k = k * 2
    try:
        scored = vectorstore.similarity_search_with_relevance_scores(query, k=k)
        return [(doc, float(score)) for doc, score in scored]
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        docs = vectorstore.similarity_search(query, k=k)
        return [(doc, 1.0) for doc in docs]


def _collect_source_scored_docs(
    query: str,
    *,
    k: int,
    source_paths: list[str],
) -> list[tuple[Document, float]]:
    """Run semantic retrieval inside selected articles when the store supports it."""
    with state._state_lock:
        vectorstore = state.vectorstore
    if vectorstore is None:
        return []

    requested = [str(value or "").strip() for value in source_paths if str(value or "").strip()]
    if not requested:
        return _collect_scored_docs(query, k=k)

    per_source_k = max(k, 8)
    scored: list[tuple[Document, float]] = []
    try:
        for rel_path in requested:
            for stored_path in _source_filter_path_candidates(rel_path):
                rows = vectorstore.similarity_search_with_relevance_scores(
                    query,
                    k=per_source_k,
                    filter={"rel_path": stored_path},
                )
                scored.extend((doc, float(score)) for doc, score in rows)
    except (AttributeError, NotImplementedError, TypeError, ValueError):
        # Older/custom vector stores may not accept metadata filters. The
        # downstream exact filter still prevents chunks from other articles.
        scored = _collect_scored_docs(query, k=max(k * max(2, len(requested)), 32))

    unique: list[tuple[Document, float]] = []
    seen: set[tuple[str, str, str, str]] = set()
    filters = {"sources": requested}
    for doc, score in scored:
        if not _doc_matches_filters(doc.metadata or {}, filters):
            continue
        key = _document_identity_key(doc)
        if key in seen:
            continue
        seen.add(key)
        unique.append((doc, score))
    return unique


def _collect_lexical_metadata_docs(
    query: str,
    *,
    max_docs: int,
    filters: dict[str, list[str]] | None = None,
    scan_limit: int | None = None,
    excluded_keys: frozenset[str] = frozenset(),
) -> list[tuple[Document, float]]:
    with state._state_lock:
        vectorstore = state.vectorstore
    if vectorstore is None or not hasattr(vectorstore, "get"):
        return []

    has_strict_metadata_filter = (
        any(filters.get(key) for key in _STRICT_FILTER_KEYS) if filters else False
    )
    has_source_filter = bool(filters and filters.get("sources"))
    runtime_db_path = str(state.runtime_db_path or "").strip()
    if runtime_db_path and ensure_lexical_index(runtime_db_path, vectorstore):
        try:
            candidates = search_lexical_index(
                runtime_db_path,
                query,
                limit=min(2000, max(250, max_docs * 20)),
                stopwords=_LEXICAL_SEARCH_STOPWORDS,
            )
        except Exception:
            _logger.exception("Lexical FTS search failed; using bounded Chroma scan")
        else:
            named_anchor_groups = _named_query_anchor_groups(query)
            ranked_fts: list[tuple[float, Document]] = []
            for candidate in candidates:
                metadata = dict(candidate.metadata or {})
                if filters and not _doc_matches_filters(metadata, filters):
                    continue
                if is_metadata_excluded(metadata, excluded_keys):
                    continue
                if metadata.get("content_type") == "wiki_page":
                    continue
                doc = _sanitize_document(candidate)
                score = _generic_lexical_score(
                    query,
                    metadata=metadata,
                    content=doc.page_content,
                )
                if (
                    _matches_named_query_anchors(
                        named_anchor_groups,
                        metadata=metadata,
                        content=doc.page_content,
                    )
                    and named_anchor_groups
                ):
                    score += 0.75
                if score > 0:
                    ranked_fts.append((score, doc))
            ranked_fts.sort(key=lambda item: item[0], reverse=True)
            return [(doc, score) for score, doc in ranked_fts[:max_docs]]

    total_count = 50000
    collection = getattr(vectorstore, "_collection", None)
    if collection is not None and hasattr(collection, "count"):
        try:
            total_count = max(0, int(collection.count()))
        except Exception:
            _logger.debug(
                "Recovered from an error in _collect_lexical_metadata_docs.", exc_info=True
            )
            total_count = 50000
    scan_limit_raw = os.getenv("RAG_LEXICAL_SCAN_LIMIT", "").strip()
    if scan_limit_raw:
        try:
            scan_limit_env = int(scan_limit_raw)
            if scan_limit_env > 0:
                total_count = min(total_count, scan_limit_env)
        except ValueError:
            pass
    # Caller-supplied cap (e.g. the quick tier bounds the scan for latency).
    if scan_limit and scan_limit > 0:
        total_count = min(total_count, scan_limit)
    total_count = max(max_docs, total_count)

    named_anchor_groups = _named_query_anchor_groups(query)
    ranked: list[tuple[float, Document]] = []
    page_size = min(3000, max(250, total_count))
    include_fields = (
        ["documents", "metadatas"]
        if has_source_filter or not has_strict_metadata_filter
        else ["metadatas"]
    )
    for offset in range(0, total_count, page_size):
        try:
            payload = vectorstore.get(
                include=include_fields,
                limit=min(page_size, total_count - offset),
                offset=offset,
            )
        except TypeError:
            if offset > 0:
                break
            try:
                payload = vectorstore.get(
                    include=include_fields,
                    limit=scan_limit,
                )
            except Exception:
                _logger.debug(
                    "Recovered from an error in _collect_lexical_metadata_docs.", exc_info=True
                )
                return []
        except Exception:
            _logger.debug(
                "Recovered from an error in _collect_lexical_metadata_docs.", exc_info=True
            )
            if offset == 0:
                return []
            break

        documents = list(payload.get("documents") or [])
        metadatas = list(payload.get("metadatas") or [])
        for index, metadata_raw in enumerate(metadatas):
            metadata = dict(metadata_raw or {})
            if filters and not _doc_matches_filters(metadata, filters):
                continue
            if is_metadata_excluded(metadata, excluded_keys):
                continue
            if metadata.get("content_type") == "wiki_page":
                continue
            text = documents[index] if index < len(documents) else ""
            doc = _sanitize_document(Document(page_content=str(text or ""), metadata=metadata))
            score = _generic_lexical_score(query, metadata=metadata, content=doc.page_content)
            if (
                _matches_named_query_anchors(
                    named_anchor_groups,
                    metadata=metadata,
                    content=doc.page_content,
                )
                and named_anchor_groups
            ):
                score += 0.75
            if score <= 0:
                continue
            ranked.append((score, doc))

    ranked.sort(key=lambda item: item[0], reverse=True)
    unique: list[tuple[Document, float]] = []
    seen: set[tuple[Any, ...]] = set()
    for score, doc in ranked:
        key = (
            _metadata_paper_identity_key(doc.metadata or {})
            if has_strict_metadata_filter and not has_source_filter
            else _document_identity_key(doc)
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append((doc, score))
        if len(unique) >= max_docs:
            break
    return unique


def _interleave_vector_order(
    ranked: list[tuple[float, Document]],
    vector_scored: list[tuple[Document, float]],
) -> list[tuple[float, Document]]:
    """Alternate the adjusted-score order with the plain vector order.

    Lexical scores (up to ~1.5) and vector relevance (~0.5-0.75) are not on
    one scale, and title-overlap bonuses come on top: the reranker pool used
    to fill up with keyword-boosted passages, so the best semantic matches
    never reached the cross-encoder. Interleaving gives each signal half the
    pool; the reranker then judges them all. Only candidates that already
    passed the filters (they are in ``ranked``) are used.
    """
    vector_rank = {
        _document_identity_key(doc): position for position, (doc, _score) in enumerate(vector_scored)
    }
    by_vector = sorted(
        (item for item in ranked if _document_identity_key(item[1]) in vector_rank),
        key=lambda item: vector_rank[_document_identity_key(item[1])],
    )
    interleaved: list[tuple[float, Document]] = []
    seen: set[tuple[str, str, str, str]] = set()
    # Adjusted order leads: when the pool is too small to be reranked, its
    # order is kept as is.
    for pair in itertools.zip_longest(ranked, by_vector):
        for item in pair:
            if item is None:
                continue
            key = _document_identity_key(item[1])
            if key not in seen:
                seen.add(key)
                interleaved.append(item)
    return interleaved


def _merge_vector_and_lexical_scored_docs(
    vector_scored: list[tuple[Document, float]],
    lexical_scored: list[tuple[Document, float]],
) -> list[tuple[Document, float]]:
    if not lexical_scored:
        return list(vector_scored)

    merged: dict[tuple[str, str, str, str], tuple[Document, float]] = {}
    order: list[tuple[str, str, str, str]] = []

    for doc, score in vector_scored:
        key = _document_identity_key(doc)
        if key not in merged:
            order.append(key)
        merged[key] = (doc, float(score))

    for doc, lexical_score in lexical_scored:
        key = _document_identity_key(doc)
        combined_score = float(lexical_score)
        if key in merged:
            existing_doc, existing_score = merged[key]
            merged[key] = (existing_doc, max(existing_score, combined_score))
        else:
            order.append(key)
            merged[key] = (doc, combined_score)

    return [merged[key] for key in order]


def _quick_lexical_scan_limit() -> int:
    raw = os.getenv("RAG_QUICK_LEXICAL_SCAN_LIMIT", "").strip()
    try:
        value = int(raw) if raw else 6000
    except ValueError:
        value = 6000
    return max(500, value)


def _deep_lexical_scan_limit() -> int:
    raw = os.getenv("RAG_LEXICAL_SCAN_LIMIT", "").strip()
    try:
        value = int(raw) if raw else 12000
    except ValueError:
        value = 12000
    return max(1000, value)


# Room for the answer (and thinking, when enabled) on top of the prompt.
_CHAT_OUTPUT_TOKENS_RESERVE = 2048
# A question on several selected articles gets one passage per article, up to
# this many; num_ctx grows with the prompt, so the passages are not truncated.
_MULTI_ARTICLE_MAX_CHUNKS = 12
# Same floor as discovery, so switching features does not reload the model.
_CHAT_MIN_CONTEXT_TOKENS = 6000


def _chat_context_options(model_name: str, *parts: str) -> dict[str, int] | None:
    """num_ctx for a chat answer; Ollama Auto would otherwise cap it at 4096.

    With six long passages plus the history, the prompt can exceed 4096
    tokens, and Ollama then silently drops its beginning (the instructions).
    """
    prompt_tokens = sum(len(str(part or "")) for part in parts) // 3 + 1
    context_length = resolve_ollama_context_length(
        model_name,
        required_tokens=max(_CHAT_MIN_CONTEXT_TOKENS, prompt_tokens + _CHAT_OUTPUT_TOKENS_RESERVE),
    )
    return {"num_ctx": context_length} if context_length else None


def _content_key(doc: Document) -> str:
    """Same passage, whatever copy of the PDF it comes from."""
    return re.sub(r"\s+", " ", str(doc.page_content or "")).strip().casefold()[:300]


def _dedupe_documents_by_content(docs: list[Document]) -> list[Document]:
    kept: list[Document] = []
    seen: set[str] = set()
    for raw_doc in docs:
        # Lexical hits come straight from the index text: repair them too, so
        # a passage and its unrepaired twin are recognised as one.
        doc = _sanitize_document(raw_doc)
        key = _content_key(doc)
        if key and key in seen:
            continue
        seen.add(key)
        kept.append(doc)
    return kept


_translation_executor_lock = threading.Lock()
_translation_executor_instance: concurrent.futures.ThreadPoolExecutor | None = None


def _translation_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _translation_executor_instance
    with _translation_executor_lock:
        if _translation_executor_instance is None:
            _translation_executor_instance = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="bibliobrain-query-translation"
            )
        return _translation_executor_instance


def _retrieve_documents(
    query: str,
    threshold: float,
    max_chunks: int,
    filters: dict[str, list[str]],
    *,
    use_reranker: bool = True,
    depth: str = "deep",
) -> list[Document]:
    """Retrieve passages, also searching with the English version of a French question.

    Most of the library is in English: the lexical search only matched the
    French words of a French question, so "courants d'écrantage" returned one
    French thesis (twice, from two copies of the PDF) instead of the screening
    current papers. The candidate pools of both queries are merged without
    duplicates and the reranker, which reads both languages, judges them once
    and keeps the best ``max_chunks``. Each query used to be reranked on its
    own before a third rerank of the two results: a French question took
    1.3 s where an English one took 0.5 s.
    """
    single = {"use_reranker": use_reranker, "depth": depth}

    def searched_alone() -> list[Document]:
        return _dedupe_documents_by_content(
            _retrieve_documents_single(query, threshold, max_chunks, filters, **single)
        )[:max_chunks]

    if is_library_overview_request(query) or not _needs_english_retrieval_query(query):
        return searched_alone()
    # The translation (0.3 s on CPU) runs while the French pool is searched.
    translation = _translation_executor().submit(_english_retrieval_query, query)
    docs = _retrieve_documents_single(
        query, threshold, max_chunks, filters, pool_only=True, **single
    )
    try:
        translated = translation.result()
    except Exception:
        _logger.warning("English retrieval query unavailable", exc_info=True)
        translated = ""
    if not translated:
        return searched_alone()
    english_docs = _retrieve_documents_single(
        translated, threshold, max_chunks, filters, pool_only=True, **single
    )
    merged: list[Document] = []
    for pair in zip(english_docs, docs, strict=False):
        merged.extend(pair)
    longer = english_docs if len(english_docs) > len(docs) else docs
    merged.extend(longer[min(len(english_docs), len(docs)) :])
    # Both pools whole: cut to one pool's size, a paper ranked 7th in the
    # French pool was dropped before the reranker could see it.
    merged = _dedupe_documents_by_content(merged)
    spread_articles = len((filters or {}).get("sources") or []) > 1
    if use_reranker and _is_reranker_enabled() and len(merged) > max_chunks:
        try:
            quick = str(depth or "").strip().lower() == "quick"
            reranked = _rerank_documents(
                _expand_symbolic_retrieval_query(query),
                merged,
                top_k=len(merged) if spread_articles else max_chunks,
                model_name=_reranker_model_for_depth(depth),
                max_text_chars=_reranker_quick_text_chars() if quick else None,
            )
            if spread_articles:
                return _diversify_documents_by_article(reranked, limit=max_chunks)
            return reranked
        except Exception:
            _logger.warning("Bilingual rerank failed; keeping interleaved order", exc_info=True)
    if spread_articles:
        return _diversify_documents_by_article(merged, limit=max_chunks)
    return merged[:max_chunks]


_CRITICAL_QUANTITY_RE = re.compile(
    r"\b(?:courant|temp[eé]rature|champ|densit[eé] de courant|[eé]tat|surface|rayon|longueur"
    r"|valeur|seuil|vitesse|taille|point)s?\s+critiques?\b"
)


def _without_critical_quantities(text: str) -> str:
    """"Courant critique" is a quantity, not a request for a critique."""
    return _CRITICAL_QUANTITY_RE.sub(" ", text)


def _retrieve_documents_single(
    query: str,
    threshold: float,
    max_chunks: int,
    filters: dict[str, list[str]],
    *,
    use_reranker: bool = True,
    depth: str = "deep",
    pool_only: bool = False,
) -> list[Document]:
    """The passages for one query; ``pool_only`` returns the whole candidate pool.

    The pool is what the reranker chooses from: a bilingual question merges
    the pools of both languages and reranks them once (see _retrieve_documents).
    """
    quick = str(depth or "").strip().lower() == "quick"
    library_overview = is_library_overview_request(query)
    query_lc = query.lower()
    target_author, target_year = _extract_target_study(query)
    query_terms, domain_query = _retrieval_query_terms(query)
    named_anchor_groups = _named_query_anchor_groups(query)
    exclude_primary_study = ("autres articles" in query_lc) or ("other articles" in query_lc)
    critique_query = any(
        term in _without_critical_quantities(query_lc)
        for term in ("critique", "limitation", "faiblesse", "weakness", "bias", "biais")
    )
    critique_terms = (
        "critique",
        "limitation",
        "faiblesse",
        "weakness",
        "bias",
        "biais",
        "concern",
        "problem",
    )

    reranker_enabled = bool(use_reranker and _is_reranker_enabled())
    reranker_predict_error = _has_reranker_predict_error()
    reranker_on = reranker_enabled and not reranker_predict_error
    mult = _reranker_candidate_multiplier()
    rerank_model_name = _reranker_model_for_depth(depth)
    rerank_max_text = _reranker_quick_text_chars() if quick else None
    if quick:
        # Tight pool: just enough for the reranker to reorder, no wide fan-out.
        candidate_k = (
            min(48, max(max_chunks * 2, max_chunks * mult))
            if reranker_on
            else min(40, max(16, max_chunks * 3))
        )
    elif domain_query:
        candidate_k = min(96, max(36, max_chunks * 8))
    else:
        candidate_k = min(64, max(24, max_chunks * 4))
    if reranker_on and not quick:
        # Pull a wider pool so the reranker has real choices to make.
        candidate_k = min(160, max(candidate_k, max_chunks * mult * 2))
    if library_overview:
        # A generic prompt such as "main themes in my library" has no topical
        # anchor. Search broadly, then keep evidence from different articles.
        candidate_k = min(160, max(candidate_k, max_chunks * 12, 64))
    retrieval_started = time.perf_counter()
    vector_started = time.perf_counter()
    expanded_query = _expand_symbolic_retrieval_query(query)
    source_paths = list(filters.get("sources") or []) if filters else []
    # Papers excluded from the RAG stay reachable when the user explicitly
    # scopes the question to them; every other search skips them.
    excluded_paths = [] if source_paths else excluded_rel_paths()
    excluded_keys = frozenset() if source_paths else excluded_path_keys()
    if source_paths:
        vector_scored = _collect_source_scored_docs(
            expanded_query, k=candidate_k, source_paths=source_paths
        )
    elif excluded_paths:
        vector_scored = _collect_scored_docs(
            expanded_query, k=candidate_k, excluded_paths=excluded_paths
        )
    else:
        vector_scored = _collect_scored_docs(expanded_query, k=candidate_k)
    vector_search_ms = _duration_ms(vector_started)
    lexical_started = time.perf_counter()
    has_strict_filter = bool(filters and any(filters.get(key) for key in _STRICT_FILTER_KEYS))
    # Quick tier bounds the full-collection Python scan for latency — but only
    # when no strict metadata filter is active (those need the complete scan to
    # surface late-indexed matches).
    if quick and not has_strict_filter:
        lexical_scored = _collect_lexical_metadata_docs(
            query,
            max_docs=min(32, max(8, max_chunks * 4)),
            filters=filters,
            scan_limit=_quick_lexical_scan_limit(),
            excluded_keys=excluded_keys,
        )
    else:
        lexical_scored = _collect_lexical_metadata_docs(
            query,
            max_docs=min(64, max(12, max_chunks * 6)),
            filters=filters,
            scan_limit=_deep_lexical_scan_limit(),
            excluded_keys=excluded_keys,
        )
    lexical_search_ms = _duration_ms(lexical_started)
    scored = _merge_vector_and_lexical_scored_docs(
        vector_scored,
        lexical_scored,
    )
    ranked: list[tuple[float, Document]] = []
    rejected_named_anchor = 0

    def add_candidates(min_score: float, *, anchor_groups: list[tuple[str, ...]]) -> None:
        nonlocal rejected_named_anchor
        for doc, score in scored:
            metadata = doc.metadata or {}
            if filters and not _doc_matches_filters(metadata, filters):
                continue
            if is_metadata_excluded(metadata, excluded_keys):
                continue
            if metadata.get("content_type") == "wiki_page":
                continue
            if not _matches_named_query_anchors(
                anchor_groups,
                metadata=metadata,
                content=doc.page_content,
            ):
                rejected_named_anchor += 1
                continue

            first_author_lc = _normalize_text(str(metadata.get("zotero_first_author_lc", "")))
            year = str(metadata.get("zotero_year", "")).strip()
            is_primary_target = bool(
                target_author
                and target_year
                and (target_author in first_author_lc)
                and (year == target_year)
            )
            if exclude_primary_study and is_primary_target:
                continue

            adjusted_score = score
            if metadata.get("content_type") == "zotero_note":
                adjusted_score += 0.18

            content_lc = doc.page_content.lower()
            adjusted_score += _retrieval_overlap_adjustment(
                query_terms,
                metadata=metadata,
                content=content_lc,
                domain_query=domain_query,
            )
            adjusted_score += _method_anchor_adjustment(
                query,
                metadata=metadata,
                content=doc.page_content,
            )
            if critique_query and any(
                term in _without_critical_quantities(content_lc) for term in critique_terms
            ):
                adjusted_score += 0.08

            if target_author and target_year:
                if (
                    (target_author in content_lc)
                    and (target_year in content_lc)
                    and not is_primary_target
                ):
                    adjusted_score += 0.12

            if adjusted_score < min_score:
                continue
            ranked.append((adjusted_score, doc))

    # Once the user explicitly chose an article, precision comes from the exact
    # source filter. Keeping the global relevance threshold could otherwise
    # discard useful chunks for short prompts such as "résume cet article".
    # The same goes for named anchors: a pasted sentence saying "Metal-Insulated
    # (MI)" made "metal insulated" a hard filter, and a paper that writes
    # "metal-insulation" or "MI" lost every chunk, so the chat claimed the
    # selected paper had no indexed text.
    min_score = 0.0 if source_paths else threshold
    add_candidates(min_score, anchor_groups=[] if source_paths else named_anchor_groups)
    if not ranked and rejected_named_anchor:
        # Anchors sharpen a pool; they must not empty it.
        add_candidates(min_score, anchor_groups=[])

    ranked.sort(key=lambda item: item[0], reverse=True)
    # Several selected articles: a comparison must not be answered from the
    # single closest paper, so each article's best chunk enters the pool first.
    spread_articles = library_overview or len(source_paths) > 1
    if len(source_paths) > 1:
        best_per_article: list[tuple[float, Document]] = []
        other_chunks: list[tuple[float, Document]] = []
        seen_articles: set[tuple[str, str, str]] = set()
        for item in ranked:
            article_key = _metadata_paper_identity_key(item[1].metadata or {})
            (other_chunks if article_key in seen_articles else best_per_article).append(item)
            seen_articles.add(article_key)
        ranked = [*best_per_article, *other_chunks]

    # Only when the cross-encoder judges the pool: without it, this order is
    # the answer, and a strong metadata match (an author named in the
    # question) must stay first.
    if reranker_on and not spread_articles:
        ranked = _interleave_vector_order(ranked, vector_scored)

    unique_docs: list[Document] = []
    seen: set[tuple[str, str, str, str]] = set()
    # When reranking, keep a wider pool of unique candidates and let the
    # cross-encoder pick the final ``max_chunks``. Quick keeps it tight.
    if library_overview:
        pool_cap = min(candidate_k, max(max_chunks * 6, 48))
    elif not reranker_on:
        pool_cap = max_chunks
    elif quick:
        pool_cap = min(candidate_k, max(max_chunks, max_chunks * mult))
    else:
        pool_cap = min(candidate_k, max(max_chunks, max_chunks * mult * 2))
    # The same page from two copies of a PDF (a duplicate Zotero attachment)
    # used to take two places in the pool the reranker chooses from: on an
    # 804-PDF library a question came back with 3 distinct passages of 6.
    seen_texts: set[str] = set()
    for retrieval_score, doc in ranked:
        metadata = doc.metadata or {}
        key = (
            str(metadata.get("source", "")),
            str(metadata.get("page", "")),
            str(metadata.get("content_type", "")),
            doc.page_content[:180],
        )
        text_key = _content_key(_sanitize_document(doc))
        if key in seen or (text_key and text_key in seen_texts):
            continue
        seen.add(key)
        seen_texts.add(text_key)
        unique_docs.append(
            _clone_document_with_metadata(
                doc,
                {"rag_retrieval_score": round(float(retrieval_score), 6)},
            )
        )
        if len(unique_docs) >= pool_cap:
            break

    unique_count_before_rerank = len(unique_docs)
    rerank_top_k = max_chunks
    if spread_articles:
        rerank_top_k = min(len(unique_docs), max_chunks * 4)
    # Several selected articles keep the whole pool so the cut below stays
    # per article; the cross-encoder only orders it.
    if pool_only:
        pass
    elif reranker_on and (
        len(unique_docs) > rerank_top_k
        or (len(source_paths) > 1 and len(unique_docs) > max_chunks)
    ):
        unique_docs = _rerank_documents(
            expanded_query,
            unique_docs,
            top_k=rerank_top_k,
            model_name=rerank_model_name,
            max_text_chars=rerank_max_text,
        )
    elif reranker_on:
        # Enabled but no point running the cross-encoder: ≤max_chunks candidates
        # already, so no reordering would change the final cut. Still record a
        # metric so the user sees the stage in the debug panel.
        from src.rag.reranker import _record_metrics as _record_rerank_metrics

        _record_rerank_metrics(
            {
                "enabled": True,
                "applied": False,
                "candidates_in": len(unique_docs),
                "top_k": max_chunks,
                "skipped_reason": "candidates_le_top_k",
            }
        )
    elif reranker_enabled and reranker_predict_error:
        from src.rag.reranker import _record_metrics as _record_rerank_metrics

        _record_rerank_metrics(
            {
                "enabled": True,
                "applied": False,
                "predict_error": _get_reranker_status().get("predict_error", ""),
                "candidates_in": len(unique_docs),
                "top_k": max_chunks,
                "skipped_reason": "previous_predict_error",
            }
        )

    _record_pipeline_metric(
        "retrieval",
        {
            "duration_ms": _duration_ms(retrieval_started),
            "vector_search_ms": vector_search_ms,
            "lexical_search_ms": lexical_search_ms,
            "candidate_k": candidate_k,
            "scored_candidates": len(scored),
            "vector_candidates": len(vector_scored),
            "lexical_candidates": len(lexical_scored),
            "ranked_candidates": len(ranked),
            "unique_candidates": unique_count_before_rerank,
            "returned": min(len(unique_docs), max_chunks),
            "depth": "quick" if quick else "deep",
            "reranker_model": rerank_model_name or _get_reranker_status().get("model_name", ""),
            "reranker_enabled": reranker_enabled,
            "reranker_available": reranker_on,
            "named_anchor_groups": len(named_anchor_groups),
            "rejected_named_anchor": rejected_named_anchor,
            "library_overview": library_overview,
            "pool_only": pool_only,
        },
    )

    if pool_only:
        return unique_docs
    if spread_articles:
        unique_docs = _diversify_documents_by_article(unique_docs, limit=max_chunks)
    return unique_docs[:max_chunks]


def _build_history_context(
    conversation_history: Any,
    *,
    max_turns: int = 4,
    max_chars: int = 1200,
) -> str:
    if not isinstance(conversation_history, list):
        return ""

    kept: list[tuple[str, str, list[str]]] = []
    total_chars = 0
    for raw_entry in reversed(conversation_history):
        if not isinstance(raw_entry, dict):
            continue
        role = str(raw_entry.get("role", "")).strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = strip_thinking_blocks(str(raw_entry.get("content", "")).strip())
        content = re.sub(r"\s+", " ", content)
        if not content:
            continue
        if len(content) > 320:
            content = content[:320].rstrip() + "..."
        source_lines: list[str] = []
        raw_meta = raw_entry.get("meta")
        raw_sources = raw_meta.get("sources") if isinstance(raw_meta, dict) else None
        if role == "assistant" and isinstance(raw_sources, list):
            for index, raw_source in enumerate(raw_sources[:8], start=1):
                if not isinstance(raw_source, dict):
                    continue
                ref_label = str(raw_source.get("ref_label") or f"S{index}").strip().upper()
                title = str(raw_source.get("title") or raw_source.get("file") or "").strip()
                if not ref_label or not title:
                    continue
                source_lines.append(f"[{ref_label}] {title}")
        approx_len = len(content) + sum(len(line) + 2 for line in source_lines) + 24
        if kept and (len(kept) >= max_turns or (total_chars + approx_len) > max_chars):
            break
        kept.append((role, content, source_lines))
        total_chars += approx_len

    if not kept:
        return ""

    kept.reverse()
    lines = ["Historique recent de la conversation (contexte):"]
    for role, content, source_lines in kept:
        speaker = "Utilisateur" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {content}")
        if source_lines:
            lines.append(
                "Correspondance des citations de cette reponse (historique immuable; "
                "ne pas remapper ces labels):"
            )
            lines.extend(source_lines)
    return "\n".join(lines)


def _resolve_historical_bibliographic_reference(
    message: str,
    conversation_history: Any,
) -> tuple[str, dict[str, Any]] | None:
    """Resolve explicit questions such as "what was S1's title?" from chat metadata."""
    if not isinstance(conversation_history, list):
        return None

    ref_match = re.search(r"\bS(\d+)\b", str(message or ""), flags=re.IGNORECASE)
    if not ref_match:
        return None
    ref_label = f"S{int(ref_match.group(1))}"
    normalized = _normalize_text(message)
    bibliographic_markers = (
        "titre",
        "title",
        "quel document",
        "quelle source",
        "document cite",
        "source citee",
        "reference citee",
        "bibliographique",
    )
    if not any(marker in normalized for marker in bibliographic_markers):
        return None

    # A citation label is local to one assistant response. Use the newest
    # source-bearing response only so an older S1 can never shadow "S1 above".
    for raw_entry in reversed(conversation_history):
        if not isinstance(raw_entry, dict):
            continue
        if str(raw_entry.get("role", "")).strip().lower() != "assistant":
            continue
        raw_meta = raw_entry.get("meta")
        raw_sources = raw_meta.get("sources") if isinstance(raw_meta, dict) else None
        if not isinstance(raw_sources, list) or not raw_sources:
            continue
        for index, raw_source in enumerate(raw_sources, start=1):
            if not isinstance(raw_source, dict):
                continue
            candidate_label = str(raw_source.get("ref_label") or f"S{index}").strip().upper()
            if candidate_label == ref_label:
                return ref_label, dict(raw_source)
        return None
    return None


def _historical_bibliographic_answer(ref_label: str, source: dict[str, Any]) -> str:
    title = str(source.get("title") or source.get("file") or "").strip()
    if not title:
        title = "document sans titre renseigne"
    if _current_ui_language() == "en":
        return f"The exact title of [{ref_label}] is “{title}”."
    return f"Le titre exact du document [{ref_label}] est « {title} »."


def _has_usable_conversation_history(conversation_history: Any) -> bool:
    if not isinstance(conversation_history, list):
        return False
    return any(
        isinstance(entry, dict)
        and str(entry.get("role", "")).strip().lower() in {"user", "assistant"}
        and bool(str(entry.get("content", "")).strip())
        for entry in conversation_history
    )


def _needs_reference_clarification(message: str, conversation_history: Any) -> bool:
    """Detect context-free comparative pronouns before launching retrieval."""
    if _has_usable_conversation_history(conversation_history):
        return False

    normalized = _normalize_text(message)
    if not normalized:
        return False

    french_clitic_comparison = re.search(
        r"\b(?:compare|comparez|comparons)-(?:le|la|les)\b",
        normalized,
    )
    french_bare_choice = re.search(
        r"(?:^|[.!?]\s*)(?:lequel|laquelle|lesquels|lesquelles)\b",
        normalized,
    )
    english_bare_choice = re.search(
        r"(?:^|[.!?]\s*)which (?:one|ones)\b",
        normalized,
    )
    return bool(french_clitic_comparison or french_bare_choice or english_bare_choice)


def _reference_clarification_message() -> str:
    if _current_ui_language() == "en":
        return (
            "I can compare them, but I do not have enough context to know what "
            '"them" refers to. Which documents, methods, or items should I compare?'
        )
    return (
        "Je peux les comparer, mais je n'ai pas assez de contexte pour savoir à quoi "
        '« les » fait référence. Quels documents, méthodes ou éléments voulez-vous comparer ?'
    )


def _single_article_comparison_message() -> str:
    if _current_ui_language() == "en":
        return (
            "Your questions are limited to a single article, so there is nothing to "
            "compare it with. Select several articles in the Library (checkboxes, then "
            '"Ask in chat"), or remove the limit with the × above the input to compare '
            "with the rest of your library."
        )
    return (
        "Vos questions sont limitées à un seul article : il n'y a rien à quoi le "
        "comparer. Sélectionnez plusieurs articles dans la Bibliothèque (cases à cocher, "
        "puis « Questionner dans le chat »), ou retirez la limite avec le × au-dessus "
        "de la zone de saisie pour comparer avec le reste de votre bibliothèque."
    )


def retrieve_for_question(
    question: str,
    conversation_history: Any,
    threshold: float,
    max_chunks: int,
    filters: dict[str, list[str]],
    *,
    depth: str = "deep",
) -> list[Document]:
    """The passages a chat question is answered from, as the chat finds them.

    The retrieval benchmark calls it too, so it measures what the chat does.
    """
    return _retrieve_documents(
        _build_retrieval_query(question, conversation_history),
        threshold,
        max_chunks,
        filters,
        depth=depth,
    )


_FOLLOW_UP_OPENING_RE = re.compile(
    r"^(?:et|and|ou|or|mais|but|aussi|also|puis|then|alors|so|donc|quid|pareil|idem|same"
    r"|what about|how about)\b"
)
# "Qu'est-ce que", "ce que dit l'article": not a pointer to the previous answer.
_NON_ANAPHORIC_CE_RE = re.compile(r"\b(?:est ce|ce que|ce qui|ce dont|c est)\b")
_ANAPHOR_WORDS = frozenset(
    "il ils elle elles ce cet cette ces celui celle ceux celles ca cela ceci leur leurs "
    "son sa ses lui eux it its they them their this these those he she his her him".split()
)


def _is_follow_up_question(message: str) -> bool:
    """Whether a question leans on the previous one ("Et à 4 K ?", "What are its limits?").

    Opening with "and"/"et", two words or fewer, or a pronoun in a short
    question. A question that names its own subject is not a follow-up,
    even with a pronoun pointing back to it ("Un plasma ... peut-il servir
    à la propulsion ?"): those name five or more things, follow-ups four or
    fewer.
    """
    text = unicodedata.normalize("NFKD", str(message or "").lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    words = re.findall(r"[a-z0-9]+", text)
    if not words:
        return False
    joined = " ".join(words)
    if _FOLLOW_UP_OPENING_RE.match(joined) or len(words) <= 2:
        return True
    words = _NON_ANAPHORIC_CE_RE.sub(" ", joined).split()
    content = [word for word in words if len(word) >= 4 and word not in _RETRIEVAL_STOPWORDS]
    return len(content) <= 4 and any(word in _ANAPHOR_WORDS for word in words)


def _build_retrieval_query(message: str, conversation_history: Any) -> str:
    """The question, preceded by the previous one when it is a follow-up.

    The previous question used to be prepended to every question: after a
    topic change it pulled the search back to the old topic (2 of 10 topic
    switches lost their paper on the maintainer's library).
    """
    if not isinstance(conversation_history, list) or not _is_follow_up_question(message):
        return message

    recent_user_msg = ""
    for raw_entry in reversed(conversation_history):
        if not isinstance(raw_entry, dict):
            continue
        if str(raw_entry.get("role", "")).strip().lower() != "user":
            continue
        content = re.sub(r"\s+", " ", str(raw_entry.get("content", "")).strip())
        if not content:
            continue
        if len(content) > 120:
            content = content[:120].rstrip() + "..."
        recent_user_msg = content
        break

    if not recent_user_msg:
        return message

    return f"{recent_user_msg}\n\nQuestion actuelle: {message}"


def _dedupe_source_docs(docs: list[Document], *, limit: int | None = None) -> list[Document]:
    deduped: list[Document] = []
    seen: set[tuple[str, ...]] = set()

    for doc in docs:
        metadata = doc.metadata or {}
        chunk_uid = str(metadata.get("chunk_uid", "") or "").strip()
        article_id = str(metadata.get("article_id", "") or "").strip()
        chunk_id = str(metadata.get("chunk_id", "") or "").strip()
        if chunk_uid:
            key: tuple[str, ...] = ("chunk_uid", chunk_uid)
        elif article_id and chunk_id:
            key = ("article_chunk", article_id, chunk_id)
        else:
            key = (
                "source_page_text",
                str(metadata.get("source", "") or metadata.get("rel_path", "")).strip(),
                str(metadata.get("page", "")).strip(),
                re.sub(r"\s+", " ", str(doc.page_content or "")).strip()[:240],
            )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(doc)
        if limit is not None and len(deduped) >= limit:
            break

    return deduped


def _clone_document_with_metadata(doc: Document, metadata_updates: dict[str, Any]) -> Document:
    metadata = dict(doc.metadata or {})
    metadata.update(metadata_updates)
    cloned = Document(page_content=doc.page_content, metadata=metadata)
    if hasattr(doc, "id"):
        try:
            cloned.id = doc.id
        except (AttributeError, TypeError):
            pass
    return cloned


def _pin_source_ref_labels(docs: list[Document]) -> list[Document]:
    pinned: list[Document] = []
    for index, doc in enumerate(_dedupe_source_docs(docs), start=1):
        metadata = dict(doc.metadata or {})
        if metadata.get("ref_label"):
            pinned.append(doc)
            continue
        pinned.append(
            _clone_document_with_metadata(doc, {"ref_label": _source_ref_label(doc, index)})
        )
    return pinned


def _build_source_citation_guide(docs: list[Document]) -> str:
    all_docs = _dedupe_source_docs(docs)
    unique_docs = all_docs[:8]
    if not unique_docs:
        return ""

    # Titles of the first 8 only (the context shows them all), but every label
    # is allowed: an answer about a 40-paper list cites beyond [S8].
    allowed_labels: list[str] = []
    other_labels = [
        _source_ref_label(doc, index) for index, doc in enumerate(all_docs, start=1) if index > 8
    ]
    lines = [
        "Guide de citations [Sx] (obligatoire):",
        "Utilise uniquement les labels exacts presents ci-dessous.",
        "Ne cite jamais un document absent du guide.",
        "Recopie les labels du guide sans les renumeroter ; n'invente aucun autre label.",
        "N'utilise jamais des references numeriques du PDF comme [12] ou [53].",
    ]
    for index, doc in enumerate(unique_docs, start=1):
        metadata = doc.metadata or {}
        ref_label = _source_ref_label(doc, index)
        allowed_labels.append(ref_label)
        title = str(
            metadata.get("zotero_title") or _safe_relpath(str(metadata.get("source", "source")))
        )
        authors = str(
            metadata.get("zotero_authors") or metadata.get("zotero_first_author") or ""
        ).strip()
        year = str(metadata.get("zotero_year") or "").strip()
        page = str(metadata.get("page") or "").strip()
        details = " · ".join(part for part in [authors, year, f"p. {page}" if page else ""] if part)
        suffix = f" ({details})" if details else ""
        lines.append(f"[{ref_label}] {title}{suffix}")

    lines.append(
        "Regle: cite directement le document avec son label [Sx] dans chaque phrase "
        "qui repose sur une source. Si aucun label autorise ne soutient une affirmation, "
        "n'ecris pas cette affirmation."
    )
    allowed_labels.extend(other_labels)
    lines.append("Labels autorises: " + ", ".join(f"[{label}]" for label in allowed_labels) + ".")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Localization helpers
# ---------------------------------------------------------------------------

_L10N: dict[str, dict[str, str]] = {
    "reranker_warmup": {
        "fr": "Préchauffage du reranker {model} (premier appel, téléchargement Hugging Face éventuel)…",
        "en": "Warming up reranker {model} (first call, Hugging Face download may occur)…",
    },
    "reranker_loading": {
        "fr": "Chargement du reranker… {elapsed}s écoulées (téléchargement HuggingFace au 1er appel).",
        "en": "Loading reranker… {elapsed}s elapsed (HuggingFace download on 1st call).",
    },
    "reranker_ready": {
        "fr": "Reranker prêt ({total}s). Recherche en cours…",
        "en": "Reranker ready ({total}s). Searching…",
    },
    "reranker_unavailable": {
        "fr": "Reranker indisponible ({error}), fallback au tri vectoriel.",
        "en": "Reranker unavailable ({error}), falling back to vector ranking.",
    },
    "searching_vector_db": {
        "fr": "Recherche dans la base vectorielle...",
        "en": "Searching vector database...",
    },
    "previous_answer_sources": {
        "fr": "Réponse à partir des {count} sources de la réponse précédente…",
        "en": "Answering from the {count} sources of the previous answer…",
    },
    "filters_active": {
        "fr": "Filtres Zotero actifs: {summary}",
        "en": "Active Zotero filters: {summary}",
    },
    "sources_found_generating": {
        "fr": "Sources trouvees, generation en cours...",
        "en": "Sources found, generating response...",
    },
    "visual_judging": {
        "fr": "Le LLM evalue si la lecture des figures serait utile...",
        "en": "The LLM is deciding whether reading figures would be useful...",
    },
    "visual_reading": {
        "fr": "Lecture des figures de {title} (3 pages maximum)...",
        "en": "Reading figures from {title} (up to 3 pages)...",
    },
    "visual_forced_reading": {
        "fr": "Lecture forcee des figures de {title} (3 pages maximum)...",
        "en": "Forced figure reading from {title} (up to 3 pages)...",
    },
    "visual_added": {
        "fr": "Analyse visuelle ajoutee aux sources ({count} page(s)).",
        "en": "Visual analysis added to the sources ({count} page(s)).",
    },
    "visual_not_needed": {
        "fr": "Le LLM estime que les figures ne sont pas necessaires pour cette question.",
        "en": "The LLM determined that figures are not needed for this question.",
    },
    "visual_no_result": {
        "fr": "Aucune figure exploitable detectee ; poursuite avec les sources textuelles.",
        "en": "No usable figure detected; continuing with text sources.",
    },
    "visual_unavailable": {
        "fr": "Analyse visuelle indisponible ; poursuite avec les sources textuelles.",
        "en": "Visual analysis unavailable; continuing with text sources.",
    },
    "fragile_detected_repairing": {
        "fr": "Reponse fragile detectee, reparation en cours...",
        "en": "Fragile response detected, repairing...",
    },
    "response_repaired": {
        "fr": "Reponse reparee.",
        "en": "Response repaired.",
    },
    "values_to_check": {
        "fr": "*Valeurs à vérifier, absentes des passages retrouvés : {values}.*",
        "en": "*Values to check, not found in the retrieved passages: {values}.*",
    },
    "repair_impossible_fallback": {
        "fr": "Reparation impossible, reponse prudente utilisee ({reason}).",
        "en": "Repair impossible, using cautious response ({reason}).",
    },
    "searching_papers": {
        "fr": "Recherche des papiers a comparer...",
        "en": "Searching for papers to compare...",
    },
    "searching_evidence": {
        "fr": "Recherche des preuves dans l'index RAG...",
        "en": "Searching for evidence in RAG index...",
    },
    "searching_fact_check": {
        "fr": "Recherche de sources pour verifier le passage...",
        "en": "Searching for sources to verify the passage...",
    },
    "sources_kept_analyzing": {
        "fr": "{count} source(s) retenue(s), analyse en cours...",
        "en": "{count} source(s) kept, analyzing...",
    },
    "no_relevant_source": {
        "fr": "Aucune source pertinente retrouvee dans l'index RAG pour cette demande.",
        "en": "No relevant source found in the RAG index for this request.",
    },
    "no_sources_in_conversation": {
        "fr": "Je n'ai pas trouve de sources citees dans cette conversation. Generez d'abord une reponse RAG sourcee.",
        "en": "I could not find cited sources in this conversation. First generate a sourced RAG response.",
    },
    "no_recent_answer_for_notebook": {
        "fr": "Je n'ai pas trouve de reponse ou de sources recentes a transformer en carnet. Generez d'abord une reponse sourcee.",
        "en": "I could not find a recent answer or sources to transform into a notebook. First generate a sourced response.",
    },
    "notebook_created": {
        "fr": "Carnet cree : **{title}**.",
        "en": "Notebook created: **{title}**.",
    },
    "sources_added": {
        "fr": "- Sources ajoutees : {count}",
        "en": "- Sources added: {count}",
    },
    "notes_added": {
        "fr": "- Notes ajoutees : {value}",
        "en": "- Notes added: {value}",
    },
    "open_notebooks_tab": {
        "fr": "Ouvrez l'onglet Carnets pour le consulter.",
        "en": "Open the Notebooks tab to view it.",
    },
    "no_sources_found": {
        "fr": "_Aucune source trouvee._",
        "en": "_No sources found._",
    },
    "searching_in_progress": {
        "fr": "_Recherche en cours..._",
        "en": "_Searching..._",
    },
    "processing": {
        "fr": "_Traitement..._",
        "en": "_Processing..._",
    },
    "error_prefix": {
        "fr": "Erreur :",
        "en": "Error:",
    },
    "unknown_error": {
        "fr": "inconnue",
        "en": "unknown",
    },
    "remote_fallback": {
        "fr": "Provider distant inaccessible - basculement automatique sur Ollama local. Detail : {message}",
        "en": "Remote provider inaccessible - automatic fallback to local Ollama. Detail: {message}",
    },
    "invalid_model_name": {
        "fr": "Nom de modele invalide. Rechargez la liste des modeles puis selectionnez-en un valide.",
        "en": "Invalid model name. Reload the model list and select a valid one.",
    },
    "analysis_unknown_type": {
        "fr": "Type d'analyse inconnu.",
        "en": "Unknown analysis type.",
    },
    "analysis_prompt_empty": {
        "fr": "Le prompt ne peut pas être vide.",
        "en": "The prompt cannot be empty.",
    },
    "analysis_prompt_missing_placeholder": {
        "fr": "Le prompt doit contenir le placeholder {text}.",
        "en": "The prompt must contain the {text} placeholder.",
    },
    "no_readable_content": {
        "fr": "Aucun contenu lisible trouve dans les documents.",
        "en": "No readable content found in the documents.",
    },
    "preparing_index": {
        "fr": "Preparation de l'index...",
        "en": "Preparing index...",
    },
    "initial_indexing": {
        "fr": "Indexation initiale (reprenable) | documents: {pdf_indexed} indexes, {pdf_resumed} repris | total: {total} items | {page_count} unites | {total_chunks} chunks total | workers: {workers} | taille lot: {batch_size}",
        "en": "Initial indexing (resumable) | documents: {pdf_indexed} indexed, {pdf_resumed} resumed | total: {total} items | {page_count} units | {total_chunks} total chunks | workers: {workers} | batch size: {batch_size}",
    },
    "embedding_model_mismatch": {
        "fr": "La base vectorielle existante a ete construite avec un autre modele d'embedding ({existing} au lieu de {expected}). Utilisez `Recharger PDF` pour reconstruire l'index.",
        "en": "The existing vector database was built with a different embedding model ({existing} instead of {expected}). Use `Reload PDFs` to rebuild the index.",
    },
    "cannot_open_vector_db": {
        "fr": "Impossible d'ouvrir la base vectorielle existante. Utilisez `Recharger PDF` pour la reconstruire. Detail: {error}",
        "en": "Cannot open existing vector database. Use `Reload PDFs` to rebuild it. Detail: {error}",
    },
    "vector_db_loaded": {
        "fr": "Base vectorielle chargee depuis {path}",
        "en": "Vector database loaded from {path}",
    },
    "loading_model": {
        "fr": "Chargement du modele {model}...",
        "en": "Loading model {model}...",
    },
    "llm_config_error": {
        "fr": "Erreur configuration LLM: {error}",
        "en": "LLM configuration error: {error}",
    },
    "ready_short": {
        "fr": "Pret !",
        "en": "Ready!",
    },
    "ready_full": {
        "fr": "Pret | modele : {model} | seuil : {threshold} | provider: {provider} | {source}",
        "en": "Ready | model: {model} | threshold: {threshold} | provider: {provider} | {source}",
    },
    "ready_without_chat_model": {
        "fr": (
            "Documents prets | aucun modele de conversation : installez-en un pour poser "
            "des questions | provider: {provider} | {source}"
        ),
        "en": (
            "Documents ready | no chat model: install one to ask questions | "
            "provider: {provider} | {source}"
        ),
    },
    "loading_paused": {
        "fr": "Chargement mis en pause.",
        "en": "Loading paused.",
    },
    "loading_existing_db": {
        "fr": "Chargement de la base vectorielle existante...",
        "en": "Loading existing vector database...",
    },
}


def _t(key: str, **kwargs: object) -> str:
    """Return a localized string from the _L10N table.

    Falls back to the ``fr`` value if the key or the current language is
    missing.  Any ``{placeholder}`` tokens are filled with ``**kwargs``.
    """
    lang = _current_ui_language()
    entry = _L10N.get(key, {})
    template = entry.get(lang, entry.get("fr", f"<{key}>"))
    if kwargs:
        return template.format(**kwargs)
    return template


def _current_ui_language() -> str:
    """Language of the answer: the question's during a chat request, else the UI's."""
    from src.response_language import current_response_language

    if requested := current_response_language():
        return requested
    try:
        language = str(get_app_settings().get("ui_language") or "").strip().lower()
    except Exception:
        _logger.debug("Recovered from an error in _current_ui_language.", exc_info=True)
        language = ""
    return language if language in {"fr", "en"} else "fr"


def _response_language_instruction() -> str:
    from src.prompts import get_response_language_instruction

    return get_response_language_instruction(_current_ui_language())


def _language_bound_system_prompt(context_text: str) -> str:
    return f"{SYSTEM_PROMPT.format(context=context_text)}\n\n{_response_language_instruction()}"


def _final_response_language_constraint() -> str:
    if _current_ui_language() == "en":
        return (
            "FINAL PRIORITY CONSTRAINT: write the entire visible answer in English, "
            "including headings and transitions. Do not switch languages because the "
            "sources use another language."
        )
    return (
        "CONTRAINTE FINALE PRIORITAIRE : redige toute la reponse visible en francais, "
        "y compris les titres et les transitions. Ne change pas de langue parce que les "
        "sources sont redigees dans une autre langue."
    )


def _localized_no_results_message(*, source_scoped: bool = False) -> str:
    if _current_ui_language() == "en":
        if source_scoped:
            return (
                "I cannot search this paper because no indexed text is available for it. "
                "Reload the PDF index, or replace the local PDF if indexing still fails."
            )
        return (
            "I cannot find a matching passage with the current filters. "
            "Try removing a filter or rephrasing the question."
        )
    if source_scoped:
        return (
            "Je ne peux pas rechercher dans cet article car aucun texte indexé n'est disponible. "
            "Rechargez l'index PDF ou remplacez le PDF local si l'indexation échoue encore."
        )
    return (
        "Je ne trouve pas de passage correspondant dans les filtres actuels. "
        "Essayez d'enlever un filtre ou de reformuler la question."
    )


def _build_response_style_guide(message: str, model_name: str | None = None) -> str:
    normalized = _normalize_text(message)
    deep_analysis = is_deep_article_analysis_request(message)
    wants_list = any(
        marker in normalized
        for marker in (
            "liste",
            "list ",
            "bullet",
            "puces",
            "tableau",
            "table",
            "compare",
            "comparaison",
            "etapes",
            "steps",
            # Grouping or ranking the papers of a previous answer.
            "classe",
            "classer",
            "classify",
            "regroupe",
            "group",
            "categor",
            "theme",
            "thème",
            "sujets",
            "topics",
            "trie",
            "sort ",
        )
    )
    broad_question = any(
        marker in normalized
        for marker in (
            "qu'est-ce que tu sais",
            "que sais-tu",
            "explique",
            "resume",
            "résume",
            "en bref",
            "synthese",
            "synthèse",
            "c'est quoi",
        )
    )

    if _current_ui_language() == "en":
        lines = [
            "Response style rules:",
            _response_language_instruction(),
            "Answer the question directly in natural prose.",
            "Do not invent anything: if the sources are insufficient, say so clearly.",
            "Every claim based on retrieved sources must carry a valid inline citation [Sx].",
            "Do not produce a simple list of sources or peripheral facts.",
            "Only cover points that help answer the current question.",
            "Do not use mandatory sections such as Answer, Explanation or Response note.",
        ]
    else:
        lines = [
            "Style de reponse a respecter:",
            _response_language_instruction(),
            "Reponds directement a la question en texte naturel.",
            "N'invente rien: si les sources ne permettent pas de conclure, dis-le clairement.",
            "Chaque affirmation issue des sources doit porter une citation inline valide [Sx].",
            "Ne fais pas une simple liste de sources ou de faits periferiques.",
            "Ne couvre que les points utiles pour repondre a la question actuelle.",
            "N'utilise pas de sections imposees comme Answer, Explanation ou Response note.",
        ]
    if deep_analysis:
        lines.append(
            deep_analysis_guidance(
                language=_current_ui_language(),
                citation_format="[Sx]",
            )
        )
    elif not wants_list:
        if _current_ui_language() == "en":
            lines.append("By default, prefer 1 or 2 short paragraphs over bullet points.")
        else:
            lines.append("Par defaut, prefere 1 ou 2 courts paragraphes a une liste a puces.")
    if broad_question:
        if _current_ui_language() == "en":
            lines.append(
                "If the question is broad, synthesize the 2 to 4 most important ideas before details."
            )
        else:
            lines.append(
                "Si la question est large, fais une synthese des 2 a 4 idees les plus importantes avant les details."
            )
    if is_library_overview_request(message):
        if _current_ui_language() == "en":
            lines.extend(
                [
                    "Library-overview task: infer 2 to 4 recurring research themes across the retrieved excerpts.",
                    "Name each theme and briefly connect the representative sources that support it.",
                    "The source excerpts below are library context; do not claim that no library context was supplied.",
                    "Describe this as an overview of the indexed evidence, not a guaranteed exhaustive census of the library.",
                ]
            )
        else:
            lines.extend(
                [
                    "Tache de vue d'ensemble de la bibliotheque: deduis 2 a 4 themes de recherche recurrents dans les extraits recuperes.",
                    "Nomme chaque theme et relie brievement les sources representatives qui le soutiennent.",
                    "Les extraits ci-dessous constituent bien le contexte de la bibliotheque; ne dis pas qu'aucun contexte n'a ete fourni.",
                    "Presente le resultat comme une vue d'ensemble des elements indexes, pas comme un recensement necessairement exhaustif.",
                ]
            )
    model_note = sanitize_utf8_text(get_chat_response_note(model_name)).strip()
    if model_note:
        lines.extend(["", "Consignes specifiques au modele:", model_note])
    return "\n".join(lines)


def _source_ref_label(doc: Document, index: int) -> str:
    metadata = doc.metadata or {}
    explicit_ref_label = str(metadata.get("ref_label", "")).strip()
    if explicit_ref_label:
        return explicit_ref_label
    article_id = str(metadata.get("article_id", "")).strip()
    chunk_id = metadata.get("chunk_id")
    if metadata.get("wiki_provenance") and article_id and chunk_id not in ("", None):
        return f"{article_id}:{chunk_id}".upper()
    return f"S{index}"


def _extract_answer_citation_labels(answer_text: str) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    value = str(answer_text or "")
    for match in re.finditer(r"\[([^\]]+)\]", value):
        raw = str(match.group(1) or "")
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        for part in parts:
            normalized = ""
            source_match = re.fullmatch(r"S(\d+)", part, flags=re.IGNORECASE)
            if source_match:
                normalized = f"S{int(source_match.group(1))}"
            elif re.fullmatch(r"[A-Za-z0-9:_\-.]+:\d+", part, flags=re.IGNORECASE):
                normalized = part.upper()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            labels.append(normalized)
    return labels


def _filter_citation_docs_by_answer(answer_text: str, docs: list[Document]) -> list[Document]:
    unique_docs = _dedupe_source_docs(docs)
    if not unique_docs:
        return []

    cited_labels = _extract_answer_citation_labels(answer_text)
    if not cited_labels:
        return []

    by_label = {
        _source_ref_label(doc, index): doc for index, doc in enumerate(unique_docs, start=1)
    }
    filtered = [by_label[label] for label in cited_labels if label in by_label]
    return _dedupe_source_docs(filtered)


def _format_context_for_direct_chat(docs: list[Document]) -> str:
    parts: list[str] = []
    for index, doc in enumerate(docs or [], start=1):
        text = str(getattr(doc, "page_content", "") or "").strip()
        if not text:
            continue
        metadata = doc.metadata or {}
        title = str(
            metadata.get("zotero_title") or _safe_relpath(str(metadata.get("source", "source")))
        ).strip()
        page = str(metadata.get("page", "?")).strip() or "?"
        ref_label = _source_ref_label(doc, index)
        parts.append(f"[{ref_label}] {title} | page {page}\n{text}")
    return "\n\n".join(parts)


def grounding_passages(docs: list[Document]) -> list[str]:
    """The text of each passage under the title, authors and year of its paper.

    The page is shown to the model too, but it stays out: a figure equal to a
    page number is not a value the passages report.
    """
    passages: list[str] = []
    for doc in docs or []:
        metadata = doc.metadata or {}
        paper = " ".join(
            str(part or "").strip()
            for part in (
                metadata.get("zotero_title")
                or _safe_relpath(str(metadata.get("source", "source"))),
                metadata.get("zotero_authors") or metadata.get("zotero_first_author"),
                metadata.get("zotero_year"),
            )
        )
        passages.append(f"{paper}\n{getattr(doc, 'page_content', '') or ''}")
    return passages


# Instructions, history and the answer itself: what a previous answer's
# sources may not take from the model's window.
_PREVIOUS_ANSWER_RESERVED_TOKENS = 4000
_PREVIOUS_ANSWER_REMOTE_TOKENS = 24000
_CHARS_PER_TOKEN = 3


def _previous_answer_char_budget(model_name: str) -> int:
    window = 0
    if is_ollama_provider():
        window = resolve_ollama_context_length(model_name) or 8192
    tokens = (window or _PREVIOUS_ANSWER_REMOTE_TOKENS) - _PREVIOUS_ANSWER_RESERVED_TOKENS
    return max(2000, tokens) * _CHARS_PER_TOKEN


def _previous_answer_context(
    message: str,
    conversation_history: Any,
    model_name: str,
) -> tuple[list[Document], str, int] | None:
    """The sources of the previous answer, when the question is about them.

    Returns ``(documents, prompt note, sources left out)``. "Classe ces
    articles par thème" after a list of 40 papers used to search again and
    answer from six passages; the history passed the model 8 titles. Every
    source keeps its label, with its title, authors, year and the passage
    shown, or without the passage when they would not all fit the window.
    """
    if not refers_to_previous_answer(message, conversation_history):
        return None
    previous = next(
        row
        for row in reversed(conversation_history)
        if isinstance(row, dict) and str(row.get("role") or "") == "assistant"
    )
    sources = [source for source in previous["meta"]["sources"] if isinstance(source, dict)]

    def document(index: int, source: dict[str, Any], *, with_passage: bool) -> Document:
        label = str(source.get("ref_label") or f"S{index}").strip().upper()
        authors = str(source.get("authors") or source.get("author") or "").strip()
        year = str(source.get("year") or "").strip()
        described = ", ".join(part for part in (authors, year) if part) or "-"
        passage = str(source.get("snippet") or "").strip() if with_passage else ""
        return Document(
            page_content=f"{described}\n{passage}".strip(),
            metadata={
                "zotero_title": str(source.get("title") or source.get("file") or "").strip(),
                "zotero_authors": authors,
                "zotero_first_author": str(source.get("author") or "").strip(),
                "zotero_year": year,
                "zotero_doi": str(source.get("doi") or "").strip(),
                "page": source.get("page") or "?",
                "source": str(source.get("file") or "").strip(),
                "article_id": str(source.get("article_id") or "").strip(),
                "content_type": source.get("content_type") or "pdf_page",
                "chunk_uid": f"previous-answer:{label}",
                "ref_label": label,
            },
        )

    budget = _previous_answer_char_budget(model_name)
    docs: list[Document] = []
    for with_passage in (True, False):
        docs = [
            document(index, source, with_passage=with_passage)
            for index, source in enumerate(sources, start=1)
        ]
        if len(_format_context_for_direct_chat(docs)) <= budget:
            break
    kept: list[Document] = []
    used = 0
    for doc in docs:
        size = len(_format_context_for_direct_chat([doc])) + 2
        if used + size > budget:
            break
        kept.append(doc)
        used += size
    omitted = len(docs) - len(kept)
    if _current_ui_language() == "en":
        note = (
            f"The sources below are the {len(sources)} sources of your previous answer "
            "(title, authors, year"
            + (", passage" if kept and "\n" in kept[0].page_content else "")
            + "). Answer from them and cite each with its label. To group, rank or "
            "classify them, infer from titles and passages and say when a category is "
            "inferred from the title only; do not refuse because the method is not stated."
        )
        if omitted:
            note += f" Only the first {len(kept)} fit here; say so if it matters."
    else:
        note = (
            f"Les sources ci-dessous sont les {len(sources)} sources de ta reponse precedente "
            "(titre, auteurs, annee"
            + (", extrait" if kept and "\n" in kept[0].page_content else "")
            + "). Reponds a partir d'elles et cite chacune avec son label. Pour les "
            "regrouper, les classer ou les comparer, deduis-le de leurs titres et extraits "
            "et signale quand une categorie ne repose que sur le titre ; ne refuse pas "
            "parce que la methode n'est pas ecrite."
        )
        if omitted:
            note += f" Seules les {len(kept)} premieres tiennent ici ; dis-le si cela compte."
    return kept, note, omitted


def build_chat_messages(
    question: str,
    retrieved_docs: list[Document],
    citation_docs: list[Document],
    *,
    history_context: str = "",
    filter_summary: str = "",
    model_name: str | None = None,
    context_note: str = "",
) -> list[dict[str, str]]:
    """The system and user messages of a sourced chat answer.

    One place builds them, so the benchmarks measure the prompt the chat sends.
    ``context_note`` says where the sources come from when they are not a
    retrieval (the previous answer's sources).
    """
    prompt_input = question
    if history_context:
        prompt_input = (
            f"{history_context}\n\n"
            f"Question actuelle de l'utilisateur: {question}\n\n"
            "Repondez a la question actuelle en utilisant le contexte de la conversation "
            "uniquement si cela reste coherent avec les sources."
        )
    if context_note:
        prompt_input += f"\n\n{context_note}"
    citation_guide = _build_source_citation_guide(citation_docs)
    if citation_guide:
        prompt_input += f"\n\n{citation_guide}"
    if filter_summary:
        prompt_input += (
            "\n\nFiltres Zotero a respecter strictement: "
            f"{filter_summary}. Si un element est hors filtre, ignore-le."
        )
    response_style_guide = _build_response_style_guide(question, model_name=model_name)
    if response_style_guide:
        prompt_input += f"\n\n{response_style_guide}"
    prompt_input += f"\n\n{_final_response_language_constraint()}"
    system_prompt = _language_bound_system_prompt(_format_context_for_direct_chat(retrieved_docs))
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt_input},
    ]


def _visual_analysis_config() -> tuple[bool, bool, str, str]:
    try:
        settings = get_app_settings()
    except Exception:
        _logger.debug("Recovered from an error in _visual_analysis_config.", exc_info=True)
        return False, False, "", ""
    mode = str(settings.get("visual_analysis_mode") or "off").strip().lower()
    model = str(settings.get("vision_model") or "").strip()
    analysis_model = str(settings.get("visual_description_model") or "").strip()
    available = mode != "off" and bool(model or analysis_model)
    automatic = available and bool(settings.get("visual_analysis_auto_chat"))
    return available, automatic, model, analysis_model


def _visual_candidate_rel_path(metadata: dict[str, Any]) -> str:
    rel_path = str(metadata.get("rel_path") or "").strip().replace("\\", "/")
    if rel_path:
        return rel_path
    source = str(metadata.get("source") or "").strip()
    if not source:
        return ""
    try:
        return str(Path(source).resolve().relative_to(Path(config.DATA_PATH).resolve())).replace(
            "\\", "/"
        )
    except (OSError, ValueError):
        return ""


def _build_auto_visual_candidates(
    docs: list[Document],
    *,
    include_visual: bool = False,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    for doc in docs or []:
        metadata = dict(doc.metadata or {})
        if str(metadata.get("content_type") or "") == VISUAL_CONTENT_TYPE and not include_visual:
            continue
        rel_path = _visual_candidate_rel_path(metadata)
        if not rel_path:
            continue
        candidate = by_path.get(rel_path)
        if candidate is None:
            if len(candidates) >= 3:
                continue
            candidate = {
                "rel_path": rel_path,
                "title": str(metadata.get("zotero_title") or Path(rel_path).name).strip(),
                "metadata": metadata,
                "snippets": [],
            }
            by_path[rel_path] = candidate
            candidates.append(candidate)
        snippet = re.sub(r"\s+", " ", str(doc.page_content or "")).strip()
        if snippet and len(candidate["snippets"]) < 2:
            candidate["snippets"].append(snippet[:700])
    return candidates


def _decide_auto_visual_analysis(
    question: str,
    model_name: str,
    candidates: list[dict[str, Any]],
) -> AutoVisualAnalysisDecision:
    if not candidates:
        return AutoVisualAnalysisDecision(analyze=False, source_index=0, reason="")
    source_lines: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        excerpts = "\n".join(str(value) for value in candidate.get("snippets", []))
        source_lines.append(
            f"SOURCE {index} — {candidate.get('title') or candidate.get('rel_path')}\n{excerpts}"
        )
    prompt = (
        "Decide whether visual inspection is materially necessary for the exact user question. "
        "Set analyze=true only when axes, plotted trends, figure geometry, table cells, legends, "
        "or values visible in an image could change or substantiate the answer. Set it to false for "
        "bibliographic, conceptual, summary, or text-sufficient questions. Select exactly one source "
        "when analysis is useful. Do not request a visual inspection merely because an excerpt mentions "
        "a figure.\n\n"
        f"USER QUESTION:\n{question}\n\nRETRIEVED SOURCES:\n" + "\n\n".join(source_lines)
    )
    decision = invoke_structured_chat(
        model_name,
        [
            {
                "role": "system",
                "content": (
                    "You are a conservative routing component for scientific PDF visual analysis. "
                    "Return only the structured decision requested."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        response_model=AutoVisualAnalysisDecision,
        temperature=0.0,
        top_p=1.0,
    )
    if not isinstance(decision, AutoVisualAnalysisDecision):
        raise TypeError("invalid_visual_analysis_decision")
    if not decision.analyze or not 1 <= decision.source_index <= len(candidates):
        return AutoVisualAnalysisDecision(analyze=False, source_index=0, reason=decision.reason)
    return decision


def _analyze_auto_visual_candidate(
    candidate: dict[str, Any],
    *,
    vision_model: str,
    visual_description_model: str = "",
    question: str = "",
    forced: bool = False,
) -> list[Document]:
    rel_path = str(candidate.get("rel_path") or "").strip()
    full_path = resolve_pdf_path(config.DATA_PATH, rel_path)
    if full_path is None:
        return []
    analysis_kwargs: dict[str, Any] = {
        "model": vision_model,
        "max_pages": 3,
        "question": question,
    }
    if visual_description_model:
        analysis_kwargs["analysis_model"] = visual_description_model
    payload = analyze_pdf_visuals(str(full_path), **analysis_kwargs)
    base_metadata = dict(candidate.get("metadata") or {})
    visual_docs: list[Document] = []
    for result in payload.get("results", []):
        if not isinstance(result, dict):
            continue
        text = str(result.get("text") or "").strip()
        if not text:
            continue
        page = result.get("page", "?")
        article_id = str(base_metadata.get("article_id") or Path(rel_path).stem).strip()
        uid_seed = f"{rel_path}|{page}|{vision_model}|{visual_description_model}|{text}".encode()
        metadata = dict(base_metadata)
        metadata.update(
            {
                "source": str(full_path),
                "rel_path": rel_path,
                "article_id": article_id,
                "page": page,
                "content_type": VISUAL_CONTENT_TYPE,
                "vision_model": vision_model or visual_description_model,
                "ocr_model": vision_model,
                "visual_analysis_model": visual_description_model,
                "visual_source": str(result.get("visual_source") or "page_render_fallback"),
                "figure_label": str(result.get("figure_label") or ""),
                "figure_caption": str(result.get("caption") or ""),
                "auto_visual_analysis": not forced,
                "forced_visual_analysis": bool(forced),
                "chunk_uid": f"visual:{hashlib.sha1(uid_seed).hexdigest()}",
            }
        )
        visual_docs.append(_sanitize_document(Document(page_content=text, metadata=metadata)))
    return visual_docs


def _stream_auto_visual_enrichment(
    question: str,
    model_name: str,
    docs: list[Document],
    *,
    force_visual_analysis: bool = False,
):
    available, automatic, vision_model, visual_description_model = _visual_analysis_config()
    if not docs:
        return docs
    if force_visual_analysis and not available:
        yield {"type": "status", "msg": _t("visual_unavailable")}
        return docs
    if not force_visual_analysis and not automatic:
        return docs
    candidates = _build_auto_visual_candidates(
        docs,
        include_visual=force_visual_analysis,
    )
    if not candidates:
        if force_visual_analysis:
            yield {"type": "status", "msg": _t("visual_no_result")}
        return docs
    if force_visual_analysis:
        # Candidates preserve retrieval order. With an article scope the list is
        # already restricted to that PDF; otherwise the first item is the
        # highest-ranked retrievable source.
        candidate = candidates[0]
    else:
        yield {"type": "status", "msg": _t("visual_judging")}
        try:
            decision = _decide_auto_visual_analysis(question, model_name, candidates)
        except Exception:
            _logger.debug(
                "Recovered from an error in _stream_auto_visual_enrichment.", exc_info=True
            )
            yield {"type": "status", "msg": _t("visual_unavailable")}
            return docs
        if not decision.analyze:
            yield {"type": "status", "msg": _t("visual_not_needed")}
            return docs
        candidate = candidates[decision.source_index - 1]
    title = str(candidate.get("title") or candidate.get("rel_path") or "PDF")
    status_key = "visual_forced_reading" if force_visual_analysis else "visual_reading"
    yield {"type": "status", "msg": _t(status_key, title=title)}
    try:
        visual_models = {vision_model, visual_description_model} - {""}
        if not is_remote_provider() and model_name not in visual_models:
            unload_ollama_model(model_name, timeout_seconds=2)
        visual_docs = _analyze_auto_visual_candidate(
            candidate,
            vision_model=vision_model,
            visual_description_model=visual_description_model,
            question=question,
            forced=force_visual_analysis,
        )
    except Exception:
        _logger.debug("Recovered from an error in _stream_auto_visual_enrichment.", exc_info=True)
        yield {"type": "status", "msg": _t("visual_unavailable")}
        return docs
    if not visual_docs:
        yield {"type": "status", "msg": _t("visual_no_result")}
        return docs
    yield {"type": "status", "msg": _t("visual_added", count=len(visual_docs))}
    return [*docs, *visual_docs]


def _guard_issue_summary(report: ChatGuardReport) -> str:
    parts: list[str] = []
    for issue in report.issues:
        detail = ""
        if issue.detail:
            detail = f" | detail={issue.detail}"
        parts.append(f"- {issue.code}: {issue.message}{detail}")
    return "\n".join(parts) or "- unknown_guard_issue"


def _guard_repair_fallback() -> str:
    if _current_ui_language() == "en":
        return (
            "I cannot produce a reliable answer from the retrieved excerpts without risking "
            "invented information or citations. Rephrase the question, broaden the filters, "
            "or open the sources to check the full context."
        )
    return (
        "Je ne peux pas produire une reponse fiable a partir des extraits recuperes sans "
        "risquer d'inventer une information ou une citation. Reformulez la question, "
        "elargissez les filtres, ou ouvrez les sources pour verifier le contexte complet."
    )


_LEGACY_CHAT_SECTION_RE = re.compile(
    r"(?im)^\s*\*{0,2}\s*(Answer|Explanation|Response note)\s*\*{0,2}\s*:\s*"
)


def _build_guard_repair_prompt(
    *,
    user_query: str,
    answer_text: str,
    source_docs: list[Document],
    guard_report: ChatGuardReport,
) -> str:
    context_text = _format_context_for_direct_chat(source_docs)
    citation_guide = _build_source_citation_guide(source_docs)
    return (
        "Tu corriges une reponse RAG avant affichage utilisateur.\n\n"
        "Objectif:\n"
        f"- {_response_language_instruction()}\n"
        "- Reparer uniquement les citations invalides et les artefacts visibles.\n"
        "- Ne jamais ajouter de fait absent des sources.\n"
        "- Utiliser uniquement les citations autorisees dans le contexte.\n"
        "- Si la reponse ne peut pas etre reparee sans inventer, produire une reponse "
        "prudente disant que les sources ne permettent pas de conclure.\n\n"
        f"Question utilisateur:\n{user_query}\n\n"
        f"Problemes detectes par le guard:\n{_guard_issue_summary(guard_report)}\n\n"
        f"{citation_guide}\n\n"
        f"Sources disponibles:\n{context_text}\n\n"
        f"Reponse a corriger:\n{answer_text}\n\n"
        "Retourne uniquement une reponse courte en texte naturel, sans sections imposees."
    )


def _repair_guarded_chat_output(
    *,
    model_name: str,
    user_query: str,
    answer_text: str,
    source_docs: list[Document],
    guard_report: ChatGuardReport,
) -> tuple[str, ChatGuardReport, bool, str]:
    """Try one structured repair pass after a guard error."""

    fallback = _guard_repair_fallback()
    try:
        repair_prompt = _build_guard_repair_prompt(
            user_query=user_query,
            answer_text=answer_text,
            source_docs=source_docs,
            guard_report=guard_report,
        )
        structured = invoke_structured_chat(
            model_name,
            [
                {
                    "role": "system",
                    "content": (
                        "Tu es un correcteur de sortie RAG. Tu conserves seulement les "
                        "faits soutenus par les sources et tu respectes strictement les "
                        "citations autorisees. "
                        f"{_response_language_instruction()}"
                    ),
                },
                {"role": "user", "content": repair_prompt},
            ],
            temperature=0.0,
            top_p=1.0,
        )
        if not isinstance(structured, ChatAnswer):
            raise TypeError(
                f"Expected ChatAnswer from repair pass, got {type(structured).__name__}"
            )
        candidate = normalize_chunk_citation_markup(structured.answer).strip()
        candidate = strip_invalid_citations(candidate, sources=source_docs)
        repaired_report = validate_chat_output(
            candidate,
            sources=source_docs,
            context_text=_format_context_for_direct_chat(source_docs),
            require_structure=False,
        )
        if repaired_report.severity != "error":
            return repaired_report.normalized_text, repaired_report, True, ""
        reason = f"repair_failed_guard:{','.join(issue.code for issue in repaired_report.issues)}"
    except Exception as exc:
        _logger.debug("Recovered from an error in _repair_guarded_chat_output.", exc_info=True)
        reason = f"repair_exception:{exc}"

    fallback_report = validate_chat_output(
        fallback,
        sources=source_docs,
        context_text=_format_context_for_direct_chat(source_docs),
        require_structure=False,
    )
    return fallback_report.normalized_text, fallback_report, False, reason


def init_rag(
    model_name: str,
    threshold: float,
    force_reload: bool,
    progress: state.ProgressFn | None = None,
    max_chunks: int = 6,
    chunk_size: int | None = None,
) -> tuple[bool, str]:
    if progress is None:
        progress = state._noop_progress

    resolved_chunk_size = get_persisted_chunk_size(_load_manifest_chunk_size() or 1000)
    if chunk_size is not None:
        try:
            resolved_chunk_size = max(1, int(chunk_size))
        except (TypeError, ValueError):
            resolved_chunk_size = get_persisted_chunk_size(_load_manifest_chunk_size() or 1000)
    chunk_size = resolved_chunk_size

    with state._state_lock:
        state._max_chunks_default = max_chunks
    state._reset_vector_load_pause()

    try:
        embedding_model = _current_embedding_model()
        embedding_profile = current_embedding_profile_label(embedding_model)
        embedding_provider_mode = get_embedding_provider_mode()
        if embedding_provider_mode == "lmstudio" or (
            embedding_provider_mode == "ollama" and requires_local_ollama_embeddings()
        ):
            ok, validated_model, validation_msg = validate_local_embedding_model(embedding_model)
            if not ok:
                return False, validation_msg
            embedding_model = validated_model or embedding_model
            embedding_profile = current_embedding_profile_label(embedding_model)
        embeddings = create_embeddings(embedding_model)
    except Exception as exc:
        _logger.debug("Recovered from an error in init_rag.", exc_info=True)
        return False, f"Erreur configuration embeddings: {exc}"
    # Same model under another name or service: reuse its index, do not rebuild.
    adopt_equivalent_vector_index(chunk_size=chunk_size, embedding_model=embedding_profile)
    current_profile = resolve_vector_index_profile(
        chunk_size=chunk_size,
        embedding_model=embedding_profile,
    )
    db_path = str(current_profile.get("path") or "")
    db_exists = bool(db_path) and os.path.exists(db_path) and bool(os.listdir(db_path))
    # An index keeps the task prompts its passages were embedded with (none
    # before they existed); only a build from scratch takes the model's own.
    raw_embeddings = embeddings
    build_prompts = default_embedding_prompts(embedding_model)
    index_prompts = (
        _load_manifest_embedding_prompts(db_path=db_path) if db_exists else build_prompts
    )
    embeddings = with_embedding_prompts(raw_embeddings, index_prompts)

    def checkpoint_manifest(entries: dict[str, dict[str, Any]]) -> None:
        # Saved after every indexed group, so that a pause, a crash or a closed
        # app resumes the build where it stopped instead of rebuilding from 0.
        _save_manifest_entries(
            entries,
            chunk_size,
            embedding_profile,
            db_path=db_path,
            embedding_prompts=index_prompts,
        )

    try:
        recovered_incomplete_msg = ""
        if not force_reload and db_exists and _has_incomplete_index_marker(db_path=db_path):
            recovered, recovered_msg = _try_recover_stale_index_marker(embeddings, db_path=db_path)
            if not recovered:
                return (
                    False,
                    "La base vectorielle est dans un etat incomplet ou en pause. "
                    f"Cliquez sur `Recharger PDF` pour reprendre l'indexation. Detail: {recovered_msg}",
                )
            recovered_incomplete_msg = recovered_msg

        pdf_list: list[tuple[str, str]] = []
        if force_reload or not db_exists:
            if force_reload:
                # A manual reload explicitly asks for the current filesystem,
                # including changes made during the short library scan cache.
                state.clear_document_scan_cache()
            _refresh_zotero_library_cache()
            pdf_list = _filter_active_zotero_pdf_entries(state.find_pdfs())
            if not pdf_list and not (force_reload and db_exists):
                return False, "Aucun document pris en charge a indexer dans ./data."

        if force_reload and db_exists:
            previous_entries = _load_manifest_entries(db_path=db_path)
            previous_chunk_size = _load_manifest_chunk_size(db_path=db_path)
            previous_embedding_model = _load_manifest_embedding_model(db_path=db_path)
            current_pdf_entries = _compute_index_entries(pdf_list)
            current_entries = dict(current_pdf_entries)

            needs_full_rebuild = (not previous_entries) or (
                previous_chunk_size is not None and previous_chunk_size != int(chunk_size)
            )
            embedding_model_changed = bool(
                previous_embedding_model
            ) and not manifest_model_matches(db_path, embedding_profile)
            if embedding_model_changed:
                needs_full_rebuild = True

            if needs_full_rebuild:
                _mark_index_incomplete(db_path=db_path)
                if not previous_entries:
                    progress(
                        0.02, desc="Migration vers l'indexation incrementale (une seule fois)..."
                    )
                elif embedding_model_changed:
                    progress(
                        0.02,
                        desc="Modele d'embedding modifie: reconstruction complete de l'index...",
                    )
                else:
                    progress(
                        0.02, desc="Chunk size modifiee: reconstruction complete de l'index..."
                    )
                with state._state_lock:
                    state.document_chain = None
                    state.vectorstore = None
                gc.collect()
                shutil.rmtree(db_path, ignore_errors=True)
                index_prompts = build_prompts
                embeddings = with_embedding_prompts(raw_embeddings, index_prompts)
                progress(0.04, desc="Preparation reconstruction complete...")
                with state._state_lock:
                    state.vectorstore = Chroma(
                        persist_directory=db_path,
                        embedding_function=embeddings,
                        collection_metadata={"hnsw:space": "cosine"},
                        client_settings=_chroma_client_settings(),
                    )
                (
                    _manifest_entries,
                    pdf_resumed,
                    indexed_count,
                    page_count,
                    note_chunk_count,
                    pdf_chunk_count,
                ) = _index_pdf_groups_with_manifest(
                    target_vectorstore=state.vectorstore,
                    pdf_list=pdf_list,
                    current_entries=current_entries,
                    manifest_entries={},
                    chunk_size=chunk_size,
                    progress=progress,
                    progress_start=0.05,
                    progress_end=0.85,
                    read_label="Lecture des documents (rebuild)",
                    index_label="Vectorisation",
                    index_path=db_path,
                    checkpoint=checkpoint_manifest,
                )
                _save_manifest_entries(
                    _manifest_entries,
                    chunk_size,
                    embedding_profile,
                    db_path=db_path,
                    embedding_prompts=index_prompts,
                )
                _clear_incomplete_index_marker(db_path=db_path)

                total_chunks = note_chunk_count + pdf_chunk_count
                if total_chunks == 0:
                    return False, _t("no_readable_content")

                source_msg = (
                    "Reconstruction complete (reprenable) | "
                    f"documents: {indexed_count} indexes, {pdf_resumed} repris | "
                    f"total: {len(current_entries)} items | "
                    f"{page_count} pages | "
                    f"{total_chunks} chunks total | "
                    f"workers: {state._get_pdf_worker_count()} | "
                    f"taille lot: {state._get_pdf_group_size()}"
                )
            else:
                progress(0.05, desc="Analyse des changements documents/metadonnees...")
                with state._state_lock:
                    state.vectorstore = Chroma(
                        persist_directory=db_path,
                        embedding_function=embeddings,
                        client_settings=_chroma_client_settings(),
                    )
                ensure_lexical_index(db_path, state.vectorstore)

                all_current_paths = set(current_entries.keys())
                removed_paths = sorted(set(previous_entries) - all_current_paths)

                changed_pdf_list: list[tuple[str, str]] = []
                for rel_path, entry in current_pdf_entries.items():
                    previous = previous_entries.get(rel_path)

                    if not _manifest_entry_matches(previous, entry):
                        changed_pdf_list.append((rel_path, entry["abs_path"]))

                working_entries = dict(previous_entries)
                did_mutate_index = bool(removed_paths or changed_pdf_list)
                for rel_path, entry in current_pdf_entries.items():
                    previous = previous_entries.get(rel_path)
                    if _manifest_entry_matches(previous, entry):
                        working_entries[rel_path] = _stable_manifest_entry(
                            entry,
                            chunk_count=_manifest_chunk_count(previous),
                            no_text=_manifest_no_text(previous),
                        )
                if did_mutate_index:
                    _mark_index_incomplete(db_path=db_path)
                if removed_paths:
                    progress(
                        0.10,
                        desc=f"Suppression selective des documents supprimes ({len(removed_paths)})...",
                    )
                    for rel_path in removed_paths:
                        state._raise_if_vector_load_paused()
                        try:
                            state.vectorstore.delete(where={"rel_path": rel_path})
                        except (AttributeError, RuntimeError, ValueError):
                            pass
                        if rel_path in working_entries:
                            del working_entries[rel_path]
                    delete_lexical_paths(db_path, removed_paths)

                if changed_pdf_list:
                    (
                        working_entries,
                        pdf_resumed,
                        pdf_indexed,
                        page_count,
                        note_chunk_count,
                        pdf_chunk_count,
                    ) = _index_pdf_groups_with_manifest(
                        target_vectorstore=state.vectorstore,
                        pdf_list=changed_pdf_list,
                        current_entries=current_pdf_entries,
                        manifest_entries=working_entries,
                        chunk_size=chunk_size,
                        progress=progress,
                        progress_start=0.12,
                        progress_end=0.80,
                        read_label="Relecture des documents modifies (incremental)",
                        index_label="Maj vectorielle",
                        index_path=db_path,
                        checkpoint=checkpoint_manifest,
                    )
                else:
                    pdf_resumed, pdf_indexed, page_count, note_chunk_count, pdf_chunk_count = (
                        0,
                        0,
                        0,
                        0,
                        0,
                    )

                _save_manifest_entries(
                    working_entries,
                    chunk_size,
                    embedding_profile,
                    db_path=db_path,
                    embedding_prompts=index_prompts,
                )
                # Also clears a marker left by a build interrupted after its
                # last checkpoint: nothing is left to index, so it is complete.
                _clear_incomplete_index_marker(db_path=db_path)

                source_msg = (
                    "Mise a jour incrementale (reprenable) | "
                    f"documents mod/add: {len(changed_pdf_list)} | "
                    f"supprimes: {len(removed_paths)} documents | "
                    f"total: {len(current_entries)} documents | "
                    f"reindexes: {pdf_indexed} documents | "
                    f"workers: {state._get_pdf_worker_count()} | "
                    f"taille lot: {state._get_pdf_group_size()}"
                )

        elif force_reload or not db_exists:
            current_pdf_entries = _compute_index_entries(pdf_list)
            current_entries = dict(current_pdf_entries)

            _mark_index_incomplete(db_path=db_path)
            if force_reload and os.path.exists(db_path):
                with state._state_lock:
                    state.document_chain = None
                    state.vectorstore = None
                gc.collect()
                shutil.rmtree(db_path, ignore_errors=True)
            index_prompts = build_prompts
            embeddings = with_embedding_prompts(raw_embeddings, index_prompts)

            progress(0.02, desc=_t("preparing_index"))
            with state._state_lock:
                state.vectorstore = Chroma(
                    persist_directory=db_path,
                    embedding_function=embeddings,
                    collection_metadata={"hnsw:space": "cosine"},
                    client_settings=_chroma_client_settings(),
                )

            (
                _manifest_entries,
                pdf_resumed,
                pdf_indexed,
                page_count,
                note_chunk_count,
                pdf_chunk_count,
            ) = _index_pdf_groups_with_manifest(
                target_vectorstore=state.vectorstore,
                pdf_list=pdf_list,
                current_entries=current_pdf_entries,
                manifest_entries={},
                chunk_size=chunk_size,
                progress=progress,
                progress_start=0.04,
                progress_end=0.85,
                read_label="Lecture des documents",
                index_label="Vectorisation",
                index_path=db_path,
                checkpoint=checkpoint_manifest,
            )
            _save_manifest_entries(
                _manifest_entries,
                chunk_size,
                embedding_profile,
                db_path=db_path,
                embedding_prompts=index_prompts,
            )
            _clear_incomplete_index_marker(db_path=db_path)

            total_chunks = note_chunk_count + pdf_chunk_count
            if total_chunks == 0:
                return False, _t("no_readable_content")

            source_msg = _t(
                "initial_indexing",
                pdf_indexed=pdf_indexed,
                pdf_resumed=pdf_resumed,
                total=len(current_entries),
                page_count=page_count,
                total_chunks=total_chunks,
                workers=state._get_pdf_worker_count(),
                batch_size=state._get_pdf_group_size(),
            )

        else:
            progress(0.5, desc=_t("loading_existing_db"))
            try:
                manifest_embedding_model = _load_manifest_embedding_model(db_path=db_path)
                if manifest_embedding_model and not manifest_model_matches(
                    db_path, embedding_profile
                ):
                    return (
                        False,
                        _t(
                            "embedding_model_mismatch",
                            existing=manifest_embedding_model,
                            expected=embedding_profile,
                        ),
                    )
                with state._state_lock:
                    state.vectorstore = Chroma(
                        persist_directory=db_path,
                        embedding_function=embeddings,
                        client_settings=_chroma_client_settings(),
                    )
                ensure_lexical_index(db_path, state.vectorstore)
            except Exception as exc:
                # A DLL blocked by Windows is not a damaged index: rebuilding
                # it would not help, so say what actually failed.
                _logger.debug("Recovered from an error in init_rag.", exc_info=True)
                native_msg = native_library_message(exc, _current_ui_language())
                return (
                    False,
                    native_msg or _t("cannot_open_vector_db", error=exc),
                )
            source_msg = _t("vector_db_loaded", path=db_path)
            if recovered_incomplete_msg:
                source_msg += f" | {recovered_incomplete_msg}"

        model_name = str(model_name or "").strip()
        if model_name:
            progress(0.95, desc=_t("loading_model", model=model_name))
            try:
                llm = create_chat_llm(model_name)
            except Exception as exc:
                _logger.debug("Recovered from an error in init_rag.", exc_info=True)
                return False, _t("llm_config_error", error=exc)
            prompt = ChatPromptTemplate.from_messages(
                [
                    ("system", f"{SYSTEM_PROMPT}\n\n{_response_language_instruction()}"),
                    ("human", "{input}"),
                ]
            )
            document_chain: Any = create_stuff_documents_chain(llm, prompt)
        else:
            # Reading the documents needs only the embedding model. Every
            # question builds its own chat model, so the index is ready
            # before any chat model is installed.
            document_chain = state.INDEX_ONLY_CHAIN
        with state._state_lock:
            state.document_chain = document_chain
        set_active_vector_index_profile(
            chunk_size=chunk_size,
            embedding_model=embedding_profile,
            db_path=db_path,
            exists=True,
        )

        progress(1.0, desc=_t("ready_short"))
        runtime = provider_runtime_label(embedding_model)
        msg = (
            _t(
                "ready_full",
                model=model_name,
                threshold=threshold,
                provider=runtime,
                source=source_msg,
            )
            if model_name
            else _t("ready_without_chat_model", provider=runtime, source=source_msg)
        )
        state.mark_runtime_ready(
            model=model_name,
            threshold=threshold,
            chunk_size=chunk_size,
            embedding_model=embedding_profile,
            db_path=db_path,
            provider=runtime,
            message=msg,
        )
        return (
            True,
            msg,
        )

    except state.VectorLoadPaused as exc:
        state.clear_runtime_state()
        gc.collect()
        progress(1.0, desc=_t("loading_paused"))
        return False, str(exc)


def try_init(model_name: str, threshold: float, progress: state.ProgressFn | None = None) -> str:
    _, msg = init_rag(model_name, threshold, force_reload=False, progress=progress)
    return msg


def reload_db(model_name: str, threshold: float, progress: state.ProgressFn | None = None) -> str:
    _, msg = init_rag(model_name, threshold, force_reload=True, progress=progress)
    return msg


def _has_query_content(message: str) -> bool:
    """Reject empty/emoji-only input, preserving short scientific expressions."""
    return any(
        char.isalnum() or unicodedata.category(char) == "Sm" or char in "%‰‱°℃℉" for char in message
    )


def _stream_retrieval(
    message: str,
    model_name: str,
    threshold: float,
    effective_max_chunks: int,
    *,
    tags: Any,
    collections: Any,
    source_paths: Any,
    conversation_history: Any,
    depth: str,
    force_visual_analysis: bool,
):
    """Retrieve the passages of a chat answer, with its status and metrics events.

    Returns ``(docs, cleaned message, filters, filter summary, pipeline
    metrics, reranker metrics)`` as the generator's value.
    """
    yield {"type": "status", "msg": _t("searching_vector_db")}

    # If the reranker is enabled but cold, emit progress events while it warms up
    # so the user knows where the latency is going.
    warmup_started = time.perf_counter()
    warmup_yielded = False
    warmup_model_name = _reranker_model_for_depth(depth)
    for event in _stream_reranker_warmup_status(warmup_model_name):
        warmup_yielded = True
        yield event
    if warmup_yielded:
        yield _build_pipeline_metrics_event("reranker_warmup", warmup_started)

    explicit_filters = _prepare_explicit_filters(
        tags=tags,
        collections=collections,
        source_paths=source_paths,
    )
    cleaned_message, inline_filters = _extract_inline_filters(message)
    filters = _merge_filter_maps(explicit_filters, inline_filters)

    summary = _filter_summary(filters)
    if summary:
        yield {"type": "status", "msg": _t("filters_active", summary=summary)}

    retrieved_docs = retrieve_for_question(
        cleaned_message,
        conversation_history,
        threshold,
        effective_max_chunks,
        filters,
        depth=depth,
    )
    _pending_pipeline_metrics = _consume_pipeline_metrics()
    # Capture rerank metrics immediately — before any yield — because asyncio.to_thread(next, gen)
    # may dispatch each generator resumption to a different thread-pool thread, making
    # threading.local() unreliable across yield boundaries.
    _pending_rerank_metrics = _build_reranker_metrics_event()
    retrieved_docs = yield from _stream_auto_visual_enrichment(
        cleaned_message,
        model_name,
        retrieved_docs,
        force_visual_analysis=force_visual_analysis,
    )
    return (
        retrieved_docs,
        cleaned_message,
        filters,
        summary,
        _pending_pipeline_metrics,
        _pending_rerank_metrics,
    )


def stream_chat_api(
    message: str,
    model_name: str,
    threshold: float,
    tags: Any = None,
    collections: Any = None,
    source_paths: Any = None,
    conversation_history: Any = None,
    thinking_enabled: bool = False,
    abort_signal: StreamAbortSignal | None = None,
    depth: str = "deep",
    force_visual_analysis: bool = False,
):
    """Generator API: yields JSON serialisable chunks.

    ``depth`` selects the retrieval tier: ``"quick"`` (lighter reranker model,
    tight candidate pool, bounded lexical scan) or ``"deep"`` (full pool and the
    heavier reranker model).
    """

    if not _has_query_content(message):
        with state._state_lock:
            state.last_sources = []
        clarification = (
            "Please provide a question or a topic to search for."
            if _current_ui_language() == "en"
            else "Précisez votre question ou le sujet à rechercher."
        )
        yield {"type": "token", "text": clarification}
        yield {"type": "sources", "data": [], "answer_origin": "clarification"}
        return

    init_started = time.perf_counter()
    init_ran = False
    with _RAG_INIT_LOCK:
        with state._state_lock:
            needs_init = state.document_chain is None or state.vectorstore is None
        if needs_init:
            init_ran = True
            ok, msg = init_rag(model_name, threshold, force_reload=False)
            if not ok:
                yield {"type": "error", "msg": msg}
                return
    if init_ran:
        yield _build_pipeline_metrics_event("rag_init", init_started)

    with state._state_lock:
        state.last_sources = []
        effective_max_chunks = min(state._max_chunks_default, state._CHAT_MAX_CHUNKS_CAP)
    if is_deep_article_analysis_request(message) or is_library_overview_request(message):
        effective_max_chunks = max(
            effective_max_chunks,
            min(10, state._CHAT_MAX_CHUNKS_CAP),
        )
    selected_articles = len(_dedupe_keep_order(_csv_to_list(source_paths)))
    if selected_articles > 1:
        effective_max_chunks = max(
            effective_max_chunks,
            min(selected_articles, _MULTI_ARTICLE_MAX_CHUNKS),
        )

    historical_reference = _resolve_historical_bibliographic_reference(
        message,
        conversation_history,
    )
    if historical_reference is not None:
        ref_label, historical_source = historical_reference
        answer = _historical_bibliographic_answer(ref_label, historical_source)
        yield {"type": "token", "text": answer}
        yield {
            "type": "sources",
            "data": [historical_source],
            "answer_origin": "conversation_history",
        }
        return

    if _needs_reference_clarification(message, conversation_history):
        yield {"type": "token", "text": _reference_clarification_message()}
        yield {"type": "sources", "data": [], "answer_origin": "clarification"}
        return

    # The scope forces plain RAG over one paper: the model got six passages of
    # it and answered that there was nothing to compare.
    if selected_articles == 1 and is_multi_paper_comparison_request(message):
        yield {"type": "token", "text": _single_article_comparison_message()}
        yield {"type": "sources", "data": [], "answer_origin": "clarification"}
        return

    context_note = ""
    previous_answer = _previous_answer_context(message, conversation_history, model_name)
    if previous_answer is not None:
        retrieved_docs, context_note, omitted = previous_answer
        cleaned_message, filters, summary = message, {}, ""
        yield {
            "type": "status",
            "msg": _t("previous_answer_sources", count=len(retrieved_docs)),
        }
        _pending_pipeline_metrics = [
            {
                "type": "pipeline_metrics",
                "stage": "previous_answer",
                "data": {"sources": len(retrieved_docs), "omitted": omitted},
            }
        ]
        _pending_rerank_metrics = None
    else:
        (
            retrieved_docs,
            cleaned_message,
            filters,
            summary,
            _pending_pipeline_metrics,
            _pending_rerank_metrics,
        ) = yield from _stream_retrieval(
            message,
            model_name,
            threshold,
            effective_max_chunks,
            tags=tags,
            collections=collections,
            source_paths=source_paths,
            conversation_history=conversation_history,
            depth=depth,
            force_visual_analysis=force_visual_analysis,
        )
    history_context = _build_history_context(conversation_history)
    citation_docs = _pin_source_ref_labels(retrieved_docs)
    with state._state_lock:
        state.last_sources = citation_docs

    if not retrieved_docs:
        yield {
            "type": "token",
            "text": _localized_no_results_message(source_scoped=bool(filters.get("sources"))),
        }
        yield {"type": "sources", "data": [], "answer_origin": "rag"}
        for metric in _pending_pipeline_metrics:
            yield metric
        if _pending_rerank_metrics:
            yield _pending_rerank_metrics
        return

    yield {"type": "status", "msg": _t("sources_found_generating")}

    answer_text = ""
    chat_messages = build_chat_messages(
        cleaned_message,
        retrieved_docs,
        citation_docs,
        history_context=history_context,
        filter_summary=summary,
        model_name=model_name,
        context_note=context_note,
    )

    streamed_display_text = ""
    streamed_answer_text = ""
    thinking_display_text = ""
    generation_timings: dict[str, Any] = {}
    try:
        generation_started = time.perf_counter()
        use_direct_ollama_chat = is_ollama_provider()
        if use_direct_ollama_chat:
            opened_thinking = False
            think_mode = resolve_chat_think_mode(model_name, thinking_enabled)
            show_thinking = bool(thinking_enabled and think_mode not in (None, False, ""))
            raw_content = ""
            for chunk in stream_ollama_chat(
                model_name,
                chat_messages,
                think=think_mode,
                abort_signal=abort_signal,
                options_override=_chat_context_options(
                    model_name, *(entry["content"] for entry in chat_messages)
                ),
            ):
                if chunk.get("timings"):
                    generation_timings = dict(chunk["timings"])
                thinking_piece = str(chunk.get("thinking") or "")
                content_piece = str(chunk.get("content") or "")
                if show_thinking and thinking_piece:
                    if not opened_thinking:
                        piece = "<think>"
                        thinking_display_text += piece
                        streamed_display_text += piece
                        yield {"type": "token", "text": piece}
                        opened_thinking = True
                    thinking_display_text += thinking_piece
                    streamed_display_text += thinking_piece
                    yield {"type": "token", "text": thinking_piece}
                if content_piece:
                    if show_thinking and opened_thinking:
                        piece = "</think>\n\n"
                        thinking_display_text += piece
                        streamed_display_text += piece
                        yield {"type": "token", "text": piece}
                        opened_thinking = False
                    raw_content += content_piece
                    visible_content = sanitize_assistant_content(
                        raw_content,
                        thinking_enabled=False,
                    )
                    delta = visible_content[len(streamed_answer_text) :]
                    if delta:
                        streamed_answer_text += delta
                        streamed_display_text += delta
                        yield {"type": "token", "text": delta}
                if chunk.get("content_replace") is not None:
                    # The final text no longer extends what was streamed: the
                    # answer_replace below shows it once the answer is checked.
                    raw_content = str(chunk["content_replace"])
            if opened_thinking:
                piece = "</think>"
                thinking_display_text += piece
                streamed_display_text += piece
                yield {"type": "token", "text": piece}
            answer_text = sanitize_assistant_content(
                raw_content,
                thinking_enabled=False,
            )
        else:
            llm = create_chat_llm(model_name)
            raw_content = ""
            for content_piece in stream_text(
                llm,
                chat_messages,
                abort_signal=abort_signal,
            ):
                raw_content += str(content_piece or "")
                visible_content = sanitize_assistant_content(
                    raw_content,
                    thinking_enabled=False,
                )
                delta = visible_content[len(streamed_answer_text) :]
                if delta:
                    streamed_answer_text += delta
                    streamed_display_text += delta
                    yield {"type": "token", "text": delta}
            answer_text = normalize_chunk_citation_markup(
                sanitize_assistant_content(raw_content, thinking_enabled=False)
            ).strip()
        yield _build_pipeline_metrics_event(
            "llm_generation",
            generation_started,
            {"output_chars": len(answer_text or ""), **generation_timings},
        )
    except Exception as exc:
        _logger.debug("Recovered from an error in stream_chat_api.", exc_info=True)
        message = str(exc).strip() or "Erreur inconnue"
        # Stop already closed the stream, which ends the generation: unloading
        # would only make the next question reload the model.
        stopped = isinstance(exc, InterruptedError) or bool(
            abort_signal is not None and abort_signal.is_aborted()
        )
        if not is_remote_provider() and not stopped:
            unload_ollama_model(model_name, timeout_seconds=2)
        remote_fallback = False
        if is_remote_provider() and is_remote_connection_error(exc):
            activate_fallback(message)
            # Reset the chain so the next request rebuilds it with the local LLM.
            with state._state_lock:
                state.document_chain = None
            message = _t("remote_fallback", message=message)
            remote_fallback = True
        elif "invalid model name" in message.lower():
            message = _t("invalid_model_name")
        yield {"type": "error", "msg": message, "remote_fallback": remote_fallback}
        return

    guard_started = time.perf_counter()
    guard_report = validate_chat_output(
        answer_text,
        sources=citation_docs,
        context_text=_format_context_for_direct_chat(citation_docs),
        require_structure=False,
    )
    answer_text = strip_invalid_citations(guard_report.normalized_text, sources=citation_docs)
    if answer_text != guard_report.normalized_text:
        guard_report = validate_chat_output(
            answer_text,
            sources=citation_docs,
            context_text=_format_context_for_direct_chat(citation_docs),
            require_structure=False,
        )
    if guard_report.severity == "error":
        yield {"type": "status", "msg": _t("fragile_detected_repairing")}
        answer_text, guard_report, repaired_ok, repair_reason = _repair_guarded_chat_output(
            model_name=model_name,
            user_query=cleaned_message,
            answer_text=answer_text,
            source_docs=citation_docs,
            guard_report=guard_report,
        )
        if repaired_ok:
            yield {"type": "status", "msg": _t("response_repaired")}
        elif repair_reason:
            yield {
                "type": "status",
                "msg": _t("repair_impossible_fallback", reason=repair_reason),
            }
    grounding = check_answer_grounding(
        answer_text,
        grounding_passages(citation_docs),
        question=cleaned_message,
        extra_texts=[history_context],
    )
    answer_text = grounding.text
    if grounding.ungrounded_values:
        # Said, not removed: the figure may be a correct calculation, and
        # deleting a wrong one would leave a sentence that reads as sourced.
        note = _t("values_to_check", values=" ; ".join(grounding.ungrounded_values))
        answer_text = f"{answer_text.rstrip()}\n\n{note}"
    yield _build_pipeline_metrics_event(
        "answer_guard",
        guard_started,
        {
            "severity": str(getattr(guard_report, "severity", "") or ""),
            "values_to_check": len(grounding.ungrounded_values),
        },
    )
    cited_docs = _filter_citation_docs_by_answer(answer_text, citation_docs)
    if not cited_docs:
        cited_docs = citation_docs
    with state._state_lock:
        state.last_sources = cited_docs
    final_display_text = f"{thinking_display_text}{answer_text}"
    if streamed_display_text:
        if final_display_text != streamed_display_text:
            yield {"type": "answer_replace", "text": final_display_text}
    else:
        for piece in _split_text_for_streaming(final_display_text):
            yield {"type": "token", "text": piece}
    yield {"type": "sources", "data": format_sources_api(cited_docs), "answer_origin": "rag"}
    for metric in _pending_pipeline_metrics:
        yield metric
    if _pending_rerank_metrics:
        yield _pending_rerank_metrics
    anchor_event = _build_anchor_event(answer_text, cited_docs)
    if anchor_event:
        yield anchor_event


def chat(
    message: str,
    history: list,
    model_name: str,
    threshold: float,
    tags: Any = None,
    collections: Any = None,
    thinking_enabled: bool = False,
):
    """Gradio chat wrapper."""
    conversation_history = list(history or [])
    history = conversation_history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": _t("searching_vector_db")},
    ]
    yield history, _t("searching_in_progress")

    full_answer = ""
    for event in stream_chat_api(
        message=message,
        model_name=model_name,
        threshold=threshold,
        tags=tags,
        collections=collections,
        conversation_history=conversation_history,
        thinking_enabled=thinking_enabled,
    ):
        kind = event.get("type")
        if kind == "status":
            history[-1]["content"] = f"_{event.get('msg', _t('processing'))}_"
            yield history, _t("processing")
        elif kind == "token":
            if not full_answer:
                history[-1]["content"] = ""
            full_answer += event.get("text", "")
            history[-1]["content"] = full_answer
            yield history, format_sources(state.last_sources)
        elif kind == "answer_replace":
            full_answer = str(event.get("text") or "")
            history[-1]["content"] = full_answer
            yield history, format_sources(state.last_sources)
        elif kind == "sources":
            yield history, format_sources(state.last_sources)
        elif kind == "error":
            history[-1]["content"] = f"{_t('error_prefix')} {event.get('msg', _t('unknown_error'))}"
            yield history, ""
            return


def format_sources(docs: list) -> str:
    unique_docs = _dedupe_source_docs(docs)
    if not unique_docs:
        return _t("no_sources_found")

    lines: list[str] = []
    for index, doc in enumerate(unique_docs, start=1):
        metadata = doc.metadata or {}
        src = metadata.get("source", "inconnu")
        page = metadata.get("page", "?")
        rel = _safe_relpath(str(src))
        snippet = _source_snippet_for_display(doc.page_content, limit=220)
        ref_label = _source_ref_label(doc, index)

        content_type = metadata.get("content_type", "pdf_page")
        location_label = str(metadata.get("location_label") or "").strip()
        citation = metadata.get("zotero_citation", "")
        tags = metadata.get("zotero_tags", "")
        collections = metadata.get("zotero_collections", "")

        extras: list[str] = []
        if content_type == "zotero_note":
            extras.append("note Zotero")
        if citation:
            extras.append(str(citation))
        if tags:
            extras.append(f"tags: {tags}")
        if collections:
            extras.append(f"collections: {collections}")

        extra_line = f"\n\n<small>{' | '.join(extras)}</small>" if extras else ""
        location = location_label or f"page {page}"
        lines.append(
            f"**[{ref_label}] {rel}** - {location}{extra_line}\n\n<small>{snippet}...</small>"
        )

    return "\n\n---\n\n".join(lines)


_PDF_PRIVATE_GLYPH_RE = re.compile(r"[\ue000-\uf8ff]")
_READABLE_WORD_RE = r"(?:[A-ZÀ-ÖØ-Þ]?[a-zà-öø-ÿ]{2,}|[aAiIàÀ])"
_READABLE_TEXT_RUN_RE = re.compile(rf"\b{_READABLE_WORD_RE}" rf"(?:\s+{_READABLE_WORD_RE}){{5,}}")


def _formula_noise_score(value: str) -> float:
    text = str(value or "")
    visible = [char for char in text if not char.isspace()]
    if not visible:
        return 0.0
    private_glyphs = len(_PDF_PRIVATE_GLYPH_RE.findall(text))
    math_symbols = sum(1 for char in visible if char in "=+-−*/^_()[]{}<>∂∇×·⋅→←↔≈≠≤≥")
    letters = sum(1 for char in visible if char.isalpha())
    short_tokens = len(re.findall(r"\b[a-zA-Z]\b", text))
    return (private_glyphs * 2.5 + math_symbols + short_tokens * 0.5) / max(
        1, letters + len(visible)
    )


def _source_snippet_for_display(text: str, *, limit: int = 320) -> str:
    """Return a UI-friendly source preview without mutating the RAG context."""
    raw = str(text or "")
    if not raw.strip():
        return ""

    normalized = _PDF_PRIVATE_GLYPH_RE.sub(" ", raw)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized:
        return ""

    readable = _READABLE_TEXT_RUN_RE.search(normalized)
    if readable:
        prefix = normalized[: readable.start()]
        if len(prefix) >= 24 and _formula_noise_score(prefix) >= 0.18:
            normalized = normalized[readable.start() :].lstrip(" .;:,-")

    # Remove leftover dense equation fragments from the beginning of the preview.
    for _ in range(3):
        parts = normalized.split(" ", 1)
        head = parts[0] if parts else ""
        if not head or len(head) > 18 or _formula_noise_score(head) < 0.35:
            break
        normalized = parts[1].lstrip() if len(parts) > 1 else ""

    snippet = normalized[:limit].rstrip()
    if len(normalized) > limit:
        snippet = snippet.rstrip(" .,;:") + "..."
    return snippet


def format_sources_api(docs: list) -> list[dict]:
    """API JSON format for sources."""
    result: list[dict] = []
    for index, doc in enumerate(_dedupe_source_docs(docs), start=1):
        metadata = doc.metadata or {}
        src = metadata.get("source", "inconnu")
        page = metadata.get("page", "?")
        rel = _safe_relpath(str(src))
        snippet = _source_snippet_for_display(doc.page_content, limit=320)
        article_id = str(metadata.get("article_id", "")).strip()
        chunk_id = metadata.get("chunk_id")
        chunk_uid = str(metadata.get("chunk_uid", "")).strip()
        ref_label = _source_ref_label(doc, index)
        result.append(
            {
                "index": index,
                "ref_label": ref_label,
                "file": rel,
                "page": page,
                "snippet": snippet,
                "content_type": metadata.get("content_type", "pdf_page"),
                "location_label": metadata.get("location_label", ""),
                "citation": metadata.get("zotero_citation", ""),
                "title": metadata.get("zotero_title", ""),
                "authors": metadata.get("zotero_authors", ""),
                "author": metadata.get("zotero_first_author", ""),
                "year": metadata.get("zotero_year", ""),
                "journal": metadata.get("zotero_journal", ""),
                "doi": metadata.get("zotero_doi", ""),
                "tags": metadata.get("zotero_tags", ""),
                "collections": metadata.get("zotero_collections", ""),
                "article_id": article_id,
                "chunk_id": chunk_id,
                "chunk_uid": chunk_uid,
                "retrieval_score": metadata.get("rag_retrieval_score"),
            }
        )
    return result


def _split_text_for_streaming(text: str, frame_chars: int = 220) -> list[str]:
    text = str(text or "")
    if not text:
        return []
    return [text[i : i + frame_chars] for i in range(0, len(text), frame_chars)]
