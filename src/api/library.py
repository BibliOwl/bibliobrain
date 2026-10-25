"""Library HTTP routes and feature-specific helpers."""

import asyncio
import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from src import zotero_outbox
from src.analysis_prompts import (
    list_analysis_prompt_payloads,
    list_analysis_types,
    reset_analysis_prompt_override,
    set_analysis_prompt_override,
)
from src.api import application
from src.api.common import (
    ExternalApiAllowed,
    api_error,
    external_api_permission_response,
    is_loopback_request,
    sse,
)
from src.api_models import (
    AnalysisPromptRequest,
    DirectoryPickerRequest,
    LibraryExportRequest,
    LibraryMatchRequest,
    RagExclusionRequest,
    SetDataPathRequest,
    SummaryRequest,
)
from src.app_settings import get_app_settings
from src.library import (
    build_library_export,
    generate_summary,
    get_cached_summary,
    get_item_citations,
    get_item_detail,
    get_library_filter_metadata,
    get_library_stats,
    get_zotero_facets,
    init_zotero,
    list_duplicate_documents,
    list_library,
    list_metadata_issues,
    match_library_papers,
    propose_metadata_correction,
)
from src.library_import import ImportRejected, receive_document, upload_target
from src.model_profiles import resolve_effective_thinking
from src.native_dialogs import (
    NativeDialogBusy,
    NativeDialogUnavailable,
    choose_directory,
    choose_file,
)
from src.rag.exclusions import excluded_path_keys, normalize_rel_path, set_rel_paths_excluded
from src.zotero_db import find_zotero_db, suggest_zotero_pdf_folder

_logger = logging.getLogger(__name__)

router = APIRouter()

_summary_inflight: dict[tuple[str, str, str, str, str], asyncio.Future[str]] = {}


_summary_inflight_lock = asyncio.Lock()


def _normalized_library_path(value: object) -> str:
    return str(value or "").strip().replace("\\", "/").lower()


def _library_theme_paths() -> dict[str, list[str]]:
    """Map each indexed document to its deepest cached/local taxonomy path."""
    try:
        from src.library_taxonomy import build_article_taxonomy_payload

        payload = build_article_taxonomy_payload()
    except Exception:
        _logger.debug("Recovered from an error in _library_theme_paths.", exc_info=True)
        return {}

    nodes = {
        str(node.get("id") or ""): node
        for node in payload.get("nodes", [])
        if isinstance(node, dict)
    }
    result: dict[str, list[str]] = {}
    depths: dict[str, int] = {}
    for node_id, articles in (payload.get("articles_by_concept") or {}).items():
        node = nodes.get(str(node_id), {})
        theme_path = [
            str(label).strip()
            for label in (node.get("taxonomy_path") or [])
            if str(label).strip()
        ]
        depth = len(theme_path)
        if not theme_path or not isinstance(articles, list):
            continue
        for article in articles:
            if not isinstance(article, dict):
                continue
            path = _normalized_library_path(article.get("path"))
            if path and depth > depths.get(path, -1):
                result[path] = theme_path
                depths[path] = depth
    return result


@router.get("/api/library")
def api_library(indexed_only: bool = True, limit: int = 250, offset: int = 0):
    rows, paths, stats = list_library(indexed_only=indexed_only, include_stats=True)
    page_size = max(1, min(limit, 1000))
    page_offset = max(0, offset)
    total = min(len(rows), len(paths))
    end = page_offset + page_size
    page_rows = rows[page_offset:end]
    page_paths = paths[page_offset:end]
    page_metadata = get_library_filter_metadata(page_paths)
    theme_paths = _library_theme_paths()
    excluded = excluded_path_keys()
    for path, metadata in zip(page_paths, page_metadata):
        metadata["theme_path"] = theme_paths.get(_normalized_library_path(path), [])
        metadata["rag_excluded"] = normalize_rel_path(path) in excluded
    return {
        "rows": page_rows,
        "paths": page_paths,
        "metadata": page_metadata,
        "facets": get_zotero_facets(),
        "stats": stats,
        "pagination": {
            "limit": page_size,
            "offset": page_offset,
            "total": total,
            "has_more": page_offset + len(page_rows) < total,
        },
    }


@router.post("/api/library/rag-exclusion")
def api_library_rag_exclusion(req: RagExclusionRequest):
    """Exclude papers from RAG searches, or bring them back. No reindex needed."""
    try:
        result = set_rel_paths_excluded(req.paths, req.excluded)
    except ValueError:
        return api_error(
            "no_library", status_code=400, message="Aucune bibliothèque configurée."
        )
    return {"ok": True, **result}


@router.post("/api/library/export")
def api_library_export(req: LibraryExportRequest):
    exported = build_library_export(req.export_format, req.paths)
    return Response(
        content=exported["content"].encode("utf-8"),
        media_type=exported["media_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{exported["filename"]}"',
            "X-Exported-Items": str(exported["count"]),
        },
    )


@router.post("/api/library/match")
async def api_library_match(req: LibraryMatchRequest):
    papers = [paper.model_dump() for paper in req.papers]
    return {"matches": await asyncio.to_thread(match_library_papers, papers)}


@router.get("/api/library/upload-target")
async def api_library_upload_target():
    return {"ok": True, **await asyncio.to_thread(upload_target)}


@router.post("/api/library/upload")
async def api_library_upload(request: Request, filename: str):
    """Add one dropped document to the library; the body is the raw file."""
    try:
        outcome = await receive_document(filename, request.stream())
    except ImportRejected as exc:
        return api_error(exc.code, status_code=exc.status_code, message=exc.message)
    return {"ok": True, **outcome}


@router.post("/api/library/zotero-outbox/collect")
async def api_library_zotero_outbox_collect():
    """PDFs still waiting for Zotero, and those delivered since the last call."""
    return {"ok": True, **await asyncio.to_thread(zotero_outbox.collect)}


@router.get("/api/item-detail")
def api_item_detail(path: str):
    return {"markdown": get_item_detail(path)}


@router.get("/api/library/metadata-issues")
async def api_library_metadata_issues():
    """Zotero records with a doubtful title, year or authors (local check)."""
    items = await asyncio.to_thread(list_metadata_issues)
    return {"ok": True, "count": len(items), "items": items[:200]}


@router.get("/api/library/duplicates")
async def api_library_duplicates():
    """Probable duplicate documents (same DOI, title and year, or file)."""
    groups = await asyncio.to_thread(list_duplicate_documents)
    return {
        "ok": True,
        "group_count": len(groups),
        "extra_files": sum(len(group["items"]) - 1 for group in groups),
        "groups": groups[:100],
    }


@router.get("/api/library/metadata-fix")
async def api_library_metadata_fix(path: str, *, external_api_allowed: ExternalApiAllowed):
    """Proposed correction from OpenAlex/Crossref or the PDF's first page."""
    if not external_api_allowed:
        return external_api_permission_response()
    proposal = await asyncio.to_thread(propose_metadata_correction, path)
    return {"ok": True, "proposal": proposal}


@router.get("/api/item-citations")
def api_item_citations(path: str):
    return get_item_citations(path)


def _analysis_prompt_language(language: str = "") -> str:
    requested = str(language or "").strip().lower()
    if requested in {"fr", "en"}:
        return requested
    configured = str(get_app_settings().get("ui_language") or "").strip().lower()
    return configured if configured in {"fr", "en"} else "fr"


@router.get("/api/analysis-types")
async def api_analysis_types(language: str = ""):
    safe_language = _analysis_prompt_language(language)
    prompts = list_analysis_prompt_payloads(language=safe_language)
    return {
        "types": list_analysis_types(),
        "type_options": [{"type": prompt["type"], "label": prompt["label"]} for prompt in prompts],
        "language": safe_language,
    }


@router.get("/api/analysis-prompts")
async def api_analysis_prompts(language: str = ""):
    safe_language = _analysis_prompt_language(language)
    return {
        "prompts": list_analysis_prompt_payloads(language=safe_language),
        "language": safe_language,
    }


@router.post("/api/analysis-prompts")
async def api_set_analysis_prompt(req: AnalysisPromptRequest):
    try:
        payload = set_analysis_prompt_override(
            req.analysis_type,
            req.prompt,
            language=_analysis_prompt_language(req.language),
        )
    except ValueError as exc:
        return api_error("invalid_analysis_prompt", status_code=422, message=str(exc))
    return {"ok": True, "prompt": payload}


@router.post("/api/analysis-prompts/reset")
async def api_reset_analysis_prompt(req: AnalysisPromptRequest):
    try:
        payload = reset_analysis_prompt_override(
            req.analysis_type,
            language=_analysis_prompt_language(req.language),
        )
    except ValueError as exc:
        return api_error("invalid_analysis_prompt", status_code=422, message=str(exc))
    return {"ok": True, "prompt": payload}


@router.get("/api/analysis-cached")
async def api_analysis_cached(path: str, analysis_type: str):
    text = get_cached_summary(path, analysis_type)
    return {"found": bool(text), "text": text}


@router.get("/api/zotero-detect")
def api_zotero_detect():
    path = find_zotero_db() or ""
    # With ZotMoov or linked files the PDFs are not in storage/.
    storage = suggest_zotero_pdf_folder(path) if path else None
    if path:
        status = init_zotero(path)
        rows, paths = list_library(indexed_only=True)
    else:
        status = init_zotero("")
        rows, paths = list_library(indexed_only=True)
    facets = get_zotero_facets()
    return {
        "path": path,
        "status": status,
        "storage": storage or "",
        "rows": rows,
        "paths": paths,
        "facets": facets,
        "stats": get_library_stats(),
    }


@router.post("/api/set-data-path")
async def api_set_data_path(req: SetDataPathRequest):
    import os

    from src.config import set_data_path

    if not os.path.isdir(req.path):
        return api_error(
            "data_directory_not_found",
            status_code=422,
            message="Dossier de données introuvable.",
            extra={"rows": [], "paths": [], "facets": get_zotero_facets()},
        )
    from src.rag.state import find_documents

    set_data_path(req.path)
    persist_warning = application._persist_app_settings_best_effort(data_path=req.path)
    rows, paths = list_library(indexed_only=True)
    count = len(paths)
    # Documents on disk, indexed or not: lets the first run catch an empty
    # or wrong folder before preparation starts.
    document_count = len(find_documents())
    return {
        "ok": True,
        # "0 indexed" read as "nothing found" before the first preparation.
        "msg": (
            f"Dossier source : {req.path} ({document_count} documents trouves, "
            f"sous-dossiers compris ; {count} deja prepares){persist_warning}"
        ),
        "rows": rows,
        "paths": paths,
        "facets": get_zotero_facets(),
        "document_count": document_count,
        "indexed_count": count,
        "persist_warning": persist_warning,
    }


@router.post("/api/select-data-directory")
def api_select_data_directory(req: DirectoryPickerRequest, request: Request):
    """Open a folder chooser on the local Bibliobrain host without saving yet."""

    if not is_loopback_request(request):
        return api_error(
            "local_request_required",
            status_code=403,
            message="Le sélecteur de dossier est disponible uniquement sur cette machine.",
        )

    import src.config as _cfg

    try:
        path = choose_directory(
            initial_path=req.path or _cfg.DATA_PATH,
            title="Choisir le dossier contenant les PDF",
        )
    except NativeDialogBusy:
        return api_error(
            "directory_picker_busy",
            status_code=409,
            message="Un sélecteur de dossier est déjà ouvert.",
        )
    except NativeDialogUnavailable:
        return api_error(
            "directory_picker_unavailable",
            status_code=503,
            message="Impossible d’ouvrir le sélecteur de dossier du système.",
        )

    if not path:
        return {"ok": True, "cancelled": True, "path": ""}
    return {"ok": True, "cancelled": False, "path": path}


@router.post("/api/select-metadata-file")
def api_select_metadata_file(req: DirectoryPickerRequest, request: Request):
    """Open a local bibliographic metadata file chooser without saving yet."""

    if not is_loopback_request(request):
        return api_error(
            "local_request_required",
            status_code=403,
            message="Le sélecteur de fichier est disponible uniquement sur cette machine.",
        )

    try:
        path = choose_file(
            initial_path=req.path,
            title="Choisir un fichier Zotero ou BibTeX",
        )
    except NativeDialogBusy:
        return api_error(
            "file_picker_busy",
            status_code=409,
            message="Un sélecteur de fichier est déjà ouvert.",
        )
    except NativeDialogUnavailable:
        return api_error(
            "file_picker_unavailable",
            status_code=503,
            message="Impossible d’ouvrir le sélecteur de fichier du système.",
        )

    if not path:
        return {"ok": True, "cancelled": True, "path": ""}
    return {"ok": True, "cancelled": False, "path": path}


@router.get("/api/data-path")
async def api_get_data_path():
    import src.config as _cfg

    return {
        "path": _cfg.DATA_PATH,
        "configured": bool(get_app_settings().get("data_path_configured")),
    }


@router.post("/api/summary")
async def api_summary(req: SummaryRequest):
    """SSE stream : résumé via provider LLM configuré (local ou distant).

    Deduplicates concurrent identical requests so a double-click on the
    "Analyser" button doesn't trigger two LLM calls for the same article.
    """
    effective_thinking = resolve_effective_thinking(
        req.model,
        req.thinking,
        allow_small_model_override=req.force_thinking_small_models,
    )
    key = (
        str(req.rel_path),
        str(req.analysis_type),
        str(req.model),
        str(effective_thinking),
        str(req.force_thinking_small_models),
    )
    loop = asyncio.get_running_loop()

    async with _summary_inflight_lock:
        existing = _summary_inflight.get(key)
        if existing is None:
            future: asyncio.Future[str] = loop.create_future()
            _summary_inflight[key] = future

            def cleanup(completed: asyncio.Future[str]) -> None:
                async def remove_completed() -> None:
                    async with _summary_inflight_lock:
                        if _summary_inflight.get(key) is completed:
                            _summary_inflight.pop(key, None)

                loop.create_task(remove_completed())

            future.add_done_callback(cleanup)

            def run() -> None:
                try:
                    result = generate_summary(
                        req.rel_path,
                        req.analysis_type,
                        req.model,
                        thinking_enabled=effective_thinking,
                    )
                    loop.call_soon_threadsafe(future.set_result, result)
                except Exception as exc:
                    loop.call_soon_threadsafe(future.set_exception, exc)

            threading.Thread(target=run, daemon=True).start()
        else:
            future = existing

    async def generate():
        while True:
            try:
                result = await asyncio.wait_for(asyncio.shield(future), timeout=1.0)
                yield sse({"type": "done", "text": result})
                return
            except TimeoutError:
                yield sse({"type": "progress", "desc": "Génération en cours…"})
            except Exception:
                _logger.exception("Summary generation failed")
                yield sse({"type": "error", "msg": "summary_generation_failed"})
                return

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
