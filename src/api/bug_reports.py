"""Bug reports HTTP routes and feature-specific helpers."""

import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from src.api import application
from src.api.streams import normalize_stream_abort_reason
from src.api_models import BugReportRequest
from src.app_settings import get_app_settings
from src.bug_reports import bug_report_status, deliver_bug_report
from src.chat_history_db import get_conversation_with_messages
from src.llm_provider import public_provider_info, required_embedding_model_name

_logger = logging.getLogger(__name__)

router = APIRouter()


_BUG_REPORT_DEBUG_KEYS = {
    "model",
    "latency_ms",
    "generation_seconds",
    "output_tokens_est",
    "tokens_per_second_est",
    "answer_origin",
}


_BUG_REPORT_PIPELINE_KEYS = {
    "applied",
    "candidate_multiplier",
    "candidates_in",
    "duration_ms",
    "device",
    "dtype",
    "profile",
    "queue_duration_ms",
    "max_query_chars",
    "max_candidate_chars",
    "query_translated",
    "translation_duration_ms",
    "source",
    "mode",
    "explicit",
    "choice",
    "enabled",
    "load_duration_ms",
    "load_failed",
    "loaded",
    "loading",
    "model_name",
    "predict_duration_ms",
    "predict_error",
    "returned",
    "skipped_reason",
    "tier",
    "top_k",
    "triggered_load",
    "unique_candidates",
    "unloaded_after_use",
    "vector_search_ms",
}


def _safe_bug_report_debug_metrics(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    cleaned = {key: value[key] for key in _BUG_REPORT_DEBUG_KEYS if key in value}
    raw_pipeline = value.get("pipeline_metrics")
    pipeline: list[dict[str, Any]] = []
    if isinstance(raw_pipeline, list):
        for raw_item in raw_pipeline[-20:]:
            if not isinstance(raw_item, dict):
                continue
            data = raw_item.get("data")
            safe_data = (
                {key: data[key] for key in _BUG_REPORT_PIPELINE_KEYS if key in data}
                if isinstance(data, dict)
                else {}
            )
            pipeline.append(
                {
                    "stage": str(raw_item.get("stage") or "")[:80],
                    "data": safe_data,
                }
            )
    if pipeline:
        cleaned["pipeline_metrics"] = pipeline
    return cleaned or None


def _build_bug_report_chat(chat_id: str, *, include_debug: bool) -> dict[str, Any] | None:
    normalized_id = str(chat_id or "").strip()
    if not normalized_id:
        return None
    payload = get_conversation_with_messages(normalized_id, limit=20)
    if not payload:
        return None
    raw_chat = payload.get("chat")
    chat: dict[str, Any] = raw_chat if isinstance(raw_chat, dict) else {}
    entries: list[dict[str, Any]] = []
    for row in payload.get("messages") or []:
        if not isinstance(row, dict) or row.get("role") not in {"user", "assistant"}:
            continue
        entry: dict[str, Any] = {
            "role": row.get("role"),
            "content": str(row.get("content") or ""),
            "model": str(row.get("model") or ""),
            "created_at": str(row.get("ts") or ""),
        }
        if include_debug:
            meta = row.get("meta")
            debug = _safe_bug_report_debug_metrics(
                meta.get("debug_metrics") if isinstance(meta, dict) else None
            )
            if debug:
                entry["debug_metrics"] = debug
            if isinstance(meta, dict) and meta.get("interrupted"):
                entry["interrupted"] = True
                entry["interruption_reason"] = normalize_stream_abort_reason(
                    meta.get("interruption_reason")
                )
                try:
                    entry["interruption_elapsed_ms"] = max(
                        0.0, float(meta.get("interruption_elapsed_ms") or 0.0)
                    )
                except (TypeError, ValueError):
                    pass
                entry["answer_origin"] = str(meta.get("answer_origin") or "")[:80]
                entry["mode"] = str(meta.get("mode") or "")[:80]
        entries.append(entry)
    return {
        "message_count": int(chat.get("message_count") or len(entries)),
        "entries": entries,
    }


def _build_bug_report_runtime_context(
    *, current_model: str, attached_chat: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        settings = get_app_settings()
    except Exception:
        _logger.debug(
            "Recovered from an error in _build_bug_report_runtime_context.", exc_info=True
        )
        settings = {}
    try:
        provider = public_provider_info()
    except Exception:
        _logger.debug(
            "Recovered from an error in _build_bug_report_runtime_context.", exc_info=True
        )
        provider = {}
    try:
        runtime = application.get_runtime_status()
    except Exception:
        _logger.debug(
            "Recovered from an error in _build_bug_report_runtime_context.", exc_info=True
        )
        runtime = {}
    try:
        from src.rag.reranker import get_reranker_status

        reranker = get_reranker_status()
    except Exception:
        _logger.debug(
            "Recovered from an error in _build_bug_report_runtime_context.", exc_info=True
        )
        reranker = {}

    used_models: list[str] = []
    for candidate in (
        current_model,
        settings.get("selected_model"),
        runtime.get("model"),
    ):
        normalized = str(candidate or "").strip()
        if normalized and normalized not in used_models:
            used_models.append(normalized)
    for entry in (attached_chat or {}).get("entries", []):
        normalized = str(entry.get("model") or "").strip()
        if normalized and normalized not in used_models:
            used_models.append(normalized)

    model_info = {
        "chat_models": used_models[:20],
        "provider_mode": str(provider.get("effective_mode") or provider.get("mode") or ""),
        "embedding_provider": str(provider.get("embedding_provider") or ""),
        "embedding_model": str(
            provider.get("required_embedding_model")
            or runtime.get("embedding_model")
            or required_embedding_model_name()
            or ""
        ),
        "reranker_model": str(reranker.get("model_name") or ""),
    }
    debug_info = {
        "runtime": {
            key: runtime[key]
            for key in ("ready", "model", "threshold", "chunk_size", "embedding_model", "provider")
            if key in runtime
        },
        "settings": {
            key: settings[key]
            for key in (
                "debug_metrics",
                "llm_thinking",
                "max_chunks",
                "reranker_enabled",
                "reranker_profile",
                "reranker_translate_french",
                "threshold",
            )
            if key in settings
        },
        "reranker": {
            "enabled": bool(reranker.get("enabled")),
            "loaded": bool(reranker.get("loaded")),
            "loading": bool(reranker.get("loading")),
            "load_error": bool(reranker.get("load_error")),
            "predict_error": bool(reranker.get("predict_error")),
            "candidate_multiplier": reranker.get("candidate_multiplier"),
            "profile": reranker.get("profile"),
            "device": reranker.get("device"),
            "dtype": reranker.get("dtype"),
            "gpu": reranker.get("gpu") or {},
            "slow_gpu_fallback": reranker.get("slow_gpu_fallback") or "",
        },
    }
    return model_info, debug_info


@router.get("/api/bug-reports/status")
async def api_bug_report_status():
    return {"ok": True, **bug_report_status()}


@router.post("/api/bug-reports")
async def api_bug_reports(req: BugReportRequest, request: Request):
    client_context = dict(req.client_context)
    client_context["user_agent"] = request.headers.get("user-agent", "")
    try:
        attached_chat = (
            _build_bug_report_chat(req.active_chat_id, include_debug=req.include_diagnostics)
            if req.include_chat
            else None
        )
        model_info: dict[str, Any] | None = None
        debug_info: dict[str, Any] | None = None
        if req.include_diagnostics:
            model_info, debug_info = _build_bug_report_runtime_context(
                current_model=req.current_model,
                attached_chat=attached_chat,
            )
        result = await deliver_bug_report(
            description=req.description,
            steps=req.steps,
            contact=req.contact,
            include_diagnostics=req.include_diagnostics,
            client_logs=req.client_logs if req.include_diagnostics else [],
            client_context=client_context,
            attached_chat=attached_chat,
            model_info=model_info,
            debug_info=debug_info,
            screenshot_data_url=req.screenshot_data_url,
        )
    except ValueError as exc:
        code = str(exc)
        status_code = (
            503
            if code == "bug_report_not_configured"
            else 400
            if code == "bug_report_invalid_screenshot"
            else 500
        )
        return JSONResponse(
            status_code=status_code,
            content={"ok": False, "error": code},
        )
    except RuntimeError as exc:
        _logger.warning("Bug report delivery failed: %s", exc)
        rate_limited = str(exc) == "bug_report_rate_limited"
        return JSONResponse(
            status_code=429 if rate_limited else 502,
            content={
                "ok": False,
                "error": "bug_report_rate_limited"
                if rate_limited
                else "bug_report_delivery_failed",
            },
        )
    except Exception:
        _logger.exception("Unexpected bug report delivery failure")
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": "bug_report_internal_error"},
        )
    if result.get("delivery") == "unconfirmed":
        # Accepted for sending, outcome unknown: the client must not announce a success.
        _logger.warning("Bug report delivery still unconfirmed when the wait ended")
        return JSONResponse(status_code=202, content={"ok": True, **result})
    return {"ok": True, **result}
