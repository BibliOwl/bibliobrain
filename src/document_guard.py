"""Admission checks run before any parser touches a document.

A document reaches the library by many routes (drop, Zotero storage, a folder
copied by hand, the LAN upload), so the parsers cannot assume the upload route
already vetted it. The checks here are deliberately cheap (one ``stat`` and a
1 KiB read) and format-aware: a size ceiling and the container signature the
extension promises.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

_MIB = 1024 * 1024
_HEAD_BYTES = 1024

PDF_EXTENSIONS = frozenset({".pdf"})
ZIP_CONTAINER_EXTENSIONS = frozenset({".docx", ".odt", ".epub", ".pptx"})

_DEFAULT_BINARY_MIB = 768
_TEXT_LIMIT_BYTES = 24 * _MIB

# Signatures that never belong in a plain-text file.
_BINARY_SIGNATURES = (b"%PDF-", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x7fELF")


class DocumentRejected(ValueError):
    """The file failed an admission check; the message is user-facing."""


def _binary_limit_bytes() -> int:
    raw = os.environ.get("BIBLIOBRAIN_MAX_DOCUMENT_MB", "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_BINARY_MIB
    except ValueError:
        value = _DEFAULT_BINARY_MIB
    return max(1, value) * _MIB


def max_document_bytes(suffix: str) -> int:
    """Largest file the parsers accept for this extension."""
    suffix = suffix.lower()
    if suffix in PDF_EXTENSIONS or suffix in ZIP_CONTAINER_EXTENSIONS:
        return _binary_limit_bytes()
    return _TEXT_LIMIT_BYTES


def content_matches(head: bytes, suffix: str) -> bool:
    """Whether the first bytes of a file fit what its extension promises."""
    suffix = suffix.lower()
    if suffix in PDF_EXTENSIONS:
        # The PDF header may follow a few junk bytes.
        return b"%PDF-" in head
    if suffix in ZIP_CONTAINER_EXTENSIONS:
        return head.startswith(b"PK\x03\x04")
    if head.startswith(_BINARY_SIGNATURES):
        return False
    # "MZ" alone also opens plain text; a Windows executable carries a PE header.
    return not (head.startswith(b"MZ") and b"PE" + bytes(2) in head)


def check_document(path: str | os.PathLike[str]) -> None:
    """Raise ``DocumentRejected`` unless the file is safe to hand to a parser."""
    full_path = Path(path)
    suffix = full_path.suffix.lower()
    try:
        info = full_path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise DocumentRejected("Ce n'est pas un fichier ordinaire")
        limit = max_document_bytes(suffix)
        if info.st_size > limit:
            raise DocumentRejected(
                f"Fichier trop volumineux ({info.st_size // _MIB} Mo, maximum {limit // _MIB} Mo)"
            )
        with full_path.open("rb") as source:
            head = source.read(_HEAD_BYTES)
    except OSError as exc:
        raise DocumentRejected(f"Fichier illisible: {exc}") from exc
    if not content_matches(head, suffix):
        raise DocumentRejected(f"Le contenu ne correspond pas au format {suffix or 'attendu'}")
