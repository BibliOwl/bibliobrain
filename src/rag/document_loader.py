"""PDF loading, chunking, and prepared-article cache helpers."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any

from langchain_community.document_loaders import PyPDFLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from src.chunk_cache import (
    compute_prepared_article_cache_key,
    load_cached_prepared_article_compatible,
    save_cached_prepared_article,
)
from src.document_formats import (
    DocumentFormatError,
    document_index_variant,
    load_document_units,
    resolve_document_metadata,
)
from src.pdf_metadata import resolve_pdf_metadata
from src.unicode_utils import repair_pdf_text_artifacts, sanitize_utf8_data, sanitize_utf8_text
from src.visual_analysis import (
    VISUAL_CONTENT_TYPE,
    analyze_pdf_visuals,
    index_visual_signature,
    visual_analysis_model_settings,
)
from src.zotero_db import ZoteroItem

from . import state
from .zotero_integration import (
    _build_item_metadata,
    _build_note_documents,
    _file_signature,
    _zotero_signature,
)

_logger = logging.getLogger(__name__)


def _sanitize_document(doc: Document) -> Document:
    return Document(
        page_content=repair_pdf_text_artifacts(getattr(doc, "page_content", "") or ""),
        metadata=sanitize_utf8_data(dict(getattr(doc, "metadata", {}) or {})),
    )


def _sanitize_documents(docs: list[Document]) -> list[Document]:
    return [_sanitize_document(doc) for doc in docs]


def _build_note_chunks(
    base_metadata: dict[str, Any],
    item: ZoteroItem | None,
    chunk_size: int,
) -> list[Document]:
    note_docs = [_sanitize_document(doc) for doc in _build_note_documents(base_metadata, item)]
    if not note_docs:
        return []

    note_chunk_size = max(280, min(700, chunk_size // 2))
    note_splitter = RecursiveCharacterTextSplitter(
        chunk_size=note_chunk_size,
        chunk_overlap=min(note_chunk_size // 6, 80),
    )
    note_chunks = note_splitter.split_documents(note_docs)
    article_id = str(base_metadata.get("article_id", "")).strip() or _article_id_from_item(
        str(base_metadata.get("rel_path", "") or ""),
        item,
    )
    for chunk in note_chunks:
        chunk.metadata["content_type"] = "zotero_note"
        chunk.metadata["page"] = "note"
    note_chunks = _sanitize_documents(note_chunks)
    _assign_stable_chunk_metadata(note_chunks, article_id=article_id, content_type="zotero_note")
    return note_chunks


def _resolve_index_metadata(rel_path: str, full_path: str) -> ZoteroItem | None:
    resolver = (
        resolve_pdf_metadata
        if Path(full_path).suffix.lower() == ".pdf"
        else resolve_document_metadata
    )
    return resolver(
        rel_path,
        full_path,
        state._zotero_library,
        allow_external_enrichment=True,
    )


def _serialize_prepared_chunk(doc: Document, *, chunk_id: int | None = None) -> dict[str, Any]:
    metadata = sanitize_utf8_data(dict(doc.metadata or {}))
    if chunk_id is not None:
        metadata["chunk_id"] = int(chunk_id)
    chunk_uid = sanitize_utf8_text(metadata.get("chunk_uid", "")).strip()
    payload = {
        "text": sanitize_utf8_text(doc.page_content),
        "metadata": metadata,
        "chunk_uid": chunk_uid,
    }
    if chunk_id is not None:
        payload["chunk_id"] = int(chunk_id)
    return payload


def _deserialize_prepared_chunk(payload: dict[str, Any]) -> Document:
    metadata = sanitize_utf8_data(dict(payload.get("metadata") or {}))
    chunk_uid = sanitize_utf8_text(
        payload.get("chunk_uid", "") or metadata.get("chunk_uid", "")
    ).strip()
    if chunk_uid:
        metadata["chunk_uid"] = chunk_uid
    raw_chunk_id = payload.get("chunk_id")
    chunk_id_text = str(raw_chunk_id).strip() if raw_chunk_id is not None else ""
    if chunk_id_text:
        try:
            normalized_chunk_id = int(chunk_id_text)
            metadata.setdefault("chunk_id", normalized_chunk_id)
        except (TypeError, ValueError):
            pass
    return _sanitize_document(
        Document(page_content=sanitize_utf8_text(payload.get("text", "")), metadata=metadata)
    )


def _deserialize_prepared_chunks(payloads: list[dict[str, Any]]) -> list[Document]:
    docs: list[Document] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        docs.append(_deserialize_prepared_chunk(payload))
    return docs


def _prepared_article_pdf_payloads(article: dict[str, Any]) -> list[dict[str, Any]]:
    payloads = article.get("pdf_chunks")
    if isinstance(payloads, list) and payloads:
        return [payload for payload in payloads if isinstance(payload, dict)]
    payloads = article.get("chunks")
    if isinstance(payloads, list):
        return [payload for payload in payloads if isinstance(payload, dict)]
    return []


def _prepared_article_note_payloads(article: dict[str, Any]) -> list[dict[str, Any]]:
    payloads = article.get("note_chunks")
    if not isinstance(payloads, list):
        return []
    return [payload for payload in payloads if isinstance(payload, dict)]


def _prepared_article_page_count(article: dict[str, Any]) -> int:
    try:
        value = int(article.get("page_count", 0) or 0)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass

    pages: set[str] = set()
    for payload in _prepared_article_pdf_payloads(article):
        metadata = dict(payload.get("metadata") or {})
        page = str(metadata.get("page", "")).strip()
        if page and page.lower() != "note":
            pages.add(page)
    return len(pages)


def _build_prepared_article_payload(
    rel_path: str,
    pdf_chunks: list[Document],
    note_chunks: list[Document],
    item: ZoteroItem | None,
    *,
    page_count: int,
) -> dict[str, Any]:
    article_id = _article_id_from_item(rel_path, item)
    article_title = sanitize_utf8_text(item.title if item and item.title else Path(rel_path).stem)
    prepared_pdf_chunks = [
        _serialize_prepared_chunk(doc, chunk_id=chunk_id) for chunk_id, doc in enumerate(pdf_chunks)
    ]
    prepared_note_chunks = [
        _serialize_prepared_chunk(doc, chunk_id=chunk_id)
        for chunk_id, doc in enumerate(note_chunks)
    ]
    return sanitize_utf8_data(
        {
            "article_id": sanitize_utf8_text(article_id),
            "title": article_title,
            "rel_path": sanitize_utf8_text(rel_path),
            "author": sanitize_utf8_text(item.first_author() if item else ""),
            "year": sanitize_utf8_text(item.year if item else ""),
            "tags": list(item.tags[:8]) if item else [],
            "collections": list(item.collections[:6]) if item else [],
            "page_count": int(page_count),
            "chunks": prepared_pdf_chunks,
            "pdf_chunks": prepared_pdf_chunks,
            "note_chunks": prepared_note_chunks,
        }
    )


def _load_and_chunk_single_pdf(
    rel_path: str,
    full_path: str,
    chunk_size: int,
    *,
    textless_paths: set[str] | None = None,
) -> tuple[list[Document], list[Document], int]:
    state._wait_while_vector_load_suspended()
    if state._vector_load_pause_event.is_set():
        return [], [], 0
    try:
        units = load_document_units(full_path, pdf_loader_class=PyPDFLoader)
    except (OSError, ValueError, DocumentFormatError):
        return [], [], 0

    if state._vector_load_pause_event.is_set():
        return [], [], 0
    item = _resolve_index_metadata(rel_path, full_path)
    base_metadata = _build_item_metadata(rel_path, full_path, item)
    article_id = _article_id_from_item(rel_path, item)
    base_metadata["article_id"] = article_id

    prepared_pages: list[Document] = []
    for unit in units:
        merged = dict(base_metadata)
        merged.update(
            {
                "page": unit.page,
                "content_type": unit.content_type,
                "location_label": unit.location_label,
                "document_format": Path(full_path).suffix.lower().lstrip("."),
            }
        )
        prepared_pages.append(
            _sanitize_document(
                Document(
                    page_content=sanitize_utf8_text(unit.text),
                    metadata=merged,
                )
            )
        )

    pdf_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=min(chunk_size // 5, 200),
    )
    pdf_chunks = (
        _sanitize_documents(pdf_splitter.split_documents(prepared_pages)) if prepared_pages else []
    )
    _assign_stable_chunk_metadata(pdf_chunks, article_id=article_id, content_type="document")

    visual_mode, vision_model, visual_description_model = visual_analysis_model_settings()
    if (
        Path(full_path).suffix.lower() == ".pdf"
        and visual_mode == "index"
        and (vision_model or visual_description_model)
        and not state._vector_load_pause_event.is_set()
    ):
        try:
            visual_payload = analyze_pdf_visuals(
                full_path,
                model=vision_model,
                analysis_model=visual_description_model,
                page_texts=[unit.text for unit in units],
            )
        except Exception:
            # L'analyse visuelle est un enrichissement optionnel : une panne du
            # modèle ou du renderer ne doit jamais empêcher l'indexation texte.
            _logger.debug("Recovered from an error in _load_and_chunk_single_pdf.", exc_info=True)
            visual_payload = {"results": []}
        visual_documents: list[Document] = []
        for result in visual_payload.get("results", []):
            if not isinstance(result, dict) or not str(result.get("text") or "").strip():
                continue
            metadata = dict(base_metadata)
            metadata.update(
                {
                    "content_type": VISUAL_CONTENT_TYPE,
                    "page": result.get("page", "?"),
                    "vision_model": vision_model or visual_description_model,
                    "ocr_model": vision_model,
                    "visual_analysis_model": visual_description_model,
                    "visual_source": str(result.get("visual_source") or "page_render_fallback"),
                    "figure_label": str(result.get("figure_label") or ""),
                    "figure_caption": str(result.get("caption") or ""),
                }
            )
            visual_documents.append(
                Document(page_content=str(result.get("text") or "").strip(), metadata=metadata)
            )
        visual_chunks = _sanitize_documents(pdf_splitter.split_documents(visual_documents))
        _assign_stable_chunk_metadata(
            visual_chunks,
            article_id=article_id,
            content_type=VISUAL_CONTENT_TYPE,
        )
        pdf_chunks.extend(visual_chunks)

    note_chunks = _build_note_chunks(base_metadata, item, chunk_size)

    prepared_article = _build_prepared_article_payload(
        rel_path,
        pdf_chunks,
        note_chunks,
        item,
        page_count=len(prepared_pages),
    )
    file_sig = _file_signature(full_path)
    zotero_sig = _zotero_signature(item)
    index_variant = document_index_variant(
        full_path,
        pdf_variant=index_visual_signature(),
    )
    prepared_cache_key = compute_prepared_article_cache_key(
        rel_path=rel_path,
        file_sig=file_sig,
        zotero_sig=zotero_sig,
        chunk_size=chunk_size,
        variant=index_variant,
    )
    save_cached_prepared_article(
        prepared_article,
        cache_key=prepared_cache_key,
        file_sig=file_sig,
        zotero_sig=zotero_sig,
        chunk_size=chunk_size,
    )

    # Read in full and nothing to index (a scan without a text layer): the
    # caller records it so the next reload does not offer it as new forever.
    # The early returns above (pause, unreadable file) never land here.
    if (
        textless_paths is not None
        and not pdf_chunks
        and not note_chunks
        and not state._vector_load_pause_event.is_set()
    ):
        textless_paths.add(rel_path)
    return note_chunks, pdf_chunks, len(prepared_pages)


def _load_or_prepare_single_pdf(
    rel_path: str,
    full_path: str,
    chunk_size: int,
    *,
    textless_paths: set[str] | None = None,
) -> tuple[list[Document], list[Document], int]:
    state._wait_while_vector_load_suspended()
    if state._vector_load_pause_event.is_set():
        return [], [], 0

    item = _resolve_index_metadata(rel_path, full_path)
    article_id = _article_id_from_item(rel_path, item)
    file_sig = _file_signature(full_path)
    zotero_sig = _zotero_signature(item)
    visual_signature = document_index_variant(
        full_path,
        pdf_variant=index_visual_signature(),
    )
    prepared_cache_key = compute_prepared_article_cache_key(
        rel_path=rel_path,
        file_sig=file_sig,
        zotero_sig=zotero_sig,
        chunk_size=chunk_size,
        variant=visual_signature,
    )
    cached_article, cache_was_legacy = load_cached_prepared_article_compatible(
        article_id,
        prepared_cache_key,
        file_sig=file_sig,
        zotero_sig=zotero_sig,
        chunk_size=chunk_size,
        allow_legacy=Path(full_path).suffix.lower() == ".pdf" and not visual_signature,
    )
    if cached_article is not None and not visual_signature:
        cached_payloads = _prepared_article_pdf_payloads(cached_article)
        if any(
            str((payload.get("metadata") or {}).get("content_type") or "") == VISUAL_CONTENT_TYPE
            for payload in cached_payloads
            if isinstance(payload, dict)
        ):
            cached_article = None
    if cached_article is not None:
        pdf_payloads = _prepared_article_pdf_payloads(cached_article)
        if pdf_payloads:
            pdf_chunks = _deserialize_prepared_chunks(pdf_payloads)
            note_payloads = _prepared_article_note_payloads(cached_article)
            if note_payloads:
                note_chunks = _deserialize_prepared_chunks(note_payloads)
            else:
                base_metadata = _build_item_metadata(rel_path, full_path, item)
                base_metadata["article_id"] = article_id
                note_chunks = _build_note_chunks(base_metadata, item, chunk_size)
                upgraded_article = _build_prepared_article_payload(
                    rel_path,
                    pdf_chunks,
                    note_chunks,
                    item,
                    page_count=_prepared_article_page_count(cached_article),
                )
                save_cached_prepared_article(
                    upgraded_article,
                    cache_key=prepared_cache_key,
                    file_sig=file_sig,
                    zotero_sig=zotero_sig,
                    chunk_size=chunk_size,
                )
            if cache_was_legacy and note_payloads:
                save_cached_prepared_article(
                    cached_article,
                    cache_key=prepared_cache_key,
                    file_sig=file_sig,
                    zotero_sig=zotero_sig,
                    chunk_size=chunk_size,
                )
            return note_chunks, pdf_chunks, _prepared_article_page_count(cached_article)

    return _load_and_chunk_single_pdf(
        rel_path, full_path, chunk_size, textless_paths=textless_paths
    )


def _build_chunks_from_pdfs(
    pdf_list: list[tuple[str, str]],
    chunk_size: int,
    progress: state.ProgressFn,
    progress_start: float,
    progress_end: float,
    status_label: str = "Lecture PDF",
    textless_paths: set[str] | None = None,
) -> tuple[list[Document], list[Document], int]:
    """``textless_paths`` receives the files read without error that gave no chunk."""
    if not pdf_list:
        return [], [], 0

    total = len(pdf_list)
    span = max(0.0, progress_end - progress_start)
    workers = state._get_pdf_worker_count()

    note_chunks: list[Document] = []
    pdf_chunks: list[Document] = []
    page_count = 0
    load_errors: list[str] = []

    progress(
        progress_start,
        desc=f"{status_label}: demarrage parallel ({workers} workers) - {total} documents",
    )

    load_kwargs = {} if textless_paths is None else {"textless_paths": textless_paths}
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {
        executor.submit(
            _load_or_prepare_single_pdf, rel_path, full_path, chunk_size, **load_kwargs
        ): (
            rel_path,
            full_path,
        )
        for rel_path, full_path in pdf_list
    }
    pending = set(futures)

    try:
        completed = 0
        while pending:
            state._raise_if_vector_load_paused()
            done, pending = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                continue

            for future in done:
                _rel_path, full_path = futures[future]
                completed += 1
                frac = progress_start + min(1.0, completed / total) * span
                progress(
                    frac, desc=f"{status_label} {completed}/{total} : {os.path.basename(full_path)}"
                )
                try:
                    note_part, pdf_part, page_part = future.result()
                except Exception as exc:
                    _logger.debug(
                        "Recovered from an error in _build_chunks_from_pdfs.", exc_info=True
                    )
                    load_errors.append(f"{os.path.basename(full_path)}: {exc}")
                    continue

                if note_part:
                    note_chunks.extend(note_part)
                if pdf_part:
                    pdf_chunks.extend(pdf_part)
                page_count += page_part
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    if load_errors and not note_chunks and not pdf_chunks:
        joined = "; ".join(load_errors[:3])
        raise RuntimeError(f"Echec du chargement des documents pour le lot en cours: {joined}")

    return note_chunks, pdf_chunks, page_count


def _sanitize_article_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9:_\-.]+", "_", str(value or "").strip())
    return cleaned.strip("_") or "article"


def _article_id_from_item(rel_path: str, item: ZoteroItem | None) -> str:
    if item and item.item_key:
        return _sanitize_article_id(item.item_key)
    stem = Path(rel_path or "article").stem
    return _sanitize_article_id(stem)


def _stable_chunk_uid(
    article_id: str,
    content_type: str,
    page: Any,
    ordinal: int,
    text: str,
) -> str:
    normalized_text = re.sub(r"\s+", " ", sanitize_utf8_text(text).strip())
    payload = "||".join(
        [
            _sanitize_article_id(sanitize_utf8_text(article_id)),
            sanitize_utf8_text(content_type or "").strip() or "pdf_page",
            sanitize_utf8_text(page if page not in (None, "") else "?"),
            str(int(ordinal)),
            normalized_text,
        ]
    )
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
    return f"{_sanitize_article_id(article_id)}:{int(ordinal)}:{digest}"


def _assign_stable_chunk_metadata(
    chunks: list[Document],
    *,
    article_id: str,
    content_type: str,
) -> None:
    for ordinal, chunk in enumerate(chunks):
        metadata = dict(chunk.metadata or {})
        metadata["article_id"] = article_id
        resolved_content_type = str(metadata.get("content_type") or content_type or "document")
        metadata["content_type"] = resolved_content_type
        page = metadata.get("page", "?")
        metadata["chunk_uid"] = _stable_chunk_uid(
            article_id,
            resolved_content_type,
            page,
            ordinal,
            chunk.page_content,
        )
        chunk.metadata = metadata
