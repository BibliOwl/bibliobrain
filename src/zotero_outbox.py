"""PDFs waiting for Zotero.

Zotero has no watched folder: a file only joins its library through the running
application, and a file merely copied into its storage is unknown to it. So a
PDF dropped while Zotero cannot answer (closed, or held at its update prompt)
is kept here and sent as soon as the connector replies, without asking the user
to drop it again.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from src import config, zotero_drops
from src.zotero_connector import save_pdf_to_zotero, zotero_is_running

_logger = logging.getLogger(__name__)

_POLL_SECONDS = 5.0

_lock = threading.Lock()
_worker: threading.Thread | None = None
# Deliveries not yet reported to a page; see collect().
_delivered: list[dict[str, str]] = []


def _outbox() -> Path:
    return Path(config.BIBLIOBRAIN_HOME) / "uploads" / "zotero-outbox"


def _read(entry: Path) -> dict:
    try:
        data = json.loads(entry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _order(entry: Path) -> int:
    try:
        return int(_read(entry).get("order", 0))
    except (TypeError, ValueError):
        return 0


def _entries() -> list[Path]:
    """One ``.json`` per waiting PDF, in the order they were dropped."""
    return sorted(_outbox().glob("*.json"), key=_order)


def pending_count() -> int:
    return len(_entries())


def enqueue(staged: Path, filename: str, sha256: str) -> None:
    """Take ownership of ``staged``; the same PDF dropped twice waits once."""
    outbox = _outbox()
    outbox.mkdir(parents=True, exist_ok=True)
    pdf = outbox / f"{sha256}.pdf"
    entry = outbox / f"{sha256}.json"
    with _lock:
        # A PDF already waiting keeps its name and its place in the queue.
        if not entry.exists():
            if not pdf.exists():
                staged.replace(pdf)
            order = max((_order(other) for other in _entries()), default=0) + 1
            # Written last, in one step: an entry always has its PDF, and the
            # delivery thread never reads half an entry.
            draft = entry.with_suffix(".tmp")
            draft.write_text(json.dumps({"filename": filename, "order": order}), encoding="utf-8")
            draft.replace(entry)
    resume()


def resume() -> None:
    """Start delivering when PDFs are waiting; also called when the server starts."""
    global _worker

    with _lock:
        if _worker is not None or not _entries():
            return
        _worker = threading.Thread(
            target=_deliver_until_empty, name="bibliobrain-zotero-outbox", daemon=True
        )
        _worker.start()


def collect() -> dict:
    """How many PDFs still wait, and what was delivered since the last call."""
    with _lock:
        delivered = list(_delivered)
        _delivered.clear()
    return {"pending": pending_count(), "delivered": delivered}


def _deliver_until_empty() -> None:
    global _worker

    while True:
        try:
            if zotero_is_running():
                _deliver_pending()
        except Exception:
            _logger.exception("Zotero outbox delivery failed; retrying")
        with _lock:
            # Checked under the lock so an enqueue() racing with this exit
            # starts a new worker instead of relying on this one.
            if not _entries():
                _worker = None
                return
        time.sleep(_POLL_SECONDS)


def _deliver_pending() -> None:
    for entry in _entries():
        pdf = entry.with_suffix(".pdf")
        if not pdf.is_file():
            entry.unlink(missing_ok=True)
            continue
        filename = str(_read(entry).get("filename") or "document.pdf")

        result = save_pdf_to_zotero(pdf, filename=filename)
        if not result.saved and result.error != "rejected":
            # Zotero went away again: keep this PDF and the following ones.
            return
        if not result.saved:
            status = "zotero_rejected"
        elif result.recognized:
            status = "zotero_added"
        else:
            status = "zotero_unrecognized"
            # The entry is named after the PDF's SHA-256, see enqueue().
            zotero_drops.remember(pdf, filename, entry.stem)
        with _lock:
            _delivered.append({"filename": filename, "status": status})
        entry.unlink(missing_ok=True)
        pdf.unlink(missing_ok=True)
