from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import config
from src.atomic_io import atomic_write_json

_DRAFT_VERSION = 1
_MAX_DRAFT_HTML_CHARS = 5_000_000
_LOCK = threading.RLock()


def drafting_document_path() -> Path:
    return Path(config.STORAGE_DIR) / "drafting_document.json"


def load_drafting_document() -> dict[str, Any] | None:
    path = drafting_document_path()
    with _LOCK:
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
    if not isinstance(payload, dict) or payload.get("version") != _DRAFT_VERSION:
        return None
    html = payload.get("html")
    if not isinstance(html, str) or len(html) > _MAX_DRAFT_HTML_CHARS:
        return None
    try:
        citation_start_number = max(1, int(payload.get("citation_start_number", 1)))
    except (TypeError, ValueError):
        citation_start_number = 1
    return {
        "html": html,
        "citation_start_number": citation_start_number,
        "updated_at": str(payload.get("updated_at") or ""),
    }


def save_drafting_document(html: str, citation_start_number: int = 1) -> dict[str, Any]:
    normalized_html = str(html or "")
    if len(normalized_html) > _MAX_DRAFT_HTML_CHARS:
        raise ValueError("draft_too_large")
    document = {
        "html": normalized_html,
        "citation_start_number": max(1, int(citation_start_number)),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    with _LOCK:
        atomic_write_json(
            drafting_document_path(),
            {"version": _DRAFT_VERSION, **document},
        )
    return document


def clear_drafting_document() -> None:
    with _LOCK:
        try:
            drafting_document_path().unlink()
        except FileNotFoundError:
            pass
