"""
RAG global state variables + pause / resume control.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from os import environ
from pathlib import Path
from threading import Event, RLock
from typing import Any, Protocol

from langchain_core.documents import Document

from src.document_formats import SUPPORTED_DOCUMENT_EXTENSIONS
from src.zotero_db import ZoteroItem


# ------ Progress ------------------------------------------------
class ProgressFn(Protocol):
    def __call__(self, frac: float, desc: str = "") -> None: ...


def _noop_progress(frac: float, desc: str = "") -> None:
    _ = (frac, desc)


# ------ Runtime globals (reset on reload / pause) ---------------
_state_lock = RLock()

# ------ Answers reading the loaded index -------------------------------------
# A chat answer or a literature review reads ``vectorstore`` for its whole
# stream, long after the init lock is released. Switching library closes that
# store: it waits for no reader and refuses while one runs. An entry older than
# _READER_STALE_SECONDS (a stream never closed) no longer blocks.
_READER_STALE_SECONDS = 30 * 60
_vector_readers_lock = RLock()
_vector_readers: dict[int, float] = {}
_vector_reader_ids = itertools.count(1)


def begin_vector_reader() -> int:
    with _vector_readers_lock:
        token = next(_vector_reader_ids)
        _vector_readers[token] = time.monotonic()
        return token


def end_vector_reader(token: int) -> None:
    with _vector_readers_lock:
        _vector_readers.pop(token, None)


@contextlib.contextmanager
def vector_readers_paused() -> Iterator[int]:
    """Hold new readers back; yields how many still read the index."""
    with _vector_readers_lock:
        now = time.monotonic()
        active = sum(
            1 for started in _vector_readers.values() if now - started < _READER_STALE_SECONDS
        )
        yield active


# Stands for the answer chain when the index was prepared before any chat
# model was installed: questions build their own chat model.
INDEX_ONLY_CHAIN: Any = object()
document_chain: Any = None
vectorstore: Any = None
last_sources: list[Document] = []
_zotero_library: dict[str, ZoteroItem] = {}
_max_chunks_default: int = 8
runtime_model: str = ""
runtime_threshold: float | None = None
runtime_chunk_size: int | None = None
runtime_embedding_model: str = ""
runtime_db_path: str = ""
runtime_provider: str = ""
runtime_ready_at: str = ""
runtime_message: str = ""
_pdf_scan_cache_key: str = ""
_pdf_scan_cache_at: float = 0.0
_pdf_scan_cache: list[tuple[str, str]] = []

# ------ Manifest / stale-index markers --------------------------
_MANIFEST_FILE = "index_manifest.json"
_INCOMPLETE_INDEX_FILE = ".index_incomplete"

# ------ Pause / resume ------------------------------------------
# Despite its name, _vector_load_pause_event *stops* the load (it raises
# VectorLoadPaused). _vector_load_suspend_event is the real pause: loaders
# block at their next checkpoint until it is cleared, keeping all progress.
_vector_load_pause_event = Event()
_vector_load_suspend_event = Event()

try:
    _chat_cap_raw = int(environ.get("RAG_CHAT_MAX_CHUNKS_CAP", "12"))
except (TypeError, ValueError):
    _chat_cap_raw = 12
_CHAT_MAX_CHUNKS_CAP = max(1, min(12, _chat_cap_raw))


class VectorLoadPaused(RuntimeError):
    """Raised when a vector database load/reload is paused by the user."""


def _reset_vector_load_pause() -> None:
    _vector_load_pause_event.clear()
    _vector_load_suspend_event.clear()


def _wait_while_vector_load_suspended() -> None:
    """Block the calling loader thread while the user has paused indexing.

    A stop request ends the wait so the caller can raise VectorLoadPaused.
    """
    while _vector_load_suspend_event.is_set() and not _vector_load_pause_event.is_set():
        _vector_load_pause_event.wait(0.25)


def _raise_if_vector_load_paused() -> None:
    _wait_while_vector_load_suspended()
    if _vector_load_pause_event.is_set():
        raise VectorLoadPaused(
            "Rechargement de la base vectorielle arrete. "
            "Cliquez sur `Recharger PDF` pour reprendre."
        )


def pause_vector_loading() -> str:
    _vector_load_pause_event.set()
    _vector_load_suspend_event.clear()
    return (
        "Arret demande pour le traitement RAG en cours. "
        "Le traitement courant va s'arreter proprement puis pourra etre repris."
    )


def suspend_vector_loading() -> None:
    _vector_load_suspend_event.set()


def resume_vector_loading() -> None:
    _vector_load_suspend_event.clear()


def is_vector_loading_suspended() -> bool:
    return _vector_load_suspend_event.is_set()


def mark_runtime_ready(
    *,
    model: str,
    threshold: float,
    chunk_size: int,
    embedding_model: str,
    db_path: str,
    provider: str,
    message: str,
) -> None:
    global runtime_model
    global runtime_threshold
    global runtime_chunk_size
    global runtime_embedding_model
    global runtime_db_path
    global runtime_provider
    global runtime_ready_at
    global runtime_message
    with _state_lock:
        runtime_model = str(model or "").strip()
        runtime_threshold = float(threshold)
        runtime_chunk_size = int(chunk_size)
        runtime_embedding_model = str(embedding_model or "").strip()
        runtime_db_path = str(db_path or "").strip()
        runtime_provider = str(provider or "").strip()
        runtime_ready_at = datetime.now(UTC).isoformat()
        runtime_message = str(message or "").strip()


def clear_runtime_state() -> None:
    global document_chain
    global vectorstore
    global last_sources
    global runtime_model
    global runtime_threshold
    global runtime_chunk_size
    global runtime_embedding_model
    global runtime_db_path
    global runtime_provider
    global runtime_ready_at
    global runtime_message
    with _state_lock:
        document_chain = None
        vectorstore = None
        last_sources = []
        runtime_model = ""
        runtime_threshold = None
        runtime_chunk_size = None
        runtime_embedding_model = ""
        runtime_db_path = ""
        runtime_provider = ""
        runtime_ready_at = ""
        runtime_message = ""


def get_runtime_status() -> dict[str, Any]:
    with _state_lock:
        ready = document_chain is not None and vectorstore is not None
        return {
            "ready": ready,
            "model": runtime_model if ready else "",
            "threshold": runtime_threshold if ready else None,
            "chunk_size": runtime_chunk_size if ready else None,
            "embedding_model": runtime_embedding_model if ready else "",
            "db_path": runtime_db_path if ready else "",
            "provider": runtime_provider if ready else "",
            "ready_at": runtime_ready_at if ready else "",
            "message": runtime_message if ready else "",
        }


# ------ Document discovery --------------------------------------
def find_documents() -> list[tuple[str, str]]:
    """Recursively scan DATA_PATH for every supported document format."""
    global _pdf_scan_cache_at, _pdf_scan_cache, _pdf_scan_cache_key

    from src import config

    results: list[tuple[str, str]] = []
    data_path = str(config.DATA_PATH)
    now = time.monotonic()
    with _state_lock:
        if _pdf_scan_cache_key == data_path and now - _pdf_scan_cache_at < 5.0:
            return list(_pdf_scan_cache)
    if not Path(data_path).exists():
        with _state_lock:
            _pdf_scan_cache_key = data_path
            _pdf_scan_cache_at = now
            _pdf_scan_cache = []
        return results

    for root, dirs, files in os.walk(data_path):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__MACOSX")
        for filename in sorted(files):
            if Path(filename).suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS:
                full_path = os.path.join(root, filename)
                rel_path = os.path.relpath(full_path, data_path)
                results.append((rel_path, full_path))
    with _state_lock:
        _pdf_scan_cache_key = data_path
        _pdf_scan_cache_at = now
        _pdf_scan_cache = list(results)
    return results


def find_pdfs() -> list[tuple[str, str]]:
    """Backward-compatible alias returning all supported RAG documents."""
    return find_documents()


def clear_document_scan_cache() -> None:
    """Invalidate the short-lived library scan cache after path/content changes."""
    global _pdf_scan_cache_at, _pdf_scan_cache, _pdf_scan_cache_key
    with _state_lock:
        _pdf_scan_cache_at = 0.0
        _pdf_scan_cache = []
        _pdf_scan_cache_key = ""


def clear_pdf_scan_cache() -> None:
    """Backward-compatible alias for the document scan cache."""
    clear_document_scan_cache()


# ------ Worker count / group size --------------------------------
def _get_pdf_worker_count() -> int:
    env_value = environ.get("RAG_PDF_WORKERS", "").strip()
    if env_value.isdigit():
        value = int(env_value)
        if value >= 1:
            return min(value, 16)
    cpu_count = os.cpu_count() or 4
    return max(2, min(6, cpu_count))


def _get_pdf_group_size() -> int:
    env_value = environ.get("RAG_PDF_GROUP_SIZE", "").strip()
    if env_value.isdigit():
        value = int(env_value)
        if value >= 1:
            return min(value, 64)
    workers = _get_pdf_worker_count()
    return max(2, min(4, workers * 3))
