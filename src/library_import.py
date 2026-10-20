"""Receive a document dropped on the app and hand it to the library.

The library is whatever the index scans. In a plain documents folder the file
is copied into ``imports/`` and the next incremental reload indexes it. Zotero's
storage folder belongs to Zotero, and the index ignores any file Zotero does
not list, so there the file is sent to Zotero instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
import unicodedata
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from src import config, zotero_drops, zotero_outbox
from src.document_formats import SUPPORTED_DOCUMENT_EXTENSIONS
from src.document_guard import content_matches
from src.http_security import max_upload_bytes
from src.zotero_connector import (
    ensure_zotero_running,
    save_pdf_to_zotero,
    zotero_is_running,
)
from src.zotero_db import filter_active_zotero_pdf_entries, is_zotero_storage_path

_logger = logging.getLogger(__name__)

IMPORT_FOLDER = "imports"

_FORBIDDEN_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_MAX_STEM_CHARS = 120


class ImportRejected(Exception):
    """The dropped file cannot join the library; ``code`` is stable for the UI."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def library_is_zotero_storage() -> bool:
    return is_zotero_storage_path(config.DATA_PATH, config.ZOTERO_DB_PATH)


def upload_target() -> dict[str, Any]:
    """What the drop zone may accept, so it can refuse a file before sending it."""
    to_zotero = library_is_zotero_storage()
    target: dict[str, Any] = {
        "target": "zotero" if to_zotero else "folder",
        "extensions": [".pdf"] if to_zotero else sorted(SUPPORTED_DOCUMENT_EXTENSIONS),
        "max_bytes": max_upload_bytes(),
    }
    if to_zotero:
        # Closed, Zotero is started by the first upload: the page says so.
        target["zotero_running"] = zotero_is_running()
    return target


def safe_document_name(filename: str) -> str:
    """Reduce a client-supplied name to a bare, portable file name."""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = _FORBIDDEN_NAME_CHARS.sub("_", name).strip(" .")
    stem, suffix = os.path.splitext(name)
    suffix = suffix.lower()
    if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise ImportRejected(
            "unsupported_format",
            "Format non pris en charge.",
            status_code=415,
        )
    stem = stem.strip(" .")[:_MAX_STEM_CHARS].strip(" .") or "document"
    if stem.lower() in _WINDOWS_RESERVED_NAMES:
        stem = f"_{stem}"
    return f"{stem}{suffix}"


async def _stage(
    chunks: AsyncIterator[bytes], directory: Path, suffix: str
) -> tuple[Path, int, str]:
    """Write the body to a ``.part`` file the library scan ignores."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(dir=directory, prefix=".import-", suffix=".part")
    except OSError as exc:
        _logger.exception("Unable to stage a dropped document")
        raise ImportRejected(
            "storage_failed", "Impossible d’enregistrer le fichier.", status_code=500
        ) from exc

    staged = Path(raw_path)
    limit = max_upload_bytes()
    digest = hashlib.sha256()
    size = 0
    head = b""
    try:
        with os.fdopen(descriptor, "wb") as handle:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise ImportRejected(
                        "file_too_large", "Fichier trop volumineux.", status_code=413
                    )
                if len(head) < 1024:
                    head += chunk[: 1024 - len(head)]
                digest.update(chunk)
                await asyncio.to_thread(handle.write, chunk)
        if not size:
            raise ImportRejected("empty_file", "Le fichier est vide.")
        if not content_matches(head, suffix):
            raise ImportRejected(
                "content_mismatch",
                "Le contenu du fichier ne correspond pas à son extension.",
                status_code=415,
            )
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    return staged, size, digest.hexdigest()


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _find_identical_document(size: int, sha256: str) -> str:
    """Relative path of a library document with the same bytes, or ""."""
    from src.rag import state

    with state._state_lock:
        zotero_library = dict(state._zotero_library)
    documents = filter_active_zotero_pdf_entries(
        state.find_documents(),
        zotero_library,
        data_path=config.DATA_PATH,
        zotero_db_path=config.ZOTERO_DB_PATH,
    )
    for rel_path, full_path in documents:
        try:
            # Hashing only same-size files keeps this to a stat per document.
            if os.path.getsize(full_path) == size and _sha256(full_path) == sha256:
                return Path(rel_path).as_posix()
        except OSError:
            continue
    return ""


def _send_to_zotero(staged: Path, name: str, sha256: str) -> dict[str, Any]:
    """Give the PDF to Zotero, starting Zotero first when it is closed.

    A Zotero that still does not answer (held at its update prompt, not
    installed here) gets the PDF later, from the outbox.
    """
    queued = {"status": "zotero_queued", "filename": name}
    if zotero_outbox.pending_count():
        # Earlier PDFs still wait: keep the drop order and do not wait again.
        zotero_outbox.enqueue(staged, name, sha256)
        return queued

    result = save_pdf_to_zotero(staged, filename=name)
    if result.error == "unreachable" and ensure_zotero_running() in {"running", "started"}:
        result = save_pdf_to_zotero(staged, filename=name)
    if result.error == "unreachable":
        zotero_outbox.enqueue(staged, name, sha256)
        return queued
    if result.error == "rejected":
        raise ImportRejected("zotero_rejected", "Zotero a refusé le fichier.", status_code=502)
    if not result.saved:
        raise ImportRejected(
            "zotero_disabled",
            "L’envoi vers Zotero est désactivé dans la configuration.",
            status_code=503,
        )
    if not result.recognized:
        # A standalone attachment in Zotero: indexed all the same, see zotero_drops.
        zotero_drops.remember(staged, name, sha256)
    return {
        "status": "zotero_added" if result.recognized else "zotero_unrecognized",
        "filename": name,
        "title": result.title,
    }


def _move_into_library(staged: Path, name: str) -> str:
    from src.rag import state

    stem, suffix = os.path.splitext(name)
    target = staged.parent / name
    counter = 2
    while target.exists():
        target = staged.parent / f"{stem}_{counter}{suffix}"
        counter += 1
    staged.rename(target)
    state.clear_document_scan_cache()
    return target.relative_to(Path(config.DATA_PATH)).as_posix()


async def receive_document(filename: str, chunks: AsyncIterator[bytes]) -> dict[str, Any]:
    """Store one dropped document; ``chunks`` is the raw file.

    Returns ``status``: ``added`` (copied, waiting for the next reload),
    ``duplicate`` (already in the library), ``zotero_added`` (Zotero filed it
    under a recognized record), ``zotero_unrecognized`` (in Zotero without a
    record, indexed under its file name) or ``zotero_queued`` (kept for a
    Zotero that is not answering yet).
    """
    name = safe_document_name(filename)
    suffix = Path(name).suffix
    to_zotero = library_is_zotero_storage()
    if to_zotero and suffix != ".pdf":
        raise ImportRejected(
            "zotero_pdf_only",
            "Avec une bibliothèque Zotero, seuls les PDF peuvent être ajoutés.",
            status_code=415,
        )

    # Staged next to its destination so the final move is a rename; a file
    # bound for Zotero only passes through the app's own folder.
    staging_dir = (
        Path(config.BIBLIOBRAIN_HOME) / "uploads"
        if to_zotero
        else Path(config.DATA_PATH) / IMPORT_FOLDER
    )
    staged, size, sha256 = await _stage(chunks, staging_dir, suffix)
    try:
        existing = await asyncio.to_thread(_find_identical_document, size, sha256)
        if existing:
            return {"status": "duplicate", "filename": name, "rel_path": existing}

        if to_zotero:
            return await asyncio.to_thread(_send_to_zotero, staged, name, sha256)

        try:
            rel_path = await asyncio.to_thread(_move_into_library, staged, name)
        except OSError as exc:
            _logger.exception("Unable to move a dropped document into the library")
            raise ImportRejected(
                "storage_failed", "Impossible d’enregistrer le fichier.", status_code=500
            ) from exc
        return {"status": "added", "filename": name, "rel_path": rel_path}
    finally:
        staged.unlink(missing_ok=True)
