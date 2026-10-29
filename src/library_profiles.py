"""Separate libraries ("HTS", "Nb3Sn") built from the same PDF folder.

A profile is a name, a selection of what to read (src/index_selection.py) and
an index folder of its own: each index holds only its selection, so it is
quicker to build and lighter. Switching profile loads the other index, which
takes seconds once it is built.

The main library keeps the historical index folder (CHROMA_PATH) and the
historical ``index_selection`` setting, so an existing install sees no change.
Other profiles keep their indexes under ``CHROMA_PATH/libraries/<id>``.
All profiles read the same PDF folder and Zotero library: a bibliography given
by several people is grouped in a Zotero collection or a sub-folder.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from typing import Any

DEFAULT_PROFILE_ID = "default"
LIBRARIES_DIR = "libraries"
PROFILES_KEY = "library_profiles"
SELECTION_KEY = "index_selection"
MAX_PROFILES = 30
MAX_NAME_LENGTH = 60

_lock = threading.RLock()


class ProfileError(ValueError):
    """A profile request that cannot be applied; ``code`` names the reason."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _payload() -> dict[str, Any]:
    from src.app_settings import _load_app_settings_payload

    return _load_app_settings_payload()


def _save(update: dict[str, Any]) -> None:
    from src.app_settings import _save_app_settings_payload

    _save_app_settings_payload(update)


def _clean_name(value: Any) -> str:
    name = " ".join(str(value or "").split())[:MAX_NAME_LENGTH]
    return name


def _slug(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug[:32] or "bibliotheque"


def _stored() -> dict[str, Any]:
    raw = _payload().get(PROFILES_KEY)
    data = raw if isinstance(raw, dict) else {}
    items: list[dict[str, Any]] = []
    seen: set[str] = {DEFAULT_PROFILE_ID}
    for entry in data.get("items") or []:
        if not isinstance(entry, dict):
            continue
        profile_id = str(entry.get("id") or "").strip()
        name = _clean_name(entry.get("name"))
        if not profile_id or profile_id in seen or not re.fullmatch(r"[a-z0-9-]{1,40}", profile_id):
            continue
        seen.add(profile_id)
        items.append(
            {
                "id": profile_id,
                "name": name or profile_id,
                "selection": entry.get("selection") or {},
            }
        )
    active = str(data.get("active") or DEFAULT_PROFILE_ID)
    if active not in seen:
        active = DEFAULT_PROFILE_ID
    return {
        "active": active,
        "default_name": _clean_name(data.get("default_name")),
        "items": items,
    }


def _write(stored: dict[str, Any]) -> None:
    _save(
        {
            PROFILES_KEY: {
                "active": stored["active"],
                "default_name": stored.get("default_name") or "",
                "items": stored["items"],
            }
        }
    )


def list_profiles() -> dict[str, Any]:
    """Every profile, the main library first, with the active one marked."""
    stored = _stored()
    profiles = [
        {
            "id": DEFAULT_PROFILE_ID,
            "name": stored["default_name"],
            "is_default": True,
            "selection": _payload().get(SELECTION_KEY) or {},
        }
    ]
    profiles += [{**item, "is_default": False} for item in stored["items"]]
    for profile in profiles:
        profile["active"] = profile["id"] == stored["active"]
        profile["index_root"] = profile_index_root(profile["id"])
    return {"active": stored["active"], "profiles": profiles}


def active_profile() -> dict[str, Any]:
    listing = list_profiles()
    return next(p for p in listing["profiles"] if p["active"])


def active_profile_id() -> str:
    return _stored()["active"]


def profile_index_root(profile_id: str, base: str | None = None) -> str:
    """The folder that holds every index of a profile (under ``base``, CHROMA_PATH)."""
    if base is None:
        from src.rag import vector_store

        base = str(vector_store.CHROMA_PATH)
    if profile_id == DEFAULT_PROFILE_ID:
        return base
    return os.path.join(base, LIBRARIES_DIR, profile_id)


def active_index_root() -> str:
    return profile_index_root(active_profile_id())


def save_active_selection(selection: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        stored = _stored()
        if stored["active"] == DEFAULT_PROFILE_ID:
            _save({SELECTION_KEY: selection})
            return selection
        for item in stored["items"]:
            if item["id"] == stored["active"]:
                item["selection"] = selection
        _write(stored)
        return selection


def create_profile(name: Any, selection: dict[str, Any] | None = None) -> dict[str, Any]:
    clean = _clean_name(name)
    if not clean:
        raise ProfileError("name_required", "Donnez un nom à cette bibliothèque.")
    with _lock:
        stored = _stored()
        if len(stored["items"]) + 1 >= MAX_PROFILES:
            raise ProfileError("too_many_profiles", "Trop de bibliothèques.")
        taken = {item["id"] for item in stored["items"]} | {DEFAULT_PROFILE_ID}
        base = _slug(clean)
        profile_id = base
        suffix = 2
        while profile_id in taken:
            profile_id = f"{base[:28]}-{suffix}"
            suffix += 1
        from src.index_selection import SUGGESTED_MAX_FILE_MB, normalize_selection

        item = {
            "id": profile_id,
            "name": clean,
            # A new library has no index yet: the size guard can start on.
            "selection": normalize_selection(selection or {"max_file_mb": SUGGESTED_MAX_FILE_MB}),
        }
        stored["items"].append(item)
        _write(stored)
        return {**item, "is_default": False}


def rename_profile(profile_id: str, name: Any) -> None:
    clean = _clean_name(name)
    with _lock:
        stored = _stored()
        if profile_id == DEFAULT_PROFILE_ID:
            stored["default_name"] = clean
        else:
            if not clean:
                raise ProfileError("name_required", "Donnez un nom à cette bibliothèque.")
            matches = [item for item in stored["items"] if item["id"] == profile_id]
            if not matches:
                raise ProfileError("not_found", "Bibliothèque introuvable.")
            matches[0]["name"] = clean
        _write(stored)


def set_active_profile(profile_id: str) -> None:
    with _lock:
        stored = _stored()
        known = {item["id"] for item in stored["items"]} | {DEFAULT_PROFILE_ID}
        if profile_id not in known:
            raise ProfileError("not_found", "Bibliothèque introuvable.")
        stored["active"] = profile_id
        _write(stored)


def delete_profile(profile_id: str) -> str:
    """Forget a profile and return its index folder, for the caller to remove."""
    if profile_id == DEFAULT_PROFILE_ID:
        raise ProfileError(
            "default_profile", "La bibliothèque principale ne peut pas être supprimée."
        )
    with _lock:
        stored = _stored()
        remaining = [item for item in stored["items"] if item["id"] != profile_id]
        if len(remaining) == len(stored["items"]):
            raise ProfileError("not_found", "Bibliothèque introuvable.")
        if stored["active"] == profile_id:
            raise ProfileError("active_profile", "Changez d’abord de bibliothèque.")
        stored["items"] = remaining
        _write(stored)
    return profile_index_root(profile_id)
