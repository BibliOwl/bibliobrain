"""Helpers for safe local file access under configured roots."""

from __future__ import annotations

import os
from pathlib import Path

from src.document_formats import SUPPORTED_DOCUMENT_EXTENSIONS


def _normalize_requested_path(requested_path: str) -> Path | None:
    raw = str(requested_path or "").strip().replace("\\", "/")
    if not raw:
        return None

    parts: list[str] = []
    for part in raw.split("/"):
        cleaned = part.strip()
        if not cleaned or cleaned == ".":
            continue
        if cleaned == "..":
            return None
        parts.append(cleaned)

    if not parts:
        return None
    return Path(*parts)


def resolve_document_path(
    data_path: str,
    requested_path: str,
    *,
    allowed_extensions: frozenset[str] = SUPPORTED_DOCUMENT_EXTENSIONS,
) -> Path | None:
    """Resolve a requested document path strictly under ``data_path``.

    The request path must stay inside ``data_path`` after normalization.
    For backward compatibility, a leading ``storage/`` prefix is tolerated
    and stripped when resolving the candidate path.
    """

    normalized_rel = _normalize_requested_path(requested_path)
    if normalized_rel is None:
        return None

    base_real = Path(os.path.realpath(str(Path(data_path))))
    candidates = [normalized_rel]
    if normalized_rel.parts and normalized_rel.parts[0].lower() == "storage":
        stripped = Path(*normalized_rel.parts[1:])
        if stripped.parts:
            candidates.append(stripped)

    base_real_str = str(base_real)
    for rel_path in candidates:
        candidate_real = Path(os.path.realpath(str(base_real / rel_path)))
        candidate_str = str(candidate_real)
        if not (candidate_str == base_real_str or candidate_str.startswith(base_real_str + os.sep)):
            continue
        if candidate_real.is_file() and candidate_real.suffix.lower() in allowed_extensions:
            return candidate_real

    return None


def resolve_pdf_path(data_path: str, requested_path: str) -> Path | None:
    """Backward-compatible PDF-only resolver."""
    return resolve_document_path(
        data_path,
        requested_path,
        allowed_extensions=frozenset({".pdf"}),
    )
