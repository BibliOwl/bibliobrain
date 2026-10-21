"""Index transfer HTTP routes: move the vector index to another computer.

Files are chosen with the native pickers of this machine, so an index of
several gigabytes never goes through the browser. The import is two-step:
``inspect`` analyzes the chosen archive and returns a token, ``import``
installs it once the user has read the report. The client only ever holds
that token, never a path.
"""

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from src import config
from src.api import application
from src.api.common import api_error, is_loopback_request
from src.api_models import (
    IndexTransferExportRequest,
    IndexTransferImportRequest,
    IndexTransferPullRequest,
)
from src.atomic_io import atomic_write_json
from src.native_dialogs import (
    NativeDialogBusy,
    NativeDialogUnavailable,
    choose_file,
    choose_save_file,
)

_logger = logging.getLogger(__name__)

router = APIRouter()

_ARCHIVE_FILE_TYPES = (("Index Bibliobrain", ("*.zip",)),)
# Analyses waiting for the user's confirmation, kept on disk: a restart of
# the server (or a long pause before clicking Import) must not lose them.
_PENDING_FILE_NAME = "index_transfer_pending.json"
_MAX_PENDING_IMPORTS = 10
_pending_lock = threading.Lock()

_ERROR_STATUS = {
    "archive_not_found": 404,
    "not_an_index": 422,
    "corrupted_archive": 422,
    "newer_format": 422,
    "invalid_extension": 422,
    "invalid_destination": 422,
    "no_ready_index": 409,
    "empty_index": 409,
    "index_locked": 409,
    "disk_space": 507,
    "local_ollama_required": 409,
}


def _local_only_error(request: Request):
    if is_loopback_request(request):
        return None
    return api_error(
        "local_request_required",
        status_code=403,
        message="Le transfert d'index est disponible uniquement sur cette machine.",
    )


def _transfer_error(exc: Exception):
    from src.rag.index_transfer import IndexTransferError

    if isinstance(exc, IndexTransferError):
        return api_error(exc.code, status_code=_ERROR_STATUS.get(exc.code, 500), message=exc.message)
    if isinstance(exc, NativeDialogBusy):
        return api_error(
            "file_picker_busy", status_code=409, message="Un sélecteur de fichier est déjà ouvert."
        )
    if isinstance(exc, NativeDialogUnavailable):
        return api_error(
            "file_picker_unavailable",
            status_code=503,
            message="Impossible d’ouvrir le sélecteur de fichier du système.",
        )
    _logger.exception("Index transfer failed")
    return api_error("index_transfer_failed", status_code=500, message="Erreur interne du transfert.")


def _busy_error():
    return api_error(
        "rag_initialization_in_progress",
        status_code=409,
        message="Une préparation ou un OCR est en cours. Attendez sa fin ou arrêtez-le.",
    )


def _documents_folder() -> str:
    documents = Path.home() / "Documents"
    return str(documents if documents.is_dir() else Path.home())


def _pending_file() -> Path:
    return Path(config.STORAGE_DIR) / _PENDING_FILE_NAME


def _load_pending() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(_pending_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_pending(pending: dict[str, dict[str, Any]]) -> None:
    try:
        atomic_write_json(_pending_file(), pending)
    except OSError:
        _logger.warning("Unable to save the pending index imports", exc_info=True)


def _remember(report: dict[str, Any]) -> str:
    """Keep the analysis until it is imported, across restarts and without expiry.

    Only the most recent analyses are kept, so the file stays small.
    """
    token = secrets.token_urlsafe(18)
    with _pending_lock:
        pending = _load_pending()
        pending[token] = {"at": time.time(), "path": report["archive"]["path"], "report": report}
        newest = sorted(pending.items(), key=lambda item: item[1].get("at", 0), reverse=True)
        _save_pending(dict(newest[:_MAX_PENDING_IMPORTS]))
    return token


def _pending(token: str) -> dict[str, Any] | None:
    with _pending_lock:
        item = _load_pending().get(token)
    return item if isinstance(item, dict) else None


def _update_pending(token: str, report: dict[str, Any]) -> None:
    with _pending_lock:
        pending = _load_pending()
        if token in pending:
            pending[token]["report"] = report
            _save_pending(pending)


def _forget_pending(token: str) -> None:
    with _pending_lock:
        pending = _load_pending()
        if pending.pop(token, None) is not None:
            _save_pending(pending)


def _unknown_token():
    return api_error(
        "unknown_import",
        status_code=404,
        message="Analyse introuvable : choisissez de nouveau le fichier à importer.",
    )


def _no_ready_index_error():
    return api_error(
        "no_ready_index",
        status_code=409,
        message=(
            "Aucun index complet pour le modèle d'embedding actuel. "
            "Préparez d'abord vos articles, puis exportez."
        ),
    )


@router.get("/api/index-transfer/progress")
def api_index_transfer_progress(request: Request):
    """Progress of the running download, export or import (polled by the UI)."""
    from src.rag.index_transfer import transfer_progress

    error = _local_only_error(request)
    if error is not None:
        return error
    return {"ok": True, **transfer_progress()}


@router.get("/api/index-transfer/export-estimate")
def api_index_transfer_export_estimate(request: Request):
    """Sizes of the export with and without the documents (file sizes only, nothing is read)."""
    from src.rag.index_transfer import estimate_export

    error = _local_only_error(request)
    if error is not None:
        return error
    if not application.get_vector_database_status().get("profile_ready"):
        return _no_ready_index_error()
    try:
        return {"ok": True, **estimate_export()}
    except Exception as exc:
        return _transfer_error(exc)


@router.post("/api/index-transfer/export")
def api_index_transfer_export(request: Request, req: IndexTransferExportRequest | None = None):
    """Ask where to save, then write the active index (and the documents) there."""
    from src.rag.index_transfer import default_export_name, export_active_index

    include_documents = True if req is None else req.include_documents
    error = _local_only_error(request)
    if error is not None:
        return error
    status = application.get_vector_database_status()
    if not status.get("profile_ready"):
        return _no_ready_index_error()
    try:
        path = choose_save_file(
            default_name=default_export_name(),
            file_types=_ARCHIVE_FILE_TYPES,
            initial_path=_documents_folder(),
            title="Exporter l'index Bibliobrain",
        )
    except (NativeDialogBusy, NativeDialogUnavailable) as exc:
        return _transfer_error(exc)
    if not path:
        return {"ok": True, "cancelled": True}

    if not application._rag_init_lock.acquire(blocking=False):
        return _busy_error()
    try:
        with application._managed_operation():
            result = export_active_index(path, include_documents=include_documents)
    except Exception as exc:
        return _transfer_error(exc)
    finally:
        application._rag_init_lock.release()
    return {"ok": True, "cancelled": False, **result}


@router.post("/api/index-transfer/inspect")
def api_index_transfer_inspect(request: Request):
    """Pick an archive and report what importing it would change."""
    from src.rag.index_transfer import inspect_archive

    error = _local_only_error(request)
    if error is not None:
        return error
    try:
        path = choose_file(
            initial_path=_documents_folder(),
            title="Choisir un index Bibliobrain à importer",
            file_types=_ARCHIVE_FILE_TYPES,
        )
        if not path:
            return {"ok": True, "cancelled": True}
        with application._managed_operation():
            report = inspect_archive(path)
    except Exception as exc:
        return _transfer_error(exc)
    return {"ok": True, "cancelled": False, "token": _remember(report), "report": report}


@router.post("/api/index-transfer/pull-model")
def api_index_transfer_pull_model(req: IndexTransferPullRequest, request: Request):
    """Download the embedding model named by the analyzed archive, once the user accepted."""
    from src.rag.index_transfer import (
        embedding_compatibility,
        finalize_report,
        pull_embedding_model,
        read_index_info,
    )

    error = _local_only_error(request)
    if error is not None:
        return error
    if not req.confirm:
        return api_error(
            "confirmation_required",
            status_code=400,
            message="Le téléchargement du modèle doit être confirmé.",
        )
    pending = _pending(req.token)
    if pending is None:
        return _unknown_token()
    report = pending["report"]
    try:
        index_info = read_index_info(pending["path"])
        compatibility = embedding_compatibility(index_info)
        if compatibility["action"] == "download":
            with application._managed_operation():
                pull_embedding_model(compatibility["model_name"])
            compatibility = embedding_compatibility(index_info)
    except Exception as exc:
        return _transfer_error(exc)
    report["embedding"] = compatibility
    finalize_report(report)
    _update_pending(req.token, report)
    return {"ok": True, "report": report}


@router.post("/api/index-transfer/import")
def api_index_transfer_import(req: IndexTransferImportRequest, request: Request):
    from src.rag.index_transfer import import_archive

    error = _local_only_error(request)
    if error is not None:
        return error
    if not req.confirm:
        return api_error("confirmation_required", status_code=400, message="Confirmation requise.")
    pending = _pending(req.token)
    if pending is None:
        return _unknown_token()
    if not application._rag_init_lock.acquire(blocking=False):
        return _busy_error()
    try:
        with application._managed_operation():
            result = import_archive(pending["path"], adopt_settings=req.adopt_settings)
    except Exception as exc:
        return _transfer_error(exc)
    finally:
        application._rag_init_lock.release()
    _forget_pending(req.token)
    records = result.get("records") if isinstance(result, dict) else None
    if isinstance(records, dict) and records.get("used") and records.get("path"):
        # The Library tab shows the imported records at once, even before a reload.
        from src.library import init_zotero

        try:
            init_zotero(str(records["path"]))
        except Exception:
            _logger.warning("Unable to load the imported records in the Library tab", exc_info=True)
    return {"ok": True, **result, "vector_db": application.get_vector_database_status()}
