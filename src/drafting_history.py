"""History of the Rédaction tab: past assistant analyses and draft versions.

Analyses (citations, proofreading, argument ideas) are stored with their full
results so they can be reopened later. Versions are snapshots of the draft,
taken periodically while writing and before bulk changes.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import config
from src.common import utc_now_iso

RUN_KINDS = ("citations", "proofread", "arguments")
VERSION_REASONS = ("auto", "before_proofread", "before_citations", "before_arguments", "before_clear", "before_restore")
MAX_RUNS = 100
MAX_VERSIONS = 40
MAX_PAYLOAD_CHARS = 2_000_000
MAX_VERSION_HTML_CHARS = 5_000_000
AUTO_VERSION_INTERVAL_SECONDS = 600

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_TAG_RE = re.compile(r"<[^>]+>")
_LOCK = threading.RLock()


def drafting_history_path() -> Path:
    return Path(config.STORAGE_DIR) / "drafting_history.sqlite3"


def _connect() -> sqlite3.Connection:
    path = drafting_history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Default rollback journal: the file stays self-contained for backups.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            excerpt TEXT NOT NULL DEFAULT '',
            summary TEXT NOT NULL DEFAULT '',
            result_count INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_runs_updated_at ON runs(updated_at DESC);

        CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            html TEXT NOT NULL,
            citation_start_number INTEGER NOT NULL DEFAULT 1,
            word_count INTEGER NOT NULL DEFAULT 0,
            preview TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL
        );
        """
    )
    return conn


def _preview(text: str, limit: int = 180) -> str:
    clean = " ".join(str(text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _html_text(html: str) -> str:
    # Block ends become spaces so words of adjacent paragraphs stay apart.
    spaced = re.sub(r"</(p|div|li|h[1-6]|blockquote)>|<br\s*/?>", " ", html, flags=re.IGNORECASE)
    return " ".join(html_lib.unescape(_TAG_RE.sub("", spaced)).split())


def is_valid_run_id(run_id: str) -> bool:
    return bool(_RUN_ID_RE.match(str(run_id or "")))


# ---- Analyses ----


def _run_row(row: sqlite3.Row, *, with_payload: bool) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "kind": row["kind"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "model": row["model"],
        "excerpt": row["excerpt"],
        "summary": row["summary"],
        "result_count": row["result_count"],
    }
    if with_payload:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {}
        item["payload"] = payload if isinstance(payload, dict) else {}
    return item


def save_run(
    run_id: str,
    *,
    kind: str,
    payload: dict[str, Any],
    model: str = "",
    excerpt: str = "",
    summary: str = "",
    result_count: int = 0,
) -> dict[str, Any]:
    """Create or update one analysis. Updating keeps its date and excerpt.

    Retrying failed sentences adds results to the analysis that produced them.
    """

    if not is_valid_run_id(run_id):
        raise ValueError("invalid_run_id")
    if kind not in RUN_KINDS:
        raise ValueError("invalid_kind")
    payload_json = json.dumps(payload, ensure_ascii=False)
    if len(payload_json) > MAX_PAYLOAD_CHARS:
        raise ValueError("payload_too_large")
    now = utc_now_iso()
    with _LOCK, closing(_connect()) as conn, conn:
        conn.execute(
            """
            INSERT INTO runs (id, kind, created_at, updated_at, model, excerpt, summary, result_count, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                updated_at = excluded.updated_at,
                model = excluded.model,
                summary = excluded.summary,
                result_count = excluded.result_count,
                payload_json = excluded.payload_json
            """,
            (
                run_id,
                kind,
                now,
                now,
                str(model or "")[:200],
                _preview(excerpt),
                _preview(summary, 240),
                max(0, int(result_count)),
                payload_json,
            ),
        )
        conn.execute(
            "DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY updated_at DESC LIMIT ?)",
            (MAX_RUNS,),
        )
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _run_row(row, with_payload=False)


def list_runs(limit: int = MAX_RUNS) -> list[dict[str, Any]]:
    with _LOCK, closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
    return [_run_row(row, with_payload=False) for row in rows]


def get_run(run_id: str) -> dict[str, Any] | None:
    if not is_valid_run_id(run_id):
        return None
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _run_row(row, with_payload=True) if row else None


def delete_run(run_id: str) -> bool:
    if not is_valid_run_id(run_id):
        return False
    with _LOCK, closing(_connect()) as conn, conn:
        return conn.execute("DELETE FROM runs WHERE id = ?", (run_id,)).rowcount > 0


# ---- Draft versions ----


def _version_row(row: sqlite3.Row, *, with_html: bool) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "created_at": row["created_at"],
        "reason": row["reason"],
        "citation_start_number": row["citation_start_number"],
        "word_count": row["word_count"],
        "preview": row["preview"],
    }
    if with_html:
        item["html"] = row["html"]
    return item


def save_version(
    html: str,
    citation_start_number: int = 1,
    reason: str = "auto",
    *,
    min_interval_seconds: int = 0,
) -> dict[str, Any] | None:
    """Store a snapshot of the draft; returns None when nothing was stored.

    An empty draft, a draft identical to the latest version or, with
    ``min_interval_seconds``, a version taken too soon is skipped.
    """

    if reason not in VERSION_REASONS:
        raise ValueError("invalid_reason")
    normalized = str(html or "")
    if len(normalized) > MAX_VERSION_HTML_CHARS:
        raise ValueError("draft_too_large")
    text = _html_text(normalized)
    if not text:
        return None
    content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    with _LOCK, closing(_connect()) as conn, conn:
        latest = conn.execute(
            "SELECT content_hash, created_at FROM versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if latest is not None:
            if latest["content_hash"] == content_hash:
                return None
            if min_interval_seconds > 0:
                try:
                    age = datetime.now(UTC) - datetime.fromisoformat(latest["created_at"])
                except ValueError:
                    age = None
                if age is not None and age.total_seconds() < min_interval_seconds:
                    return None
        cursor = conn.execute(
            """
            INSERT INTO versions (created_at, reason, html, citation_start_number, word_count, preview, content_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now_iso(),
                reason,
                normalized,
                max(1, int(citation_start_number)),
                len(text.split()),
                _preview(text),
                content_hash,
            ),
        )
        conn.execute(
            "DELETE FROM versions WHERE id NOT IN (SELECT id FROM versions ORDER BY id DESC LIMIT ?)",
            (MAX_VERSIONS,),
        )
        row = conn.execute("SELECT * FROM versions WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _version_row(row, with_html=False)


def list_versions(limit: int = MAX_VERSIONS) -> list[dict[str, Any]]:
    with _LOCK, closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM versions ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
    return [_version_row(row, with_html=False) for row in rows]


def get_version(version_id: int) -> dict[str, Any] | None:
    with _LOCK, closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM versions WHERE id = ?", (int(version_id),)).fetchone()
    return _version_row(row, with_html=True) if row else None


def delete_version(version_id: int) -> bool:
    with _LOCK, closing(_connect()) as conn, conn:
        return conn.execute("DELETE FROM versions WHERE id = ?", (int(version_id),)).rowcount > 0


def clear_history() -> dict[str, int]:
    with _LOCK, closing(_connect()) as conn, conn:
        runs_deleted = conn.execute("DELETE FROM runs").rowcount
        versions_deleted = conn.execute("DELETE FROM versions").rowcount
    return {"runs_deleted": runs_deleted, "versions_deleted": versions_deleted}
