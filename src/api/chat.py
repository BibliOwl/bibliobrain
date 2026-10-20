"""Chat HTTP routes and feature-specific helpers."""

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Literal, cast

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from src.api import application
from src.api.common import (
    EXTERNAL_API_PERMISSION_MESSAGE,
    ExternalApiAllowed,
    api_error,
    external_api_permission_response,
    iter_sync_gen,
    sse,
)
from src.api.streams import (
    abort_stream_request,
    abort_stream_requests_for_model,
    normalize_stream_abort_reason,
    register_discovery_abort_signal,
    register_stream_abort_signal,
    release_discovery_abort_signal,
    release_model_after_stop_async,
    release_stream_abort_signal,
    stop_local_ollama_model_async,
    stream_abort_reason,
    stream_interruption_text,
)
from src.api_models import (
    AbortStreamRequest,
    ChatAutoTitleRequest,
    ChatCreateRequest,
    ChatDiscoverRequest,
    ChatRequest,
    ChatTitleRequest,
    ConfirmRequest,
    LiteratureReviewRequest,
)
from src.app_settings import get_app_settings
from src.chat_export import (
    clear_all_export_files,
    collect_export_filenames_from_messages,
    delete_export_files,
)
from src.chat_harness import (
    ToolPlan,
    build_llm_tool_planner_prompt,
    intent_from_llm_tool_choice,
    intent_from_ui_hint,
    parse_llm_tool_choice,
    plan_chat_tools,
    tool_event,
)
from src.chat_history_db import add_message as add_chat_message
from src.chat_history_db import (
    count_conversations,
    create_conversation,
    delete_conversation,
    ensure_conversation,
    get_conversation_messages,
    get_conversation_with_messages,
    list_conversations,
    rename_conversation,
)
from src.chat_intents import (
    ChatIntent,
    detect_chat_intent,
    is_app_help_followup,
    is_draft_citation_followup,
    needs_llm_tool_planner,
    paper_list_followup,
    refers_to_previous_answer,
    resolve_retrieval_depth,
)
from src.common import env_int
from src.discovery_history_db import save_search as save_discovery_search
from src.llm_provider import (
    StreamAbortSignal,
    create_chat_llm,
    invoke_ollama_generate,
    invoke_text,
    is_lmstudio_provider,
    is_remote_provider,
)
from src.model_profiles import resolve_effective_thinking
from src.response_language import detect_question_language, set_response_language

_logger = logging.getLogger(__name__)

router = APIRouter()
_CHAT_TOOL_PLANNER_TIMEOUT_SECONDS = 5.0
_CHAT_CONTEXT_MESSAGE_LIMIT = env_int("CHAT_CONTEXT_MESSAGE_LIMIT", 14, minimum=6, maximum=40)
_DISCOVERY_COMMAND_RE = re.compile(
    r"^\s*/(?:discover|discovery|articles?)\b[:\s-]*(?P<rest>.*)$", re.I
)
_DISCOVERY_TOPIC_PATTERNS = (
    re.compile(
        r"\b(?:cherche(?:r|s|z)?|recherch(?:e|er)|trouve(?:r|s|z)?|find|search|show|montre|"
        r"donne|recense|identify|identifie|suggest|recommend)\b"
        r"(?:[\w\s,'’/-]{0,60}?)\b(?:sur|au\s+sujet\s+de|en\s+rapport\s+avec|related\s+to|"
        r"about|on|regarding|qui\s+parlent\s+de)\b\s*(?P<topic>.+)$",
        re.I,
    ),
    re.compile(
        r"^\s*(?:peux[-\s]*tu\s+|pourrais[-\s]*tu\s+)?"
        r"(?:cherche(?:r|s|z)?|recherch(?:e|er)|trouve(?:r|s|z)?|find|search|show|montre|"
        r"donne|recense)\s+(?:moi\s+)?(?:des?\s+|les\s+)?(?:nouveaux\s+)?"
        r"(?:articles?|papers?|publications?)\s*(?P<topic>.+)$",
        re.I,
    ),
)
_DISCOVERY_GENERIC_QUERY_RE = re.compile(
    r"^(?:en\s+rapport\s+avec\s+)?(?:sur\s+)?(?:ce|cet|cette|this|that)?\s*"
    r"(?:sujet|theme|question|discussion|topic|subject)(?:\s+ci)?$",
    re.I,
)
_DISCOVERY_SUMMARY_MAX_RESULTS = 5


def _estimate_debug_token_count(text: str) -> int:
    value = str(text or "").strip()
    if not value:
        return 0
    words = len([part for part in value.split() if part])
    chars = len(value)
    return max(1, round(max(chars / 4, words * 1.3)))


def _build_chat_debug_metrics(
    *,
    model: str,
    started_at: float,
    first_token_at: float | None,
    output_text: str,
    answer_origin: str = "",
    pipeline_metrics: list[dict] | None = None,
) -> dict[str, object]:
    output_tokens = _estimate_debug_token_count(output_text)
    extra_pipeline = list(pipeline_metrics or [])
    payload: dict[str, object]
    if not output_tokens:
        payload = {"model": str(model or "").strip()}
        if str(answer_origin or "").strip():
            payload["answer_origin"] = str(answer_origin or "").strip()
        if extra_pipeline:
            payload["pipeline_metrics"] = extra_pipeline
        return payload
    now = time.perf_counter()
    first = float(first_token_at or now)
    latency_ms = max(0.0, (first - started_at) * 1000.0)
    generation_seconds = max(0.001, now - first)
    payload = {
        "model": str(model or "").strip(),
        "latency_ms": round(latency_ms, 3),
        "generation_seconds": round(generation_seconds, 3),
        "output_tokens_est": int(output_tokens),
        "tokens_per_second_est": round(output_tokens / generation_seconds, 3),
    }
    if str(answer_origin or "").strip():
        payload["answer_origin"] = str(answer_origin or "").strip()
    if extra_pipeline:
        payload["pipeline_metrics"] = extra_pipeline
    return payload


def _last_user_message_matches(messages: list[dict] | None, message: str) -> bool:
    target = str(message or "").strip()
    if not target:
        return False
    for item in reversed(messages or []):
        if str(item.get("role") or "").strip().lower() != "user":
            continue
        return str(item.get("content") or "").strip() == target
    return False


def _chat_external_search_confirmation_payload(
    message: str,
    *,
    external_allowed: bool,
) -> dict[str, object]:
    explicit_query = _extract_explicit_discovery_query(message)
    search_subject = explicit_query or str(message or "").strip()
    text = (
        "Ou voulez-vous chercher ces articles ?\n\n"
        "- En local : recherche dans les articles deja indexes.\n"
        "- Sources externes : recherche sur Semantic Scholar, Crossref, OpenAlex ou Unpaywall, "
        "ce qui peut envoyer le sujet de recherche a ces services."
    )
    if search_subject:
        text += f"\n\nSujet propose : {search_subject}"
    return {
        "msg": text,
        "message": str(message or "").strip(),
        "query": search_subject,
        "external_allowed": external_allowed,
    }


def _classify_chat_tool_intent_with_llm(
    *,
    message: str,
    model_name: str,
    previous_messages: list[dict] | None,
    timings: dict | None = None,
):
    prompt = build_llm_tool_planner_prompt(
        message,
        previous_messages=previous_messages,
    )
    if not is_remote_provider() and not is_lmstudio_provider():
        _thinking, raw_output = invoke_ollama_generate(
            model_name,
            prompt,
            think=False,
            temperature=0.0,
            repeat_penalty=1.05,
            top_p=0.1,
            options_override={"num_predict": 16},
            request_timeout_seconds=_CHAT_TOOL_PLANNER_TIMEOUT_SECONDS,
            timings=timings,
        )
        raw_output = str(raw_output or "").strip()
    else:
        llm = create_chat_llm(
            model_name,
            temperature=0.0,
            repeat_penalty=1.05,
            top_p=0.1,
            max_output_tokens=16,
            request_timeout_seconds=_CHAT_TOOL_PLANNER_TIMEOUT_SECONDS,
            max_retries=0,
        )
        raw_output = str(invoke_text(llm, prompt) or "").strip()
    return (
        intent_from_llm_tool_choice(message, raw_output, allow_write=False),
        parse_llm_tool_choice(raw_output),
    )


def _build_local_article_search_query(message: str, previous_messages: list[dict] | None) -> str:
    explicit_query = _extract_explicit_discovery_query(message)
    if explicit_query:
        return explicit_query
    return (
        _build_chat_discovery_input_text(
            explicit_query="",
            conversation_text=_conversation_messages_to_text(previous_messages),
        )
        or str(message or "").strip()
    )


def _discovery_request_needs_topic(
    message: str,
    previous_messages: list[dict] | None,
) -> bool:
    """Reject a contextual discovery request when there is no context to resolve."""
    if _extract_explicit_discovery_query(message):
        return False
    return not bool(_conversation_messages_to_text(previous_messages))


def _missing_discovery_topic_message() -> str:
    return (
        "Quel sujet souhaitez-vous utiliser pour la recherche d'articles ? "
        "Précisez le thème, quelques mots-clés ou une question de recherche."
    )


def _conversation_messages_to_text(messages: list[dict] | None) -> str:
    parts: list[str] = []
    for msg in messages or []:
        role = str(msg.get("role") or "assistant").strip() or "assistant"
        content = str(msg.get("content") or "").strip()
        if content:
            parts.append(f"[{role}]: {content}")
    return "\n\n".join(parts).strip()


def _normalize_discovery_query(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^[\s:;,?!\-–—]+", "", text)
    text = re.sub(r"[\s:;,?!\-–—]+$", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \"'`“”‘’")


def _extract_explicit_discovery_query(message: str) -> str:
    raw = str(message or "").strip()
    if not raw:
        return ""

    command = _DISCOVERY_COMMAND_RE.match(raw)
    if command:
        candidate = _normalize_discovery_query(command.group("rest"))
        normalized = candidate.lower()
        if candidate and not _DISCOVERY_GENERIC_QUERY_RE.fullmatch(normalized):
            return candidate
        return ""

    for pattern in _DISCOVERY_TOPIC_PATTERNS:
        match = pattern.search(raw)
        if not match:
            continue
        candidate = _normalize_discovery_query(match.group("topic"))
        normalized = candidate.lower()
        if candidate and not _DISCOVERY_GENERIC_QUERY_RE.fullmatch(normalized):
            return candidate
        return ""

    return ""


def _build_chat_discovery_input_text(*, explicit_query: str, conversation_text: str) -> str:
    query = _normalize_discovery_query(explicit_query)
    context = str(conversation_text or "").strip()
    if query and context:
        return f"Sujet prioritaire de recherche:\n{query}\n\nContexte recent du chat:\n{context}"
    if query:
        return query
    return context


def _build_discovery_session_meta(payload: dict, *, input_text: str) -> dict[str, object]:
    raw_meta = payload.get("meta")
    meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
    return {
        "history_id": str(payload.get("history_id") or "").strip(),
        "result_count": int(meta.get("result_count") or len(payload.get("results") or [])),
        "input_text": str(input_text or payload.get("input_text") or "").strip(),
        "provider_used": str(meta.get("provider_used") or "").strip(),
        "elapsed_ms": meta.get("elapsed_ms"),
    }


def _build_discovery_chat_sources(results: list[dict] | None) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for idx, item in enumerate(results or [], start=1):
        if not isinstance(item, dict):
            continue
        authors = item.get("authors") or []
        rows.append(
            {
                "ref_label": f"S{idx}",
                "index": idx,
                "title": str(item.get("title") or "Article"),
                "authors": ", ".join(
                    str(author).strip() for author in authors if str(author).strip()
                ),
                "year": str(item.get("year") or "").strip(),
                "journal": str(item.get("venue") or item.get("journal") or "").strip(),
                "doi": str(item.get("doi") or "").strip(),
                "snippet": str(
                    item.get("llm_rationale")
                    or item.get("short_summary")
                    or item.get("abstract_preview")
                    or ""
                ).strip(),
            }
        )
    return rows


def _fallback_chat_discovery_answer(results: list[dict], *, result_count: int) -> str:
    if not results:
        return (
            "Je n'ai pas trouve d'article suffisamment pertinent pour cette demande.\n\n"
            "Vous pouvez reformuler le sujet ou ouvrir l'onglet Decouverte pour relancer une recherche plus ciblee."
        )

    lines = [f"J'ai trouve {result_count} article(s) pertinent(s)."]
    for item in results[:_DISCOVERY_SUMMARY_MAX_RESULTS]:
        title = str(item.get("title") or "Article sans titre").strip()
        year = str(item.get("year") or "").strip()
        rationale = str(item.get("llm_rationale") or item.get("short_summary") or "").strip()
        label = f"**{title}**"
        if year:
            label += f" ({year})"
        if rationale:
            lines.append(f"- {label} — {rationale}")
        else:
            lines.append(f"- {label}")
    lines.append("")
    lines.append("Ouvrez l'onglet Decouverte pour consulter tous les resultats et leurs liens.")
    return "\n".join(lines)


def _format_chat_discovery_answer(
    payload: dict,
    *,
    model_name: str,
    user_message: str,
) -> str:
    results = [row for row in (payload.get("results") or []) if isinstance(row, dict)]
    raw_meta = payload.get("meta")
    meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
    result_count = int(meta.get("result_count") or len(results))
    fallback = _fallback_chat_discovery_answer(results, result_count=result_count)
    if not model_name or not results:
        return fallback

    excerpt = {
        "user_request": str(user_message or "").strip(),
        "result_count": result_count,
        "provider_used": str(meta.get("provider_used") or "").strip(),
        "results": [
            {
                "title": str(item.get("title") or "").strip(),
                "year": str(item.get("year") or "").strip(),
                "authors": str(item.get("authors_line") or "").strip(),
                "rationale": str(item.get("llm_rationale") or "").strip(),
                "summary": str(
                    item.get("short_summary") or item.get("abstract_preview") or ""
                ).strip(),
            }
            for item in results[:_DISCOVERY_SUMMARY_MAX_RESULTS]
        ],
    }
    prompt = (
        "Tu rediges une reponse d'assistant en francais, concise et utile.\n"
        "Objectif: presenter une recherche d'articles scientifiques deja effectuee.\n"
        "Contraintes:\n"
        "- Commence par une phrase courte avec le nombre de resultats.\n"
        "- Puis 3 a 5 puces maximum.\n"
        "- Chaque puce: **Titre** (annee) — raison de pertinence.\n"
        "- Termine par une phrase invitant a ouvrir l'onglet Decouverte pour voir tous les resultats.\n"
        "- N'invente aucun article ni aucune information.\n"
        "- Pas de tableau, pas de preambule, pas de mention technique interne.\n\n"
        f"Donnees:\n{json.dumps(excerpt, ensure_ascii=False)}"
    )
    try:
        llm = create_chat_llm(model_name, temperature=0.1, top_p=0.9)
        text = str(invoke_text(llm, prompt) or "").strip()
        return text or fallback
    except Exception:
        _logger.debug("Recovered from an error in _format_chat_discovery_answer.", exc_info=True)
        return fallback


def _chat_discovery_render_timeout(payload: dict) -> float:
    raw_meta = payload.get("meta")
    meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
    try:
        discovery_seconds = max(0.0, float(meta.get("elapsed_ms") or 0.0) / 1000.0)
    except (TypeError, ValueError):
        discovery_seconds = 0.0
    # Keep the complete interactive flow below one minute, including the final
    # prose rendering. The deterministic fallback is already fully usable.
    return max(0.0, min(5.0, 58.5 - discovery_seconds))


def _iter_chat_text_chunks(text: str, *, chunk_size: int = 120):
    value = str(text or "")
    if not value:
        return
    current = ""
    for token in re.findall(r"\S+\s*|\n+", value):
        if current and len(current) + len(token) > chunk_size:
            yield current
            current = token
        else:
            current += token
    if current:
        yield current


async def _run_chat_discovery(
    *,
    chat_id: str = "",
    explicit_query: str = "",
    message_context: list[dict] | None = None,
    model_name: str = "",
    max_results: int = 8,
    include_unpaywall: bool = True,
    debug: bool = False,
    fallback_to_latest_chat: bool = False,
) -> dict[str, object]:
    normalized_chat_id = str(chat_id or "").strip()
    messages = list(message_context or [])
    if not messages and normalized_chat_id:
        messages = get_conversation_messages(normalized_chat_id, limit=_CHAT_CONTEXT_MESSAGE_LIMIT)
    if not messages and fallback_to_latest_chat and not normalized_chat_id:
        conversations = list_conversations()
        if conversations:
            last_chat_id = str((conversations[0] or {}).get("id") or "").strip()
            if last_chat_id:
                normalized_chat_id = last_chat_id
                messages = get_conversation_messages(
                    last_chat_id, limit=_CHAT_CONTEXT_MESSAGE_LIMIT
                )

    conversation_text = _conversation_messages_to_text(messages)
    input_text = _build_chat_discovery_input_text(
        explicit_query=explicit_query,
        conversation_text=conversation_text,
    )
    if not input_text.strip():
        return {
            "ok": False,
            "results": [],
            "warnings": [],
            "msg": "Aucun contexte de conversation disponible pour la decouverte.",
            "input_text": "",
            "history_id": "",
            "meta": {},
        }

    abort_signal = register_discovery_abort_signal()
    started_at = time.perf_counter()
    app_settings = get_app_settings()
    query_planner_enabled = bool(app_settings.get("discovery_query_planner"))

    try:
        response = await application.discover_articles(
            mode="search",
            input_text=input_text,
            model_name=model_name,
            notebook_id=None,
            max_results=max_results,
            include_unpaywall=include_unpaywall,
            unpaywall_email=None,
            query_planner_enabled=query_planner_enabled,
            debug=debug,
            abort_signal=abort_signal,
        )
        elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
        saved = save_discovery_search(
            mode="search",
            input_text=input_text,
            notebook_id="",
            model=model_name or "",
            max_results=max_results,
            ok=bool(response.get("ok")),
            result_count=int(
                (response.get("meta") or {}).get("result_count", len(response.get("results") or []))
                or 0
            ),
            elapsed_ms=elapsed_ms,
            status_msg=str(response.get("msg") or ""),
            request_payload={
                "mode": "search",
                "input_text": input_text,
                "notebook_id": None,
                "model": model_name,
                "max_results": max_results,
                "from_chat": True,
                "explicit_query": explicit_query,
            },
            response_payload=response,
            meta={
                "provider_used": (response.get("meta") or {}).get("provider_used", ""),
                "providers_used": (response.get("meta") or {}).get("providers_used", []),
                "query_planner_enabled": query_planner_enabled,
                "debug_enabled": bool(debug),
                "elapsed_ms": elapsed_ms,
                "from_chat_id": normalized_chat_id,
                "explicit_query": explicit_query,
            },
        )
        response["history_id"] = saved.get("search", {}).get("id") or saved.get("id", "")
        response["input_text"] = input_text
        response.setdefault("meta", {})
        response["meta"]["elapsed_ms"] = elapsed_ms
        response["meta"]["from_chat_id"] = normalized_chat_id
        if explicit_query:
            response["meta"]["explicit_query"] = explicit_query
        return response
    except InterruptedError:
        await stop_local_ollama_model_async(model_name)
        return {
            "ok": False,
            "results": [],
            "warnings": [],
            "msg": "Recherche interrompue.",
            "input_text": input_text,
            "history_id": "",
            "meta": {},
        }
    except asyncio.CancelledError:
        abort_signal.abort()
        await stop_local_ollama_model_async(model_name)
        raise
    except Exception:
        _logger.debug("Chat-triggered article discovery failed.", exc_info=True)
        return {
            "ok": False,
            "results": [],
            "warnings": [],
            "msg": "Erreur decouverte depuis le chat.",
            "input_text": input_text,
            "history_id": "",
            "meta": {},
        }
    finally:
        release_discovery_abort_signal(abort_signal)


@router.get("/api/chat-exports/{filename}")
async def api_chat_export_file(filename: str):
    """Serve a generated chat export from STORAGE_DIR/chat_exports only."""
    from src.chat_export import resolve_export_file

    path = resolve_export_file(filename)
    if path is None:
        return JSONResponse({"error": "Export introuvable"}, status_code=404)
    media_type = (
        "text/markdown; charset=utf-8" if path.suffix.lower() == ".md" else "application/x-tex"
    )
    return FileResponse(
        str(path),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


@router.post("/api/chat-exports/clear")
async def api_clear_chat_exports(req: ConfirmRequest):
    """Supprime tous les exports Markdown/LaTeX générés par le chat."""
    if not req.confirm:
        return api_error(
            "confirmation_required",
            status_code=400,
            message="Confirmation requise.",
        )
    result = clear_all_export_files()
    return {
        "ok": True,
        "msg": f"{result.get('files_deleted', 0)} export(s) chat supprimé(s).",
        "chat_exports": result,
    }


@router.get("/api/chats")
async def api_list_chats(
    limit: int = 100,
    offset: int = 0,
):
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    total = count_conversations()
    chats = list_conversations(limit=page_size, offset=page_offset)
    return {
        "chats": chats,
        "pagination": {
            "limit": page_size,
            "offset": page_offset,
            "total": total,
            "has_more": page_offset + len(chats) < total,
        },
    }


@router.post("/api/chats")
async def api_create_chat(req: ChatCreateRequest):
    chat = create_conversation(title=(req.title or "").strip())
    return {"chat": chat}


@router.get("/api/chats/{chat_id}")
async def api_get_chat(chat_id: str, limit: int = 200, offset: int = 0):
    from fastapi import HTTPException

    page_size = max(1, min(limit, 300))
    page_offset = max(0, offset)
    payload = get_conversation_with_messages(chat_id, limit=page_size, offset=page_offset)
    if payload is None:
        raise HTTPException(status_code=404, detail="Conversation introuvable")
    total = int((payload.get("chat") or {}).get("message_count") or 0)
    payload["pagination"] = {
        "limit": page_size,
        "offset": page_offset,
        "total": total,
        "has_more": page_offset + len(payload.get("messages") or []) < total,
    }
    return payload


@router.post("/api/chats/{chat_id}/title")
async def api_rename_chat(chat_id: str, req: ChatTitleRequest):
    ok = rename_conversation(chat_id, req.title)
    return {"ok": ok, "title": " ".join(str(req.title or "").split())[:120]}


def _sanitize_generated_chat_title(raw: str) -> str:
    from src.llm_provider import strip_thinking_blocks

    # Certains modèles inlinent leur raisonnement <think> dans la sortie ;
    # d'autres raisonnent sans balises et donnent le titre entre guillemets
    # au fil du texte. On essaie plusieurs candidats, du plus fiable au moins.
    text = strip_thinking_blocks(str(raw or "")).strip()

    def _clean(candidate: str) -> str:
        value = str(candidate or "").strip().strip("\"'`«»").strip()
        value = re.sub(r"^(titre|title)\s*:\s*", "", value, flags=re.IGNORECASE)
        value = value.strip().strip("\"'`«»")
        value = " ".join(value.split()).rstrip(".!,;:")
        if len(value) > 70:
            value = value[:70].rsplit(" ", 1)[0]
        return value

    def _valid(value: str) -> bool:
        words = value.split()
        return 2 <= len(words) <= 12

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    quoted = re.findall(r'[«"]([^"«»\n]{8,90})[»"]', text)
    candidates: list[str] = []
    if len(lines) == 1:
        candidates.append(lines[0])
    candidates.extend(reversed(quoted))
    if lines:
        candidates.append(lines[-1])
        candidates.append(lines[0])
    if not text:
        # Sortie tronquée au milieu d'un bloc <think> jamais fermé : le titre
        # choisi apparaît généralement entre guillemets dans le raisonnement.
        raw_text = str(raw or "")
        candidates.extend(reversed(re.findall(r'[«"]([^"«»\n]{8,90})[»"]', raw_text)))
    for candidate in candidates:
        cleaned = _clean(candidate)
        if _valid(cleaned):
            return cleaned
    return ""


_CHAT_AUTOTITLE_PROMPT = (
    "Voici le début d'une conversation entre un chercheur et un assistant "
    "de bibliothèque scientifique.\n\n"
    "Question : {question}\n\n"
    "Réponse (extrait) : {answer}\n\n"
    "Donne un titre court (4 à 8 mots) qui résume le sujet de cette conversation, "
    "dans la même langue que la question. Réponds uniquement avec le titre, "
    "sans guillemets ni ponctuation finale."
)


@router.post("/api/chats/{chat_id}/autotitle")
async def api_autotitle_chat(chat_id: str, req: ChatAutoTitleRequest):
    messages = get_conversation_messages(chat_id, limit=4)
    question = ""
    answer = ""
    for msg in messages or []:
        role = str(msg.get("role") or "")
        content = str(msg.get("content") or "").strip()
        if role == "user" and not question:
            question = content
        elif role == "assistant" and not answer:
            answer = content
    if not question:
        return api_error(
            "empty_conversation",
            status_code=422,
            message="Conversation vide.",
        )

    prompt = _CHAT_AUTOTITLE_PROMPT.format(
        question=question[:600],
        answer=answer[:600] or "(pas encore de réponse)",
    )
    model_name = (req.model or "").strip()
    if not model_name:
        return api_error("model_required", status_code=422, message="Modèle requis.")
    try:
        title = ""
        if not is_remote_provider() and not is_lmstudio_provider():
            # Budget large : certains modèles raisonnent dans la sortie avant
            # de donner le titre (le bloc <think> est retiré au nettoyage).
            title_timings: dict = {}
            _thinking, raw_output = await asyncio.to_thread(
                invoke_ollama_generate,
                model_name,
                prompt,
                think=False,
                temperature=0.2,
                options_override={"num_predict": 64},
                request_timeout_seconds=45,
                timings=title_timings,
            )
            if title_timings.get("reloaded"):
                _logger.info(
                    "Chat title reloaded %s (%.0f ms)", model_name, title_timings["load_ms"]
                )
            title = _sanitize_generated_chat_title(raw_output)
        else:
            llm = create_chat_llm(model_name, temperature=0.2, max_output_tokens=300)
            title = _sanitize_generated_chat_title(
                await asyncio.to_thread(invoke_text, llm, prompt)
            )
    except Exception:
        _logger.exception("Chat title generation failed")
        return api_error(
            "chat_title_generation_failed",
            status_code=502,
            message="Génération du titre impossible.",
        )
    if not title or len(title.split()) < 2:
        return api_error(
            "invalid_generated_title",
            status_code=502,
            message="Titre généré invalide.",
        )
    ok = rename_conversation(chat_id, title)
    return {"ok": ok, "title": title}


@router.delete("/api/chats/{chat_id}")
async def api_delete_chat(chat_id: str):
    messages = get_conversation_messages(chat_id)
    export_filenames = collect_export_filenames_from_messages(messages)
    ok = delete_conversation(chat_id)
    export_result = (
        delete_export_files(export_filenames)
        if ok
        else {
            "deleted": [],
            "missing": [],
            "files_deleted": 0,
            "bytes_deleted": 0,
        }
    )
    return {
        "ok": ok,
        "chat_exports": export_result,
        "export_files_deleted": export_result.get("files_deleted", 0),
    }


@dataclass
class _ChatTurn:
    """State of one /api/chat generation, shared by its stages and error paths."""

    req: ChatRequest
    message: str
    abort_signal: StreamAbortSignal
    ui_language: str
    chat_id: str = ""
    previous_messages: list[dict] = field(default_factory=list)
    article_path: str = ""
    article_title: str = ""
    article_paths: list[str] = field(default_factory=list)
    chat_mode: str = "rag"
    intent_query: str = ""
    planner_source: str = "deterministic"
    planner_choice: str = ""
    tool_plan: ToolPlan | None = None
    paper_list_excluded_sources: tuple[dict, ...] = ()
    discovery_topic_missing: bool = False
    answer_origin: str = ""
    full_answer: str = ""
    final_sources: list[dict] = field(default_factory=list)
    export_file_meta: dict | None = None
    discovery_session_meta: dict[str, object] | None = None
    external_search_confirmation_meta: dict[str, object] | None = None
    pipeline_metrics: list[dict] = field(default_factory=list)
    saw_error_msg: str = ""
    started_at: float = field(default_factory=time.perf_counter)
    first_token_at: float | None = None

    def add_answer_text(self, text: str) -> None:
        if text and self.first_token_at is None:
            self.first_token_at = time.perf_counter()
        self.full_answer += text

    def replace_answer_text(self, text: str) -> None:
        if text and self.first_token_at is None:
            self.first_token_at = time.perf_counter()
        self.full_answer = text

    def debug_metrics(self) -> dict[str, object]:
        return _build_chat_debug_metrics(
            model=self.req.model,
            started_at=self.started_at,
            first_token_at=self.first_token_at,
            output_text=self.full_answer,
            answer_origin=self.answer_origin,
            pipeline_metrics=self.pipeline_metrics or None,
        )

    def discovery_session_entry(self) -> dict[str, object]:
        if not self.discovery_session_meta:
            return {}
        return {"discovery_session": self.discovery_session_meta}


def _chat_response_language(message: str) -> str:
    """Answer in the question's language; an ambiguous question keeps the UI language.

    "How does ...?" gets English even with a French UI. The context variable
    lives in this request's task and reaches the generation threads, which
    copy the context.
    """
    ui_language = str(get_app_settings().get("ui_language") or "").strip().lower()
    if ui_language not in {"fr", "en"}:
        ui_language = "fr"
    ui_language = detect_question_language(message) or ui_language
    set_response_language(ui_language)
    return ui_language


def _request_article_paths(req: ChatRequest) -> list[str]:
    """Articles the question is limited to: the single path, then the multi-selection."""
    paths: list[str] = []
    for value in [req.article_path, *req.article_paths]:
        path = str(value or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


def _deterministic_chat_intent(
    turn: _ChatTurn,
    message: str,
    previous_messages: list[dict],
) -> tuple[ChatIntent, str]:
    """Intent from the article scope, the UI hint, then the message itself."""
    req = turn.req
    if req.force_visual_analysis:
        return ChatIntent(mode="rag", query=message, explicit=True), "deterministic"
    ui_hint_intent = intent_from_ui_hint(message, req.hint_action)
    intent = ui_hint_intent or detect_chat_intent(message)
    if (
        intent.mode == "rag"
        and not intent.explicit
        and is_app_help_followup(message, previous_messages)
    ):
        return ChatIntent(mode="app_help", query=message), "previous_help"
    # An attached article scopes the answer, except for help and the origin
    # search, which starts from that article.
    if _request_article_paths(req) and intent.mode not in {"app_help", "origin_trace"}:
        return ChatIntent(mode="rag", query=message, explicit=True), "deterministic"
    # "Liste les thèmes de ces articles" is about the papers just listed, not a
    # new list; an export or a bibliography of "cette liste" keeps its tool.
    if (
        intent.mode in {"rag", "paper_list"}
        and not intent.explicit
        and refers_to_previous_answer(message, previous_messages)
    ):
        return ChatIntent(mode="rag", query=message, explicit=True), "previous_answer"
    if intent.mode == "rag" and not intent.explicit:
        if is_draft_citation_followup(message, previous_messages):
            intent = ChatIntent(mode="drafting", query=message)
        elif followup := paper_list_followup(message, previous_messages):
            intent = ChatIntent(mode="paper_list", query=followup.query)
            turn.paper_list_excluded_sources = followup.excluded_sources
    return intent, "ui_hint" if ui_hint_intent is not None else "deterministic"


async def _plan_chat_intent_with_llm(
    intent: ChatIntent,
    *,
    message: str,
    model_name: str,
    previous_messages: list[dict],
    timings: dict | None = None,
) -> tuple[ChatIntent, str, str]:
    """Ask the model to pick a tool; keep ``intent`` when it cannot decide in time."""
    try:
        llm_intent, llm_choice = await asyncio.wait_for(
            asyncio.to_thread(
                _classify_chat_tool_intent_with_llm,
                message=message,
                model_name=model_name,
                previous_messages=previous_messages,
                timings=timings,
            ),
            timeout=_CHAT_TOOL_PLANNER_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        _logger.warning("Chat intent classifier failed, using fallback: %s", exc)
        return intent, "llm_fallback", ""
    if llm_intent is None:
        return intent, "llm_fallback", ""
    return llm_intent, "llm", str(llm_choice or "")


def _planner_metric(
    turn: _ChatTurn, intent: ChatIntent, started_at: float, timings: dict | None = None
) -> dict:
    return {
        "type": "pipeline_metrics",
        "stage": "tool_planner",
        "data": {
            "duration_ms": round((time.perf_counter() - started_at) * 1000, 1),
            "source": turn.planner_source,
            "mode": intent.mode,
            "explicit": intent.explicit,
            "choice": turn.planner_choice,
            **(timings or {}),
        },
    }


def _persist_user_message(turn: _ChatTurn, tool_name: str | None) -> None:
    # A confirmed article search replays the question the user already sent.
    if (
        turn.chat_mode == "article_discovery"
        and tool_name in {"search.local_articles", "search.external_articles"}
        and _last_user_message_matches(turn.previous_messages, turn.message)
    ):
        return
    req = turn.req
    add_chat_message(
        turn.chat_id,
        "user",
        turn.message,
        model=req.model,
        meta={
            "threshold": req.threshold,
            "tags": req.tags,
            "collections": req.collections,
            "thinking": req.thinking,
            "mode": turn.chat_mode,
            "article_path": turn.article_path,
            "article_title": turn.article_title,
            "article_paths": turn.article_paths,
            "article_scope_once": bool(req.article_scope_once),
            "force_visual_analysis": bool(req.force_visual_analysis),
            "hint_action": str(req.hint_action or "").strip().lower(),
        },
    )


def _text_token_events(turn: _ChatTurn, text: str):
    for token_piece in _iter_chat_text_chunks(text):
        turn.add_answer_text(token_piece)
        yield sse({"type": "token", "text": token_piece})


async def _render_chat_discovery_answer(
    turn: _ChatTurn,
    payload: dict,
    results: list[dict],
) -> str:
    """LLM prose for the found articles, or the deterministic list when time is short."""
    raw_meta = payload.get("meta")
    meta: dict = raw_meta if isinstance(raw_meta, dict) else {}
    render_timeout = _chat_discovery_render_timeout(payload)
    if render_timeout >= 0.75:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(
                    _format_chat_discovery_answer,
                    payload,
                    model_name=turn.req.model,
                    user_message=turn.message,
                ),
                timeout=render_timeout,
            )
        except TimeoutError:
            pass
    return _fallback_chat_discovery_answer(
        results,
        result_count=int(meta.get("result_count") or len(results)),
    )


async def _article_discovery_events(turn: _ChatTurn, *, external_api_allowed: bool):
    """Answer an article-discovery request that is not a local index search."""
    tool_plan = turn.tool_plan
    assert tool_plan is not None
    if turn.discovery_topic_missing:
        for event in _text_token_events(turn, _missing_discovery_topic_message()):
            yield event
    elif tool_plan.action == "confirm":
        turn.external_search_confirmation_meta = _chat_external_search_confirmation_payload(
            turn.message, external_allowed=external_api_allowed
        )
        turn.full_answer = str(turn.external_search_confirmation_meta.get("msg") or "").strip()
        if tool_plan.confirmation is not None:
            yield sse(
                {
                    "type": "tool_confirmation",
                    "confirmation": tool_plan.confirmation.event_data(),
                }
            )
        yield sse(
            {"type": "external_search_confirmation", **turn.external_search_confirmation_meta}
        )
    elif not external_api_allowed:
        for event in _text_token_events(turn, EXTERNAL_API_PERMISSION_MESSAGE):
            yield event
    else:
        yield sse({"type": "status", "msg": "Recherche d'articles externes en cours..."})
        discovery_payload = await _run_chat_discovery(
            chat_id=turn.chat_id,
            explicit_query=_extract_explicit_discovery_query(turn.message),
            message_context=turn.previous_messages,
            model_name=turn.req.model,
            max_results=8,
            include_unpaywall=True,
            debug=False,
        )
        if discovery_payload.get("ok"):
            raw_results = discovery_payload.get("results")
            results = (
                [row for row in raw_results if isinstance(row, dict)]
                if isinstance(raw_results, list)
                else []
            )
            turn.discovery_session_meta = _build_discovery_session_meta(
                discovery_payload,
                input_text=str(discovery_payload.get("input_text") or ""),
            )
            turn.final_sources = _build_discovery_chat_sources(results)
            yield sse({"type": "discovery_session", "data": turn.discovery_session_meta})
            rendered_answer = await _render_chat_discovery_answer(turn, discovery_payload, results)
        else:
            rendered_answer = str(
                discovery_payload.get("msg") or "La recherche d'articles n'a pas pu aboutir."
            ).strip()
        for event in _text_token_events(turn, rendered_answer):
            yield event
    yield sse({"type": "sources", "data": turn.final_sources, "answer_origin": turn.answer_origin})


def _paper_list_stream(turn: _ChatTurn, query: str):
    from src.rag.paper_list import FOLLOWUP_PAPER_LIST_CHUNKS, stream_paper_list

    req = turn.req
    excluded = turn.paper_list_excluded_sources
    return stream_paper_list(
        query,
        req.model,
        req.threshold,
        tags=req.tags,
        collections=req.collections,
        target_chunks=FOLLOWUP_PAPER_LIST_CHUNKS if excluded else 32,
        abort_signal=turn.abort_signal,
        language=turn.ui_language,
        exclude_sources=list(excluded),
    )


def _tool_stream(turn: _ChatTurn, intent: ChatIntent, tool_name: str | None):
    """Synchronous event generator of the tool that answers this turn."""
    req = turn.req
    query = turn.intent_query
    if tool_name == "app.help":
        from src.app_help import stream_app_help

        return stream_app_help(
            query,
            req.model,
            previous_messages=turn.previous_messages,
            language=turn.ui_language,
            abort_signal=turn.abort_signal,
        )
    if tool_name == "research.literature_review":
        from src.rag.literature_review import stream_literature_review

        return stream_literature_review(
            query,
            req.model,
            req.threshold,
            tags=req.tags,
            collections=req.collections,
            target_chunks=24,
            abort_signal=turn.abort_signal,
        )
    if tool_name == "search.local_articles":
        return _paper_list_stream(turn, query)
    if tool_name in {"research.compare_papers", "research.evidence_matrix", "research.fact_check"}:
        from src.rag.native_chat import stream_native_retrieval_action

        native_mode = cast(
            Literal["paper_compare", "evidence_matrix", "fact_check"],
            turn.chat_mode,
        )
        return stream_native_retrieval_action(
            native_mode,
            query,
            req.model,
            req.threshold,
            tags=req.tags,
            collections=req.collections,
            target_chunks=28,
        )
    if tool_name == "research.origin_trace":
        from src.chat_origin import stream_chat_origin

        return stream_chat_origin(
            query,
            req.model,
            req.threshold,
            tags=req.tags,
            collections=req.collections,
            article_paths=turn.article_paths,
            abort_signal=turn.abort_signal,
        )
    if tool_name == "chat.bibliography":
        from src.rag.native_chat import stream_chat_bibliography

        return stream_chat_bibliography(
            previous_messages=turn.previous_messages,
            scope=intent.export_scope,
        )
    if tool_name == "notebook.create":
        from src.rag.native_chat import stream_notebook_from_chat

        return stream_notebook_from_chat(
            user_query=query,
            previous_messages=turn.previous_messages,
        )
    if tool_name == "chat.draft":
        from src.chat_drafting import stream_chat_drafting

        return stream_chat_drafting(
            query,
            req.model,
            previous_messages=turn.previous_messages,
            language=turn.ui_language,
            abort_signal=turn.abort_signal,
            threshold=req.threshold,
            tags=req.tags,
            collections=req.collections,
        )
    if tool_name == "chat.export":
        from src.chat_export import stream_chat_export

        return stream_chat_export(
            user_query=query,
            model_name=req.model,
            previous_messages=turn.previous_messages,
            export_format=intent.export_format or "markdown",
            scope=intent.export_scope,
        )
    effective_thinking = resolve_effective_thinking(
        req.model,
        req.thinking,
        allow_small_model_override=req.force_thinking_small_models,
    )
    return application.stream_chat_api(
        query,
        req.model,
        req.threshold,
        tags=req.tags,
        collections=req.collections,
        source_paths=turn.article_paths or None,
        conversation_history=turn.previous_messages,
        thinking_enabled=effective_thinking,
        abort_signal=turn.abort_signal,
        depth=resolve_retrieval_depth(query, req.depth),
        force_visual_analysis=req.force_visual_analysis,
    )


async def _relay_tool_stream(turn: _ChatTurn, gen):
    """Forward a tool's events to the client while recording the answer state."""
    async for chunk in iter_sync_gen(gen):
        kind = chunk.get("type")
        if kind == "token":
            token_piece = chunk.get("text", "") or ""
            turn.add_answer_text(token_piece)
            yield sse({"type": "token", "text": token_piece})
            continue
        if kind == "answer_replace":
            replacement = str(chunk.get("text") or "")
            turn.replace_answer_text(replacement)
            yield sse({"type": "answer_replace", "text": replacement})
            continue
        if kind == "sources":
            turn.final_sources = list(chunk.get("data") or [])
            turn.answer_origin = str(chunk.get("answer_origin") or "").strip()
        elif kind == "pipeline_metrics":
            turn.pipeline_metrics.append(dict(chunk))
        elif kind == "export_file":
            data = chunk.get("data")
            turn.export_file_meta = dict(data) if isinstance(data, dict) else None
        elif kind == "error":
            turn.saw_error_msg = chunk.get("msg", "") or "Erreur inconnue"
        yield sse(chunk)


def _save_assistant_answer(turn: _ChatTurn) -> None:
    req = turn.req
    if turn.full_answer.strip():
        tool_plan = turn.tool_plan
        assert tool_plan is not None
        assistant_meta: dict[str, object] = {
            "sources": turn.final_sources,
            "debug_metrics": turn.debug_metrics(),
            "mode": turn.chat_mode,
            "rag_query": turn.intent_query if turn.answer_origin == "rag" else "",
            "article_path": turn.article_path,
            "article_title": turn.article_title,
            "article_paths": turn.article_paths,
            "article_scope_once": bool(req.article_scope_once),
            "force_visual_analysis": bool(req.force_visual_analysis),
            "tool_plan": {
                "action": "clarify" if turn.discovery_topic_missing else tool_plan.action,
                "calls": (
                    []
                    if turn.discovery_topic_missing
                    else [call.event_data() for call in tool_plan.calls]
                ),
                "planner_source": turn.planner_source,
                "planner_choice": turn.planner_choice,
            },
        }
        if turn.export_file_meta:
            assistant_meta["export_file"] = turn.export_file_meta
        assistant_meta.update(turn.discovery_session_entry())
        if turn.external_search_confirmation_meta:
            assistant_meta["external_search_confirmation"] = turn.external_search_confirmation_meta
        add_chat_message(
            turn.chat_id, "assistant", turn.full_answer, model=req.model, meta=assistant_meta
        )
    elif turn.saw_error_msg:
        add_chat_message(
            turn.chat_id,
            "assistant",
            f"Erreur: {turn.saw_error_msg}",
            model=req.model,
            meta={"error": True, "mode": turn.chat_mode},
        )


def _save_interrupted_answer(turn: _ChatTurn, reason: str) -> None:
    """Keep the partial answer (or a notice) when the generation is stopped."""
    interruption_text = stream_interruption_text(reason)
    interruption_meta = {
        "interrupted": True,
        "interruption_reason": reason,
        "interruption_elapsed_ms": round((time.perf_counter() - turn.started_at) * 1000.0, 1),
        "answer_origin": turn.answer_origin,
        "mode": turn.chat_mode,
    }
    if not turn.full_answer.strip():
        add_chat_message(
            turn.chat_id,
            "assistant",
            interruption_text,
            model=turn.req.model,
            meta=interruption_meta,
        )
        return
    add_chat_message(
        turn.chat_id,
        "assistant",
        turn.full_answer + f"\n\n{interruption_text}",
        model=turn.req.model,
        meta={
            "sources": turn.final_sources,
            "debug_metrics": turn.debug_metrics(),
            **interruption_meta,
            **turn.discovery_session_entry(),
        },
    )


def _save_failed_answer(turn: _ChatTurn, error_code: str) -> None:
    if not turn.full_answer.strip():
        add_chat_message(
            turn.chat_id,
            "assistant",
            f"Erreur: {error_code}",
            model=turn.req.model,
            meta={"error": True, "mode": turn.chat_mode},
        )
        return
    add_chat_message(
        turn.chat_id,
        "assistant",
        turn.full_answer,
        model=turn.req.model,
        meta={
            "sources": turn.final_sources,
            "debug_metrics": turn.debug_metrics(),
            "mode": turn.chat_mode,
            **turn.discovery_session_entry(),
        },
    )


async def _chat_turn_events(turn: _ChatTurn, request_id: str, *, external_api_allowed: bool):
    """Run one chat turn: conversation, intent, tool plan, tool stream, persistence."""
    req, message = turn.req, turn.message
    chat, created = ensure_conversation(req.chat_id, seed_message=message)
    turn.chat_id = chat["id"]
    yield sse({"type": "chat_meta", "chat": chat, "created": created})
    yield sse({"type": "stream_meta", "request_id": request_id})
    turn.previous_messages = get_conversation_messages(
        turn.chat_id, limit=_CHAT_CONTEXT_MESSAGE_LIMIT
    )
    turn.article_paths = _request_article_paths(req)
    turn.article_path = turn.article_paths[0] if len(turn.article_paths) == 1 else ""
    turn.article_title = str(req.article_title or "").strip()

    intent, turn.planner_source = _deterministic_chat_intent(turn, message, turn.previous_messages)
    planner_started_at = time.perf_counter()
    planner_timings: dict = {}
    if intent.mode == "rag" and not intent.explicit and needs_llm_tool_planner(message):
        yield sse({"type": "status", "msg": "Analyse de la requete..."})
        intent, turn.planner_source, turn.planner_choice = await _plan_chat_intent_with_llm(
            intent,
            message=message,
            model_name=req.model,
            previous_messages=turn.previous_messages,
            timings=planner_timings,
        )
    planner_metric = _planner_metric(turn, intent, planner_started_at, planner_timings)
    turn.pipeline_metrics.append(planner_metric)
    yield sse(planner_metric)

    turn.chat_mode = intent.mode
    turn.intent_query = intent.query or message
    article_search_scope = str(req.article_search_scope or "").strip().lower()
    if article_search_scope not in {"local", "external"}:
        article_search_scope = ""
    tool_plan = plan_chat_tools(
        intent,
        article_search_scope=article_search_scope,
        external_search_confirmed=req.external_search_confirmed,
        privacy_ack=req.privacy_ack,
    )
    turn.tool_plan = tool_plan
    tool_call = tool_plan.primary_call
    tool_name = tool_plan.primary_tool_name
    turn.discovery_topic_missing = turn.chat_mode == "article_discovery" and (
        _discovery_request_needs_topic(message, turn.previous_messages)
    )
    _persist_user_message(turn, tool_name)

    # A clarifying question about the missing topic runs no tool.
    announced_call = None if turn.discovery_topic_missing else tool_call
    if announced_call is not None:
        yield sse(tool_event("tool_started", announced_call))
    gen = None
    if turn.chat_mode == "article_discovery":
        if tool_name == "search.local_articles" and not turn.discovery_topic_missing:
            turn.answer_origin = "paper_list"
            gen = _paper_list_stream(
                turn, _build_local_article_search_query(message, turn.previous_messages)
            )
        else:
            turn.answer_origin = "article_discovery"
            async for event in _article_discovery_events(
                turn, external_api_allowed=external_api_allowed
            ):
                yield event
    else:
        gen = _tool_stream(turn, intent, tool_name)
    if gen is not None:
        async for event in _relay_tool_stream(turn, gen):
            yield event

    if announced_call is not None:
        yield sse(
            tool_event(
                "tool_result",
                announced_call,
                ok=not bool(turn.saw_error_msg),
                answer_origin=turn.answer_origin,
            )
        )
    _save_assistant_answer(turn)


@router.post("/api/chat")
async def api_chat(req: ChatRequest, *, external_api_allowed: ExternalApiAllowed):
    """SSE stream : status / token / answer_replace / sources / error."""

    async def generate():
        request_id, abort_signal = register_stream_abort_signal(req.model)
        message = (req.message or "").strip()
        turn = _ChatTurn(
            req=req,
            message=message,
            abort_signal=abort_signal,
            ui_language=_chat_response_language(message),
        )
        if not message:
            yield sse({"type": "error", "msg": "Message vide."})
            release_stream_abort_signal(request_id)
            return

        try:
            async with contextlib.aclosing(
                _chat_turn_events(turn, request_id, external_api_allowed=external_api_allowed)
            ) as events:
                async for event in events:
                    yield event
        except InterruptedError:
            await release_model_after_stop_async(req.model, stream_abort_reason(abort_signal))
            _save_interrupted_answer(turn, stream_abort_reason(abort_signal))
            return
        except asyncio.CancelledError:
            abort_signal.abort("client_disconnect")
            await release_model_after_stop_async(
                req.model, stream_abort_reason(abort_signal, "client_disconnect")
            )
            _save_interrupted_answer(turn, stream_abort_reason(abort_signal, "client_disconnect"))
            raise
        except Exception:
            _logger.exception("Chat stream failed")
            _save_failed_answer(turn, "chat_generation_failed")
            yield sse({"type": "error", "msg": "chat_generation_failed"})
        finally:
            if abort_signal.is_aborted():
                await release_model_after_stop_async(req.model, stream_abort_reason(abort_signal))
            release_stream_abort_signal(request_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/literature-review")
async def api_literature_review(req: LiteratureReviewRequest):
    """SSE stream for literature review synthesis (status / token / sources / error)."""
    from src.rag.literature_review import stream_literature_review

    request_id, abort_signal = register_stream_abort_signal(req.model, req.request_id)

    async def generate():
        message = (req.message or "").strip()
        chat_id = ""
        full_answer = ""
        final_sources: list[dict] = []
        if not message:
            yield sse({"type": "error", "msg": "Message vide."})
            release_stream_abort_signal(request_id)
            return
        try:
            yield sse({"type": "stream_meta", "request_id": request_id})
            chat, created = ensure_conversation(req.chat_id, seed_message=message)
            chat_id = chat["id"]
            yield sse({"type": "chat_meta", "chat": chat, "created": created})
            add_chat_message(
                chat_id,
                "user",
                message,
                model=req.model,
                meta={
                    "threshold": req.threshold,
                    "tags": req.tags,
                    "collections": req.collections,
                    "mode": "literature_review",
                },
            )
            gen = stream_literature_review(
                message,
                req.model,
                req.threshold,
                tags=req.tags,
                collections=req.collections,
                target_chunks=req.target_chunks,
                abort_signal=abort_signal,
            )
            saw_error_msg = ""
            async for chunk in iter_sync_gen(gen):
                kind = chunk.get("type")
                if kind == "token":
                    full_answer += chunk.get("text", "") or ""
                elif kind == "sources":
                    final_sources = list(chunk.get("data") or [])
                elif kind == "error":
                    saw_error_msg = chunk.get("msg", "") or "Erreur inconnue"
                yield sse(chunk)

            if full_answer.strip():
                add_chat_message(
                    chat_id,
                    "assistant",
                    full_answer,
                    model=req.model,
                    meta={"sources": final_sources, "mode": "literature_review"},
                )
            elif saw_error_msg:
                add_chat_message(
                    chat_id,
                    "assistant",
                    f"Erreur: {saw_error_msg}",
                    model=req.model,
                    meta={"error": True, "mode": "literature_review"},
                )
        except InterruptedError:
            return
        except asyncio.CancelledError:
            abort_signal.abort("client_disconnected")
            await stop_local_ollama_model_async(req.model)
            raise
        except Exception:
            _logger.exception("Literature review stream failed")
            yield sse({"type": "error", "msg": "La revue de littérature a échoué."})
        finally:
            if abort_signal.is_aborted():
                await stop_local_ollama_model_async(req.model)
            release_stream_abort_signal(request_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/chat/discover")
async def api_chat_discover(req: ChatDiscoverRequest, *, external_api_allowed: ExternalApiAllowed):
    """Decouvrir des articles externes a partir du contexte d'une conversation."""
    if not req.privacy_ack or not external_api_allowed:
        return external_api_permission_response("Autorisation des requêtes API externes requise.")

    payload = await _run_chat_discovery(
        chat_id=req.chat_id,
        model_name=req.model,
        max_results=req.max_results,
        include_unpaywall=req.include_unpaywall,
        debug=req.debug,
        fallback_to_latest_chat=True,
    )
    if payload.get("ok") is False:
        message = str(payload.get("msg") or "")
        if "contexte" in message.lower():
            status_code = 422
            code = "chat_context_required"
        elif "interrompue" in message.lower():
            status_code = 409
            code = "article_discovery_interrupted"
        else:
            status_code = 502
            code = "article_discovery_failed"
        return api_error(code, status_code=status_code, message=message)
    return payload


@router.post("/api/stream/abort")
async def api_abort_stream(req: AbortStreamRequest):
    reason = normalize_stream_abort_reason(req.reason)
    request_id = str(req.request_id or "").strip()
    if request_id:
        aborted = 1 if abort_stream_request(request_id, reason) else 0
    else:
        aborted = abort_stream_requests_for_model(req.model, reason)
    # A stale request id must never unload a model that may now serve another stream.
    unloaded = (
        await release_model_after_stop_async(req.model, reason)
        if aborted or not request_id
        else False
    )
    _logger.warning(
        "Stream abort requested (reason=%s, request_found=%s, model_unloaded=%s)",
        reason,
        bool(aborted),
        unloaded,
    )
    return {
        "ok": bool(aborted or unloaded),
        "stream_aborted": bool(aborted),
        "aborted_requests": aborted,
        "model_unloaded": unloaded,
        "reason": reason,
    }
