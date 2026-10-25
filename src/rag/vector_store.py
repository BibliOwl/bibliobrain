"""Chroma vector store management, manifest handling, and index status."""

from __future__ import annotations

import contextlib
import gc
import hashlib
import json
import logging
import os
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import chromadb.api.client
from chromadb.config import Settings
from langchain_chroma import Chroma
from langchain_core.documents import Document

from src.atomic_io import atomic_write_json
from src.config import CHROMA_PATH
from src.document_formats import document_index_variant, resolve_document_metadata
from src.document_guard import max_document_bytes
from src.llm_provider import current_embedding_profile_label, required_embedding_model_name
from src.rag_settings import get_persisted_chunk_size
from src.rel_paths import normalize_rel_path
from src.unicode_utils import json_dumps_utf8
from src.version import get_app_version
from src.visual_analysis import index_visual_signature

from . import state
from .document_loader import _build_chunks_from_pdfs, _sanitize_documents
from .lexical_index import lexical_db_path
from .lexical_index import replace_paths as replace_lexical_paths
from .zotero_integration import (
    _dedupe_keep_order,
    _file_signature,
    _filter_active_zotero_pdf_entries,
    _index_signature,
    _legacy_file_signature,
    _normalize_text,
    _refresh_zotero_library_cache,
)

_INDEX_PROFILES_DIR = "indexes"
_ACTIVE_PROFILE_FILE = "active_profile.json"
_EQUIVALENT_MODELS_FILE = "equivalent_models.json"
_INDEX_PROFILE_VERSION = 1
_logger = logging.getLogger(__name__)


def _chroma_client_settings() -> Settings:
    """Keep Chroma telemetry disabled regardless of dependency defaults."""
    return Settings(anonymized_telemetry=False)


def get_vector_db_root() -> str:
    return CHROMA_PATH


def _index_profiles_root() -> str:
    return os.path.join(get_vector_db_root(), _INDEX_PROFILES_DIR)


def _active_profile_state_path() -> str:
    return os.path.join(get_vector_db_root(), _ACTIVE_PROFILE_FILE)


def _profile_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _normalize_text(value)).strip("-")
    return slug[:40] or "embedding"


def _current_chunk_size() -> int:
    return get_persisted_chunk_size(1000)


def build_vector_index_profile(
    *,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    resolved_chunk_size = _current_chunk_size()
    if chunk_size is not None:
        try:
            resolved_chunk_size = max(1, int(chunk_size))
        except (TypeError, ValueError):
            resolved_chunk_size = _current_chunk_size()
    resolved_embedding_model = (
        str(embedding_model or _current_embedding_profile()).strip() or "unknown"
    )
    normalized_embedding_model = _normalize_text(resolved_embedding_model) or "unknown"
    digest = hashlib.sha1(
        f"{normalized_embedding_model}|{resolved_chunk_size}|{_INDEX_PROFILE_VERSION}".encode()
    ).hexdigest()[:12]
    profile_id = (
        f"{_profile_slug(normalized_embedding_model)}"
        f"__chunk{resolved_chunk_size}"
        f"__v{_INDEX_PROFILE_VERSION}"
        f"__{digest}"
    )
    return {
        "profile_id": profile_id,
        "embedding_model": resolved_embedding_model,
        "chunk_size": resolved_chunk_size,
        "schema_version": _INDEX_PROFILE_VERSION,
        "path": os.path.join(_index_profiles_root(), profile_id),
    }


def _load_active_vector_index_profile() -> dict[str, Any]:
    path = _active_profile_state_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        _logger.debug(
            "Recovered from an error in _load_active_vector_index_profile.", exc_info=True
        )
        return {}
    return payload if isinstance(payload, dict) else {}


def set_active_vector_index_profile(
    *,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
    db_path: str | None = None,
    exists: bool | None = None,
) -> dict[str, Any]:
    profile = build_vector_index_profile(chunk_size=chunk_size, embedding_model=embedding_model)
    resolved_profile = (
        resolve_vector_index_profile(
            chunk_size=profile["chunk_size"],
            embedding_model=profile["embedding_model"],
        )
        if db_path is None
        else {**profile, "path": str(db_path), "source": "explicit"}
    )
    resolved_path = str(resolved_profile.get("path") or profile["path"])
    detected_exists = _db_path_has_vector_artifacts(resolved_path)
    incomplete_path = os.path.join(resolved_path, state._INCOMPLETE_INDEX_FILE)
    ready = bool(
        detected_exists
        and os.path.exists(os.path.join(resolved_path, "chroma.sqlite3"))
        and not os.path.exists(incomplete_path)
    )
    payload = {
        **profile,
        "path": resolved_path,
        "exists": bool(detected_exists if exists is None else (exists or detected_exists)),
        "ready": ready,
        "reused": bool(db_path is None and ready),
        "source": str(resolved_profile.get("source") or "profile"),
        "updated_at": time.time(),
    }
    os.makedirs(get_vector_db_root(), exist_ok=True)
    with open(_active_profile_state_path(), "w", encoding="utf-8") as handle:
        handle.write(json_dumps_utf8(payload, ensure_ascii=False, indent=2))
    return payload


def _db_path_has_vector_artifacts(path: str) -> bool:
    normalized = str(path or "").strip()
    if not normalized or not os.path.isdir(normalized):
        return False
    sqlite_path = os.path.join(normalized, "chroma.sqlite3")
    manifest_path = os.path.join(normalized, state._MANIFEST_FILE)
    incomplete_path = os.path.join(normalized, state._INCOMPLETE_INDEX_FILE)
    if (
        os.path.exists(sqlite_path)
        or os.path.exists(manifest_path)
        or os.path.exists(incomplete_path)
    ):
        return True
    try:
        for entry in os.scandir(normalized):
            if entry.name in {_INDEX_PROFILES_DIR, _ACTIVE_PROFILE_FILE}:
                continue
            return True
    except Exception:
        _logger.debug("Recovered from an error in _db_path_has_vector_artifacts.", exc_info=True)
        return False
    return False


def _legacy_vector_db_path() -> str:
    return get_vector_db_root()


def _equivalent_models(db_path: str) -> set[str]:
    """Model labels verified to reproduce this index's vectors (see embedding_equivalence)."""
    try:
        with open(os.path.join(db_path, _EQUIVALENT_MODELS_FILE), encoding="utf-8") as handle:
            labels = json.load(handle).get("labels", [])
    except (OSError, ValueError, AttributeError):
        return set()
    return {_normalize_text(str(label)) for label in labels if str(label or "").strip()}


def register_equivalent_model(db_path: str, labels: list[str]) -> None:
    known = _equivalent_models(db_path)
    merged = sorted(known | {_normalize_text(label) for label in labels if label.strip()})
    atomic_write_json(Path(db_path) / _EQUIVALENT_MODELS_FILE, {"labels": merged})


def manifest_model_matches(db_path: str, embedding_model: str) -> bool:
    """Whether vectors of ``embedding_model`` fit the index at ``db_path``.

    True for the model that built it, for any model registered as giving the
    same vectors under another name or service, and when nothing is recorded.
    """
    manifest_model = _load_manifest_embedding_model(db_path=db_path)
    if not manifest_model:
        return True
    wanted = _normalize_text(embedding_model)
    return _normalize_text(manifest_model) == wanted or wanted in _equivalent_models(db_path)


def _registered_equivalent_profile(profile: dict[str, Any]) -> str:
    root = _index_profiles_root()
    if not os.path.isdir(root):
        return ""
    wanted = _normalize_text(str(profile["embedding_model"]))
    try:
        entries = sorted(os.scandir(root), key=lambda item: item.name)
    except OSError:
        return ""
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if wanted not in _equivalent_models(entry.path):
            continue
        if _load_manifest_chunk_size(db_path=entry.path) != int(profile["chunk_size"]):
            continue
        if os.path.exists(os.path.join(entry.path, "chroma.sqlite3")):
            return entry.path
    return ""


def resolve_vector_index_profile(
    *,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> dict[str, Any]:
    profile = build_vector_index_profile(chunk_size=chunk_size, embedding_model=embedding_model)
    candidate_path = str(profile["path"])
    if _db_path_has_vector_artifacts(candidate_path):
        return {**profile, "path": candidate_path, "exists": True, "source": "profile"}

    legacy_path = _legacy_vector_db_path()
    if candidate_path != legacy_path and _db_path_has_vector_artifacts(legacy_path):
        legacy_chunk_size = _load_manifest_chunk_size(db_path=legacy_path)
        legacy_embedding_model = _load_manifest_embedding_model(db_path=legacy_path)
        if legacy_chunk_size == int(profile["chunk_size"]) and _normalize_text(
            legacy_embedding_model
        ) == _normalize_text(str(profile["embedding_model"])):
            return {**profile, "path": legacy_path, "exists": True, "source": "legacy_root"}

    equivalent_path = _registered_equivalent_profile(profile)
    if equivalent_path:
        return {**profile, "path": equivalent_path, "exists": True, "source": "equivalent"}

    return {**profile, "path": candidate_path, "exists": False, "source": "profile"}


def list_vector_index_profiles() -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    root = _index_profiles_root()
    if os.path.isdir(root):
        try:
            for entry in sorted(os.scandir(root), key=lambda item: item.name):
                # Dot folders are index-transfer staging areas and backups.
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                manifest_chunk_size = _load_manifest_chunk_size(db_path=entry.path)
                manifest_embedding_model = _load_manifest_embedding_model(db_path=entry.path)
                has_sqlite = os.path.exists(os.path.join(entry.path, "chroma.sqlite3"))
                incomplete = os.path.exists(os.path.join(entry.path, state._INCOMPLETE_INDEX_FILE))
                profiles.append(
                    {
                        "profile_id": entry.name,
                        "path": entry.path,
                        "exists": _db_path_has_vector_artifacts(entry.path),
                        "ready": bool(has_sqlite and not incomplete),
                        "status": (
                            "incomplete" if incomplete else ("present" if has_sqlite else "partial")
                        ),
                        "embedding_model": manifest_embedding_model,
                        "chunk_size": manifest_chunk_size,
                        "source": "profile",
                    }
                )
        except Exception:
            _logger.debug("Unable to describe the active embedding profile", exc_info=True)

    legacy_path = _legacy_vector_db_path()
    if legacy_path != root and _db_path_has_vector_artifacts(legacy_path):
        legacy_has_sqlite = os.path.exists(os.path.join(legacy_path, "chroma.sqlite3"))
        legacy_incomplete = os.path.exists(os.path.join(legacy_path, state._INCOMPLETE_INDEX_FILE))
        profiles.append(
            {
                "profile_id": "legacy-root",
                "path": legacy_path,
                "exists": True,
                "ready": bool(legacy_has_sqlite and not legacy_incomplete),
                "status": (
                    "incomplete"
                    if legacy_incomplete
                    else ("present" if legacy_has_sqlite else "partial")
                ),
                "embedding_model": _load_manifest_embedding_model(db_path=legacy_path),
                "chunk_size": _load_manifest_chunk_size(db_path=legacy_path),
                "source": "legacy_root",
            }
        )

    return profiles


def _resolve_db_path(
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> str:
    if db_path is not None:
        return str(db_path)
    return str(
        resolve_vector_index_profile(
            chunk_size=chunk_size,
            embedding_model=embedding_model,
        )["path"]
    )


def _close_chroma_store(store: Any) -> None:
    if store is None:
        return
    client = getattr(store, "_client", None)
    try:
        if client is not None and hasattr(client, "close"):
            client.close()
    except Exception:
        _logger.debug("Unable to close the Chroma client", exc_info=True)
    try:
        chromadb.api.client.SharedSystemClient.clear_system_cache()
    except Exception:
        _logger.debug("Unable to clear the Chroma system cache", exc_info=True)


def _incomplete_index_path(
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> str:
    return os.path.join(
        _resolve_db_path(
            db_path=db_path,
            chunk_size=chunk_size,
            embedding_model=embedding_model,
        ),
        state._INCOMPLETE_INDEX_FILE,
    )


def _mark_index_incomplete(
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> None:
    resolved_path = _resolve_db_path(
        db_path=db_path,
        chunk_size=chunk_size,
        embedding_model=embedding_model,
    )
    os.makedirs(resolved_path, exist_ok=True)
    with open(
        _incomplete_index_path(
            db_path=resolved_path,
        ),
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write("incomplete\n")


def _clear_incomplete_index_marker(
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> None:
    try:
        os.remove(
            _incomplete_index_path(
                db_path=db_path,
                chunk_size=chunk_size,
                embedding_model=embedding_model,
            )
        )
    except FileNotFoundError:
        pass


def _has_incomplete_index_marker(
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> bool:
    return os.path.exists(
        _incomplete_index_path(
            db_path=db_path,
            chunk_size=chunk_size,
            embedding_model=embedding_model,
        )
    )


def _try_recover_stale_index_marker(
    embeddings: Any,
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> tuple[bool, str]:
    """Allow loading an existing Chroma collection when the incomplete marker is stale."""
    resolved_path = _resolve_db_path(
        db_path=db_path,
        chunk_size=chunk_size,
        embedding_model=embedding_model,
    )
    candidate = None
    try:
        candidate = Chroma(
            persist_directory=resolved_path,
            embedding_function=embeddings,
            client_settings=_chroma_client_settings(),
        )
        count = candidate._collection.count()
    except Exception as exc:
        _logger.debug("Recovered from an error in _try_recover_stale_index_marker.", exc_info=True)
        return False, f"index inaccessible: {exc}"
    finally:
        _close_chroma_store(candidate)

    if count <= 0:
        return False, "index vide"

    try:
        _clear_incomplete_index_marker(db_path=resolved_path)
    except Exception:
        _logger.debug("Unable to clear the incomplete-index marker", exc_info=True)
    return True, f"Base existante recuperee ({count} chunks)."


def _manifest_path(
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> str:
    return os.path.join(
        _resolve_db_path(
            db_path=db_path,
            chunk_size=chunk_size,
            embedding_model=embedding_model,
        ),
        state._MANIFEST_FILE,
    )


def _load_manifest_entries(
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> dict[str, dict[str, Any]]:
    path = _manifest_path(db_path=db_path, chunk_size=chunk_size, embedding_model=embedding_model)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        entries = payload.get("entries", {})
        if isinstance(entries, dict):
            loaded = {str(key): value for key, value in entries.items() if isinstance(value, dict)}
            return _backfill_legacy_chunk_counts(os.path.dirname(path), loaded)
    except Exception:
        _logger.warning("Recovered from an error in _load_manifest_entries.", exc_info=True)
        return {}
    return {}


def _backfill_legacy_chunk_counts(
    index_dir: str,
    entries: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Give pre-v3 manifest entries the chunk count they never recorded.

    Version 3 treats an entry without chunks as not indexed, so a v2 manifest
    made every document "modified" and the next reload re-embedded the whole
    library. The lexical index holds one row per indexed chunk: counting them
    per file restores the real count without touching any embedding. A file
    that really produced no chunk stays at 0 and is retried, as v3 intends.
    """
    legacy = [rel_path for rel_path, entry in entries.items() if "chunk_count" not in entry]
    if not legacy:
        return entries
    counts: dict[str, int] = {}
    lexical_path = lexical_db_path(index_dir)
    if os.path.exists(lexical_path):
        try:
            import sqlite3

            with contextlib.closing(
                sqlite3.connect(f"file:{lexical_path}?mode=ro", uri=True, timeout=15.0)
            ) as connection:
                for rel_path, count in connection.execute(
                    "SELECT rel_path, COUNT(*) FROM lexical_chunks GROUP BY rel_path"
                ):
                    counts[normalize_rel_path(str(rel_path or ""))] = int(count)
        except Exception:
            _logger.warning("Unable to read chunk counts from the lexical index", exc_info=True)
            counts = {}
    for rel_path in legacy:
        entry = dict(entries[rel_path])
        # Without the lexical index, keep the pre-v3 meaning: listed = indexed.
        entry["chunk_count"] = counts.get(normalize_rel_path(rel_path), 0) if counts else 1
        entries[rel_path] = entry
    return entries


def _load_manifest_chunk_size(
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> int | None:
    path = _manifest_path(db_path=db_path, chunk_size=chunk_size, embedding_model=embedding_model)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        value = payload.get("chunk_size")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    except Exception:
        _logger.warning("Recovered from an error in _load_manifest_chunk_size.", exc_info=True)
        return None
    return None


def _load_manifest_embedding_model(
    *,
    db_path: str | None = None,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> str:
    path = _manifest_path(db_path=db_path, chunk_size=chunk_size, embedding_model=embedding_model)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return str(payload.get("embedding_model") or "").strip()
    except Exception:
        _logger.warning("Recovered from an error in _load_manifest_embedding_model.", exc_info=True)
        return ""


def _load_manifest_embedding_prompts(*, db_path: str | None = None) -> str:
    """Prompt scheme the index's passages were embedded with ("" = raw text)."""
    path = _manifest_path(db_path=db_path)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return str(payload.get("embedding_prompts") or "").strip()
    except Exception:
        _logger.warning("Recovered from an error in _load_manifest_embedding_prompts.", exc_info=True)
        return ""


def get_indexed_rel_paths() -> set[str]:
    entries = _load_manifest_entries()
    result: set[str] = set()
    for rel_path, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        if not (entry.get("file_sig") or entry.get("zotero_sig")):
            continue
        if _manifest_chunk_count(entry) <= 0:
            continue
        result.add(str(rel_path))
    return result


def _save_manifest_entries(
    entries: dict[str, dict[str, str]],
    chunk_size: int,
    embedding_model: str,
    *,
    db_path: str | None = None,
    embedding_prompts: str | None = None,
) -> None:
    """Write the manifest; ``embedding_prompts`` None keeps the recorded scheme."""
    resolved_path = _resolve_db_path(
        db_path=db_path,
        chunk_size=chunk_size,
        embedding_model=embedding_model,
    )
    if embedding_prompts is None:
        embedding_prompts = _load_manifest_embedding_prompts(db_path=resolved_path)
    os.makedirs(resolved_path, exist_ok=True)
    serialized = {
        rel_path: _stable_manifest_entry(
            entry,
            chunk_count=_manifest_chunk_count(entry),
            no_text=_manifest_no_text(entry),
            failed=_manifest_failure(entry),
        )
        for rel_path, entry in entries.items()
        if _manifest_entry_settled(entry)
    }
    payload = {
        "version": 3,
        "chunk_size": int(chunk_size),
        "embedding_model": str(embedding_model or "").strip(),
        "entries": serialized,
    }
    if embedding_prompts:
        payload["embedding_prompts"] = embedding_prompts
    atomic_write_json(
        Path(
            _manifest_path(
                db_path=resolved_path,
            )
        ),
        payload,
    )


def _exceeds_document_limit(full_path: str) -> bool:
    try:
        return os.path.getsize(full_path) > max_document_bytes(Path(full_path).suffix)
    except OSError:
        return False


def _compute_index_entries(pdf_list: list[tuple[str, str]]) -> dict[str, dict[str, str]]:
    entries: dict[str, dict[str, str]] = {}
    for rel_path, full_path in pdf_list:
        item = resolve_document_metadata(
            rel_path,
            full_path,
            state._zotero_library,
            allow_external_enrichment=True,
        )
        entries[rel_path] = {
            "abs_path": full_path,
            # A file the guard refuses is never indexed: hashing gigabytes of
            # video on every reload would only slow the scan down.
            "file_sig": (
                _legacy_file_signature(full_path)
                if _exceeds_document_limit(full_path)
                else _file_signature(full_path)
            ),
            "legacy_file_sig": _legacy_file_signature(full_path),
            "zotero_sig": _index_signature(rel_path, item),
            "visual_sig": document_index_variant(
                full_path,
                pdf_variant=index_visual_signature(),
            ),
        }
    return entries


def _manifest_file_signature_matches(
    previous: dict[str, str],
    current: dict[str, str],
) -> bool:
    previous_file_sig = str(previous.get("file_sig", "") or "")
    if not previous_file_sig:
        return False
    if previous_file_sig == str(current.get("file_sig", "") or ""):
        return True
    if previous_file_sig == str(current.get("legacy_file_sig", "") or ""):
        return True
    previous_parts = previous_file_sig.split(":")
    current_parts = str(current.get("file_sig", "") or "").split(":")
    return (
        len(previous_parts) == 2
        and len(current_parts) == 3
        and current_parts[1] == "sha256"
        and previous_parts[0].isdigit()
        and previous_parts[0] == current_parts[0]
    )


def _manifest_entry_matches(
    previous: dict[str, Any] | None,
    current: dict[str, str],
) -> bool:
    if not previous:
        return False
    return (
        _manifest_entry_settled(previous)
        and _manifest_file_signature_matches(previous, current)
        and str(previous.get("zotero_sig", "") or "") == str(current.get("zotero_sig", "") or "")
        and str(previous.get("visual_sig", "off") or "off")
        == str(current.get("visual_sig", "off") or "off")
    )


def _manifest_chunk_count(entry: dict[str, Any] | None) -> int:
    if not entry:
        return 0
    try:
        return max(0, int(entry.get("chunk_count", 0)))
    except (TypeError, ValueError):
        return 0


def _manifest_no_text(entry: dict[str, Any] | None) -> bool:
    return entry is not None and entry.get("no_text") is True and _manifest_chunk_count(entry) == 0


def _manifest_failure(entry: dict[str, Any] | None) -> dict[str, str] | None:
    """The recorded read failure of this file, or None.

    A failure is only kept for the app version that met it: a newer version
    may read the file, so it retries it.
    """
    if not entry:
        return None
    failure = entry.get("failed")
    if not isinstance(failure, dict):
        return None
    if str(failure.get("version") or "") != get_app_version():
        return None
    return {
        "reason": str(failure.get("reason") or "")[:300],
        "version": str(failure.get("version") or ""),
    }


def _manifest_entry_settled(entry: dict[str, Any] | None) -> bool:
    """Indexed, read in full without any text (a scan the OCR pass covers), or
    already failed with this app version (retried once the file or the app changes).

    Any other entry without chunks is retried: its read was cut.
    """
    return (
        _manifest_chunk_count(entry) > 0
        or _manifest_no_text(entry)
        or _manifest_failure(entry) is not None
    )


def _stable_manifest_entry(
    entry: dict[str, Any],
    *,
    chunk_count: int,
    no_text: bool = False,
    failed: dict[str, str] | None = None,
) -> dict[str, Any]:
    stable: dict[str, Any] = {
        "file_sig": str(entry.get("file_sig", "")),
        "zotero_sig": str(entry.get("zotero_sig", "")),
        "visual_sig": str(entry.get("visual_sig", "off")),
        "chunk_count": max(0, int(chunk_count)),
    }
    if no_text and stable["chunk_count"] == 0:
        stable["no_text"] = True
    if failed:
        stable["failed"] = {
            "reason": str(failed.get("reason") or "")[:300],
            "version": str(failed.get("version") or get_app_version()),
        }
    return stable


def _preview_manifest_changes(
    *,
    current_entries: dict[str, dict[str, str]],
    previous_entries: dict[str, dict[str, str]],
) -> dict[str, Any]:
    current_paths = set(current_entries)
    previous_paths = set(previous_entries)
    new_paths = sorted(current_paths - previous_paths)
    removed_paths = sorted(previous_paths - current_paths)
    unchanged_paths: list[str] = []
    modified_paths: list[str] = []
    metadata_changed_paths: list[str] = []
    content_changed_paths: list[str] = []
    migrated_legacy_paths: list[str] = []

    for rel_path in sorted(current_paths & previous_paths):
        previous = previous_entries.get(rel_path) or {}
        current = current_entries.get(rel_path) or {}
        file_matches = _manifest_file_signature_matches(previous, current)
        zotero_matches = str(previous.get("zotero_sig", "") or "") == str(
            current.get("zotero_sig", "") or ""
        )
        if file_matches and zotero_matches and _manifest_entry_settled(previous):
            unchanged_paths.append(rel_path)
            previous_sig = str(previous.get("file_sig", "") or "")
            legacy_sig = str(current.get("legacy_file_sig", "") or "")
            if previous_sig == legacy_sig:
                migrated_legacy_paths.append(rel_path)
            continue
        modified_paths.append(rel_path)
        if not file_matches:
            content_changed_paths.append(rel_path)
        if not zotero_matches:
            metadata_changed_paths.append(rel_path)

    failed_files = [
        {"rel_path": rel_path, "reason": failure["reason"]}
        for rel_path in unchanged_paths
        if (failure := _manifest_failure(previous_entries.get(rel_path))) is not None
    ]

    return {
        "failed_files": failed_files,
        "failed_count": len(failed_files),
        "new_paths": new_paths,
        "removed_paths": removed_paths,
        "modified_paths": modified_paths,
        "unchanged_paths": unchanged_paths,
        "metadata_changed_paths": metadata_changed_paths,
        "content_changed_paths": content_changed_paths,
        "migrated_legacy_paths": migrated_legacy_paths,
        "new_count": len(new_paths),
        "removed_count": len(removed_paths),
        "modified_count": len(modified_paths),
        "unchanged_count": len(unchanged_paths),
        "metadata_changed_count": len(metadata_changed_paths),
        "content_changed_count": len(content_changed_paths),
        "migrated_legacy_count": len(migrated_legacy_paths),
    }


def _current_embedding_model() -> str:
    return required_embedding_model_name()


def _current_embedding_profile() -> str:
    return current_embedding_profile_label(_current_embedding_model())


def _format_embedding_error(exc: Exception) -> str:
    text = str(exc).strip()
    text_lc = text.lower()
    embedding_model = _current_embedding_model()
    if embedding_model in text and ("not found" in text_lc or "status code: 404" in text_lc):
        return (
            f"Modele d'embedding manquant: `{embedding_model}`. "
            f"Installez-le avec: `ollama pull {embedding_model}` puis relancez l'initialisation."
        )
    if "connection refused" in text_lc or ("ollama" in text_lc and "inaccessible" in text_lc):
        return "Ollama inaccessible. Verifiez que le serveur tourne sur http://localhost:11434."
    return f"Erreur pendant la vectorisation: {text}"


def _add_chunks_in_batches(
    *,
    target_vectorstore: Chroma,
    chunks: list[Document],
    batch_size: int,
    progress: state.ProgressFn,
    progress_start: float,
    progress_end: float,
    label: str,
) -> None:
    if not chunks:
        return
    span = max(0.0, progress_end - progress_start)
    for i in range(0, len(chunks), batch_size):
        state._raise_if_vector_load_paused()
        batch = _sanitize_documents(chunks[i : i + batch_size])
        try:
            target_vectorstore.add_documents(batch)
        except Exception as exc:
            raise RuntimeError(_format_embedding_error(exc)) from exc
        done = min(i + batch_size, len(chunks))
        pct = progress_start + span * done / max(1, len(chunks))
        progress(pct, desc=f"{label} : {done}/{len(chunks)} chunks")


def _index_pdf_groups_with_manifest(
    *,
    target_vectorstore: Chroma,
    pdf_list: list[tuple[str, str]],
    current_entries: dict[str, dict[str, str]],
    manifest_entries: dict[str, dict[str, str]],
    chunk_size: int,
    progress: state.ProgressFn,
    progress_start: float,
    progress_end: float,
    read_label: str,
    index_label: str,
    index_path: str | None = None,
    checkpoint: Callable[[dict[str, dict[str, Any]]], None] | None = None,
) -> tuple[dict[str, dict[str, str]], int, int, int, int, int]:
    """Index the documents group by group, resuming those already in the manifest.

    ``checkpoint`` receives the manifest after every group whose chunks are
    stored: an interrupted build then resumes from the last finished group
    instead of starting over, because the manifest only ever lists documents
    whose vectors are really in the store.
    """
    if not pdf_list:
        return dict(manifest_entries), 0, 0, 0, 0, 0

    entries = dict(manifest_entries)
    span = max(0.0, progress_end - progress_start)
    total = len(pdf_list)

    pending: list[tuple[str, str]] = []
    resumed_count = 0
    for rel_path, full_path in pdf_list:
        cur = current_entries.get(rel_path) or {}
        prev = entries.get(rel_path) or {}
        if _manifest_entry_matches(prev, cur):
            entries[rel_path] = _stable_manifest_entry(
                cur,
                chunk_count=_manifest_chunk_count(prev),
                no_text=_manifest_no_text(prev),
                failed=_manifest_failure(prev),
            )
            resumed_count += 1
            continue
        pending.append((rel_path, full_path))

    page_count = 0
    note_chunk_count = 0
    pdf_chunk_count = 0
    processed_count = resumed_count
    indexed_count = 0

    if resumed_count:
        frac = progress_start + span * (resumed_count / max(1, total))
        progress(frac, desc=f"Reprise: {resumed_count}/{total} documents deja indexes.")

    if not pending:
        return entries, resumed_count, 0, page_count, note_chunk_count, pdf_chunk_count

    group_size = state._get_pdf_group_size()
    for i in range(0, len(pending), group_size):
        state._raise_if_vector_load_paused()
        group = pending[i : i + group_size]
        group_start_count = processed_count
        group_end_count = processed_count + len(group)

        group_start = progress_start + span * (group_start_count / max(1, total))
        group_end = progress_start + span * (group_end_count / max(1, total))
        group_mid = group_start + (group_end - group_start) * 0.55

        progress(
            group_start,
            desc=f"{read_label}: lot {group_start_count + 1}-{group_end_count}/{total}",
        )

        textless_paths: set[str] = set()
        failed_paths: dict[str, str] = {}
        note_chunks, pdf_chunks, pages = _build_chunks_from_pdfs(
            pdf_list=group,
            chunk_size=chunk_size,
            progress=progress,
            progress_start=group_start,
            progress_end=group_mid,
            status_label=read_label,
            textless_paths=textless_paths,
            failed_paths=failed_paths,
        )
        # A file that could not be read keeps what it already has in the
        # index: dropping its chunks would make it vanish from the answers
        # (and come back as "new" on every reload) until the read works again.
        failed_keys = {normalize_rel_path(path) for path in failed_paths}
        if failed_keys:
            note_chunks = _without_paths(note_chunks, failed_keys)
            pdf_chunks = _without_paths(pdf_chunks, failed_keys)
        chunks = note_chunks + pdf_chunks
        replaced_paths = [
            rel_path for rel_path, _ in group if normalize_rel_path(rel_path) not in failed_keys
        ]

        state._raise_if_vector_load_paused()
        for rel_path in replaced_paths:
            try:
                target_vectorstore.delete(where={"rel_path": rel_path})
            except Exception as exc:
                _logger.exception("Failed to delete stale Chroma chunks for %s", rel_path)
                raise RuntimeError(
                    f"unable to replace existing vector chunks for {rel_path}"
                ) from exc

        _add_chunks_in_batches(
            target_vectorstore=target_vectorstore,
            chunks=chunks,
            batch_size=50,
            progress=progress,
            progress_start=group_mid,
            progress_end=group_end,
            label=index_label,
        )
        if index_path:
            replace_lexical_paths(
                index_path,
                replaced_paths,
                _sanitize_documents(chunks),
            )

        chunks_by_rel_path: dict[str, int] = {}
        for chunk in chunks:
            chunk_rel_path = normalize_rel_path(str((chunk.metadata or {}).get("rel_path") or ""))
            if chunk_rel_path:
                chunks_by_rel_path[chunk_rel_path] = chunks_by_rel_path.get(chunk_rel_path, 0) + 1

        for rel_path, _ in group:
            cur = current_entries.get(rel_path) or {}
            chunk_count = chunks_by_rel_path.get(normalize_rel_path(rel_path), 0)
            if normalize_rel_path(rel_path) in failed_keys:
                reason = next(
                    (
                        text
                        for path, text in failed_paths.items()
                        if normalize_rel_path(path) == normalize_rel_path(rel_path)
                    ),
                    "",
                )
                entries[rel_path] = _stable_manifest_entry(
                    cur,
                    chunk_count=_manifest_chunk_count(manifest_entries.get(rel_path)),
                    failed={"reason": reason, "version": get_app_version()},
                )
            elif chunk_count > 0:
                entries[rel_path] = _stable_manifest_entry(cur, chunk_count=chunk_count)
                indexed_count += 1
            elif rel_path in textless_paths:
                # Recorded so the reload preview stops listing it as new; its
                # file signature still sends it back here once it changes.
                entries[rel_path] = _stable_manifest_entry(cur, chunk_count=0, no_text=True)
            else:
                entries.pop(rel_path, None)
        if checkpoint is not None:
            checkpoint(entries)

        page_count += pages
        note_chunk_count += len(note_chunks)
        pdf_chunk_count += len(pdf_chunks)
        processed_count = group_end_count

    return entries, resumed_count, indexed_count, page_count, note_chunk_count, pdf_chunk_count


def _without_paths(chunks: list[Document], rel_paths: set[str]) -> list[Document]:
    return [
        chunk
        for chunk in chunks
        if normalize_rel_path(str((chunk.metadata or {}).get("rel_path") or "")) not in rel_paths
    ]


def _merge_filter_maps(
    first: dict[str, list[str]], second: dict[str, list[str]]
) -> dict[str, list[str]]:
    keys = {"tags", "collections", "authors", "years", "dois", "sources"}
    merged: dict[str, list[str]] = {}
    for key in keys:
        merged[key] = _dedupe_keep_order((first.get(key) or []) + (second.get(key) or []))
    return merged


def get_vector_reload_preview(*, chunk_size: int | None = None) -> dict[str, Any]:
    resolved_chunk_size = _current_chunk_size()
    if chunk_size is not None:
        try:
            resolved_chunk_size = max(1, int(chunk_size))
        except (TypeError, ValueError):
            resolved_chunk_size = _current_chunk_size()

    from .embedding_equivalence import adopt_equivalent_vector_index

    embedding_profile = _current_embedding_profile()
    adopt_equivalent_vector_index(chunk_size=resolved_chunk_size, embedding_model=embedding_profile)
    current_profile = resolve_vector_index_profile(
        chunk_size=resolved_chunk_size,
        embedding_model=embedding_profile,
    )
    db_path = str(current_profile.get("path") or "")
    db_exists = bool(db_path) and os.path.exists(db_path) and bool(os.listdir(db_path))
    previous_entries = _load_manifest_entries(db_path=db_path) if db_exists else {}
    previous_chunk_size = _load_manifest_chunk_size(db_path=db_path) if db_exists else None
    previous_embedding_model = _load_manifest_embedding_model(db_path=db_path) if db_exists else ""

    _refresh_zotero_library_cache()
    pdf_list = _filter_active_zotero_pdf_entries(state.find_pdfs())
    current_entries = _compute_index_entries(pdf_list)
    changes = _preview_manifest_changes(
        current_entries=current_entries,
        previous_entries=previous_entries,
    )

    full_rebuild_required = False
    rebuild_reason = ""
    if not pdf_list:
        rebuild_reason = "no_pdfs"
    elif db_exists and not previous_entries:
        full_rebuild_required = True
        rebuild_reason = "missing_manifest"
    elif (
        db_exists
        and previous_chunk_size is not None
        and previous_chunk_size != int(resolved_chunk_size)
    ):
        full_rebuild_required = True
        rebuild_reason = "chunk_size_changed"
    elif (
        db_exists
        and previous_embedding_model
        and not manifest_model_matches(db_path, embedding_profile)
    ):
        full_rebuild_required = True
        rebuild_reason = "embedding_model_changed"
    elif not db_exists:
        rebuild_reason = "index_missing"

    if full_rebuild_required:
        mode = "rebuild"
    elif not db_exists:
        mode = "initial_index"
    else:
        mode = "incremental"

    changed_count = changes["new_count"] + changes["modified_count"] + changes["removed_count"]
    if mode == "rebuild":
        summary = (
            "Rebuild complet requis: "
            + {
                "missing_manifest": "manifest introuvable ou non recuperable",
                "chunk_size_changed": "taille des chunks differente",
                "embedding_model_changed": "modele d'embedding different",
            }.get(rebuild_reason, rebuild_reason or "raison inconnue")
            + "."
        )
    elif mode == "initial_index":
        summary = f"Index absent: {len(current_entries)} documents a indexer."
    else:
        summary = (
            "Mise a jour incrementale: "
            f"{changes['new_count']} nouveaux, "
            f"{changes['modified_count']} modifies, "
            f"{changes['removed_count']} supprimes, "
            f"{changes['unchanged_count']} inchanges."
        )

    return {
        "ok": bool(pdf_list) or bool(previous_entries),
        "mode": mode,
        "summary": summary,
        "full_rebuild_required": full_rebuild_required,
        "rebuild_reason": rebuild_reason,
        "changed_count": changed_count,
        "total_pdf_count": len(current_entries),
        "total_document_count": len(current_entries),
        "embedding_model": embedding_profile,
        "current_profile": current_profile,
        "db_path": db_path,
        "db_exists": db_exists,
        "chunk_size": int(resolved_chunk_size),
        "previous_chunk_size": previous_chunk_size,
        "previous_embedding_model": previous_embedding_model,
        **changes,
    }


def reset_vector_database() -> dict[str, Any]:
    with state._state_lock:
        current_store = state.vectorstore
        state._zotero_library = {}
    state.clear_runtime_state()
    state._reset_vector_load_pause()
    _close_chroma_store(current_store)
    gc.collect()

    root_path = get_vector_db_root()
    existed = os.path.exists(root_path)
    removal_error = ""
    for attempt in range(1, 6):
        try:
            shutil.rmtree(root_path)
            removal_error = ""
            break
        except FileNotFoundError:
            removal_error = ""
            break
        except Exception as exc:
            removal_error = str(exc).strip()
            gc.collect()
            try:
                chromadb.api.client.SharedSystemClient.clear_system_cache()
            except Exception:
                _logger.debug("Unable to clear the Chroma system cache during retry", exc_info=True)
            if attempt >= 5:
                break
            time.sleep(0.35 * attempt)

    exists_after = os.path.exists(root_path)
    leftovers: list[str] = []
    if exists_after:
        try:
            leftovers = sorted(entry.name for entry in os.scandir(root_path))
        except Exception:
            _logger.debug("Recovered from an error in reset_vector_database.", exc_info=True)
            leftovers = ["<inaccessible>"]
    return {
        "path": root_path,
        "existed": bool(existed),
        "removed": not exists_after,
        "error": removal_error,
        "leftovers": leftovers,
    }


def get_vector_database_status() -> dict[str, Any]:
    root_path = get_vector_db_root()
    current_embedding_model = _current_embedding_profile()
    current_chunk_size = _current_chunk_size()
    current_profile = resolve_vector_index_profile(
        chunk_size=current_chunk_size,
        embedding_model=current_embedding_model,
    )
    db_path = str(current_profile.get("path") or "")

    path_exists = bool(db_path) and os.path.exists(db_path)
    entries: list[str] = []
    if path_exists:
        try:
            entries = sorted(entry.name for entry in os.scandir(db_path))
        except Exception:
            _logger.debug("Recovered from an error in get_vector_database_status.", exc_info=True)
            entries = ["<inaccessible>"]

    sqlite_path = os.path.join(db_path, "chroma.sqlite3") if db_path else ""
    manifest_path = _manifest_path(db_path=db_path) if db_path else ""
    has_sqlite = os.path.exists(sqlite_path)
    has_manifest = os.path.exists(manifest_path)
    incomplete = _has_incomplete_index_marker(db_path=db_path) if db_path else False
    index_embedding_model = _load_manifest_embedding_model(db_path=db_path) if db_path else ""
    index_chunk_size = _load_manifest_chunk_size(db_path=db_path) if db_path else None
    embedding_model_mismatch = bool(
        has_manifest
        and index_embedding_model
        and not manifest_model_matches(db_path, current_embedding_model)
    )

    status = "missing"
    if path_exists and has_sqlite:
        status = "incomplete" if incomplete else "present"
    elif path_exists:
        status = "partial"
    if embedding_model_mismatch:
        status = "embedding_mismatch"

    available_profiles = list_vector_index_profiles()

    warning = ""
    warning_level = ""
    if embedding_model_mismatch:
        warning_level = "error"
        warning = (
            "Le modele d'embedding selectionne ne correspond pas a celui utilise pour construire "
            f"les bases vectorielles disponibles ({index_embedding_model} au lieu de {current_embedding_model}). "
            "Rebuild requis via `Recharger PDF`."
        )
    elif status in {"incomplete", "partial"}:
        warning_level = "warning"
        warning = (
            "La construction de l'index pour ce modele a ete interrompue. "
            "Relancez `Recharger PDF` pour la reprendre."
        )
    elif status == "missing":
        other_profiles = [
            profile
            for profile in available_profiles
            if str(profile.get("path") or "").strip() != db_path
        ]
        if other_profiles:
            warning_level = "info"
            warning = (
                "Ce modele d'embedding n'a pas encore d'index. "
                "Lancez `Recharger PDF` pour le construire ; les autres index sont conserves."
            )

    active_profile = _load_active_vector_index_profile()

    return {
        "status": status,
        "path": root_path,
        "current_profile_id": str(current_profile.get("profile_id") or ""),
        "current_profile_path": db_path,
        "current_chunk_size": int(current_chunk_size),
        "current_profile_source": str(current_profile.get("source") or ""),
        "available_profiles": available_profiles,
        "available_profile_count": len(available_profiles),
        "active_profile": active_profile,
        "exists": path_exists,
        "has_sqlite": has_sqlite,
        "sqlite_size": os.path.getsize(sqlite_path) if has_sqlite else 0,
        "has_manifest": has_manifest,
        "incomplete": incomplete,
        "entries": entries,
        "index_chunk_size": index_chunk_size,
        "index_embedding_model": index_embedding_model,
        "current_embedding_model": current_embedding_model,
        "embedding_model_mismatch": embedding_model_mismatch,
        "warning": warning,
        "warning_level": warning_level,
        "profile_ready": status == "present" and not embedding_model_mismatch,
        "profile_reused": bool(
            status == "present"
            and str(active_profile.get("profile_id") or "")
            == str(current_profile.get("profile_id") or "")
        ),
    }


def find_documents_with_text_artifacts(*, batch_size: int = 2000) -> list[dict[str, Any]]:
    """Indexed documents whose stored text has PDF extraction artifacts.

    LaTeX PDFs were indexed with detached accents ("c´ eramiques") and
    typographic ligatures ("ﬁeld"). The text is now repaired when read, but
    the embeddings and the lexical index were built on the broken text, so
    retrieval for those documents stays degraded until they are reindexed.
    Scanning the whole collection takes ~10 s for ~45k chunks.
    """
    from src.unicode_utils import repair_pdf_text_artifacts

    with state._state_lock:
        vectorstore = state.vectorstore
    if vectorstore is None:
        raise RuntimeError("L'index n'est pas chargé.")
    collection = vectorstore._collection
    found: dict[str, dict[str, Any]] = {}
    offset = 0
    while True:
        batch = collection.get(include=["documents", "metadatas"], limit=batch_size, offset=offset)
        documents = batch.get("documents") or []
        if not documents:
            break
        for text, metadata in zip(documents, batch.get("metadatas") or [], strict=False):
            if not text or repair_pdf_text_artifacts(text) == text:
                continue
            metadata = metadata or {}
            rel_path = normalize_rel_path(str(metadata.get("rel_path") or metadata.get("source") or ""))
            if not rel_path:
                continue
            entry = found.setdefault(
                rel_path,
                {
                    "rel_path": rel_path,
                    "title": str(metadata.get("zotero_title") or metadata.get("title") or Path(rel_path).stem),
                    "affected_chunks": 0,
                },
            )
            entry["affected_chunks"] += 1
        offset += len(documents)
    return sorted(found.values(), key=lambda item: -int(item["affected_chunks"]))


def mark_documents_for_reindex(rel_paths: list[str]) -> int:
    """Make the next incremental reload re-embed these documents.

    Only the file signature is cleared: the entry keeps its chunk count and
    Zotero signature, so the library still lists the document as indexed and
    the reload treats it as a changed file (old vectors deleted, then re-added
    from the prepared-chunk cache, whose text is repaired on load).
    """
    targets = {normalize_rel_path(str(path)) for path in rel_paths if str(path or "").strip()}
    if not targets:
        return 0
    entries = _load_manifest_entries()
    marked = 0
    for rel_path, entry in entries.items():
        if normalize_rel_path(rel_path) in targets and entry.get("file_sig"):
            entry["file_sig"] = ""
            marked += 1
    if marked:
        _save_manifest_entries(
            entries,
            _load_manifest_chunk_size() or get_persisted_chunk_size(),
            _load_manifest_embedding_model() or required_embedding_model_name(),
        )
    return marked
