"""Discovery HTTP routes and feature-specific helpers."""

import asyncio
import logging
import time
from typing import Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.api import application
from src.api.common import (
    ExternalApiAllowed,
    api_error,
    external_api_permission_response,
    sse,
)
from src.api.streams import (
    abort_discovery_requests,
    register_discovery_abort_signal,
    release_discovery_abort_signal,
    stop_local_ollama_model_async,
)
from src.api_models import (
    ArticleDiscoveryAbortRequest,
    ArticleDiscoveryDownloadRequest,
    ArticleDiscoveryRequest,
    DiscoveryHistoryRenameRequest,
    ResearchTrendsRequest,
)
from src.app_settings import get_app_settings
from src.discovery_history_db import count_searches as count_discovery_searches
from src.discovery_history_db import delete_search as delete_discovery_search
from src.discovery_history_db import get_search as get_discovery_search
from src.discovery_history_db import list_searches as list_discovery_searches
from src.discovery_history_db import rename_search as rename_discovery_search
from src.discovery_history_db import save_search as save_discovery_search
from src.library import list_library

_logger = logging.getLogger(__name__)

router = APIRouter()


class ComparisonPaper(BaseModel):
    id: int = Field(ge=0)
    title: str = Field(max_length=2000)
    abstract: str = Field(default="", max_length=12000)


class ArticleComparisonRequest(BaseModel):
    papers: list[ComparisonPaper] = Field(min_length=2, max_length=4)
    model: str = Field(min_length=1, max_length=200)
    language: Literal["fr", "en"] = "fr"


@router.post("/api/article-discovery/compare")
async def api_compare_discovered_articles(req: ArticleComparisonRequest):
    from src.workspace_assistance import compare_discovered_articles

    try:
        return await asyncio.to_thread(
            compare_discovered_articles,
            [p.model_dump() for p in req.papers],
            req.model,
            req.language,
        )
    except Exception:
        _logger.exception("Article comparison failed")
        return api_error(
            "comparison_failed",
            status_code=502,
            message="La comparaison n’a pas abouti. Réessayez.",
        )


@router.post("/api/research-trends")
async def api_research_trends(
    req: ResearchTrendsRequest,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    if not req.privacy_ack or not external_api_allowed:
        return external_api_permission_response()

    from src.research_trends import (
        OpenAlexConfigurationError,
        OpenAlexTrendsError,
        build_research_trends,
    )

    try:
        return await build_research_trends(
            topics=[topic.model_dump() for topic in req.topics],
            from_year=req.from_year,
            to_year=req.to_year,
            work_types=list(req.work_types),
            open_access_only=req.open_access_only,
        )
    except OpenAlexConfigurationError as exc:
        return api_error(
            "openalex_api_key_required",
            status_code=503,
            message=str(exc),
        )
    except ValueError as exc:
        return api_error(
            "invalid_research_trends_request",
            status_code=400,
            message=str(exc),
        )
    except OpenAlexTrendsError:
        _logger.exception("OpenAlex publication trend aggregation failed")
        return api_error(
            "research_trends_unavailable",
            status_code=502,
            message="OpenAlex n'a pas renvoyé de tendances annuelles exploitables.",
        )
    except Exception:
        _logger.exception("Research trends request failed")
        return api_error(
            "research_trends_unavailable",
            status_code=502,
            message="Les tendances de publications sont temporairement indisponibles.",
        )


class OriginsLibraryRequest(BaseModel):
    idea: str = Field(..., min_length=1, max_length=600)
    model: str | None = None
    threshold: float = Field(0.3, ge=0.0, le=1.0)


class OriginsRootsRequest(BaseModel):
    topic: str = Field(..., min_length=3, max_length=300)
    privacy_ack: bool = False


@router.post("/api/discovery/origins/library")
async def api_discovery_origins_library(req: OriginsLibraryRequest):
    """Citation chain of an idea through the local library (no network)."""
    from src.chat_origin import trace_library_chain
    from src.origin_tracing import OriginTraceError

    try:
        payload = await asyncio.to_thread(trace_library_chain, req.idea, req.model, req.threshold)
    except OriginTraceError as exc:
        return api_error(exc.code, status_code=422, message=exc.message)
    if not payload.get("ok"):
        return api_error(payload.get("error") or "index_unavailable", status_code=503, message=payload.get("msg") or "")
    return payload


@router.post("/api/discovery/origins/roots")
async def api_discovery_origins_roots(
    req: OriginsRootsRequest,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    """Older works most cited by the OpenAlex literature on a topic (sends the topic)."""
    if not req.privacy_ack or not external_api_allowed:
        return external_api_permission_response()
    from src.origin_roots import OriginRootsConfigurationError, OriginRootsError, find_topic_roots

    try:
        return await find_topic_roots(req.topic)
    except OriginRootsConfigurationError as exc:
        return api_error("openalex_api_key_required", status_code=503, message=str(exc))
    except ValueError as exc:
        return api_error("invalid_origin_roots_request", status_code=400, message=str(exc))
    except OriginRootsError as exc:
        return api_error("origin_roots_empty", status_code=404, message=str(exc))
    except Exception:
        _logger.exception("Origin roots request failed")
        return api_error(
            "origin_roots_unavailable",
            status_code=502,
            message="OpenAlex ne répond pas pour le moment.",
        )


# A progressive search shows its first results after ~25 s, so the LLM
# validation can use more time than the blocking endpoint allows.
_PROGRESSIVE_DISCOVERY_BUDGET_SECONDS = 110.0


async def _run_discovery(
    req: ArticleDiscoveryRequest,
    abort_signal,
    *,
    on_event=None,
    budget_seconds: float | None = None,
) -> tuple[int, dict]:
    """Run one search and save it; returns (HTTP status, payload)."""
    started_at = time.perf_counter()
    app_settings = get_app_settings()
    query_planner_enabled = (
        bool(req.query_planner)
        if req.query_planner is not None
        else bool(app_settings.get("discovery_query_planner"))
    )
    try:
        response = await application.discover_articles(
            mode=req.mode,
            input_text=req.input_text,
            model_name=req.model,
            notebook_id=req.notebook_id,
            max_results=req.max_results,
            include_unpaywall=req.include_unpaywall,
            unpaywall_email=req.unpaywall_email,
            query_planner_enabled=query_planner_enabled,
            debug=req.debug,
            abort_signal=abort_signal,
            on_event=on_event,
            budget_seconds=budget_seconds,
        )
    except InterruptedError:
        await stop_local_ollama_model_async(req.model)
        return 409, {
            "ok": False,
            "error": "article_discovery_interrupted",
            "msg": "Recherche interrompue.",
            "results": [],
            "warnings": [],
        }
    except asyncio.CancelledError:
        abort_signal.abort()
        await stop_local_ollama_model_async(req.model)
        raise
    except Exception:
        _logger.exception("Article discovery failed")
        return 502, {
            "ok": False,
            "error": "article_discovery_failed",
            "results": [],
            "warnings": [],
            "msg": "article_discovery_failed",
        }

    elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
    saved = save_discovery_search(
        search_id=req.history_id or "",
        mode=req.mode,
        input_text=req.input_text,
        notebook_id=req.notebook_id or "",
        model=req.model or "",
        max_results=req.max_results,
        ok=bool(response.get("ok")),
        result_count=int(
            (response.get("meta") or {}).get("result_count", len(response.get("results") or []))
            or 0
        ),
        elapsed_ms=elapsed_ms,
        status_msg=str(response.get("msg") or ""),
        request_payload={
            "mode": req.mode,
            "input_text": req.input_text,
            "notebook_id": req.notebook_id,
            "model": req.model,
            "max_results": req.max_results,
            "query_planner": query_planner_enabled,
            "debug": req.debug,
        },
        response_payload=response,
        meta={
            "provider_used": (response.get("meta") or {}).get("provider_used", ""),
            "providers_used": (response.get("meta") or {}).get("providers_used", []),
            "query_planner_enabled": query_planner_enabled,
            "debug_enabled": bool(req.debug),
            "elapsed_ms": elapsed_ms,
        },
    )
    response["history_id"] = saved.get("search", {}).get("id") or saved.get("id", "")
    response.setdefault("meta", {})
    response["meta"]["elapsed_ms"] = elapsed_ms
    if not response.get("ok"):
        return 502, {
            "ok": False,
            "error": "article_discovery_failed",
            "msg": str(response.get("msg") or "La recherche d'articles a échoué."),
            "results": response.get("results") or [],
            "warnings": response.get("warnings") or [],
            "history_id": response.get("history_id") or "",
            "meta": response.get("meta") or {},
        }
    return 200, response


@router.post("/api/article-discovery")
async def api_article_discovery(
    req: ArticleDiscoveryRequest,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    if not req.privacy_ack or not external_api_allowed:
        return external_api_permission_response()
    abort_signal = register_discovery_abort_signal()
    try:
        status_code, payload = await _run_discovery(req, abort_signal)
    finally:
        release_discovery_abort_signal(abort_signal)
    if status_code == 200:
        return payload
    return JSONResponse(status_code=status_code, content=payload)


@router.post("/api/article-discovery/stream")
async def api_article_discovery_stream(
    req: ArticleDiscoveryRequest,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    """Progressive search: real progress, a preliminary list, then the result."""
    if not req.privacy_ack or not external_api_allowed:
        return external_api_permission_response()
    abort_signal = register_discovery_abort_signal()
    events: asyncio.Queue = asyncio.Queue()

    async def run() -> None:
        try:
            status_code, payload = await _run_discovery(
                req,
                abort_signal,
                on_event=events.put_nowait,
                budget_seconds=_PROGRESSIVE_DISCOVERY_BUDGET_SECONDS,
            )
            events.put_nowait({"type": "done", "status": status_code, "payload": payload})
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("Progressive article discovery failed")
            events.put_nowait(
                {
                    "type": "done",
                    "status": 502,
                    "payload": {"ok": False, "msg": "article_discovery_failed", "results": []},
                }
            )

    async def generate():
        task = asyncio.create_task(run())
        try:
            while True:
                try:
                    event = await asyncio.wait_for(events.get(), timeout=10.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield sse(event)
                if event.get("type") == "done":
                    break
        finally:
            # The client closed the stream (stop button, navigation): stop the
            # search and the local model instead of finishing in the background.
            if not task.done():
                abort_signal.abort()
                task.cancel()
            release_discovery_abort_signal(abort_signal)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/discovery-history")
async def api_discovery_history(limit: int = 100, offset: int = 0):
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    total = count_discovery_searches()
    searches = list_discovery_searches(limit=page_size, offset=page_offset)
    return {
        "searches": searches,
        "pagination": {
            "limit": page_size,
            "offset": page_offset,
            "total": total,
            "has_more": page_offset + len(searches) < total,
        },
    }


@router.get("/api/discovery-history/{search_id}")
async def api_discovery_history_item(search_id: str):
    from fastapi import HTTPException

    payload = get_discovery_search(search_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Recherche discovery introuvable")
    return payload


@router.patch("/api/discovery-history/{search_id}")
async def api_rename_discovery_history_item(search_id: str, req: DiscoveryHistoryRenameRequest):
    return {"ok": rename_discovery_search(search_id, req.title)}


@router.delete("/api/discovery-history/{search_id}")
async def api_delete_discovery_history_item(search_id: str):
    return {"ok": delete_discovery_search(search_id)}


@router.post("/api/article-discovery/abort")
async def api_article_discovery_abort(req: ArticleDiscoveryAbortRequest):
    aborted = abort_discovery_requests()
    unloaded = await stop_local_ollama_model_async(req.model)
    return {
        "ok": bool(aborted or unloaded),
        "aborted_requests": aborted,
        "model_unloaded": unloaded,
        "msg": (
            "Recherche interrompue et modele Ollama decharge."
            if aborted or unloaded
            else "Aucune recherche active ni modele Ollama a decharger."
        ),
    }


@router.post("/api/article-discovery/download-add")
async def api_article_discovery_download_add(
    req: ArticleDiscoveryDownloadRequest,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    if not req.privacy_ack or not external_api_allowed:
        return external_api_permission_response()

    try:
        result = await application.download_and_store_paper_pdf(
            title=req.title,
            doi=req.doi,
            pdf_url=req.pdf_url,
            semantic_url=req.semantic_url,
            citation_count=req.citation_count,
            influential_citation_count=req.influential_citation_count,
            add_to_zotero=req.add_to_zotero,
        )
    except Exception:
        _logger.exception("Article download failed")
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "error": "article_download_failed",
                "msg": "article_download_failed",
            },
        )

    if result.get("ok"):
        rows, paths = list_library(indexed_only=True)
        result["rows"] = rows
        result["paths"] = paths
        return result
    _logger.warning("Article download returned an unsuccessful result")
    return api_error(
        "article_download_failed",
        status_code=502,
        message="Le téléchargement de l'article a échoué.",
    )
