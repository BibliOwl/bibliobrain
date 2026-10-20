"""Documents the user keeps out of RAG retrieval.

Exclusion is applied at search time only: the chunks stay in the index, so a
paper can be excluded or brought back instantly, without re-embedding it, and
it stays visible in the library (which lists indexed documents).

The list is stored per library folder, keyed by the same ``rel_path`` the
index stores in each chunk's metadata.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from src import config
from src.atomic_io import atomic_write_json

_VERSION = 1
_LOCK = threading.RLock()
_cache: tuple[str, float, dict[str, list[str]]] | None = None


def exclusions_path() -> Path:
    return Path(config.STORAGE_DIR) / "rag_exclusions.json"


def normalize_rel_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").lower()


def _library_key() -> str:
    data_path = str(config.DATA_PATH or "").strip()
    if not data_path:
        return ""
    return os.path.normcase(os.path.realpath(data_path))


def _load_all() -> dict[str, list[str]]:
    global _cache
    path = exclusions_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    key = str(path)
    if _cache is not None and _cache[0] == key and _cache[1] == mtime:
        return _cache[2]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    libraries = payload.get("libraries") if isinstance(payload, dict) else None
    result: dict[str, list[str]] = {}
    if isinstance(libraries, dict):
        for library, paths in libraries.items():
            if isinstance(paths, list):
                result[str(library)] = [str(p) for p in paths if str(p or "").strip()]
    _cache = (key, mtime, result)
    return result


def excluded_rel_paths() -> list[str]:
    """Excluded paths of the current library, as stored in the index."""
    with _LOCK:
        return list(_load_all().get(_library_key(), []))


def excluded_path_keys() -> frozenset[str]:
    """Normalized excluded paths, for comparisons with chunk metadata."""
    return frozenset(normalize_rel_path(path) for path in excluded_rel_paths())


def is_metadata_excluded(metadata: dict[str, Any], excluded_keys: frozenset[str]) -> bool:
    if not excluded_keys:
        return False
    return normalize_rel_path(metadata.get("rel_path")) in excluded_keys


def set_rel_paths_excluded(paths: Iterable[str], excluded: bool) -> dict[str, int]:
    """Add or remove paths from the current library's exclusion list."""
    requested = [str(path or "").strip() for path in paths]
    requested = [path for path in requested if path]
    global _cache
    library = _library_key()
    if not library:
        raise ValueError("no_library")
    with _LOCK:
        all_libraries = {key: list(value) for key, value in _load_all().items()}
        current = all_libraries.get(library, [])
        current_keys = {normalize_rel_path(path) for path in current}
        changed = 0
        if excluded:
            for path in requested:
                key = normalize_rel_path(path)
                if key not in current_keys:
                    current.append(path)
                    current_keys.add(key)
                    changed += 1
        else:
            removed = {normalize_rel_path(path) for path in requested}
            kept = [path for path in current if normalize_rel_path(path) not in removed]
            changed = len(current) - len(kept)
            current = kept
        if current:
            all_libraries[library] = sorted(current, key=normalize_rel_path)
        else:
            all_libraries.pop(library, None)
        if changed:
            atomic_write_json(
                exclusions_path(),
                {"version": _VERSION, "libraries": dict(sorted(all_libraries.items()))},
            )
            # Two writes can share one modification time: never trust the cache after one.
            _cache = None
        return {"changed": changed, "excluded_count": len(current)}
