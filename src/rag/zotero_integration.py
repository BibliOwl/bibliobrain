"""Zotero cache and metadata enrichment helpers for the RAG package."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from typing import Any

from langchain_core.documents import Document

from src import config, zotero_drops
from src.app_settings import get_app_settings, index_standalone_pdfs_enabled
from src.unicode_utils import json_dumps_utf8, sanitize_utf8_data, sanitize_utf8_text
from src.zotero_db import (
    ZoteroItem,
    casefold_sort_key,
    filter_active_zotero_pdf_entries,
    find_zotero_db,
    load_zotero_library,
)

from . import state

_logger = logging.getLogger(__name__)


def _safe_relpath(path: str) -> str:
    try:
        rel = os.path.relpath(path, config.DATA_PATH)
    except Exception:
        _logger.debug("Recovered from an error in _safe_relpath.", exc_info=True)
        rel = path
    return rel.replace("\\", " / ")


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", sanitize_utf8_text(value).strip()).lower()


def _split_pipe(value: str) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split("|") if part.strip()]


def _join_pipe(values: list[str]) -> str:
    return " | ".join(value.strip() for value in values if value and value.strip())


def _csv_to_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,\n;|]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            parts.extend(re.split(r"[,\n;|]", str(item)))
    else:
        parts = [str(value)]
    return [part.strip() for part in parts if part.strip()]


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = _normalize_text(value)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value.strip())
    return result


def _file_signature(full_path: str) -> str:
    try:
        stat = os.stat(full_path)
    except OSError:
        return ""

    digest = hashlib.sha256()
    try:
        with open(full_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return f"{stat.st_size}:sha256:{digest.hexdigest()}"


def _legacy_file_signature(full_path: str) -> str:
    try:
        stat = os.stat(full_path)
    except OSError:
        return ""
    return f"{stat.st_size}:{stat.st_mtime_ns}"


_PATH_ARTICLE_ID_MARK = "path-id-v1"


def _rel_folder(rel_path: str) -> str:
    return os.path.dirname(str(rel_path or "").replace("\\", "/").strip("/"))


def _path_article_id(rel_path: str) -> str:
    """Article id of a document no Zotero or BibTeX record names.

    The file name alone is not unique once the library has sub-folders: two
    papers both called "article.pdf" would merge into one. A file at the top
    keeps the id it always had; one in a sub-folder adds a short hash of it.
    """
    stem = os.path.splitext(os.path.basename(str(rel_path or "").replace("\\", "/")))[0]
    stem = stem or "article"
    folder = _rel_folder(rel_path)
    if not folder:
        return stem
    return f"{stem}-{hashlib.sha1(folder.encode('utf-8')).hexdigest()[:8]}"


def _index_signature(rel_path: str, item: ZoteroItem | None) -> str:
    """What the index keeps about a document besides its text.

    Adds a mark for documents whose id comes from their sub-folder path, so
    an index built when the id was the bare file name re-reads them once.
    """
    signature = _zotero_signature(item)
    if (item is not None and item.item_key) or not _rel_folder(rel_path):
        return signature
    return f"{signature}|{_PATH_ARTICLE_ID_MARK}" if signature else _PATH_ARTICLE_ID_MARK


def _zotero_signature(item: ZoteroItem | None) -> str:
    if not item:
        return ""

    payload = {
        "title": item.title,
        "authors": item.authors,
        "year": item.year,
        "journal": item.journal,
        "doi": item.doi,
        "citation_count": int(item.citation_count or 0),
        "influential_citation_count": int(item.influential_citation_count or 0),
        "item_type": item.item_type,
        "tags": sorted(item.tags, key=casefold_sort_key),
        "collections": sorted(item.collections, key=casefold_sort_key),
        "notes": item.notes,
        "annotations": [
            {
                "text": annotation.text,
                "comment": annotation.comment,
                "page_label": annotation.page_label,
                "color": annotation.color,
            }
            for annotation in item.annotations
        ],
    }
    if item.shared_filename:
        # These files used to be matched by name, maybe to another record:
        # naming the record re-reads them once under their own key. Other
        # signatures stay as they were, so nothing else is re-indexed.
        payload["item_key"] = item.item_key
    raw = json_dumps_utf8(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _build_item_metadata(rel_path: str, full_path: str, item: ZoteroItem | None) -> dict[str, Any]:
    if not item:
        return sanitize_utf8_data(
            {
                "source": full_path,
                "rel_path": rel_path,
                "content_type": "pdf_page",
                "zotero_title": "",
                "zotero_citation": "",
                "zotero_first_author": "",
                "zotero_first_author_lc": "",
                "zotero_authors": "",
                "zotero_authors_lc": "",
                "zotero_year": "",
                "zotero_journal": "",
                "zotero_doi": "",
                "zotero_tags": "",
                "zotero_tags_lc": "",
                "zotero_collections": "",
                "zotero_collections_lc": "",
                "zotero_item_key": "",
                "zotero_attachment_key": "",
                "citation_count": 0,
                "influential_citation_count": 0,
            }
        )

    tags = _dedupe_keep_order(item.tags)
    collections = _dedupe_keep_order(item.collections)

    return sanitize_utf8_data(
        {
            "source": full_path,
            "rel_path": rel_path,
            "content_type": "pdf_page",
            "zotero_title": item.title or "",
            "zotero_citation": item.citation_str(),
            "zotero_first_author": item.first_author() or "",
            "zotero_first_author_lc": _normalize_text(item.first_author()),
            "zotero_authors": item.authors_str(),
            "zotero_authors_lc": _join_pipe([_normalize_text(author) for author in item.authors]),
            "zotero_year": item.year or "",
            "zotero_journal": item.journal or "",
            "zotero_doi": item.doi or "",
            "zotero_tags": _join_pipe(tags),
            "zotero_tags_lc": _join_pipe([_normalize_text(tag) for tag in tags]),
            "zotero_collections": _join_pipe(collections),
            "zotero_collections_lc": _join_pipe([_normalize_text(path) for path in collections]),
            "zotero_item_key": item.item_key or "",
            "zotero_attachment_key": item.attachment_key or "",
            "citation_count": int(item.citation_count or 0),
            "influential_citation_count": int(item.influential_citation_count or 0),
        }
    )


def _build_note_documents(base_metadata: dict[str, Any], item: ZoteroItem | None) -> list[Document]:
    if not item:
        return []

    note_docs: list[Document] = []
    citation = sanitize_utf8_text(item.citation_str())
    title = sanitize_utf8_text(item.title or "Untitled")
    note_texts = item.all_note_texts()

    for index, note_text in enumerate(note_texts, start=1):
        text = sanitize_utf8_text(note_text).strip()
        if not text:
            continue
        content = f"Researcher note {index} for: {title}\n" f"Citation: {citation}\n" f"{text}"
        metadata = dict(base_metadata)
        metadata["content_type"] = "zotero_note"
        metadata["page"] = "note"
        metadata["zotero_note_index"] = index
        note_docs.append(Document(page_content=content, metadata=metadata))

    return note_docs


def _share_with_library_tab(db_path: str, zotero_library: dict[str, ZoteroItem]) -> None:
    """Hand a fresh Zotero load to the Library tab, which keeps its own copy.

    Only a forced reload used to refresh that copy, so a PDF Zotero gained after
    startup was indexed by any other reload yet hidden from the Library tab.
    """
    library_module = sys.modules.get("src.library")
    if library_module is not None:
        library_module.adopt_zotero_library(db_path, zotero_library)


def _refresh_zotero_library_cache() -> None:
    try:
        settings = get_app_settings()
    except (OSError, TypeError, ValueError):
        settings = {}
    metadata_source = str(settings.get("metadata_source") or "none").strip().lower()
    if metadata_source not in {"zotero", "bibtex"}:
        with state._state_lock:
            state._zotero_library = {}
        return

    zotero_db_path = str(settings.get("zotero_path") or config.ZOTERO_DB_PATH or "").strip()
    if not zotero_db_path and metadata_source == "zotero":
        zotero_db_path = find_zotero_db() or ""
    if zotero_db_path and os.path.exists(zotero_db_path):
        try:
            load_zotero_library.cache_clear()
            loaded = load_zotero_library(zotero_db_path, zotero_drops.fingerprints(), index_standalone_pdfs_enabled())
            with state._state_lock:
                state._zotero_library = loaded
            _share_with_library_tab(zotero_db_path, loaded)
            return
        except Exception as exc:
            _logger.warning(
                "Zotero reload failed (%s); clearing in-memory library: %s",
                zotero_db_path,
                exc,
            )
    with state._state_lock:
        state._zotero_library = {}


def _filter_active_zotero_pdf_entries(
    pdf_entries: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """Documents the index reads: active Zotero attachments, then the user's
    selection (collections, sub-folders, size guard; see src/index_selection.py)."""
    from src.index_selection import apply_index_selection, remember_skipped

    with state._state_lock:
        library = dict(state._zotero_library)
    active = filter_active_zotero_pdf_entries(
        pdf_entries,
        library,
        data_path=config.DATA_PATH,
        zotero_db_path=config.ZOTERO_DB_PATH,
    )
    kept, skipped = apply_index_selection(active, library)
    remember_skipped(skipped)
    return kept
