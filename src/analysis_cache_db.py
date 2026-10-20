"""
Persistent analysis cache storage (SQLite).

Stores generated article analyses across sessions.
"""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from pathlib import Path

import src.config as _cfg
from src.common import utc_now_iso as _utc_now_iso
from src.rel_paths import normalize_rel_path as _normalize_rel_path

DB_DIR = Path(_cfg.STORAGE_DIR)
DB_PATH = DB_DIR / "analysis_cache.sqlite3"

_MEM_CACHE_MAX = 512
_MEM_CACHE: OrderedDict[tuple[str, str, str], str] = OrderedDict()
_MEM_CACHE_LOCK = threading.Lock()

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _mem_cache_get(key: tuple[str, str, str]) -> str | None:
    with _MEM_CACHE_LOCK:
        if key in _MEM_CACHE:
            _MEM_CACHE.move_to_end(key)
            return _MEM_CACHE[key]
    return None


def _mem_cache_set(key: tuple[str, str, str], value: str) -> None:
    with _MEM_CACHE_LOCK:
        article_key = key[:2]
        stale_keys = [
            existing for existing in _MEM_CACHE if existing[:2] == article_key and existing != key
        ]
        for stale_key in stale_keys:
            _MEM_CACHE.pop(stale_key, None)
        _MEM_CACHE[key] = value
        _MEM_CACHE.move_to_end(key)
        while len(_MEM_CACHE) > _MEM_CACHE_MAX:
            _MEM_CACHE.popitem(last=False)


def _connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            DB_PATH,
            timeout=30,
            check_same_thread=False,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        _local.conn = conn
    return conn


def init_analysis_cache_db() -> None:
    global _initialized
    with _init_lock:
        conn = _connect()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cached_analyses (
                rel_path TEXT NOT NULL,
                analysis_type TEXT NOT NULL,
                fingerprint TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (rel_path, analysis_type)
            );

            CREATE INDEX IF NOT EXISTS idx_cached_analyses_updated_at
                ON cached_analyses(updated_at DESC);

            CREATE INDEX IF NOT EXISTS idx_cached_analyses_rel_path
                ON cached_analyses(rel_path);
            """
        )
        _initialized = True


def _ensure_initialized() -> None:
    if not _initialized:
        init_analysis_cache_db()


def get_cached_analysis(rel_path: str, analysis_type: str, fingerprint: str = "") -> str | None:
    norm_path = _normalize_rel_path(rel_path)
    safe_type = str(analysis_type or "").strip()
    safe_fingerprint = str(fingerprint or "").strip()
    cache_key = (norm_path, safe_type, safe_fingerprint)

    cached = _mem_cache_get(cache_key)
    if cached is not None:
        return cached

    _ensure_initialized()
    conn = _connect()
    row = conn.execute(
        """
        SELECT content, fingerprint
        FROM cached_analyses
        WHERE rel_path = ? AND analysis_type = ?
        """,
        (norm_path, safe_type),
    ).fetchone()

    if row is None:
        return None

    stored_fingerprint = str(row["fingerprint"] or "").strip()
    if safe_fingerprint and stored_fingerprint != safe_fingerprint:
        return None

    content = str(row["content"] or "")
    _mem_cache_set(cache_key, content)
    return content


def set_cached_analysis(
    rel_path: str,
    analysis_type: str,
    content: str,
    fingerprint: str = "",
    model_name: str = "",
) -> None:
    norm_path = _normalize_rel_path(rel_path)
    safe_type = str(analysis_type or "").strip()
    safe_content = str(content or "")
    safe_fingerprint = str(fingerprint or "").strip()
    safe_model = str(model_name or "").strip()
    now = _utc_now_iso()

    _ensure_initialized()
    conn = _connect()
    conn.execute(
        """
        INSERT INTO cached_analyses (
            rel_path, analysis_type, fingerprint, content, model, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(rel_path, analysis_type) DO UPDATE SET
            fingerprint = excluded.fingerprint,
            content = excluded.content,
            model = excluded.model,
            updated_at = excluded.updated_at
        """,
        (norm_path, safe_type, safe_fingerprint, safe_content, safe_model, now, now),
    )

    _mem_cache_set((norm_path, safe_type, safe_fingerprint), safe_content)


def get_cached_analysis_types_for_paths(
    path_fingerprints: dict[str, str | dict[str, str]],
) -> dict[str, set[str]]:
    if not path_fingerprints:
        return {}

    normalized: dict[str, str | dict[str, str]] = {}
    for path, fingerprint in path_fingerprints.items():
        norm_path = _normalize_rel_path(path)
        if not norm_path:
            continue
        if isinstance(fingerprint, dict):
            normalized[norm_path] = {
                str(key or "").strip(): str(value or "").strip()
                for key, value in fingerprint.items()
                if str(key or "").strip()
            }
        else:
            normalized[norm_path] = str(fingerprint or "").strip()
    if not normalized:
        return {}

    _ensure_initialized()
    conn = _connect()
    paths = list(normalized.keys())
    result: dict[str, set[str]] = {}
    chunk_size = 500
    for offset in range(0, len(paths), chunk_size):
        batch = paths[offset : offset + chunk_size]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"""
            SELECT rel_path, analysis_type, fingerprint
            FROM cached_analyses
            WHERE rel_path IN ({placeholders})
            """,
            batch,
        ).fetchall()
        for row in rows:
            rel_path = _normalize_rel_path(row["rel_path"] or "")
            if not rel_path or rel_path not in normalized:
                continue
            analysis_type = str(row["analysis_type"] or "").strip()
            expected = normalized[rel_path]
            stored_fingerprint = str(row["fingerprint"] or "").strip()
            if isinstance(expected, dict):
                if stored_fingerprint != str(expected.get(analysis_type) or "").strip():
                    continue
            elif stored_fingerprint != expected:
                continue
            result.setdefault(rel_path, set()).add(analysis_type)
    return result
