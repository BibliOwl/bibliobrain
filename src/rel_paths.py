"""Shared helpers to normalize library-relative paths across modules."""

from __future__ import annotations

import os

from src import config


def normalize_rel_path(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def candidate_rel_paths(path: str) -> set[str]:
    normalized = normalize_rel_path(path)
    if not normalized:
        return set()

    candidates = {normalized}
    if normalized.startswith("storage/"):
        candidates.add(normalized[len("storage/") :])

    data_path_name = os.path.basename(os.path.normpath(config.DATA_PATH or "")).lower()
    if data_path_name == "storage":
        candidates.add(f"storage/{normalized}")

    return {candidate for candidate in candidates if candidate}
