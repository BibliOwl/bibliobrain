"""Persistent cache for external article-discovery API responses.

The cache deliberately stores only successful JSON responses.  It is shared
across application restarts so repeated searches and DOI enrichment do not
consume scarce upstream API quota.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import src.config as _cfg

_MEM_CACHE_MAX = 384
_MEM_CACHE: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()
_MEM_LOCK = threading.Lock()
_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


def _db_path() -> Path:
    return Path(_cfg.STORAGE_DIR) / "discovery_api_cache.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def _ensure_initialized() -> None:
    path_key = str(_db_path().resolve())
    if path_key in _INITIALIZED_PATHS:
        return
    with _INIT_LOCK:
        if path_key in _INITIALIZED_PATHS:
            return
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS discovery_api_cache (
                    cache_key TEXT PRIMARY KEY,
                    api_name TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    hit_count INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_discovery_api_cache_expires
                    ON discovery_api_cache(expires_at);
                """
            )
        _INITIALIZED_PATHS.add(path_key)


def _memory_key(cache_key: str) -> tuple[str, str]:
    return (str(_db_path().resolve()), str(cache_key or "").strip())


def get_cached_response(cache_key: str) -> Any | None:
    safe_key = str(cache_key or "").strip()
    if not safe_key:
        return None
    now = time.time()
    mem_key = _memory_key(safe_key)
    with _MEM_LOCK:
        cached = _MEM_CACHE.get(mem_key)
        if cached is not None:
            expires_at, payload_json = cached
            if expires_at > now:
                _MEM_CACHE.move_to_end(mem_key)
                try:
                    return json.loads(payload_json)
                except (TypeError, json.JSONDecodeError):
                    _MEM_CACHE.pop(mem_key, None)
            else:
                _MEM_CACHE.pop(mem_key, None)

    _ensure_initialized()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT payload_json, expires_at
            FROM discovery_api_cache
            WHERE cache_key = ?
            """,
            (safe_key,),
        ).fetchone()
        if row is None:
            return None
        expires_at = float(row["expires_at"] or 0.0)
        if expires_at <= now:
            conn.execute("DELETE FROM discovery_api_cache WHERE cache_key = ?", (safe_key,))
            return None
        payload_json = str(row["payload_json"] or "")
        conn.execute(
            "UPDATE discovery_api_cache SET hit_count = hit_count + 1 WHERE cache_key = ?",
            (safe_key,),
        )

    try:
        payload = json.loads(payload_json)
    except (TypeError, json.JSONDecodeError):
        return None
    with _MEM_LOCK:
        _MEM_CACHE[mem_key] = (expires_at, payload_json)
        _MEM_CACHE.move_to_end(mem_key)
        while len(_MEM_CACHE) > _MEM_CACHE_MAX:
            _MEM_CACHE.popitem(last=False)
    return payload


def set_cached_response(
    cache_key: str,
    api_name: str,
    payload: Any,
    *,
    ttl_seconds: float,
) -> None:
    safe_key = str(cache_key or "").strip()
    if not safe_key or ttl_seconds <= 0:
        return
    try:
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return
    now = time.time()
    expires_at = now + max(0.001, float(ttl_seconds))
    _ensure_initialized()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO discovery_api_cache (
                cache_key, api_name, payload_json, created_at, expires_at, hit_count
            ) VALUES (?, ?, ?, ?, ?, 0)
            ON CONFLICT(cache_key) DO UPDATE SET
                api_name = excluded.api_name,
                payload_json = excluded.payload_json,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at
            """,
            (safe_key, str(api_name or ""), payload_json, now, expires_at),
        )
        # Cheap bounded cleanup; it runs only after a successful network call.
        conn.execute("DELETE FROM discovery_api_cache WHERE expires_at <= ?", (now,))

    mem_key = _memory_key(safe_key)
    with _MEM_LOCK:
        _MEM_CACHE[mem_key] = (expires_at, payload_json)
        _MEM_CACHE.move_to_end(mem_key)
        while len(_MEM_CACHE) > _MEM_CACHE_MAX:
            _MEM_CACHE.popitem(last=False)


def clear_memory_cache() -> None:
    """Test/runtime helper; persistent rows are intentionally preserved."""
    with _MEM_LOCK:
        _MEM_CACHE.clear()
