"""
Persistent chat history storage (SQLite).

Stores conversations and messages for the main Chat tab, across sessions.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from pathlib import Path

import src.config as _cfg
from src.common import json_object
from src.common import utc_now_iso as _utc_now_iso

logger = logging.getLogger(__name__)

DB_DIR = Path(_cfg.STORAGE_DIR)
DB_PATH = DB_DIR / "chat_history.sqlite3"
_CONNECTION_LOCAL = threading.local()


def _normalize_preview(text: str, limit: int = 180) -> str:
    clean = " ".join((text or "").split()).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1] + "…"


def _infer_title(seed: str) -> str:
    clean = " ".join((seed or "").split()).strip()
    if not clean:
        return "Nouveau chat"
    if len(clean) <= 68:
        return clean
    return clean[:67] + "…"


def _connect() -> sqlite3.Connection:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    resolved_path = str(Path(DB_PATH).resolve())
    existing = getattr(_CONNECTION_LOCAL, "connection", None)
    existing_path = getattr(_CONNECTION_LOCAL, "path", "")
    if existing is not None and existing_path == resolved_path:
        return existing
    if existing is not None:
        existing.close()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    _CONNECTION_LOCAL.connection = conn
    _CONNECTION_LOCAL.path = resolved_path
    return conn


def init_chat_history_db() -> None:
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                last_message_preview TEXT NOT NULL DEFAULT '',
                last_role TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                meta_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_conversation_id
                ON messages(conversation_id, id);

            CREATE INDEX IF NOT EXISTS idx_conversations_updated_at
                ON conversations(updated_at DESC);
            """
        )


def create_conversation(title: str = "", seed_message: str = "") -> dict:
    conv_id = str(uuid.uuid4())
    now = _utc_now_iso()
    final_title = (title or "").strip() or _infer_title(seed_message)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (conv_id, final_title, now, now),
        )
    return {
        "id": conv_id,
        "title": final_title,
        "created": now,
        "updated": now,
        "message_count": 0,
        "last_message_preview": "",
        "last_role": "",
    }


def get_conversation(chat_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, title, created_at, updated_at, message_count, last_message_preview, last_role
            FROM conversations
            WHERE id = ?
            """,
            (chat_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "title": row["title"],
        "created": row["created_at"],
        "updated": row["updated_at"],
        "message_count": int(row["message_count"] or 0),
        "last_message_preview": row["last_message_preview"] or "",
        "last_role": row["last_role"] or "",
    }


def rename_conversation(chat_id: str, title: str) -> bool:
    clean_title = " ".join(str(title or "").split()).strip()[:120]
    if not clean_title:
        return False
    with _connect() as conn:
        cursor = conn.execute(
            "UPDATE conversations SET title = ? WHERE id = ?",
            (clean_title, chat_id),
        )
    return cursor.rowcount > 0


def ensure_conversation(chat_id: str | None, seed_message: str = "") -> tuple[dict, bool]:
    raw_id = (chat_id or "").strip()
    if raw_id:
        existing = get_conversation(raw_id)
        if existing is not None:
            return existing, False
    created = create_conversation(seed_message=seed_message)
    return created, True


def add_message(
    chat_id: str,
    role: str,
    content: str,
    model: str = "",
    meta: dict | None = None,
) -> None:
    now = _utc_now_iso()
    safe_meta = meta or {}
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO messages (conversation_id, role, content, model, meta_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, role, content, model or "", json.dumps(safe_meta, ensure_ascii=False), now),
        )
        conn.execute(
            """
            UPDATE conversations
            SET
                updated_at = ?,
                message_count = message_count + 1,
                last_message_preview = ?,
                last_role = ?
            WHERE id = ?
            """,
            (now, _normalize_preview(content), role, chat_id),
        )


def count_conversations() -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM conversations").fetchone()
    return int(row["total"] if row is not None else 0)


def list_conversations(limit: int = 150, offset: int = 0) -> list[dict]:
    n = max(1, min(int(limit or 150), 500))
    start = max(0, int(offset or 0))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, title, created_at, updated_at, message_count, last_message_preview, last_role
            FROM conversations
            ORDER BY updated_at DESC
            LIMIT ? OFFSET ?
            """,
            (n, start),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "created": row["created_at"],
            "updated": row["updated_at"],
            "message_count": int(row["message_count"] or 0),
            "last_message_preview": row["last_message_preview"] or "",
            "last_role": row["last_role"] or "",
        }
        for row in rows
    ]


def get_conversation_messages(
    chat_id: str, limit: int | None = None, offset: int = 0
) -> list[dict]:
    limit_n: int | None = None
    if limit is not None:
        try:
            limit_n = max(1, min(int(limit), 300))
        except (TypeError, ValueError):
            limit_n = None
    start = max(0, int(offset or 0))

    with _connect() as conn:
        if limit_n is None:
            rows = conn.execute(
                """
                SELECT role, content, model, meta_json, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY id ASC
                """,
                (chat_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT role, content, model, meta_json, created_at
                FROM (
                    SELECT id, role, content, model, meta_json, created_at
                    FROM messages
                    WHERE conversation_id = ?
                    ORDER BY id DESC
                    LIMIT ? OFFSET ?
                ) AS recent
                ORDER BY id ASC
                """,
                (chat_id, limit_n, start),
            ).fetchall()
    return [
        {
            "role": row["role"],
            "content": row["content"],
            "model": row["model"] or "",
            "meta": json_object(row["meta_json"]),
            "ts": row["created_at"],
        }
        for row in rows
    ]


def get_conversation_with_messages(
    chat_id: str, limit: int | None = None, offset: int = 0
) -> dict | None:
    chat = get_conversation(chat_id)
    if chat is None:
        return None
    return {
        "chat": chat,
        "messages": get_conversation_messages(chat_id, limit=limit, offset=offset),
    }


def delete_conversation(chat_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM conversations WHERE id = ?", (chat_id,))
    return (cur.rowcount or 0) > 0


def clear_all_chat_history() -> dict[str, int]:
    with _connect() as conn:
        messages_deleted = conn.execute("DELETE FROM messages").rowcount or 0
        conversations_deleted = conn.execute("DELETE FROM conversations").rowcount or 0
        try:
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'messages'")
        except Exception:
            logger.debug("Unable to reset the SQLite sequence for messages", exc_info=True)
    return {
        "messages_deleted": int(messages_deleted),
        "conversations_deleted": int(conversations_deleted),
    }


init_chat_history_db()
