"""
Persistent article discovery history storage (SQLite).

Stores each completed discovery search with request context, response payload,
and lightweight debug metadata for later inspection.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import src.config as _cfg
from src.common import json_object
from src.common import utc_now_iso as _utc_now_iso

DB_DIR = Path(_cfg.STORAGE_DIR)
DB_PATH = DB_DIR / "discovery_history.sqlite3"


def _normalize_preview(text: str, limit: int = 180) -> str:
    clean = " ".join((text or "").split()).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "..."


def _infer_title(*, mode: str, input_text: str) -> str:
    clean = " ".join((input_text or "").split()).strip()
    prefix = (
        "Recommandations" if str(mode or "").strip().lower() == "recommendations" else "Recherche"
    )
    if not clean:
        return prefix
    if len(clean) <= 72:
        return clean
    return clean[:71] + "..."


def _connect() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def init_discovery_history_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS searches (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'search',
                input_text TEXT NOT NULL DEFAULT '',
                notebook_id TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                max_results INTEGER NOT NULL DEFAULT 10,
                ok INTEGER NOT NULL DEFAULT 1,
                result_count INTEGER NOT NULL DEFAULT 0,
                elapsed_ms REAL NOT NULL DEFAULT 0,
                status_msg TEXT NOT NULL DEFAULT '',
                request_json TEXT NOT NULL DEFAULT '{}',
                response_json TEXT NOT NULL DEFAULT '{}',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_discovery_searches_updated_at
                ON searches(updated_at DESC);
            """
        )


def save_search(
    *,
    mode: str,
    input_text: str,
    notebook_id: str = "",
    model: str = "",
    max_results: int = 10,
    ok: bool = True,
    result_count: int = 0,
    elapsed_ms: float = 0.0,
    status_msg: str = "",
    request_payload: dict[str, Any] | None = None,
    response_payload: dict[str, Any] | None = None,
    meta: dict[str, Any] | None = None,
    search_id: str = "",
) -> dict[str, Any]:
    """Store a search; with the id of an existing one ("Search again"), its
    results are replaced in place and its title (possibly renamed) is kept."""
    now = _utc_now_iso()
    request_json = json.dumps(request_payload or {}, ensure_ascii=False)
    response_json = json.dumps(response_payload or {}, ensure_ascii=False)
    meta_json = json.dumps(meta or {}, ensure_ascii=False)
    existing_id = str(search_id or "").strip()
    if existing_id and get_search(existing_id) is not None:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE searches SET
                    mode = ?, input_text = ?, notebook_id = ?, model = ?, max_results = ?,
                    ok = ?, result_count = ?, elapsed_ms = ?, status_msg = ?,
                    request_json = ?, response_json = ?, meta_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(mode or "").strip() or "search",
                    input_text or "",
                    notebook_id or "",
                    model or "",
                    int(max_results or 10),
                    1 if ok else 0,
                    int(result_count or 0),
                    float(elapsed_ms or 0.0),
                    status_msg or "",
                    request_json,
                    response_json,
                    meta_json,
                    now,
                    existing_id,
                ),
            )
        return get_search(existing_id) or {"id": existing_id}
    search_id = str(uuid.uuid4())
    title = _infer_title(mode=mode, input_text=input_text)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO searches (
                id, title, mode, input_text, notebook_id, model, max_results,
                ok, result_count, elapsed_ms, status_msg,
                request_json, response_json, meta_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                search_id,
                title,
                str(mode or "").strip() or "search",
                input_text or "",
                notebook_id or "",
                model or "",
                int(max_results or 10),
                1 if ok else 0,
                int(result_count or 0),
                float(elapsed_ms or 0.0),
                status_msg or "",
                request_json,
                response_json,
                meta_json,
                now,
                now,
            ),
        )
    return get_search(search_id) or {"id": search_id, "title": title}


def count_searches() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM searches").fetchone()
    return int(row["total"] if row is not None else 0)


def list_searches(limit: int = 150, offset: int = 0) -> list[dict[str, Any]]:
    n = max(1, min(int(limit or 150), 500))
    start = max(0, int(offset or 0))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, title, mode, input_text, notebook_id, model, max_results,
                ok, result_count, elapsed_ms, status_msg, meta_json,
                created_at, updated_at
            FROM searches
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (n, start),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        meta = json_object(row["meta_json"])
        items.append(
            {
                "id": row["id"],
                "title": row["title"] or "",
                "mode": row["mode"] or "search",
                "input_preview": _normalize_preview(row["input_text"] or ""),
                "notebook_id": row["notebook_id"] or "",
                "model": row["model"] or "",
                "max_results": int(row["max_results"] or 10),
                "ok": bool(row["ok"]),
                "result_count": int(row["result_count"] or 0),
                "elapsed_ms": float(row["elapsed_ms"] or 0.0),
                "status_msg": row["status_msg"] or "",
                "providers_used": meta.get("providers_used") or [],
                "created": row["created_at"],
                "updated": row["updated_at"],
            }
        )
    return items


def get_search(search_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT
                id, title, mode, input_text, notebook_id, model, max_results,
                ok, result_count, elapsed_ms, status_msg,
                request_json, response_json, meta_json,
                created_at, updated_at
            FROM searches
            WHERE id = ?
            """,
            (search_id,),
        ).fetchone()
    if row is None:
        return None
    request_payload = json_object(row["request_json"])
    response_payload = json_object(row["response_json"])
    meta = json_object(row["meta_json"])
    return {
        "search": {
            "id": row["id"],
            "title": row["title"] or "",
            "mode": row["mode"] or "search",
            "input_text": row["input_text"] or "",
            "notebook_id": row["notebook_id"] or "",
            "model": row["model"] or "",
            "max_results": int(row["max_results"] or 10),
            "ok": bool(row["ok"]),
            "result_count": int(row["result_count"] or 0),
            "elapsed_ms": float(row["elapsed_ms"] or 0.0),
            "status_msg": row["status_msg"] or "",
            "created": row["created_at"],
            "updated": row["updated_at"],
            "meta": meta,
        },
        "request": request_payload,
        "response": response_payload,
    }


def rename_search(search_id: str, title: str) -> bool:
    clean = " ".join(str(title or "").split())[:200]
    if not clean:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE searches SET title = ?, updated_at = ? WHERE id = ?",
            (clean, _utc_now_iso(), search_id),
        )
    return (cur.rowcount or 0) > 0


def delete_search(search_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM searches WHERE id = ?", (search_id,))
    return (cur.rowcount or 0) > 0


def clear_all_discovery_history() -> dict[str, int]:
    with _connect() as conn:
        searches_deleted = conn.execute("DELETE FROM searches").rowcount or 0
    return {"searches_deleted": int(searches_deleted)}


init_discovery_history_db()
