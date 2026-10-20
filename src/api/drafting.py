"""Drafting HTTP routes and feature-specific helpers."""

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src import drafting_history
from src.api.common import (
    ExternalApiAllowed,
    api_error,
    external_api_permission_response,
    iter_sync_gen,
    sse,
)
from src.api.streams import (
    register_stream_abort_signal,
    release_stream_abort_signal,
    stop_local_ollama_model_async,
)
from src.api_models import DraftingExportRequest, DraftingRequest, DraftingReviewRequest
from src.chat_export import ChatExportDocument, build_text_export_document, save_export_document
from src.drafting_citations import stream_citations_for_draft, suggest_citations_for_draft
from src.drafting_interop import (
    extract_citation_markers,
    insert_before_end_document,
    render_latex_bibliography,
    resolve_citation_records,
    resolve_word_citation_items,
    restore_latex_citations,
    strip_citation_markers,
)
from src.drafting_review import (
    proofread_draft,
    stream_proofread_draft,
    suggest_argument_improvements,
)
from src.drafting_store import (
    clear_drafting_document,
    load_drafting_document,
    save_drafting_document,
)
from src.origin_tracing import OriginTraceError, identify_reference, trace_origin

_logger = logging.getLogger(__name__)

router = APIRouter()


class DraftingRewriteRequest(BaseModel):
    text: str = Field(min_length=1, max_length=12000)
    action: Literal["rephrase", "shorten"]
    model: str = Field(min_length=1, max_length=200)


@router.post("/api/drafting-rewrite")
async def api_drafting_rewrite(req: DraftingRewriteRequest):
    from src.workspace_assistance import rewrite_passage

    try:
        return await asyncio.to_thread(rewrite_passage, req.text, req.action, req.model)
    except Exception:
        _logger.exception("Draft rewrite failed")
        return api_error(
            "rewrite_failed", status_code=502, message="La reformulation n’a pas abouti. Réessayez."
        )


class DraftingDocumentRequest(BaseModel):
    html: str = Field(default="", max_length=5_000_000)
    citation_start_number: int = Field(default=1, ge=1, le=1_000_000)


@router.get("/api/drafting-document")
async def api_drafting_document_get():
    document = await asyncio.to_thread(load_drafting_document)
    return {"ok": True, "exists": document is not None, "document": document}


@router.put("/api/drafting-document")
async def api_drafting_document_put(req: DraftingDocumentRequest):
    try:
        document = await asyncio.to_thread(
            save_drafting_document,
            req.html,
            req.citation_start_number,
        )
    except ValueError:
        return api_error(
            "draft_too_large",
            status_code=422,
            message="Le brouillon dépasse la taille maximale autorisée.",
        )
    try:
        await asyncio.to_thread(
            drafting_history.save_version,
            req.html,
            req.citation_start_number,
            "auto",
            min_interval_seconds=drafting_history.AUTO_VERSION_INTERVAL_SECONDS,
        )
    except Exception:
        # A missed snapshot must never make the draft itself fail to save.
        _logger.warning("Automatic draft version failed", exc_info=True)
    return {"ok": True, "document": document}


@router.delete("/api/drafting-document")
async def api_drafting_document_delete():
    await asyncio.to_thread(clear_drafting_document)
    return {"ok": True}


class DraftingHistoryRunRequest(BaseModel):
    kind: Literal["citations", "proofread", "arguments"]
    model: str = Field(default="", max_length=200)
    excerpt: str = Field(default="", max_length=20000)
    summary: str = Field(default="", max_length=2000)
    result_count: int = Field(default=0, ge=0, le=100_000)
    payload: dict = Field(default_factory=dict)


class DraftingHistoryVersionRequest(BaseModel):
    html: str = Field(default="", max_length=5_000_000)
    citation_start_number: int = Field(default=1, ge=1, le=1_000_000)
    reason: Literal["before_proofread", "before_citations", "before_arguments", "before_clear", "before_restore"]


@router.get("/api/drafting-history")
async def api_drafting_history_list():
    runs = await asyncio.to_thread(drafting_history.list_runs)
    versions = await asyncio.to_thread(drafting_history.list_versions)
    return {"ok": True, "runs": runs, "versions": versions}


@router.delete("/api/drafting-history")
async def api_drafting_history_clear():
    await asyncio.to_thread(drafting_history.clear_history)
    return {"ok": True}


@router.get("/api/drafting-history/runs/{run_id}")
async def api_drafting_history_run_get(run_id: str):
    run = await asyncio.to_thread(drafting_history.get_run, run_id)
    if run is None:
        return api_error("run_not_found", status_code=404, message="Analyse introuvable.")
    return {"ok": True, "run": run}


@router.put("/api/drafting-history/runs/{run_id}")
async def api_drafting_history_run_put(run_id: str, req: DraftingHistoryRunRequest):
    try:
        run = await asyncio.to_thread(
            drafting_history.save_run,
            run_id,
            kind=req.kind,
            payload=req.payload,
            model=req.model,
            excerpt=req.excerpt,
            summary=req.summary,
            result_count=req.result_count,
        )
    except ValueError as exc:
        return api_error(str(exc), status_code=422, message="Analyse non enregistrée.")
    return {"ok": True, "run": run}


@router.delete("/api/drafting-history/runs/{run_id}")
async def api_drafting_history_run_delete(run_id: str):
    deleted = await asyncio.to_thread(drafting_history.delete_run, run_id)
    return {"ok": True, "deleted": deleted}


@router.post("/api/drafting-history/versions")
async def api_drafting_history_version_post(req: DraftingHistoryVersionRequest):
    try:
        version = await asyncio.to_thread(
            drafting_history.save_version, req.html, req.citation_start_number, req.reason
        )
    except ValueError as exc:
        return api_error(str(exc), status_code=422, message="Version non enregistrée.")
    return {"ok": True, "version": version}


@router.get("/api/drafting-history/versions/{version_id}")
async def api_drafting_history_version_get(version_id: int):
    version = await asyncio.to_thread(drafting_history.get_version, version_id)
    if version is None:
        return api_error("version_not_found", status_code=404, message="Version introuvable.")
    return {"ok": True, "version": version}


@router.delete("/api/drafting-history/versions/{version_id}")
async def api_drafting_history_version_delete(version_id: int):
    deleted = await asyncio.to_thread(drafting_history.delete_version, version_id)
    return {"ok": True, "deleted": deleted}


@router.post("/api/drafting-suggestions")
async def api_drafting_suggestions(req: DraftingRequest):
    return await asyncio.to_thread(
        suggest_citations_for_draft,
        paragraph=req.paragraph,
        model_name=req.model,
        max_suggestions=req.max_suggestions,
        exclude_citation_keys=req.exclude_citation_keys,
        user_hint=req.user_hint,
        context_before=req.context_before,
        context_after=req.context_after,
    )


@router.post("/api/drafting-suggestions/stream")
async def api_drafting_suggestions_stream(req: DraftingRequest):
    async def generate():
        gen = stream_citations_for_draft(
            paragraph=req.paragraph,
            model_name=req.model,
            max_suggestions=req.max_suggestions,
            exclude_citation_keys=req.exclude_citation_keys,
            user_hint=req.user_hint,
            context_before=req.context_before,
            context_after=req.context_after,
        )
        try:
            async for event in iter_sync_gen(gen):
                yield sse(event)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Progressive citation suggestion failed")
            yield sse(
                {
                    "type": "error",
                    "msg": "citation_suggestion_failed",
                }
            )
            yield sse(
                {
                    "type": "done",
                    "suggestions": [],
                    "assistant_text": "La suggestion progressive a échoué.",
                }
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/drafting-proofread")
async def api_drafting_proofread(req: DraftingReviewRequest, request: Request):
    request_id, abort_signal = register_stream_abort_signal(req.model, req.request_id)
    accepts_sse = "text/event-stream" in str(request.headers.get("accept") or "").lower()
    if not accepts_sse:
        try:
            payload = await asyncio.to_thread(
                proofread_draft,
                text=req.text,
                model_name=req.model,
                abort_signal=abort_signal,
            )
            if isinstance(payload, dict):
                payload.setdefault("request_id", request_id)
            return payload
        finally:
            if abort_signal.is_aborted():
                await stop_local_ollama_model_async(req.model)
            release_stream_abort_signal(request_id)

    async def generate():
        try:
            yield sse({"type": "stream_meta", "request_id": request_id})
            gen = stream_proofread_draft(
                text=req.text,
                model_name=req.model,
                abort_signal=abort_signal,
            )
            async for event in iter_sync_gen(gen):
                if event.get("type") == "result" and isinstance(event.get("payload"), dict):
                    event["payload"].setdefault("request_id", request_id)
                yield sse(event)
                await asyncio.sleep(0)
        except InterruptedError:
            return
        except asyncio.CancelledError:
            abort_signal.abort()
            await stop_local_ollama_model_async(req.model)
            raise
        except Exception:
            _logger.exception("Draft proofreading failed")
            yield sse({"type": "error", "msg": "proofreading_failed"})
        finally:
            if abort_signal.is_aborted():
                await stop_local_ollama_model_async(req.model)
            release_stream_abort_signal(request_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/drafting-argument-suggestions")
async def api_drafting_argument_suggestions(req: DraftingReviewRequest):
    request_id, abort_signal = register_stream_abort_signal(req.model, req.request_id)
    try:
        payload = await asyncio.to_thread(
            suggest_argument_improvements,
            text=req.text,
            model_name=req.model,
            max_suggestions=req.max_suggestions,
            argument_kind=req.argument_kind,
            abort_signal=abort_signal,
        )
        payload.setdefault("request_id", request_id)
    except InterruptedError:
        return JSONResponse(
            status_code=409,
            content={
                "ok": False,
                "status": "cancelled",
                "summary": "Analyse annulée.",
                "request_id": request_id,
                "suggestions": [],
                "review_candidates": [],
                "claims": [],
                "sources": [],
            },
        )
    finally:
        if abort_signal.is_aborted():
            await stop_local_ollama_model_async(req.model)
        release_stream_abort_signal(request_id)
    if payload.get("ok") is not False:
        return payload
    status_codes = {
        "invalid_request": 422,
        "model_incompatible": 422,
        "generation_invalid": 502,
        "verification_failed": 502,
        "error": 503,
    }
    status_code = status_codes.get(str(payload.get("status") or ""), 500)
    return JSONResponse(status_code=status_code, content=payload)


class DraftingCitationResolveRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, max_length=500)


@router.post("/api/drafting-resolve-citations")
async def api_drafting_resolve_citations(req: DraftingCitationResolveRequest):
    records = await asyncio.to_thread(resolve_citation_records, req.keys)
    return {"ok": True, "records": records}


class DraftingOriginRequest(BaseModel):
    sentence: str = Field(..., max_length=2000)
    rel_path: str = Field(..., max_length=1000)
    evidence_quote: str = Field("", max_length=4000)
    model: str | None = None


_ORIGIN_ERROR_STATUS = {"invalid_request": 422, "document_not_found": 404}


@router.post("/api/drafting-origin")
async def api_drafting_origin(req: DraftingOriginRequest):
    """Sources the cited article credits for the sentence (local, one hop)."""
    try:
        return await asyncio.to_thread(
            trace_origin,
            req.sentence,
            req.rel_path,
            evidence_quote=req.evidence_quote,
            model_name=req.model,
        )
    except OriginTraceError as exc:
        return api_error(exc.code, status_code=_ORIGIN_ERROR_STATUS.get(exc.code, 422), message=exc.message)


class DraftingOriginIdentifyRequest(BaseModel):
    reference: str = Field(..., max_length=2000)


@router.post("/api/drafting-origin-identify")
async def api_drafting_origin_identify(
    req: DraftingOriginIdentifyRequest,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    """Crossref record of a reference outside the library (sends the reference only)."""
    if not external_api_allowed:
        return external_api_permission_response()
    try:
        work = await asyncio.to_thread(identify_reference, req.reference)
    except OriginTraceError as exc:
        status_code = 502 if exc.code == "lookup_failed" else 422
        return api_error(exc.code, status_code=status_code, message=exc.message)
    return {"ok": True, "work": work}


@router.post("/api/drafting-word-citations")
async def api_drafting_word_citations(req: DraftingCitationResolveRequest):
    items = await asyncio.to_thread(resolve_word_citation_items, req.keys)
    return {"ok": True, "items": items}


@router.post("/api/drafting-export")
async def api_drafting_export(req: DraftingExportRequest):
    content = str(req.content or "").strip()
    if not content:
        return api_error(
            "empty_draft",
            status_code=422,
            message="Aucun contenu de rédaction à exporter.",
        )

    bibliography = [item for item in req.bibliography if isinstance(item, dict)]
    if req.export_format == "latex":
        # Citations arrive as markers so they come out as \cite{key}, followed
        # by a thebibliography whose \bibitem keys match them.
        body, citation_groups = extract_citation_markers(content)
        document = build_text_export_document(title=req.title, body=body, export_format="latex")
        latex = restore_latex_citations(document.content, citation_groups)
        latex = insert_before_end_document(latex, render_latex_bibliography(bibliography))
        document = ChatExportDocument(title=document.title, content=latex)
    else:
        document = build_text_export_document(
            title=req.title,
            body=strip_citation_markers(content),
            export_format=req.export_format,
            bibliography_sources=bibliography,
        )
    saved = save_export_document(document, export_format=req.export_format)
    saved["bibliography_count"] = len(bibliography)
    msg = f"Export {'LaTeX' if req.export_format == 'latex' else 'Markdown'} généré : {saved['filename']}"
    latex_validation = saved.get("latex_validation")
    if isinstance(latex_validation, dict) and latex_validation.get("msg"):
        msg = f"{msg} ({latex_validation['msg']})"
    return {
        "ok": True,
        "msg": msg,
        "export_file": saved,
    }
