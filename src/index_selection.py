"""Which library documents the search index reads (Settings > My documents).

Only the list handed to indexing (and to the OCR pass) goes through
``apply_index_selection``; ``find_documents`` still sees every file, so the
folder counts and the duplicate check stay whole. The Library tab lists
indexed documents, so a skipped file leaves it too. An incremental reload
already drops from the index the documents that left that list, so excluding
a collection empties it from the index without a rebuild.

Each library profile (src/library_profiles.py) keeps its own selection; these
helpers read and write the active one.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from src.zotero_db import ZoteroItem, lookup_zotero_item

SETTINGS_KEY = "index_selection"
COLLECTION_MODES = ("all", "include", "exclude")
# Zotero joins nested collection names with this separator (zotero_db.py).
COLLECTION_SEPARATOR = " / "
# 0 turns the size guard off. A selection saved without a size (every install
# before this setting, and the main library of 0.9.0) keeps every file: a guard
# added later must not drop documents that are already in the index.
DEFAULT_MAX_FILE_MB = 0
# What a new library proposes; the settings card shows it as a suggestion.
SUGGESTED_MAX_FILE_MB = 100
MAX_FILE_MB_LIMIT = 10_000
_MAX_LIST_LENGTH = 2_000

_last_skipped_lock = threading.Lock()
_last_skipped: list[dict[str, Any]] = []


def _clean_list(values: Any, *, folder: bool = False) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values[:_MAX_LIST_LENGTH]:
        text = str(value or "").strip()
        if folder:
            text = text.replace("\\", "/").strip("/")
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


def normalize_selection(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    mode = str(data.get("collection_mode") or "all").strip().lower()
    if mode not in COLLECTION_MODES:
        mode = "all"
    try:
        max_file_mb = int(data.get("max_file_mb", DEFAULT_MAX_FILE_MB))
    except (TypeError, ValueError):
        max_file_mb = DEFAULT_MAX_FILE_MB
    collections = _clean_list(data.get("collections"))
    raw_keys = data.get("collection_keys")
    keys = raw_keys if isinstance(raw_keys, dict) else {}
    wanted = {name.lower() for name in collections}
    collection_keys = {
        str(name).strip(): str(key).strip()
        for name, key in list(keys.items())[:_MAX_LIST_LENGTH]
        if str(name).strip().lower() in wanted and str(key or "").strip()
    }
    return {
        "collection_mode": mode,
        "collections": collections,
        "collection_keys": collection_keys,
        "excluded_folders": _clean_list(data.get("excluded_folders"), folder=True),
        "max_file_mb": max(0, min(MAX_FILE_MB_LIMIT, max_file_mb)),
        "allowed_large": _clean_list(data.get("allowed_large"), folder=True),
    }


def get_index_selection() -> dict[str, Any]:
    from src.library_profiles import active_profile

    return normalize_selection(active_profile().get("selection"))


def set_index_selection(
    update: dict[str, Any], library: dict[str, ZoteroItem] | None = None
) -> dict[str, Any]:
    """Save the active library's choice; collections also keep their Zotero key."""
    from src.library_profiles import save_active_selection

    current = get_index_selection()
    merged = {**current, **{k: v for k, v in (update or {}).items() if v is not None}}
    selection = normalize_selection(merged)
    if library:
        selection = remember_collection_keys(selection, library)
    return save_active_selection(selection)


def collection_catalog(library: dict[str, ZoteroItem] | None) -> dict[str, str]:
    """Key -> current path of every collection the records know (parents included)."""
    catalog: dict[str, str] = {}
    seen: set[int] = set()
    for item in (library or {}).values():
        if id(item) in seen:
            continue
        seen.add(id(item))
        for path, key in (getattr(item, "collection_keys", None) or {}).items():
            if key and path:
                catalog[str(key)] = str(path)
    return catalog


def remember_collection_keys(
    selection: dict[str, Any], library: dict[str, ZoteroItem] | None
) -> dict[str, Any]:
    """Attach the Zotero key of each chosen collection, keeping known ones."""
    by_path = {path.lower(): key for key, path in collection_catalog(library).items()}
    if not by_path:
        return selection
    previous = {name.lower(): key for name, key in selection["collection_keys"].items()}
    keys: dict[str, str] = {}
    for name in selection["collections"]:
        key = by_path.get(name.lower()) or previous.get(name.lower())
        if key:
            keys[name] = key
    return {**selection, "collection_keys": keys}


def resolve_selection(
    selection: dict[str, Any], library: dict[str, ZoteroItem] | None
) -> dict[str, Any]:
    """The choice with each collection under its current name.

    A collection renamed or moved in Zotero keeps its key: the choice follows
    it instead of matching nothing (which would empty the index on reload).
    Without a key, or with a key Zotero no longer has, the saved name stays.
    """
    selection = normalize_selection(selection)
    catalog = collection_catalog(library)
    if not catalog or not selection["collection_keys"]:
        return selection
    names: list[str] = []
    keys: dict[str, str] = {}
    seen: set[str] = set()
    for name in selection["collections"]:
        key = selection["collection_keys"].get(name, "")
        current = catalog.get(key, name) if key else name
        if current.lower() in seen:
            continue
        seen.add(current.lower())
        names.append(current)
        if key:
            keys[current] = key
    return {**selection, "collections": names, "collection_keys": keys}


def _known_collection_paths(library: dict[str, ZoteroItem] | None) -> set[str]:
    paths: set[str] = set()
    for item in (library or {}).values():
        for path in item.collections or []:
            parts = [part.strip() for part in str(path).split(COLLECTION_SEPARATOR) if part.strip()]
            for depth in range(1, len(parts) + 1):
                paths.add(COLLECTION_SEPARATOR.join(parts[:depth]).lower())
    return paths


def selection_problem(
    selection: dict[str, Any] | None, library: dict[str, ZoteroItem] | None
) -> dict[str, str] | None:
    """Why a reload must not apply this choice now, or None.

    Both cases would silently rewrite the index: without the Zotero records a
    collection choice cannot be applied (it used to be ignored, which filled a
    "HTS only" library with every document), and a choice whose collections
    all left Zotero would remove every document.
    """
    selection = resolve_selection(
        normalize_selection(selection if selection is not None else get_index_selection()),
        library,
    )
    if selection["collection_mode"] == "all" or not selection["collections"]:
        return None
    if not library:
        return {
            "code": "selection_metadata_missing",
            "msg": (
                "Les collections Zotero ne sont pas lisibles pour le moment : "
                "le rechargement est suspendu pour ne pas changer le contenu de "
                "cette bibliothèque. Vérifiez la source des métadonnées puis réessayez."
            ),
        }
    known = _known_collection_paths(library)
    if selection["collection_mode"] == "include" and not any(
        name.lower() in known for name in selection["collections"]
    ):
        return {
            "code": "selection_collections_missing",
            "msg": (
                "Aucune des collections choisies n'existe plus dans Zotero : "
                "le rechargement est suspendu pour ne pas vider cette bibliothèque. "
                "Choisissez d'autres collections dans Réglages > Mes documents."
            ),
        }
    return None


def _norm_path(value: str) -> str:
    return str(value or "").replace("\\", "/").strip("/").lower()


def _in_collection(item_collections: list[str], selected: list[str]) -> bool:
    """A selected collection also covers its sub-collections."""
    for path in item_collections:
        clean = str(path or "").strip().lower()
        if not clean:
            continue
        for wanted in selected:
            if clean == wanted or clean.startswith(wanted + COLLECTION_SEPARATOR.lower()):
                return True
    return False


def apply_index_selection(
    entries: list[tuple[str, str]],
    library: dict[str, ZoteroItem] | None,
    selection: dict[str, Any] | None = None,
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Split ``(rel_path, full_path)`` entries into kept and skipped.

    Each skipped entry says why: ``folder`` (an excluded sub-folder),
    ``collection`` (outside the chosen Zotero collections), ``too_large``, or
    ``metadata_missing``: a collection choice cannot be applied without the
    Zotero records, so nothing is kept rather than every document (a reload
    checks ``selection_problem`` first and stops).
    """
    selection = resolve_selection(
        normalize_selection(selection if selection is not None else get_index_selection()),
        library,
    )
    folders = [_norm_path(folder) for folder in selection["excluded_folders"]]
    mode = selection["collection_mode"]
    collections = [path.strip().lower() for path in selection["collections"]]
    use_collections = mode != "all" and bool(collections)
    if use_collections and not library:
        return [], [{"rel_path": rel, "reason": "metadata_missing"} for rel, _full in entries]
    max_bytes = int(selection["max_file_mb"]) * 1024 * 1024
    allowed_large = {_norm_path(path) for path in selection["allowed_large"]}

    kept: list[tuple[str, str]] = []
    skipped: list[dict[str, Any]] = []
    for rel_path, full_path in entries:
        norm = _norm_path(rel_path)
        if any(norm == folder or norm.startswith(folder + "/") for folder in folders):
            skipped.append({"rel_path": rel_path, "reason": "folder"})
            continue
        if use_collections:
            item = lookup_zotero_item(rel_path, library or {}, full_path)
            inside = item is not None and _in_collection(list(item.collections or []), collections)
            if (mode == "include" and not inside) or (mode == "exclude" and inside):
                skipped.append({"rel_path": rel_path, "reason": "collection"})
                continue
        if max_bytes and norm not in allowed_large:
            try:
                size = os.path.getsize(full_path)
            except OSError:
                size = 0
            if size > max_bytes:
                skipped.append({"rel_path": rel_path, "reason": "too_large", "size": size})
                continue
        kept.append((rel_path, full_path))
    return kept, skipped


def remember_skipped(skipped: list[dict[str, Any]]) -> None:
    global _last_skipped
    with _last_skipped_lock:
        _last_skipped = list(skipped)


def last_skipped() -> list[dict[str, Any]]:
    with _last_skipped_lock:
        return list(_last_skipped)


def summarize_skipped(skipped: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"folder": 0, "collection": 0, "too_large": 0, "metadata_missing": 0}
    for entry in skipped:
        reason = str(entry.get("reason") or "")
        if reason in counts:
            counts[reason] += 1
    return counts
