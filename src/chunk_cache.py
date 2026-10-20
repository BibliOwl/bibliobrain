"""Prepared-article (PDF chunk) cache.

Extracted out of ``wiki_builder`` because the cache has nothing to do
with the wiki: it is the deterministic on-disk cache for the
``rag.document_loader`` PDF chunking pipeline. Keeping it here gives
``rag.document_loader`` a clean dependency and lets us strip the same
code from ``wiki_builder`` without churning callers.

Stored layout::

    <STORAGE_DIR>/wiki_build/articles/<article_id>.json   # one file per paper

The cache key is a SHA-1 of (rel_path, file_sig, zotero_sig, chunk_size,
version). The cache is invalidated automatically when any of those
change.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import config
from src.atomic_io import atomic_write_json

_PREPARED_ARTICLE_CACHE_VERSION = 1


def _wiki_build_root() -> Path:
    root = Path(config.STORAGE_DIR) / "wiki_build"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _article_cache_dir() -> Path:
    path = _wiki_build_root() / "articles"
    path.mkdir(parents=True, exist_ok=True)
    return path


_SAFE_CACHE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-.]+")


def _safe_cache_name(value: str) -> str:
    cleaned = _SAFE_CACHE_NAME_RE.sub("_", str(value or "").strip())
    return cleaned.strip("_") or "untitled"


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_json(path, payload)


def _cache_file_payload_matches(payload: dict[str, Any], *, cache_key: str) -> bool:
    return str(payload.get("cache_key", "")) == str(cache_key or "")


def _legacy_file_sig_matches_current_size(stored_file_sig: str, current_file_sig: str) -> bool:
    stored_parts = str(stored_file_sig or "").split(":")
    current_parts = str(current_file_sig or "").split(":")
    return (
        len(stored_parts) == 2
        and len(current_parts) == 3
        and current_parts[1] == "sha256"
        and stored_parts[0].isdigit()
        and stored_parts[0] == current_parts[0]
    )


def compute_prepared_article_cache_key(
    *,
    rel_path: str,
    file_sig: str,
    zotero_sig: str,
    chunk_size: int,
    variant: str = "",
) -> str:
    """Stable key for the prepared-article cache.

    Same algorithm the existing ``wiki_builder`` used so the cache stays
    valid across the migration.
    """
    payload = {
        "version": _PREPARED_ARTICLE_CACHE_VERSION,
        "rel_path": str(rel_path or "").replace("\\", "/"),
        "file_sig": str(file_sig or ""),
        "zotero_sig": str(zotero_sig or ""),
        "chunk_size": int(chunk_size),
        "variant": str(variant or ""),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def load_cached_prepared_article(article_id: str, cache_key: str) -> dict[str, Any] | None:
    path = _article_cache_dir() / f"{_safe_cache_name(article_id)}.json"
    payload = _read_json_file(path)
    if not payload or not _cache_file_payload_matches(payload, cache_key=cache_key):
        return None
    article = payload.get("article")
    return article if isinstance(article, dict) else None


def load_cached_prepared_article_compatible(
    article_id: str,
    cache_key: str,
    *,
    file_sig: str,
    zotero_sig: str,
    chunk_size: int,
    allow_legacy: bool = True,
) -> tuple[dict[str, Any] | None, bool]:
    path = _article_cache_dir() / f"{_safe_cache_name(article_id)}.json"
    payload = _read_json_file(path)
    if not payload:
        return None, False
    article = payload.get("article")
    if not isinstance(article, dict):
        return None, False
    if _cache_file_payload_matches(payload, cache_key=cache_key):
        return article, False
    try:
        cached_chunk_size = int(payload.get("chunk_size", 0) or 0)
    except (TypeError, ValueError):
        cached_chunk_size = 0
    legacy_compatible = bool(allow_legacy) and (
        cached_chunk_size == int(chunk_size)
        and str(payload.get("zotero_sig", "") or "") == str(zotero_sig or "")
        and _legacy_file_sig_matches_current_size(
            str(payload.get("file_sig", "") or ""),
            str(file_sig or ""),
        )
    )
    return (article, True) if legacy_compatible else (None, False)


def save_cached_prepared_article(
    article: dict[str, Any],
    *,
    cache_key: str,
    file_sig: str,
    zotero_sig: str,
    chunk_size: int,
) -> None:
    article_id = str(article.get("article_id", "")).strip()
    if not article_id:
        return
    payload = {
        "cache_key": str(cache_key or ""),
        "file_sig": str(file_sig or ""),
        "zotero_sig": str(zotero_sig or ""),
        "chunk_size": int(chunk_size),
        "saved_at": datetime.now(UTC).isoformat(),
        "article": article,
    }
    path = _article_cache_dir() / f"{_safe_cache_name(article_id)}.json"
    _write_json_file(path, payload)


__all__ = [
    "compute_prepared_article_cache_key",
    "load_cached_prepared_article",
    "load_cached_prepared_article_compatible",
    "save_cached_prepared_article",
]
