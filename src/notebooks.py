"""
Gestion des carnets - stockage JSON local dans storage/notebooks/.
"""

import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

import src.config as _cfg
from src.analysis_quality import (
    deep_analysis_guidance,
    detailed_summary_guidance,
    is_deep_article_analysis_request,
    is_detailed_article_summary_request,
)
from src.app_settings import get_app_settings
from src.atomic_io import atomic_write_json
from src.file_access import resolve_pdf_path
from src.llm_provider import (
    StreamAbortSignal,
    create_chat_llm,
    is_remote_provider,
    is_tiny_model,
    resolve_chat_think_mode,
    sanitize_assistant_content,
    stream_ollama_chat,
    stream_text,
    strip_thinking_blocks,
    unload_ollama_model,
)
from src.model_profiles import get_chat_response_note
from src.visual_analysis import analyze_pdf_visuals, results_as_markdown

_logger = logging.getLogger(__name__)
_NOTEBOOKS_LOCK = threading.RLock()


def _with_notebook_lock[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    @wraps(func)
    def locked(*args: P.args, **kwargs: P.kwargs) -> R:
        with _NOTEBOOKS_LOCK:
            return func(*args, **kwargs)

    return locked

NOTEBOOKS_DIR = Path(_cfg.STORAGE_DIR) / "notebooks"
NOTEBOOKS_DIR.mkdir(parents=True, exist_ok=True)
_NB_STREAM_FLUSH_CHARS = 8
_NB_STREAM_FLUSH_DELAY = 0.025
_NB_LOCAL_SOURCE_CHAR_LIMIT = 1200
_NB_REMOTE_SOURCE_CHAR_LIMIT = 2200
_NB_LOCAL_TOTAL_CONTEXT_CHAR_LIMIT = 8000
_NB_REMOTE_TOTAL_CONTEXT_CHAR_LIMIT = 16000
_NB_LOCAL_DEEP_SOURCE_CHAR_LIMIT = 10000
_NB_REMOTE_DEEP_SOURCE_CHAR_LIMIT = 20000
_NB_LOCAL_DEEP_TOTAL_CONTEXT_CHAR_LIMIT = 30000
_NB_REMOTE_DEEP_TOTAL_CONTEXT_CHAR_LIMIT = 60000
_NB_LOCAL_HISTORY_MESSAGE_LIMIT = 6
_NB_REMOTE_HISTORY_MESSAGE_LIMIT = 12
_NB_HISTORY_MESSAGE_CHAR_LIMIT = 700
_NB_USER_MESSAGE_CHAR_LIMIT = 2000
_NB_LOCAL_NOTES_CHAR_LIMIT = 2500
_NB_REMOTE_NOTES_CHAR_LIMIT = 6000
_NB_TINY_MODEL_MAX_PREDICT = 96
_NB_TINY_MODEL_DEEP_MAX_PREDICT = 640
_NB_PUBLIC_SOURCE_PREVIEW_CHARS = 500


def _truncate_block(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _fit_source_snippets(
    sources: list[dict],
    *,
    per_source_limit: int,
    total_limit: int,
) -> list[str]:
    snippets: list[str] = []
    used = 0
    for idx, source in enumerate(sources, start=1):
        title = str(source.get("title") or "Source sans titre").strip()
        text_content = str(source.get("content") or "").strip()
        visual_content = str(source.get("visual_content") or "").strip()
        if text_content and visual_content:
            section_overhead = len("[Texte extrait]\n\n[Figures analysées]\n")
            available = max(0, per_source_limit - section_overhead)
            visual_limit = min(len(visual_content), max(320, available // 2))
            text_limit = max(0, available - visual_limit)
            text_excerpt = _truncate_block(text_content, text_limit) if text_limit else ""
            visual_excerpt = _truncate_block(visual_content, visual_limit)
            content = (
                f"[Texte extrait]\n{text_excerpt}\n\n"
                f"[Figures analysées]\n{visual_excerpt}"
            ).strip()
        elif visual_content:
            content = f"[Figures analysées]\n{_truncate_block(visual_content, per_source_limit)}"
        else:
            content = _truncate_block(text_content, per_source_limit)
        if not content:
            continue
        snippet = f"[{idx}] {title}\n{content}"
        snippet_len = len(snippet)
        if snippets and used + snippet_len > total_limit:
            remaining = max(0, total_limit - used - len(f"\n\n[{idx}] {title}\n"))
            if remaining < 180:
                break
            truncated = _truncate_block(content, remaining)
            if not truncated:
                break
            snippet = f"[{idx}] {title}\n{truncated}"
            snippet_len = len(snippet)
        snippets.append(snippet)
        used += snippet_len + (2 if snippets else 0)
        if used >= total_limit:
            break
    return snippets


def _is_meta_notebook_question(message: str) -> bool:
    text = " ".join(str(message or "").strip().lower().split())
    if not text:
        return False
    patterns = [
        "comment tu t'appelles",
        "comment tu t appelles",
        "qui es tu",
        "qui es-tu",
        "c'est quoi ton nom",
        "quel est ton nom",
        "tu es qui",
        "tu peux m'aider",
        "tu peux aider",
        "que peux tu faire",
        "que peux-tu faire",
    ]
    return any(pattern in text for pattern in patterns)


def _is_deep_article_analysis_request(message: str) -> bool:
    return is_deep_article_analysis_request(message)


def _should_append_answer_to_notes(message: str) -> bool:
    from src.chat_harness import requests_notebook_notes_update

    return requests_notebook_notes_update(message)


_NOTE_SECTION_HEADING_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:\*\*)?\s*"
    r"(?:notes?\s+(?:pour|du|de)\s+le\s+carnet|note\s+(?:pour|du|de)\s+le\s+carnet|"
    r"notes?\s*:|note\s*:|a\s+ajouter\s+(?:aux|dans\s+les)\s+notes?)"
    r"\s*:?\s*(?:\*\*)?\s*$"
)


def _clean_note_content_from_answer(assistant_answer: str) -> str:
    text = strip_thinking_blocks(str(assistant_answer or "")).strip()
    if not text:
        return ""

    matches = list(_NOTE_SECTION_HEADING_RE.finditer(text))
    if matches:
        start = matches[-1].end()
        note = text[start:].strip()
    else:
        note = text

    note = re.sub(r"(?im)^\s*[-–—]*\s*note\s+pour\s+le\s+carnet\s*:?\s*", "", note).strip()
    note = re.sub(r"(?im)^\s*[-–—]*\s*notes?\s+du\s+carnet\s*\(ajout\)\s*:?\s*", "", note).strip()
    note = re.sub(r"(?im)^\s*\*?remarque\*?\s*:\s*", "", note).strip()
    return note


@_with_notebook_lock
def _append_chat_answer_to_notes(nb_id: str, user_message: str, assistant_answer: str) -> str | None:
    nb = _read(nb_id)
    if nb is None:
        return None
    note = _clean_note_content_from_answer(assistant_answer)
    if not note:
        return None
    existing_notes = str(nb.get("notes") or "").strip()
    combined = note if not existing_notes else f"{existing_notes}\n\n---\n\n{note}"
    nb["notes"] = combined
    _write(nb)
    return combined


def _notebook_response_style_guide(message: str, model_name: str | None = None) -> str:
    text = " ".join(str(message or "").strip().lower().split())
    deep_analysis = _is_deep_article_analysis_request(message)
    detailed_summary = is_detailed_article_summary_request(message)
    wants_list = any(
        marker in text
        for marker in (
            "liste",
            "list",
            "puces",
            "tableau",
            "compare",
            "comparaison",
            "etapes",
            "steps",
        )
    )
    lines = [
        "Reponds directement en texte naturel, avec un ton clair et utile.",
        "Ne te contente pas d'enumerer les sources ou les faits trouves.",
        "N'utilise pas de sections imposees comme Answer, Explanation ou Response note.",
        "Ajoute une note de prudence seulement si les sources sont insuffisantes, incompletes ou ambigues.",
    ]
    if deep_analysis:
        lines.insert(
            1,
            "Construis une analyse argumentee qui relie probleme, methode, preuves, limites et portee.",
        )
    elif detailed_summary:
        lines.insert(
            1,
            "Fais un resume substantiel du document en t'appuyant sur son corps, ses resultats et sa conclusion.",
        )
    else:
        lines.insert(
            1,
            "Commence par l'idee principale, puis ajoute une breve explication fondee sur les sources les plus utiles.",
        )
    if deep_analysis:
        lines.append(deep_analysis_guidance(language="fr", citation_format="[x]"))
    elif detailed_summary:
        lines.append(detailed_summary_guidance(language="fr", citation_format="[x]"))
    elif not wants_list:
        lines.append("Prefere 1 ou 2 courts paragraphes a une liste a puces.")
    model_note = str(get_chat_response_note(model_name) or "").strip()
    if model_note:
        lines.append(f"Consignes specifiques au modele: {model_note}")
    return " ".join(lines)


def _canonical_notebook_id(nb_id: str) -> str | None:
    value = str(nb_id or "").strip()
    try:
        canonical = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None
    return canonical if value.lower() == canonical else None


def _path(nb_id: str) -> Path | None:
    canonical = _canonical_notebook_id(nb_id)
    if canonical is None:
        return None
    return NOTEBOOKS_DIR / f"{canonical}.json"


def _index_path() -> Path:
    return NOTEBOOKS_DIR / "_index.json"


def _notebook_summary(nb: dict, *, modified_ns: int | None = None) -> dict[str, Any]:
    return {
        "id": str(nb["id"]),
        "title": str(nb.get("title") or ""),
        "created": str(nb.get("created") or ""),
        "source_count": len(nb.get("sources") or []),
        "message_count": len(nb.get("messages") or []),
        "modified_ns": int(modified_ns or time.time_ns()),
    }


def _write_notebook_index(entries: list[dict[str, Any]]) -> None:
    atomic_write_json(_index_path(), {"version": 1, "notebooks": entries})


def _rebuild_notebook_index() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in NOTEBOOKS_DIR.glob("*.json"):
        if path.name == "_index.json":
            continue
        try:
            nb = json.loads(path.read_text(encoding="utf-8"))
            entries.append(_notebook_summary(nb, modified_ns=path.stat().st_mtime_ns))
        except Exception as exc:
            _logger.warning("Skipping unreadable notebook %s: %s", path.name, exc)
    entries.sort(key=lambda entry: int(entry.get("modified_ns") or 0), reverse=True)
    _write_notebook_index(entries)
    return entries


def _load_notebook_index() -> list[dict[str, Any]]:
    try:
        payload = json.loads(_index_path().read_text(encoding="utf-8"))
        entries = payload.get("notebooks")
        if payload.get("version") != 1 or not isinstance(entries, list):
            raise ValueError("invalid notebook index")
        expected_ids = {
            path.stem for path in NOTEBOOKS_DIR.glob("*.json") if path.name != "_index.json"
        }
        indexed_ids = {str(entry.get("id") or "") for entry in entries if isinstance(entry, dict)}
        if expected_ids != indexed_ids:
            raise ValueError("stale notebook index")
        return [dict(entry) for entry in entries if isinstance(entry, dict)]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _rebuild_notebook_index()


def _update_notebook_index(nb: dict, *, modified_ns: int | None = None) -> None:
    entries = _load_notebook_index()
    nb_id = str(nb["id"])
    entries = [entry for entry in entries if str(entry.get("id") or "") != nb_id]
    entries.insert(0, _notebook_summary(nb, modified_ns=modified_ns))
    _write_notebook_index(entries)


@_with_notebook_lock
def _read(nb_id: str) -> dict | None:
    p = _path(nb_id)
    return json.loads(p.read_text(encoding="utf-8")) if p is not None and p.exists() else None


@_with_notebook_lock
def _write(nb: dict) -> None:
    path = _path(nb["id"])
    if path is None:
        raise ValueError("invalid_notebook_id")
    atomic_write_json(path, nb)
    _update_notebook_index(nb, modified_ns=path.stat().st_mtime_ns)


def _preview_text(text: str, limit: int = _NB_PUBLIC_SOURCE_PREVIEW_CHARS) -> str:
    value = str(text or "")
    if limit <= 0 or len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def public_source_payload(source: dict, *, preview_chars: int = _NB_PUBLIC_SOURCE_PREVIEW_CHARS) -> dict:
    """Return the UI-safe source shape without shipping full PDF text."""
    content = str(source.get("content") or "")
    visual_content = str(source.get("visual_content") or "")
    payload = dict(source)
    payload["content"] = _preview_text(content, preview_chars)
    payload["content_preview"] = payload["content"]
    payload["content_length"] = len(content)
    payload["content_truncated"] = len(payload["content"]) < len(content)
    if visual_content:
        payload["visual_content"] = _preview_text(visual_content, preview_chars)
        payload["visual_content_preview"] = payload["visual_content"]
        payload["visual_content_length"] = len(visual_content)
        payload["visual_content_truncated"] = (
            len(payload["visual_content"]) < len(visual_content)
        )
    return payload


def public_notebook_payload(nb: dict, *, preview_chars: int = _NB_PUBLIC_SOURCE_PREVIEW_CHARS) -> dict:
    payload = dict(nb)
    payload["sources"] = [
        public_source_payload(src, preview_chars=preview_chars)
        for src in payload.get("sources", [])
        if isinstance(src, dict)
    ]
    return payload


# -- CRUD carnets -------------------------------------------------------------
@_with_notebook_lock
def list_notebooks() -> list[dict]:
    return [
        {
            key: entry[key]
            for key in ("id", "title", "created", "source_count", "message_count")
        }
        for entry in _load_notebook_index()
    ]


@_with_notebook_lock
def create_notebook(title: str = "Nouveau carnet") -> dict:
    nb = {
        "id": str(uuid.uuid4()),
        "title": title,
        "created": datetime.now().isoformat(),
        "sources": [],
        "notes": "",
        "messages": [],
    }
    _write(nb)
    return nb


@_with_notebook_lock
def get_notebook(nb_id: str) -> dict | None:
    return _read(nb_id)


@_with_notebook_lock
def delete_notebook(nb_id: str) -> bool:
    p = _path(nb_id)
    if p is not None and p.exists():
        p.unlink()
        entries = [
            entry
            for entry in _load_notebook_index()
            if str(entry.get("id") or "") != nb_id
        ]
        _write_notebook_index(entries)
        return True
    return False


@_with_notebook_lock
def update_title(nb_id: str, title: str) -> bool:
    nb = _read(nb_id)
    if nb is None:
        return False
    nb["title"] = title
    _write(nb)
    return True


@_with_notebook_lock
def update_notes(nb_id: str, notes: str) -> bool:
    nb = _read(nb_id)
    if nb is None:
        return False
    nb["notes"] = notes
    _write(nb)
    return True


@_with_notebook_lock
def update_source_collection(
    nb_id: str,
    *,
    provider: str,
    name: str,
    include_descendants: bool = True,
) -> bool:
    """Persist the collection a notebook can be incrementally refreshed from."""
    nb = _read(nb_id)
    if nb is None:
        return False
    nb["source_collection"] = {
        "provider": str(provider or "").strip().lower(),
        "name": str(name or "").strip(),
        "include_descendants": bool(include_descendants),
        "last_synced": datetime.now().isoformat(),
    }
    _write(nb)
    return True


# -- Sources -----------------------------------------------------------------
@_with_notebook_lock
def add_source(
    nb_id: str,
    title: str,
    content: str,
    source_path: str = "",
    *,
    visual_content: str = "",
    visual_model: str = "",
    visual_pages: list[int] | None = None,
) -> dict | None:
    nb = _read(nb_id)
    if nb is None:
        return None
    src: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "title": title,
        "content": content,
        "added": datetime.now().isoformat(),
        "source_path": source_path or "",
    }
    if visual_content:
        src["visual_content"] = visual_content
        src["visual_model"] = visual_model
        src["visual_pages"] = list(visual_pages or [])
    nb["sources"].append(src)
    _write(nb)
    return src


@_with_notebook_lock
def add_sources(nb_id: str, sources: list[dict]) -> list[dict] | None:
    """Ajoute plusieurs sources avec une seule lecture/ecriture du carnet."""
    nb = _read(nb_id)
    if nb is None:
        return None

    added: list[dict] = []
    now = datetime.now().isoformat()
    for source in sources:
        src: dict[str, Any] = {
            "id": str(uuid.uuid4()),
            "title": str(source.get("title") or "Source sans titre"),
            "content": str(source.get("content") or ""),
            "added": now,
            "source_path": str(source.get("source_path") or ""),
        }
        visual_content = str(source.get("visual_content") or "").strip()
        if visual_content:
            src["visual_content"] = visual_content
            src["visual_model"] = str(source.get("visual_model") or "")
            src["visual_pages"] = list(source.get("visual_pages") or [])
        nb["sources"].append(src)
        added.append(src)

    if added:
        _write(nb)
    return added


@_with_notebook_lock
def update_source_visual_analysis(
    nb_id: str,
    source_id: str,
    *,
    visual_content: str,
    visual_model: str,
    visual_pages: list[int],
) -> bool:
    nb = _read(nb_id)
    if nb is None:
        return False
    for source in nb.get("sources", []):
        if not isinstance(source, dict) or source.get("id") != source_id:
            continue
        source["visual_content"] = str(visual_content or "").strip()
        source["visual_model"] = str(visual_model or "").strip()
        source["visual_pages"] = list(visual_pages or [])
        _write(nb)
        return True
    return False


@_with_notebook_lock
def remove_source(nb_id: str, source_id: str) -> bool:
    nb = _read(nb_id)
    if nb is None:
        return False
    nb["sources"] = [s for s in nb["sources"] if s["id"] != source_id]
    _write(nb)
    return True


# -- Messages ----------------------------------------------------------------
@_with_notebook_lock
def add_message(nb_id: str, role: str, content: str) -> None:
    nb = _read(nb_id)
    if nb is None:
        return
    nb["messages"].append(
        {
            "role": role,
            "content": content,
            "ts": datetime.now().isoformat(),
        }
    )
    _write(nb)


@_with_notebook_lock
def clear_messages(nb_id: str) -> bool:
    nb = _read(nb_id)
    if nb is None:
        return False
    nb["messages"] = []
    _write(nb)
    return True


@_with_notebook_lock
def remove_message(nb_id: str, message_index: int) -> bool:
    nb = _read(nb_id)
    if nb is None:
        return False
    messages = nb.get("messages", [])
    if not isinstance(messages, list):
        return False
    try:
        idx = int(message_index)
    except (TypeError, ValueError):
        return False
    if idx < 0 or idx >= len(messages):
        return False
    del messages[idx]
    nb["messages"] = messages
    _write(nb)
    return True


@_with_notebook_lock
def clear_all_notebooks() -> int:
    removed = 0
    NOTEBOOKS_DIR.mkdir(parents=True, exist_ok=True)
    for path in NOTEBOOKS_DIR.glob("*.json"):
        try:
            path.unlink()
            if path.name != "_index.json":
                removed += 1
        except Exception:
            _logger.debug("Recovered from an error in clear_all_notebooks.", exc_info=True)
            continue
    _write_notebook_index([])
    return removed


def _stream_notebook_auto_visual_enrichment(
    nb_id: str,
    nb: dict,
    message: str,
    model_name: str,
    *,
    persist: bool,
    abort_signal: StreamAbortSignal | None,
):
    """Add cached/on-demand figure evidence before building notebook context."""
    try:
        settings = get_app_settings()
    except Exception:
        _logger.debug(
            "Recovered from an error in _stream_notebook_auto_visual_enrichment.", exc_info=True
        )
        return nb
    mode = str(settings.get("visual_analysis_mode") or "off").strip().lower()
    vision_model = str(settings.get("vision_model") or "").strip()
    visual_description_model = str(
        settings.get("visual_description_model") or ""
    ).strip()
    if (
        mode == "off"
        or not (vision_model or visual_description_model)
        or not bool(settings.get("visual_analysis_auto_chat"))
        or _is_meta_notebook_question(message)
    ):
        return nb

    candidates: list[dict] = []
    candidate_sources: list[dict] = []
    for source in nb.get("sources", []):
        if not isinstance(source, dict) or str(source.get("visual_content") or "").strip():
            continue
        rel_path = str(source.get("source_path") or "").strip().replace("\\", "/")
        if not rel_path:
            continue
        candidates.append(
            {
                "rel_path": rel_path,
                "title": str(source.get("title") or Path(rel_path).name).strip(),
                "metadata": {"rel_path": rel_path},
                "snippets": [_truncate_block(str(source.get("content") or ""), 1400)],
            }
        )
        candidate_sources.append(source)
        if len(candidates) >= 3:
            break
    if not candidates:
        return nb

    try:
        from src.rag.query_engine import _decide_auto_visual_analysis, _t

        yield {"type": "status", "msg": _t("visual_judging")}
        decision = _decide_auto_visual_analysis(message, model_name, candidates)
    except Exception:
        _logger.debug(
            "Recovered from an error in _stream_notebook_auto_visual_enrichment.", exc_info=True
        )
        try:
            message_text = _t("visual_unavailable")
        except Exception:
            _logger.debug(
                "Recovered from an error in _stream_notebook_auto_visual_enrichment.", exc_info=True
            )
            message_text = "Analyse visuelle indisponible."
        yield {"type": "status", "msg": message_text}
        return nb
    if not decision.analyze or not 1 <= decision.source_index <= len(candidates):
        yield {"type": "status", "msg": _t("visual_not_needed")}
        return nb
    if abort_signal is not None and abort_signal.is_aborted():
        raise InterruptedError("Generation aborted.")

    selected_index = decision.source_index - 1
    candidate = candidates[selected_index]
    source = candidate_sources[selected_index]
    title = str(candidate.get("title") or candidate.get("rel_path") or "PDF")
    yield {"type": "status", "msg": _t("visual_reading", title=title)}
    full_path = resolve_pdf_path(_cfg.DATA_PATH, str(candidate.get("rel_path") or ""))
    if full_path is None:
        yield {"type": "status", "msg": _t("visual_unavailable")}
        return nb
    try:
        visual_models = {vision_model, visual_description_model} - {""}
        if not is_remote_provider() and model_name not in visual_models:
            unload_ollama_model(model_name, timeout_seconds=2)
        payload = analyze_pdf_visuals(
            str(full_path),
            model=vision_model,
            analysis_model=visual_description_model,
            max_pages=3,
        )
    except Exception:
        _logger.debug(
            "Recovered from an error in _stream_notebook_auto_visual_enrichment.", exc_info=True
        )
        yield {"type": "status", "msg": _t("visual_unavailable")}
        return nb
    results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(results, list) or not results:
        yield {"type": "status", "msg": _t("visual_no_result")}
        return nb

    visual_content = results_as_markdown(payload)
    visual_pages = [
        int(item["page"])
        for item in results
        if isinstance(item, dict) and str(item.get("page") or "").isdigit()
    ]
    source["visual_content"] = visual_content
    visual_model_label = " + ".join(
        model for model in (vision_model, visual_description_model) if model
    )
    source["visual_model"] = visual_model_label
    source["visual_pages"] = visual_pages
    if persist:
        update_source_visual_analysis(
            nb_id,
            str(source.get("id") or ""),
            visual_content=visual_content,
            visual_model=visual_model_label,
            visual_pages=visual_pages,
        )
    yield {"type": "status", "msg": _t("visual_added", count=len(results))}
    return nb


# -- Chat streaming -----------------------------------------------------------
def stream_notebook_chat(
    nb_id: str,
    message: str,
    model_name: str,
    append_to_notes: bool = False,
    persist_history: bool = True,
    read_only: bool = False,
    thinking_enabled: bool = False,
    abort_signal: StreamAbortSignal | None = None,
):
    """Genere une reponse LLM en utilisant les sources du carnet comme contexte."""
    nb = _read(nb_id)
    if nb is None:
        yield {"type": "error", "msg": "Carnet introuvable"}
        return

    nb = yield from _stream_notebook_auto_visual_enrichment(
        nb_id,
        nb,
        message,
        model_name,
        persist=not read_only,
        abort_signal=abort_signal,
    )
    local_provider = not is_remote_provider()
    tiny_local_model = local_provider and is_tiny_model(model_name)
    meta_question = _is_meta_notebook_question(message)
    append_to_notes_requested = (not read_only) and (
        bool(append_to_notes) or _should_append_answer_to_notes(message)
    )
    sources = nb.get("sources", [])
    usable_source_count = sum(
        1
        for source in sources
        if isinstance(source, dict)
        and (
            str(source.get("content") or "").strip()
            or str(source.get("visual_content") or "").strip()
        )
    )
    history_limit = (
        _NB_LOCAL_HISTORY_MESSAGE_LIMIT if local_provider else _NB_REMOTE_HISTORY_MESSAGE_LIMIT
    )
    if tiny_local_model:
        history_limit = min(history_limit, 4)
    if meta_question:
        history_limit = min(history_limit, 2)
    history = nb.get("messages", [])[-history_limit:] if persist_history else []

    source_limit = _NB_LOCAL_SOURCE_CHAR_LIMIT if local_provider else _NB_REMOTE_SOURCE_CHAR_LIMIT
    total_context_limit = (
        _NB_LOCAL_TOTAL_CONTEXT_CHAR_LIMIT
        if local_provider
        else _NB_REMOTE_TOTAL_CONTEXT_CHAR_LIMIT
    )
    deep_analysis_request = _is_deep_article_analysis_request(message)
    detailed_summary_request = is_detailed_article_summary_request(message)
    long_form_request = deep_analysis_request or detailed_summary_request
    if tiny_local_model:
        if long_form_request:
            source_limit = min(_NB_LOCAL_DEEP_SOURCE_CHAR_LIMIT, 3500)
            total_context_limit = min(_NB_LOCAL_DEEP_TOTAL_CONTEXT_CHAR_LIMIT, 6000)
        else:
            source_limit = min(source_limit, 700)
            total_context_limit = min(total_context_limit, 2400)
    if meta_question:
        source_limit = 0
        total_context_limit = 0
    indexed_sources = []
    if source_limit > 0 and total_context_limit > 0:
        if long_form_request and not tiny_local_model:
            source_limit = (
                _NB_LOCAL_DEEP_SOURCE_CHAR_LIMIT
                if local_provider
                else _NB_REMOTE_DEEP_SOURCE_CHAR_LIMIT
            )
            total_context_limit = (
                _NB_LOCAL_DEEP_TOTAL_CONTEXT_CHAR_LIMIT
                if local_provider
                else _NB_REMOTE_DEEP_TOTAL_CONTEXT_CHAR_LIMIT
            )
            if usable_source_count == 1:
                source_limit = total_context_limit
        indexed_sources = _fit_source_snippets(
            sources,
            per_source_limit=source_limit,
            total_limit=total_context_limit,
        )
    sources_context = "\n\n".join(indexed_sources) if indexed_sources else ""
    notes = _truncate_block(
        str(nb.get("notes") or ""),
        _NB_LOCAL_NOTES_CHAR_LIMIT if local_provider else _NB_REMOTE_NOTES_CHAR_LIMIT,
    )
    if meta_question:
        notes = ""
    # Notes are the user's own writing, not a citable document: they stay out of
    # the numbered list so [x] always points at a real source.
    notes_context = f"[Notes du carnet]\n{notes}" if notes else ""
    context = "\n\n".join(part for part in (sources_context, notes_context) if part)
    response_style_guide = _notebook_response_style_guide(message, model_name=model_name)

    system_lines = []
    if context:
        unique_source_hint = ""
        if usable_source_count == 1:
            unique_source_hint = (
                "Si l'utilisateur parle de 'cet article', de 'l'article' ou de 'this article', "
                "traite la source [1] comme l'article demande. "
            )
        context_parts = []
        if sources_context:
            context_parts.append("Voici les sources du carnet (numerotees) :\n\n" + sources_context)
        if notes_context:
            context_parts.append(
                "Voici les notes personnelles de l'utilisateur. Ce ne sont PAS des sources : "
                "elles n'ont pas de numero et ne se citent jamais avec [x].\n\n" + notes_context
            )
        citation_rules = ""
        if sources_context:
            citation_rules += (
                "Quand une information vient d'une source, ajoute une citation inline [x] "
                "correspondant au numero de cette source. "
            )
        if notes_context:
            citation_rules += (
                "Quand une information vient des notes, ne mets aucun numero : signale-le en toutes "
                "lettres, par exemple 'd'apres vos notes' ou '(selon vos notes)'. "
                "Si les notes et les sources divergent, dis-le et privilegie les sources. "
            )
        factual_basis = (
            "les sources numerotees et les notes du carnet"
            if sources_context and notes_context
            else ("les sources numerotees du carnet" if sources_context else "les notes du carnet")
        )
        system_lines.append(
            "Tu es un assistant de recherche.\n\n"
            + "\n\n".join(context_parts)
            + f"\n\n---\nLa seule base factuelle autorisee est : {factual_basis}. "
            + unique_source_hint
            + "N'utilise ni tes connaissances generales ni l'historique de conversation "
            "pour ajouter des faits absents de cette base. "
            + citation_rules
            + "N'ajoute pas de section finale 'References'. "
            "Indique clairement si une information ne s'y trouve pas. "
            "Si la question est simple, reponds en une ou deux phrases maximum. "
            + response_style_guide
        )
    elif meta_question:
        system_lines.append(
            "Tu es Bibliobrain, un assistant de recherche local. "
            "Reponds directement et tres brievement a la question de l'utilisateur. "
            "N'invente pas de contexte, n'explique pas ton raisonnement."
        )
    else:
        answer = (
            "Je ne peux pas encore repondre a cette question, car le carnet ne contient "
            "aucune source exploitable. Ajoutez un article, une source texte ou une collection "
            "Zotero au carnet, puis relancez la question."
        )
        if persist_history:
            add_message(nb_id, "user", message)
            add_message(nb_id, "assistant", answer)
        yield {"type": "token", "text": answer}
        return
    if tiny_local_model:
        if long_form_request:
            system_lines.append(
                "Mode petit modele: conserve la structure longue demandee et traite chaque rubrique "
                "avec les faits disponibles; reste precis et n'ajoute aucun remplissage."
            )
        else:
            system_lines.append(
                "Mode petit modele: reste tres concis, privilegie une reponse simple et stable, "
                "et n'ecris pas plus de trois phrases."
            )
    if append_to_notes_requested and not meta_question:
        system_lines.append(
            "L'utilisateur demande aussi d'ajouter le resultat dans les notes du carnet. "
            "Redige une seule reponse utile et directement reutilisable comme note, sans section separee "
            "'Notes pour le carnet', sans annonce d'ajout et sans recopier la demande. "
            "Le systeme enregistrera automatiquement le contenu pertinent apres generation."
        )

    user_lines = [""]
    chat_history_messages: list[dict[str, str]] = []
    for msg in history:
        role = str(msg.get("role") or "").strip().lower()
        content = _truncate_block(
            strip_thinking_blocks(str(msg.get("content") or "")),
            _NB_HISTORY_MESSAGE_CHAR_LIMIT,
        )
        if not content:
            continue
        label = "Utilisateur" if role == "user" else "Assistant"
        user_lines.append(f"{label} : {content}")
        if role in {"user", "assistant"}:
            chat_history_messages.append({"role": role, "content": content})
    if context:
        reminder = (
            "Rappel: l'historique ci-dessus sert seulement a comprendre la conversation; "
            f"les faits de la reponse doivent venir uniquement de {factual_basis}."
        )
        if notes_context:
            reminder += " Cite les sources avec [x] et signale en toutes lettres ce qui vient des notes."
        user_lines.append(reminder)
    user_lines.append(f"Utilisateur : {message}")
    user_lines.append("Assistant :")

    try:
        full_answer = ""
        system_prompt = "\n".join(system_lines).strip()
        prompt = "\n".join(user_lines)
        local_chat_messages = [{"role": "system", "content": system_prompt}]
        local_chat_messages.extend(chat_history_messages)
        local_chat_messages.append(
            {"role": "user", "content": _truncate_block(message, _NB_USER_MESSAGE_CHAR_LIMIT)}
        )
        if local_provider:
            ollama_options = {}
            think_mode = resolve_chat_think_mode(model_name, thinking_enabled)
            if tiny_local_model:
                ollama_options["num_predict"] = (
                    _NB_TINY_MODEL_DEEP_MAX_PREDICT
                    if long_form_request
                    else _NB_TINY_MODEL_MAX_PREDICT
                )
                if meta_question:
                    ollama_options["num_predict"] = min(_NB_TINY_MODEL_MAX_PREDICT, 48)
                if think_mode is not False:
                    think_mode = None
            opened_thinking = False
            raw_content = ""
            for chunk in stream_ollama_chat(
                model_name,
                local_chat_messages,
                think=think_mode,
                abort_signal=abort_signal,
                options_override=ollama_options,
            ):
                thinking_piece = str(chunk.get("thinking") or "")
                content_piece = str(chunk.get("content") or "")
                if thinking_enabled and thinking_piece:
                    if not opened_thinking:
                        piece = "<think>"
                        full_answer += piece
                        yield {"type": "token", "text": piece}
                        opened_thinking = True
                    full_answer += thinking_piece
                    yield {"type": "token", "text": thinking_piece}
                if content_piece:
                    if thinking_enabled:
                        if opened_thinking:
                            piece = "</think>\n\n"
                            full_answer += piece
                            yield {"type": "token", "text": piece}
                            opened_thinking = False
                        full_answer += content_piece
                        yield {"type": "token", "text": content_piece}
                    else:
                        raw_content += content_piece
                        visible_content = sanitize_assistant_content(
                            raw_content,
                            thinking_enabled=False,
                        )
                        delta = visible_content[len(full_answer) :]
                        if delta:
                            full_answer += delta
                            yield {"type": "token", "text": delta}
            if opened_thinking:
                full_answer += "</think>"
                yield {"type": "token", "text": "</think>"}
        else:
            llm = create_chat_llm(model_name)
            stream_buffer = ""
            raw_content = ""
            last_flush = time.monotonic()
            full_prompt = f"{system_prompt}\n\n{prompt}".strip()
            for text_chunk in stream_text(llm, full_prompt, abort_signal=abort_signal):
                if abort_signal is not None and abort_signal.is_aborted():
                    raise InterruptedError("Generation aborted.")
                raw_content += str(text_chunk or "")
                visible_content = sanitize_assistant_content(
                    raw_content,
                    thinking_enabled=thinking_enabled,
                )
                delta = visible_content[len(full_answer) :]
                if not delta:
                    continue
                full_answer += delta
                stream_buffer += delta
                now = time.monotonic()
                should_flush = (
                    len(stream_buffer) >= _NB_STREAM_FLUSH_CHARS
                    or ("\n" in stream_buffer and len(stream_buffer) >= 48)
                    or (now - last_flush) >= _NB_STREAM_FLUSH_DELAY
                )
                if should_flush:
                    yield {"type": "token", "text": stream_buffer}
                    stream_buffer = ""
                    last_flush = now
            if stream_buffer:
                yield {"type": "token", "text": stream_buffer}
        updated_notes = None
        if append_to_notes_requested:
            updated_notes = _append_chat_answer_to_notes(nb_id, message, full_answer)
        if persist_history:
            add_message(nb_id, "user", message)
            add_message(nb_id, "assistant", full_answer)
        if updated_notes is not None:
            yield {
                "type": "notes_updated",
                "notes": updated_notes,
                "msg": "Notes du carnet mises a jour.",
            }
    except InterruptedError:
        interrupted_answer = (
            full_answer + "\n\n[Reponse interrompue par l'utilisateur]"
            if full_answer.strip()
            else "[Reponse interrompue par l'utilisateur]"
        )
        if persist_history:
            add_message(nb_id, "user", message)
            add_message(nb_id, "assistant", interrupted_answer)
        raise
    except Exception as e:
        _logger.debug("Recovered from an error in stream_notebook_chat.", exc_info=True)
        yield {"type": "error", "msg": str(e)}


def stream_notebook_thematic_synthesis(
    nb_id: str,
    message: str,
    model_name: str,
    persist_history: bool = True,
    read_only: bool = False,
    abort_signal: StreamAbortSignal | None = None,
):
    """Read every source, group its key points into chapters, and save them to the notes."""
    from src.notebook_synthesis import generate_thematic_synthesis

    nb = _read(nb_id)
    if nb is None:
        yield {"type": "error", "msg": "Carnet introuvable"}
        return
    try:
        markdown = yield from generate_thematic_synthesis(
            nb.get("sources", []),
            message,
            model_name,
            abort_signal=abort_signal,
        )
    except InterruptedError:
        if persist_history:
            add_message(nb_id, "user", message)
            add_message(nb_id, "assistant", "[Reponse interrompue par l'utilisateur]")
        raise
    except RuntimeError as exc:
        yield {"type": "error", "msg": str(exc)}
        return
    except Exception:
        _logger.exception("Notebook thematic synthesis failed")
        yield {"type": "error", "msg": "La synthese thematique a echoue."}
        return
    yield {"type": "token", "text": markdown}
    # The chapters are the deliverable: they go to the notes, where they can be
    # edited, unlike chat history which is truncated in later prompts.
    updated_notes = None if read_only else _append_chat_answer_to_notes(nb_id, message, markdown)
    if persist_history:
        add_message(nb_id, "user", message)
        add_message(nb_id, "assistant", markdown)
    if updated_notes is not None:
        yield {
            "type": "notes_updated",
            "notes": updated_notes,
            "msg": "Synthese ajoutee aux notes du carnet.",
        }
