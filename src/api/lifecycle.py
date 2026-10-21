"""Lifecycle HTTP routes and feature-specific helpers."""

import asyncio
import logging
import queue
import shutil
import threading
import time
from functools import wraps
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from src import config
from src.api import application
from src.api.common import (
    api_error,
    is_loopback_request,
    sse,
)
from src.api_models import (
    BrowserSessionRequest,
    ChatWarmupRequest,
    ConfirmRequest,
    FullResetRequest,
    InitRequest,
    ZoteroLoadRequest,
)
from src.app_settings import get_app_settings
from src.chat_export import clear_all_export_files
from src.chat_history_db import clear_all_chat_history
from src.discovery_history_db import clear_all_discovery_history
from src.drafting_history import clear_history as clear_drafting_history
from src.library import (
    clear_metadata_library,
    get_library_stats,
    get_zotero_facets,
    init_zotero,
    list_library,
)
from src.llm_provider import public_provider_info
from src.notebooks import clear_all_notebooks
from src.rag_settings import get_persisted_chunk_size, set_persisted_chunk_size
from src.zotero_db import is_bibtex_source, suggest_zotero_pdf_folder

_logger = logging.getLogger(__name__)

router = APIRouter()


def _exclusive_index_reset(function):
    @wraps(function)
    async def reset(*args, **kwargs):
        if not application._rag_init_lock.acquire(blocking=False):
            return JSONResponse(
                status_code=409,
                content={
                    "ok": False,
                    "msg": "Arrêtez l’indexation ou mettez l’OCR en pause avant de réinitialiser.",
                },
            )
        try:
            return await function(*args, **kwargs)
        finally:
            application._rag_init_lock.release()

    return reset


@router.post("/api/app/window/open")
def api_app_window_open(req: BrowserSessionRequest):
    application._register_browser_session(req.session_id)
    return {
        "ok": True,
        "managed_server": application._managed_server,
        "auto_shutdown": application._managed_server,
    }


@router.post("/api/app/window/heartbeat")
def api_app_window_heartbeat(req: BrowserSessionRequest):
    # Compatibility for already cached frontends. A heartbeat may restore a
    # session after a backend restart, but its absence never expires one.
    application._register_browser_session(req.session_id)
    return {"ok": True, "managed_server": application._managed_server}


@router.post("/api/app/window/close")
def api_app_window_close(req: BrowserSessionRequest):
    application._close_browser_session(req.session_id)
    return {"ok": True, "managed_server": application._managed_server}


@router.post("/api/app/shutdown")
def api_app_shutdown(request: Request):
    if not application._managed_server:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "error": "managed_server_required"},
        )
    if not is_loopback_request(request):
        return JSONResponse(
            status_code=403,
            content={"ok": False, "error": "local_request_required"},
        )
    with application._browser_session_lock:
        application._browser_sessions.clear()
        application._cancel_browser_shutdown_locked()
    application._schedule_process_shutdown(0.4)
    return {"ok": True, "stopping": True}


@router.get("/api/vectordb-status")
def api_vectordb_status():
    return application.get_vector_database_status()


@router.get("/api/reranker-status")
async def api_reranker_status():
    from src.rag.reranker import get_reranker_status

    return get_reranker_status()


@router.post("/api/reranker-warmup")
async def api_reranker_warmup():
    """Start reranker warmup and return immediately.

    The frontend polls ``/api/reranker-status`` to render the loading banner.
    """
    return application._start_reranker_warmup_background()


@router.post("/api/chat/warmup")
async def api_chat_warmup(req: ChatWarmupRequest):
    """Load the chat and embedding models in the background before the first question.

    The page calls it when it opens and when the chat model changes; a warm-up
    already running for that model is not started twice.
    """
    return application._start_chat_warmup_background(req.model)


@router.get("/api/rag/reload-preview")
async def api_rag_reload_preview(chunk_size: int | None = None):
    return await asyncio.to_thread(application.get_vector_reload_preview, chunk_size=chunk_size)


async def _find_text_artifacts_loading_index():
    """Scan the index, loading it first when this is its first use."""
    from src.drafting_citations import ensure_drafting_rag
    from src.rag.vector_store import find_documents_with_text_artifacts

    try:
        return await asyncio.to_thread(find_documents_with_text_artifacts)
    except RuntimeError:
        model = str(get_app_settings().get("selected_model") or "").strip()
        error = await asyncio.to_thread(ensure_drafting_rag, model or None)
        if error:
            raise RuntimeError(error) from None
        return await asyncio.to_thread(find_documents_with_text_artifacts)


@router.get("/api/rag/text-artifacts")
async def api_rag_text_artifacts():
    """Documents indexed with broken PDF text (LaTeX accents, ligatures)."""
    try:
        documents = await _find_text_artifacts_loading_index()
    except RuntimeError as exc:
        return api_error("rag_not_ready", status_code=409, message=str(exc))
    return {
        "ok": True,
        "document_count": len(documents),
        "chunk_count": sum(int(item["affected_chunks"]) for item in documents),
        "documents": documents[:50],
    }


@router.post("/api/rag/text-artifacts/reindex")
async def api_rag_text_artifacts_reindex():
    """Mark the affected documents; the client then runs the usual reload."""
    from src.rag.vector_store import mark_documents_for_reindex

    if application._rag_init_lock.locked():
        return api_error(
            "rag_initialization_in_progress",
            status_code=409,
            message="Une initialisation ou indexation est déjà en cours.",
        )
    try:
        documents = await _find_text_artifacts_loading_index()
    except RuntimeError as exc:
        return api_error("rag_not_ready", status_code=409, message=str(exc))
    marked = await asyncio.to_thread(
        mark_documents_for_reindex, [item["rel_path"] for item in documents]
    )
    return {"ok": True, "marked": marked}


@router.get("/api/startup-status")
async def api_startup_status():
    """Single pre-flight snapshot for the frontend startup assistant."""

    def _load_library_count() -> int:
        try:
            return int(get_library_stats().get("indexed_pdf_count", 0) or 0)
        except Exception:
            _logger.debug("Recovered from an error in _load_library_count.", exc_info=True)
            return 0

    (
        vector_db,
        rag_runtime,
        app_settings,
        library_count,
        provider_info,
    ) = await asyncio.gather(
        asyncio.to_thread(application.get_vector_database_status),
        asyncio.to_thread(application.get_runtime_status),
        asyncio.to_thread(get_app_settings),
        asyncio.to_thread(_load_library_count),
        asyncio.to_thread(public_provider_info),
    )

    recommended_action = "ready"
    if not rag_runtime.get("ready"):
        status = str(vector_db.get("status") or "")
        if status in {"present"}:
            recommended_action = "load_rag"
        elif status in {"missing"}:
            recommended_action = "index_rag"
        elif status in {"embedding_mismatch", "incomplete", "partial"}:
            recommended_action = "manual_rebuild"
        else:
            recommended_action = "configure"

    metadata_source = str(app_settings.get("metadata_source") or "none").strip().lower()
    library_configured = bool(
        library_count
        or str(app_settings.get("data_path") or "").strip()
        or (metadata_source != "none" and str(app_settings.get("zotero_path") or "").strip())
    )

    return {
        "ok": True,
        "provider": provider_info,
        "rag": rag_runtime,
        "vector_db": vector_db,
        "library_count": library_count,
        "library_configured": library_configured,
        "settings": {
            "first_rag_init_done": bool(app_settings.get("first_rag_init_done")),
            "onboarding_done_v1": bool(app_settings.get("onboarding_done_v1")),
            "feature_tour_done_v1": bool(app_settings.get("feature_tour_done_v1")),
            "debug_metrics": bool(app_settings.get("debug_metrics")),
        },
        "recommended_action": recommended_action,
    }


@router.post("/api/reset-all")
@_exclusive_index_reset
async def api_reset_all(req: FullResetRequest):
    if not req.confirm:
        return api_error(
            "confirmation_required",
            status_code=400,
            message="Confirmation requise pour reinitialiser les donnees.",
        )

    vector_result = application.reset_vector_database()
    notebooks_deleted = clear_all_notebooks()
    chat_result = clear_all_chat_history()
    export_result = clear_all_export_files()
    discovery_history_result = clear_all_discovery_history()
    drafting_history_result = clear_drafting_history()

    discovery_removed = False
    discovery_error = ""
    if req.remove_discovery_imports:
        import src.config as _cfg

        discovery_dir = Path(_cfg.DATA_PATH) / "discovery_imports"
        if discovery_dir.exists():
            try:
                shutil.rmtree(discovery_dir)
                discovery_removed = True
            except Exception:
                _logger.exception("Unable to remove discovery imports")
                discovery_error = "discovery_import_cleanup_failed"

    rows, paths = list_library(indexed_only=True)
    msg = (
        "Reinitialisation terminee: "
        f"vector_db={'ok' if vector_result.get('removed') else 'deja vide'}, "
        f"carnets_supprimes={notebooks_deleted}, "
        f"messages_chat_supprimes={chat_result.get('messages_deleted', 0)}, "
        f"conversations_supprimees={chat_result.get('conversations_deleted', 0)}, "
        f"recherches_discovery_supprimees={discovery_history_result.get('searches_deleted', 0)}, "
        f"analyses_redaction_supprimees={drafting_history_result['runs_deleted']}, "
        f"versions_texte_supprimees={drafting_history_result['versions_deleted']}"
    )
    if req.remove_discovery_imports:
        if discovery_error:
            msg += f", discovery_imports=erreur ({discovery_error})"
        else:
            msg += f", discovery_imports={'supprimes' if discovery_removed else 'deja vide'}"
    if export_result.get("files_deleted", 0):
        msg += f", exports_chat_supprimes={export_result.get('files_deleted', 0)}"

    return {
        "ok": True,
        "msg": msg,
        "vector_db": vector_result,
        "notebooks_deleted": notebooks_deleted,
        "chat_history": chat_result,
        "chat_exports": export_result,
        "discovery_history": discovery_history_result,
        "discovery_imports_removed": discovery_removed,
        "discovery_imports_error": discovery_error,
        "rows": rows,
        "paths": paths,
        "facets": get_zotero_facets(),
    }


@router.post("/api/reset-history")
async def api_reset_history(req: ConfirmRequest):
    """Supprime l'historique des chats et des carnets. Laisse la base vectorielle et les PDF importés intacts."""
    if not req.confirm:
        return api_error(
            "confirmation_required",
            status_code=400,
            message="Confirmation requise.",
        )

    notebooks_deleted = clear_all_notebooks()
    chat_result = clear_all_chat_history()
    export_result = clear_all_export_files()
    discovery_history_result = clear_all_discovery_history()
    # Analyses and draft versions hold draft text; the current draft itself stays.
    drafting_history_result = clear_drafting_history()

    return {
        "ok": True,
        "msg": (
            f"Historique effacé : {chat_result.get('conversations_deleted', 0)} conversation(s), "
            f"{notebooks_deleted} carnet(s), "
            f"{discovery_history_result.get('searches_deleted', 0)} recherche(s) discovery, "
            f"{export_result.get('files_deleted', 0)} export(s) chat, "
            f"{drafting_history_result['runs_deleted']} analyse(s) et "
            f"{drafting_history_result['versions_deleted']} version(s) de la Rédaction."
        ),
        "notebooks_deleted": notebooks_deleted,
        "chat_history": chat_result,
        "chat_exports": export_result,
        "discovery_history": discovery_history_result,
        "drafting_history": drafting_history_result,
    }


@router.post("/api/reset-vectordb")
@_exclusive_index_reset
async def api_reset_vectordb(req: ConfirmRequest):
    """Supprime toutes les bases vectorielles locales sous CHROMA_PATH. L'historique reste intact."""
    if not req.confirm:
        return api_error(
            "confirmation_required",
            status_code=400,
            message="Confirmation requise.",
        )

    result = application.reset_vector_database()
    if not result.get("removed"):
        details = result.get("error") or ""
        leftovers = result.get("leftovers") or []
        _logger.error(
            "Incomplete vector database deletion: details=%s leftovers=%s",
            details,
            leftovers,
        )
        return api_error(
            "vector_database_cleanup_failed",
            status_code=500,
            message="Suppression incomplete des bases vectorielles.",
        )
    return {
        "ok": True,
        "msg": (
            "Bases vectorielles supprimées."
            if result.get("removed")
            else "Bases vectorielles déjà vides."
        ),
        "vector_db": result,
    }


@router.post("/api/zotero-load")
def api_zotero_load(req: ZoteroLoadRequest):
    status = init_zotero(req.path)
    ok = not status.startswith(("Erreur ", "Fichier introuvable", "Zotero non trouvé"))
    persist_warning = (
        application._persist_app_settings_best_effort(
            zotero_path=req.path or "",
            metadata_source=("bibtex" if is_bibtex_source(req.path) else "zotero"),
        )
        if ok
        else ""
    )
    storage = None
    if ok and req.path and not is_bibtex_source(req.path):
        storage = suggest_zotero_pdf_folder(req.path)
    rows, paths = list_library(indexed_only=True)
    payload = {
        "ok": ok,
        "path": req.path or "",
        "status": f"{status}{persist_warning}",
        "storage": storage or "",
        "rows": rows,
        "paths": paths,
        "facets": get_zotero_facets(),
        "stats": get_library_stats(),
    }
    if not ok:
        payload["error"] = "metadata_source_load_failed"
        payload["path"] = ""
        payload["status"] = "metadata_source_load_failed"
        return JSONResponse(status_code=422, content=payload)
    return payload


@router.get("/api/zotero-facets")
def api_zotero_facets():
    return get_zotero_facets()


# The OCR job shares _rag_init_lock, so the lock alone does not say a RAG load
# is running; only such a load can be paused.
_rag_load_running = threading.Event()


@router.post("/api/init")
async def api_init(req: InitRequest):
    """SSE stream : progress events + done."""
    if not application._rag_init_lock.acquire(blocking=False):
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "error": "rag_initialization_in_progress",
                "msg": "Une initialisation ou indexation est déjà en cours.",
            },
        )
    _rag_load_running.set()

    q: queue.Queue = queue.Queue()

    def progress_cb(frac: float, desc: str = ""):
        q.put({"type": "progress", "frac": round(frac, 3), "desc": desc})

    managed_operation_active = application._begin_managed_operation()

    def run():
        ok = False
        try:
            try:
                chunk_size = (
                    set_persisted_chunk_size(req.chunk_size)
                    if req.chunk_size is not None
                    else get_persisted_chunk_size()
                )
                ok, msg = application.init_rag(
                    req.model,
                    req.threshold,
                    req.force_reload,
                    progress_cb,
                    max_chunks=req.max_chunks,
                    chunk_size=chunk_size,
                )
                if req.force_reload:
                    # init_rag ne rafraichit que le cache Zotero cote RAG ;
                    # celui de src.library (utilise par /api/library) doit
                    # etre recharge aussi pour refleter les metadonnees a jour.
                    metadata_settings = get_app_settings()
                    metadata_source = (
                        str(metadata_settings.get("metadata_source") or "none").strip().lower()
                    )
                    metadata_path = str(
                        metadata_settings.get("zotero_path") or config.ZOTERO_DB_PATH or ""
                    ).strip()
                    if metadata_source == "zotero":
                        # Empty path means auto-detection only because the user
                        # explicitly selected Zotero.
                        init_zotero(metadata_path)
                    elif metadata_source == "bibtex" and metadata_path:
                        init_zotero(metadata_path)
                    else:
                        clear_metadata_library()
            except Exception:
                ok = False
                _logger.exception("RAG initialization failed")
                msg = "Erreur interne pendant l'initialisation."
            q.put({"type": "done", "ok": ok, "msg": msg})
        finally:
            _rag_load_running.clear()
            # A pause that landed after the last checkpoint must not linger.
            application.set_vector_loading_suspended(False)
            try:
                application._end_managed_operation(managed_operation_active)
            finally:
                application._rag_init_lock.release()

        if ok:
            from src import document_ocr
            from src.rag.ocr_ingestion import start as start_ocr

            if document_ocr.settings()["enabled"]:
                # Resumes an unfinished pass, or OCRs what the load added;
                # does nothing (and takes no lock) when OCR is up to date.
                start_ocr(only_if_needed=True)

    try:
        threading.Thread(target=run, daemon=True).start()
    except Exception:
        _rag_load_running.clear()
        application._end_managed_operation(managed_operation_active)
        application._rag_init_lock.release()
        raise

    async def generate():
        last_keepalive = time.monotonic()
        finished = False
        try:
            while True:
                try:
                    # q.get est bloquant : to_thread évite de geler l'event loop.
                    event = await asyncio.to_thread(q.get, True, 0.05)
                    yield sse(event)
                    last_keepalive = time.monotonic()
                    if event["type"] == "done":
                        finished = True
                        break
                except queue.Empty:
                    if time.monotonic() - last_keepalive >= 10.0:
                        yield ": keepalive\n\n"
                        last_keepalive = time.monotonic()
                    await asyncio.sleep(0.05)
        finally:
            # Page fermée pendant une pause : personne ne pourrait plus cliquer
            # « Reprendre », et le verrou d'init resterait pris. On reprend.
            if not finished:
                application.set_vector_loading_suspended(False)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/stop-init")
async def api_stop_init():
    return {"ok": True, "msg": application.pause_vector_loading()}


@router.post("/api/pause-init")
async def api_pause_init():
    if not _rag_load_running.is_set():
        return {"ok": False, "paused": False, "msg": "Aucun chargement du RAG en cours."}
    application.set_vector_loading_suspended(True)
    return {"ok": True, "paused": True}


@router.post("/api/resume-init")
async def api_resume_init():
    application.set_vector_loading_suspended(False)
    return {"ok": True, "paused": False}
