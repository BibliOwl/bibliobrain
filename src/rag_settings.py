from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from src import config
from src.atomic_io import atomic_write_json

_DEFAULT_CHUNK_SIZE = 1000
_MEMORY_PAYLOAD: dict[str, Any] = {}
_RAG_SETTINGS_LOCK = threading.RLock()


def _rag_settings_path() -> Path:
    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    return Path(config.STORAGE_DIR) / "rag_settings.json"


def _normalize_chunk_size(value: Any) -> int:
    try:
        chunk_size = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_CHUNK_SIZE
    return chunk_size if chunk_size > 0 else _DEFAULT_CHUNK_SIZE


def _load_rag_settings_payload() -> dict[str, Any]:
    with _RAG_SETTINGS_LOCK:
        try:
            path = _rag_settings_path()
            if not path.exists():
                return dict(_MEMORY_PAYLOAD)
        except OSError:
            return dict(_MEMORY_PAYLOAD)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            if _MEMORY_PAYLOAD:
                return dict(_MEMORY_PAYLOAD)
            return {}
        return payload if isinstance(payload, dict) else {}


def _save_rag_settings_payload(payload: dict[str, Any]) -> dict[str, Any]:
    global _MEMORY_PAYLOAD
    with _RAG_SETTINGS_LOCK:
        current = _load_rag_settings_payload()
        current.update(payload or {})
        _MEMORY_PAYLOAD = dict(current)
        try:
            path = _rag_settings_path()
            atomic_write_json(path, current)
        except OSError:
            return current
        return current


def get_rag_settings() -> dict[str, Any]:
    payload = _load_rag_settings_payload()
    return {
        "chunk_size": _normalize_chunk_size(payload.get("chunk_size", _DEFAULT_CHUNK_SIZE)),
    }


def get_persisted_chunk_size(default: int | None = None) -> int:
    fallback = _normalize_chunk_size(default if default is not None else _DEFAULT_CHUNK_SIZE)
    payload = _load_rag_settings_payload()
    return _normalize_chunk_size(payload.get("chunk_size", fallback))


def set_persisted_chunk_size(chunk_size: Any) -> int:
    normalized = _normalize_chunk_size(chunk_size)
    _save_rag_settings_payload({"chunk_size": normalized})
    return normalized
