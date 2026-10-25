"""PDFs dropped into Bibliobrain that Zotero found no record for.

Zotero keeps such a PDF as a standalone attachment, with no parent item, and
the index normally skips those. A PDF the user dropped into Bibliobrain is
still meant to be searchable, so its size and SHA-256 are kept here and the
Zotero loader indexes the standalone attachment holding the same bytes. Every
other standalone PDF of the library stays out of the index, as before.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from src import config
from src.atomic_io import atomic_write_text

_logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _registry() -> Path:
    return Path(config.BIBLIOBRAIN_HOME) / "zotero-dropped-pdfs.json"


def _read() -> list[dict]:
    try:
        data = json.loads(_registry().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        _logger.warning("Unreadable list of PDFs dropped without a Zotero record", exc_info=True)
        return []
    return [entry for entry in data if isinstance(entry, dict)] if isinstance(data, list) else []


def remember(pdf: Path, filename: str, sha256: str) -> None:
    """Index the standalone attachment Zotero made from ``pdf``."""
    size = pdf.stat().st_size
    with _lock:
        entries = _read()
        if any(entry.get("sha256") == sha256 for entry in entries):
            return
        entries.append({"sha256": sha256, "size": size, "filename": filename})
        registry = _registry()
        atomic_write_text(registry, json.dumps(entries, ensure_ascii=False, indent=1))


def fingerprints() -> frozenset[tuple[int, str]]:
    """``(size, sha256)`` of every PDF remembered, for the Zotero loader."""
    with _lock:
        entries = _read()
    found: set[tuple[int, str]] = set()
    for entry in entries:
        try:
            found.add((int(entry["size"]), str(entry["sha256"])))
        except (KeyError, TypeError, ValueError):
            continue
    return frozenset(found)
