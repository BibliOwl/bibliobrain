"""Routes for choosing what the search index reads (Settings > My documents)."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src import config
from src.index_selection import (
    COLLECTION_MODES,
    apply_index_selection,
    get_index_selection,
    resolve_selection,
    selection_problem,
    set_index_selection,
    summarize_skipped,
)
from src.zotero_db import (
    _is_zotero_key,
    filter_active_zotero_pdf_entries,
    is_zotero_storage_path,
    lookup_zotero_item,
)

router = APIRouter()

_MAX_FOLDERS = 400
_MAX_LARGE_LISTED = 200


class IndexSelectionRequest(BaseModel):
    collection_mode: str | None = Field(default=None, max_length=16)
    collections: list[str] | None = Field(default=None, max_length=2000)
    excluded_folders: list[str] | None = Field(default=None, max_length=2000)
    max_file_mb: int | None = Field(default=None, ge=0, le=10_000)
    allowed_large: list[str] | None = Field(default=None, max_length=2000)


def _metadata_library() -> dict[str, Any]:
    """The Zotero/BibTeX records already loaded, RAG copy first."""
    from src import library as library_module
    from src.rag import state

    with state._state_lock:
        rag_library = dict(state._zotero_library)
    return rag_library or dict(library_module._zotero_library)


def _folders(entries: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """Sub-folders (two levels deep) with their document counts.

    Zotero's storage/ folder holds one folder per attachment key, which says
    nothing to a reader: those are left out.
    """
    counts: dict[str, int] = {}
    for rel_path, _full_path in entries:
        parts = [part for part in rel_path.replace("\\", "/").split("/") if part][:-1]
        for depth in range(1, min(2, len(parts)) + 1):
            if any(part.isupper() and _is_zotero_key(part) for part in parts[:depth]):
                break
            key = "/".join(parts[:depth])
            counts[key] = counts.get(key, 0) + 1
    folders = [
        {"path": path, "count": count, "depth": path.count("/")}
        for path, count in sorted(counts.items(), key=lambda item: item[0].lower())
    ]
    return folders[:_MAX_FOLDERS]


def _collections(library: dict[str, Any]) -> list[dict[str, Any]]:
    """Every collection path, parents included, with its document count.

    A collection counts the documents of its sub-collections too, each once,
    because ticking it selects them all.
    """
    counts: dict[str, int] = {}
    seen: set[int] = set()
    for item in library.values():
        if id(item) in seen:
            continue
        seen.add(id(item))
        paths: set[str] = set()
        for path in item.collections or []:
            parts = [part.strip() for part in str(path).split(" / ") if part.strip()]
            for depth in range(1, len(parts) + 1):
                paths.add(" / ".join(parts[:depth]))
        for key in paths:
            counts[key] = counts.get(key, 0) + 1
    return [
        {"path": path, "count": count, "depth": path.count(" / ")}
        for path, count in sorted(counts.items(), key=lambda item: item[0].lower())
    ]


def selection_overview() -> dict[str, Any]:
    from src.rag import state

    library = _metadata_library()
    # Collections under their current Zotero names, so a renamed one stays ticked.
    selection = resolve_selection(get_index_selection(), library)
    documents = state.find_documents()
    active = filter_active_zotero_pdf_entries(
        documents,
        library,
        data_path=config.DATA_PATH,
        zotero_db_path=config.ZOTERO_DB_PATH,
    )
    kept, skipped = apply_index_selection(active, library, selection)
    large: list[dict[str, Any]] = []
    for entry in skipped:
        if entry.get("reason") != "too_large" or len(large) >= _MAX_LARGE_LISTED:
            continue
        item = lookup_zotero_item(entry["rel_path"], library, None) if library else None
        large.append(
            {
                "rel_path": entry["rel_path"],
                "size": int(entry.get("size") or 0),
                "title": (item.title if item else "") or os.path.basename(entry["rel_path"]),
            }
        )
    allowed = []
    for rel_path in selection["allowed_large"]:
        allowed.append({"rel_path": rel_path, "title": os.path.basename(rel_path)})
    zotero_storage = is_zotero_storage_path(config.DATA_PATH, config.ZOTERO_DB_PATH)
    return {
        "ok": True,
        "selection": selection,
        "modes": list(COLLECTION_MODES),
        "metadata_loaded": bool(library),
        "zotero_storage": zotero_storage,
        "collections": _collections(library),
        "folders": [] if zotero_storage else _folders(active),
        "total_count": len(active),
        "kept_count": len(kept),
        "skipped_count": len(skipped),
        "skipped_by_reason": summarize_skipped(skipped),
        "too_large": large,
        "allowed_large": allowed,
        "problem": selection_problem(selection, library),
    }


@router.get("/api/index-selection")
def api_index_selection():
    return selection_overview()


@router.put("/api/index-selection")
def api_set_index_selection(req: IndexSelectionRequest):
    set_index_selection(req.model_dump(exclude_none=True), _metadata_library())
    return selection_overview()


# ---- Library profiles (src/library_profiles.py) -----------------------------


class LibraryProfileRequest(BaseModel):
    name: str = Field(default="", max_length=120)
    copy_current: bool = False


def _profiles_payload() -> dict[str, Any]:
    from src.library_profiles import list_profiles
    from src.rag.vector_store import library_profile_index_state

    listing = list_profiles()
    profiles = []
    for profile in listing["profiles"]:
        index_state = library_profile_index_state(profile["index_root"])
        profiles.append(
            {
                "id": profile["id"],
                "name": profile["name"],
                "is_default": profile["is_default"],
                "active": profile["active"],
                # Ready for the current embedding model: switching only loads it.
                "built": index_state == "ready",
                "index_state": index_state,
            }
        )
    return {"ok": True, "active": listing["active"], "profiles": profiles}


def _profile_error(exc: Exception) -> JSONResponse:
    from src.library_profiles import ProfileError

    code = exc.code if isinstance(exc, ProfileError) else "profile_error"
    status = 404 if code == "not_found" else 409 if code == "active_profile" else 422
    return JSONResponse(status_code=status, content={"ok": False, "error": code, "msg": str(exc)})


@router.get("/api/library-profiles")
def api_library_profiles():
    return _profiles_payload()


@router.post("/api/library-profiles")
def api_create_library_profile(req: LibraryProfileRequest):
    from src.library_profiles import ProfileError, create_profile

    try:
        selection = get_index_selection() if req.copy_current else None
        created = create_profile(req.name, selection)
    except ProfileError as exc:
        return _profile_error(exc)
    return {**_profiles_payload(), "created": created["id"]}


@router.put("/api/library-profiles/{profile_id}")
def api_rename_library_profile(profile_id: str, req: LibraryProfileRequest):
    from src.library_profiles import ProfileError, rename_profile

    try:
        rename_profile(profile_id, req.name)
    except ProfileError as exc:
        return _profile_error(exc)
    return _profiles_payload()


@router.post("/api/library-profiles/{profile_id}/activate")
def api_activate_library_profile(profile_id: str):
    """Switch library; the page then loads (or builds) its index with /api/init."""
    from src.api import application
    from src.library_profiles import ProfileError
    from src.rag.vector_store import LibraryBusyError, switch_library_profile

    if not application._rag_init_lock.acquire(blocking=False):
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "rag_initialization_in_progress",
                "msg": "Une préparation ou un OCR est en cours : réessayez quand il sera fini.",
            },
        )
    try:
        switch_library_profile(profile_id)
    except ProfileError as exc:
        return _profile_error(exc)
    except LibraryBusyError:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "answer_in_progress",
                "msg": "Une réponse est en cours : changez de bibliothèque quand elle sera finie.",
            },
        )
    finally:
        application._rag_init_lock.release()
    return _profiles_payload()


@router.delete("/api/library-profiles/{profile_id}")
def api_delete_library_profile(profile_id: str):
    from src.library_profiles import ProfileError, delete_profile
    from src.rag.vector_store import remove_library_profile_index

    try:
        index_root = delete_profile(profile_id)
    except ProfileError as exc:
        return _profile_error(exc)
    removed = remove_library_profile_index(index_root)
    return {**_profiles_payload(), "index_removed": removed}
