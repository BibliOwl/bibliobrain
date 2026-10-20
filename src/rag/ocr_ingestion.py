"""Resumable page enrichment of an existing index, without re-embedding native pages."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from src import document_ocr as ocr

from . import state

_logger = logging.getLogger(__name__)
_stop = threading.Event()
_thread: threading.Thread | None = None
_lock = threading.Lock()

# A pass with nothing new used to hash every PDF and replay every cached page
# (~12 min on 500 papers) while holding the index lock. The ledger records,
# per index, the PDFs already OCR'd into it: such a PDF is skipped while its
# file, the OCR model and the OCR versions are unchanged and the index still
# holds its OCR pages (reloading a document deletes all of its chunks).
LEDGER_VERSION = 1


def _pdf_only(pdfs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    # find_pdfs is a legacy alias for find_documents and includes text/Office
    # files. Enforce the OCR boundary here for normal runs, retries and callers.
    return [
        (rel_path, full_path)
        for rel_path, full_path in pdfs
        if Path(full_path).suffix.lower() == ".pdf"
    ]


def _ledger_path(index_path: str) -> Path:
    key = hashlib.sha256(os.path.normcase(os.path.abspath(index_path)).encode()).hexdigest()
    return ocr.root() / "indexed" / f"{key[:24]}.json"


def _load_ledger(index_path: str) -> dict[str, dict]:
    saved = ocr.read_json(_ledger_path(index_path))
    if saved.get("version") != LEDGER_VERSION or saved.get("index_path") != str(index_path):
        return {}
    return dict(saved.get("documents") or {})


def _save_ledger(index_path: str, documents: dict[str, dict]) -> None:
    ocr.write_json(
        _ledger_path(index_path),
        {"version": LEDGER_VERSION, "index_path": str(index_path), "documents": documents},
    )


def _file_signature(full_path: str) -> dict[str, int] | None:
    try:
        stat = Path(full_path).stat()
    except OSError:
        return None
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _ocr_versions() -> list:
    return [ocr.VERSION, ocr.DETECTION_VERSION, ocr.BLANK_DETECTOR]


def _ocr_pages_in_index(vectorstore) -> dict[str, set[int]]:
    """One query for the whole index instead of one per PDF."""
    found = vectorstore.get(where={"extraction_method": "ocr"}, include=["metadatas"])
    pages: dict[str, set[int]] = {}
    for metadata in found.get("metadatas") or []:
        if metadata and metadata.get("rel_path") and metadata.get("page") is not None:
            pages.setdefault(metadata["rel_path"], set()).add(int(metadata["page"]))
    return pages


def _entry_is_current(
    entry: dict | None,
    signature: dict | None,
    digest: str,
    chunk_size: int,
    indexed_pages: set[int],
) -> bool:
    return bool(
        entry
        and signature
        and entry.get("signature") == signature
        and entry.get("model_digest") == digest
        and entry.get("versions") == _ocr_versions()
        and entry.get("chunk_size") == chunk_size
        and set(entry.get("ocr_pages") or []) <= indexed_pages
    )


def _ledger_entry(signature, digest, chunk_size, *, pages_total, candidates, ocr_pages=(), blank=0, errors=()):
    return {
        "signature": signature,
        "model_digest": digest,
        "versions": _ocr_versions(),
        "chunk_size": chunk_size,
        "pages_total": pages_total,
        "candidates": candidates,
        "ocr_pages": sorted(ocr_pages),
        "blank": blank,
        "errors": list(errors),
    }


def _replay_entry(report: dict[str, Any], entry: dict) -> None:
    """Count a skipped PDF as the pass that recorded it did."""
    report["pages_total"] += int(entry.get("pages_total") or 0)
    report["candidates"] += int(entry.get("candidates") or 0)
    report["completed"] += int(entry.get("candidates") or 0)
    report["indexed_pages"] += len(entry.get("ocr_pages") or [])
    report["blank"] += int(entry.get("blank") or 0)
    errors = list(entry.get("errors") or [])
    report["review"] += sum(1 for error in errors if error.get("page") is not None)
    report["errors"].extend(errors)
    report["skipped_documents"] += 1


def pending_documents(
    vectorstore,
    index_path: str,
    pdfs: list[tuple[str, str]],
    *,
    chunk_size: int,
    digest: str,
) -> list[str]:
    """PDFs an OCR pass would have to open; empty means it has nothing to do."""
    ledger = _load_ledger(index_path)
    indexed = _ocr_pages_in_index(vectorstore) if ledger else {}
    return [
        rel_path
        for rel_path, full_path in _pdf_only(pdfs)
        if not _entry_is_current(
            ledger.get(rel_path),
            _file_signature(full_path),
            digest,
            chunk_size,
            indexed.get(rel_path, set()),
        )
    ]


def needs_pass(vectorstore, index_path: str, chunk_size: int, model: str) -> bool:
    """Whether the automatic pass after a RAG load has anything to do."""
    previous = ocr.read_json(ocr.root() / "progress.json", {})
    if previous.get("status") not in {"completed", "completed_with_review"}:
        return True  # paused, interrupted, failed or never run: resume it
    if previous.get("index_path") != index_path or previous.get("model") != model:
        return True
    try:
        digest = ocr.model_identity(model)
        return bool(
            pending_documents(
                vectorstore, index_path, state.find_documents(), chunk_size=chunk_size, digest=digest
            )
        )
    except Exception:
        # Let the pass run and report the problem (Ollama down, missing model…).
        _logger.debug("Unable to tell whether OCR is up to date.", exc_info=True)
        return True


def update_timing(
    report: dict[str, Any],
    samples: deque[float],
    result: dict[str, Any],
    seconds: float,
) -> None:
    # Cached pages and blank-page skips aren't representative of inference speed.
    if not result.get("cache_hit") and result.get("attempts"):
        report["timed_pages"] += 1
        # The first actual page includes cold model loading; it is useful work,
        # but should not inflate the projected cost of every remaining page.
        if report["timed_pages"] > 1:
            samples.append(max(0.0, seconds))
    report["eta_sample_pages"] = len(samples)
    report["seconds_per_page"] = (
        round(sum(samples) / len(samples), 2) if len(samples) >= 3 else None
    )
    remaining = max(0, report["candidates"] - report["completed"])
    rate = report["seconds_per_page"]
    report["estimated_remaining_seconds"] = round(remaining * rate) if rate is not None else None


def status() -> dict:
    result = ocr.read_json(ocr.root() / "progress.json", {"status": "idle"})
    # Older jobs passed every supported document to the PDF reader. Keep the
    # persisted report intact, but don't offer to retry those non-PDF failures.
    errors = result.get("errors", [])
    result["errors"] = [
        error
        for error in errors
        if not (
            error.get("page") is None
            and Path(error.get("file", "")).suffix
            and Path(error["file"]).suffix.lower() != ".pdf"
        )
    ]
    result["ignored_non_pdf_errors"] = len(errors) - len(result["errors"])
    if (
        result.get("status") == "completed_with_review"
        and not result["errors"]
        and not result.get("review")
    ):
        result["status"] = "completed"
    active = bool(_thread and _thread.is_alive())
    if active and _stop.is_set():
        result["status"] = "pausing"
    if not active and result.get("status") in {"scanning", "running", "pausing"}:
        result["status"] = "interrupted"
    return {**result, "active": active, "settings": ocr.settings()}


def pause() -> dict:
    _stop.set()
    return {"ok": True, "msg": "Pause demandée. La transcription en cours sera interrompue."}


def page_chunks(result: dict, metadata: dict, chunk_size: int) -> list:
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    from .document_loader import _assign_stable_chunk_metadata

    content = ocr.clean_for_index(result["text"])
    metadata = {
        **metadata,
        "page": int(result["page"]),
        "content_type": "pdf_page",
        "extraction_method": "ocr",
        "ocr_model": result["model"],
        "ocr_digest": result["model_digest"],
        "ocr_key": result["key"],
    }
    # Keep HTML table rows together when possible; large rows still obey the limit.
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=min(chunk_size // 5, 200),
        separators=["\n\n", "</tr>", "\n", " ", ""],
        keep_separator="end",
    )
    chunks = splitter.split_documents([Document(page_content=content, metadata=metadata)])
    _assign_stable_chunk_metadata(
        chunks, article_id=metadata["article_id"], content_type="pdf_page"
    )
    for ordinal, chunk in enumerate(chunks):
        # Stable IDs make an interrupted upsert idempotent, including after re-chunking.
        uid = hashlib.sha256(
            f"{metadata['rel_path']}:{result['key']}:{chunk_size}:{ordinal}:{chunk.page_content}".encode()
        ).hexdigest()
        chunk.metadata.update(
            chunk_uid="ocr-" + uid, chunk_id=(int(result["page"]) + 1) * 1_000_000 + ordinal
        )
    return chunks


def index_page(vectorstore, index_path: str, chunks: list) -> int:
    """Commit new vectors before removing stale text; replay repairs the FTS sidecar."""
    if not chunks:
        return 0
    metadata = chunks[0].metadata
    where = {
        "$and": [
            {"rel_path": metadata["rel_path"]},
            {"page": metadata["page"]},
            {"content_type": "pdf_page"},
        ]
    }
    previous = vectorstore.get(where=where, include=["metadatas"])
    old_ids = set(previous["ids"])
    desired = {doc.metadata["chunk_uid"] for doc in chunks}
    missing = [doc for doc in chunks if doc.metadata["chunk_uid"] not in old_ids]
    if missing:
        vectorstore.add_documents(missing, ids=[doc.metadata["chunk_uid"] for doc in missing])
    existing_metadata = dict(zip(previous["ids"], previous.get("metadatas") or []))
    changed = [
        doc
        for doc in chunks
        if doc.metadata["chunk_uid"] in existing_metadata
        and existing_metadata[doc.metadata["chunk_uid"]] != doc.metadata
    ]
    if changed:
        # Metadata-only refresh does not call the embedding model.
        vectorstore._collection.update(
            ids=[doc.metadata["chunk_uid"] for doc in changed],
            metadatas=[doc.metadata for doc in changed],
        )
    stale = sorted(old_ids - desired)
    if stale:
        vectorstore.delete(ids=stale)
    # Both operations are replayed even when vectors already exist (crash recovery).
    # Replacing one page keeps notes, figures and other native pages untouched.
    from .lexical_index import replace_page

    replace_page(index_path, metadata["rel_path"], metadata["page"], chunks)
    return len(missing)


def enrich(
    vectorstore,
    index_path: str,
    pdfs: list[tuple[str, str]],
    *,
    chunk_size: int,
    model: str,
    stop: threading.Event,
    progress=None,
    retry_failed: bool = False,
) -> dict:
    """Usable by the background job and by an isolated acceptance benchmark."""
    from src.zotero_db import lookup_zotero_item

    from .document_loader import _article_id_from_item
    from .zotero_integration import _build_item_metadata

    pdfs = _pdf_only(pdfs)
    digest = ocr.model_identity(model)
    ledger = _load_ledger(index_path)
    indexed = _ocr_pages_in_index(vectorstore) if ledger else {}
    signatures: dict[str, dict | None] = {}
    report: dict[str, Any] = {
        "status": "scanning",
        "model": model,
        "model_digest": digest,
        "started_at": time.time(),
        "documents_total": len(pdfs),
        "documents_scanned": 0,
        "pages_total": 0,
        "candidates": 0,
        "completed": 0,
        "indexed_pages": 0,
        "new_chunks": 0,
        "cache_hits": 0,
        "skipped_documents": 0,
        "blank": 0,
        "review": 0,
        "timed_pages": 0,
        "eta_sample_pages": 0,
        "seconds_per_page": None,
        "estimated_remaining_seconds": None,
        "errors": [],
        "current_file": "",
        "current_page": None,
        "index_path": index_path,
    }
    timings: deque[float] = deque(maxlen=20)

    def publish():
        report["elapsed_seconds"] = round(time.time() - report["started_at"], 1)
        if progress:
            progress(dict(report))

    pending = []
    try:
        for rel_path, full_path in pdfs:
            ocr.check_pause(stop)
            report["current_file"] = rel_path
            # Taken before hashing: a file edited meanwhile gets a new
            # signature next time and is scanned again.
            signature = signatures[rel_path] = _file_signature(full_path)
            entry = ledger.get(rel_path)
            if entry is not None and _entry_is_current(
                entry, signature, digest, chunk_size, indexed.get(rel_path, set())
            ) and not (retry_failed and entry.get("errors")):
                _replay_entry(report, entry)
                report["documents_scanned"] += 1
                continue
            ledger.pop(rel_path, None)
            publish()
            try:
                inventory = ocr.scan_pdf(full_path, stop)
                candidates = [page for page in inventory["pages"] if page["reason"] != "native"]
                report["pages_total"] += len(inventory["pages"])
                report["candidates"] += len(candidates)
                if candidates:
                    pending.append((rel_path, full_path, inventory, candidates))
                elif signature:
                    ledger[rel_path] = _ledger_entry(
                        signature, digest, chunk_size,
                        pages_total=len(inventory["pages"]), candidates=0,
                    )
            except ocr.OCRPaused:
                raise
            except Exception as exc:
                _logger.exception("Unable to inspect PDF for OCR: %s", rel_path)
                error = {"file": rel_path, "page": None, "issue": type(exc).__name__}
                report["errors"].append(error)
                # An unreadable PDF stays so until the file changes; retry_failed
                # still reopens it.
                if signature:
                    ledger[rel_path] = _ledger_entry(
                        signature, digest, chunk_size, pages_total=0, candidates=0, errors=[error]
                    )
            report["documents_scanned"] += 1
            publish()
        current = {rel_path for rel_path, _ in pdfs}
        ledger = {rel_path: entry for rel_path, entry in ledger.items() if rel_path in current}
        _save_ledger(index_path, ledger)
        report["status"] = "running"
        # Short documents first: recover useful articles before long scanned books.
        pending.sort(key=lambda row: len(row[3]))
        for rel_path, full_path, inventory, candidates in pending:
            item = lookup_zotero_item(rel_path, state._zotero_library)
            existing = vectorstore.get(where={"rel_path": rel_path}, limit=1, include=["metadatas"])
            metadata = dict((existing.get("metadatas") or [{}])[0] or {})
            if item or not metadata:
                metadata = _build_item_metadata(rel_path, full_path, item)
            for name in (
                "page",
                "page_label",
                "chunk_id",
                "chunk_uid",
                "start_index",
                "ocr_key",
                "ocr_model",
                "ocr_digest",
                "extraction_method",
            ):
                metadata.pop(name, None)
            metadata.update(
                source=full_path,
                rel_path=rel_path,
                article_id=metadata.get("article_id") or _article_id_from_item(rel_path, item),
            )
            metadata["zotero_title"] = metadata.get("zotero_title") or Path(full_path).stem
            initial_stat = Path(full_path).stat()
            ocr_pages: list[int] = []
            blank_pages = 0
            page_errors: list[dict] = []
            for candidate in candidates:
                ocr.check_pause(stop)
                report.update(current_file=rel_path, current_page=candidate["page"] + 1)
                publish()
                page_started = time.monotonic()
                result = ocr.transcribe_page(
                    full_path,
                    inventory["fingerprint"],
                    candidate["page"],
                    model,
                    digest,
                    stop,
                    retry_failed=retry_failed,
                )
                current_stat = Path(full_path).stat()
                if (current_stat.st_size, current_stat.st_mtime_ns) != (
                    initial_stat.st_size,
                    initial_stat.st_mtime_ns,
                ):
                    raise RuntimeError("Le PDF a changé pendant son traitement. Relancez l’OCR.")
                if result["status"] == "ok":
                    chunks = page_chunks(result, metadata, chunk_size)
                    report["new_chunks"] += index_page(vectorstore, index_path, chunks)
                    report["indexed_pages"] += 1
                    if chunks:
                        ocr_pages.append(int(result["page"]))
                elif result["status"] == "blank":
                    report["blank"] += 1
                    blank_pages += 1
                else:
                    report["review"] += 1
                    error = {
                        "file": rel_path,
                        "page": candidate["page"] + 1,
                        "issue": (
                            result["attempts"][-1]["issue"] if result["attempts"] else "review"
                        ),
                        "cache_key": result["key"],
                    }
                    report["errors"].append(error)
                    page_errors.append(error)
                report["cache_hits"] += int(result["cache_hit"])
                report["completed"] += 1
                update_timing(report, timings, result, time.monotonic() - page_started)
                publish()
            signature = signatures.get(rel_path)
            if signature and signature == _file_signature(full_path):
                ledger[rel_path] = _ledger_entry(
                    signature, digest, chunk_size,
                    pages_total=len(inventory["pages"]), candidates=len(candidates),
                    ocr_pages=ocr_pages, blank=blank_pages, errors=page_errors,
                )
                _save_ledger(index_path, ledger)
        report["status"] = "completed_with_review" if report["errors"] else "completed"
    except ocr.OCRPaused:
        report["status"] = "paused"
    except Exception as exc:
        _logger.exception("OCR enrichment interrupted")
        message = (
            "Écriture ou lecture du cache impossible. Vérifiez l’accès au dossier de données puis reprenez."
            if isinstance(exc, OSError)
            else str(exc)
        )
        report.update(status="error", error=message)
    if report["status"] in {"paused", "error"}:
        report["estimated_remaining_seconds"] = None
    publish()
    return report


def start(*, retry_failed: bool = False, only_if_needed: bool = False) -> dict:
    """Share the normal ingestion lock; retrieval can continue against the live index.

    only_if_needed is for the automatic pass after a RAG load: it does not take
    the lock when the previous pass finished and nothing changed since.
    """
    from src.api import application

    global _thread
    with _lock:
        if _thread and _thread.is_alive():
            return {"ok": True, "already_running": True}
        # The codes let the settings card explain the block in the UI language.
        if not ocr.settings()["enabled"]:
            return {"ok": False, "code": "disabled", "msg": "Activez l’OCR automatique dans les réglages."}
        if only_if_needed:
            with state._state_lock:
                vectorstore, index_path = state.vectorstore, state.runtime_db_path
                chunk_size = state.runtime_chunk_size
            if (
                vectorstore is not None
                and index_path
                and chunk_size
                and not needs_pass(vectorstore, index_path, chunk_size, ocr.settings()["model"])
            ):
                return {"ok": True, "skipped": True, "msg": "OCR déjà à jour."}
        if not application._rag_init_lock.acquire(blocking=False):
            return {"ok": False, "code": "index_busy", "msg": "Une indexation est en cours. Réessayez après sa fin."}
        with state._state_lock:
            vectorstore, index_path = state.vectorstore, state.runtime_db_path
            chunk_size = state.runtime_chunk_size
        if vectorstore is None or not index_path or not chunk_size:
            application._rag_init_lock.release()
            return {"ok": False, "code": "library_not_loaded", "msg": "Chargez d’abord la bibliothèque vectorisée."}
        _stop.clear()
        model = ocr.settings()["model"]
        active = application._begin_managed_operation()

        def run():
            try:
                ocr.write_json(ocr.root() / "progress.json", {"status": "scanning", "model": model})
                enrich(
                    vectorstore,
                    index_path,
                    state.find_documents(),
                    chunk_size=chunk_size,
                    model=model,
                    stop=_stop,
                    retry_failed=retry_failed,
                    progress=lambda report: ocr.write_json(ocr.root() / "progress.json", report),
                )
            except Exception as exc:
                _logger.exception("Unable to start OCR")
                ocr.write_json(ocr.root() / "progress.json", {"status": "error", "error": str(exc)})
            finally:
                application._end_managed_operation(active)
                application._rag_init_lock.release()

        _thread = threading.Thread(target=run, name="bibliobrain-ocr", daemon=True)
        try:
            _thread.start()
        except Exception:
            application._end_managed_operation(active)
            application._rag_init_lock.release()
            raise
    return {"ok": True}


# How many PDFs are scans is only known once a pass has run, and a pass needs
# the OCR model. This dry run counts them first: it reuses scan_pdf's
# content-addressed page inventory, so the real pass later skips that work.
_inventory_stop = threading.Event()
_inventory_thread: threading.Thread | None = None
_inventory_lock = threading.Lock()


def _inventory_path() -> Path:
    return ocr.root() / "library_inventory.json"


def scan_inventory(pdfs: list[tuple[str, str]], stop: threading.Event, progress=None) -> dict:
    """Count scanned pages per PDF; unchanged files keep their previous count."""
    previous = ocr.read_json(_inventory_path()).get("files") or {}
    pdfs = _pdf_only(pdfs)
    report: dict[str, Any] = {
        "status": "running",
        "documents_total": len(pdfs),
        "documents_scanned": 0,
        "documents": 0,
        "pages": 0,
        "unreadable": 0,
    }
    files: dict[str, dict] = {}
    for rel_path, full_path in pdfs:
        ocr.check_pause(stop)
        signature = _file_signature(full_path)
        entry = previous.get(rel_path)
        if not (
            entry
            and signature
            and entry.get("signature") == signature
            and entry.get("version") == ocr.DETECTION_VERSION
        ):
            entry = {"signature": signature, "version": ocr.DETECTION_VERSION, "candidates": 0}
            try:
                inventory = ocr.scan_pdf(full_path, stop)
                entry["candidates"] = sum(
                    1 for page in inventory["pages"] if page["reason"] != "native"
                )
            except ocr.OCRPaused:
                raise
            except Exception:
                _logger.debug("Unable to inventory PDF for OCR: %s", rel_path, exc_info=True)
                entry["unreadable"] = True
        files[rel_path] = entry
        if entry.get("unreadable"):
            report["unreadable"] += 1
        elif entry["candidates"]:
            report["documents"] += 1
            report["pages"] += entry["candidates"]
        report["documents_scanned"] += 1
        if progress and report["documents_scanned"] % 10 == 0:
            progress({**report, "files": files})
    report.update(status="completed", finished_at=time.time())
    return {**report, "files": files}


def inventory_status() -> dict:
    saved = ocr.read_json(_inventory_path(), {"status": "idle"})
    saved.pop("files", None)
    active = bool(_inventory_thread and _inventory_thread.is_alive())
    if not active and saved.get("status") == "running":
        saved["status"] = "interrupted"
    return {**saved, "active": active}


def start_inventory() -> dict:
    global _inventory_thread
    with _inventory_lock:
        if _inventory_thread and _inventory_thread.is_alive():
            return {"ok": True, "already_running": True}
        if _thread and _thread.is_alive():
            # The pass scans the same PDFs itself and reports its own counts.
            return {"ok": False, "code": "ocr_running", "msg": "L’OCR est déjà en cours."}
        pdfs = state.find_documents()
        if not _pdf_only(pdfs):
            return {"ok": False, "code": "no_pdfs", "msg": "Aucun PDF trouvé dans le dossier."}
        _inventory_stop.clear()

        def run():
            def save(report):
                ocr.write_json(_inventory_path(), report)

            previous = ocr.read_json(_inventory_path())
            try:
                save({**previous, "status": "running"})
                save(scan_inventory(pdfs, _inventory_stop, progress=save))
            except ocr.OCRPaused:
                save({**ocr.read_json(_inventory_path()), "status": "interrupted"})
            except Exception as exc:
                _logger.exception("OCR inventory failed")
                save({**ocr.read_json(_inventory_path()), "status": "error", "error": str(exc)})

        _inventory_thread = threading.Thread(target=run, name="bibliobrain-ocr-inventory", daemon=True)
        _inventory_thread.start()
    return {"ok": True}
