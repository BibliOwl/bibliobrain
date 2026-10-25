from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from src import config
from src.atomic_io import atomic_write_json
from src.llm_provider import (
    get_generation_settings,
    get_ollama_context_length,
    set_generation_settings,
    set_ollama_context_length,
)

_DEFAULT_THRESHOLD = 0.30
_DEFAULT_MAX_CHUNKS = 6
_DEFAULT_THINKING = False
_DEFAULT_ALLOW_EXTERNAL_API_REQUESTS = False
_DEFAULT_RESEARCH_SCOPE_ENABLED = True
_DEFAULT_AUTO_ENRICH_PDF_METADATA = False
_DEFAULT_DISCOVERY_QUERY_PLANNER = False
_DEFAULT_DISCOVERY_DENSE_ENABLED = True
# Zotero keeps a PDF it found no record for as a standalone attachment; it is
# often a paper all the same, so it is indexed unless the user opts out.
_DEFAULT_INDEX_STANDALONE_PDFS = True
_DEFAULT_DISCOVERY_CANDIDATE_MULTIPLIER = 4
_DEFAULT_DISCOVERY_LLM_VALIDATION_MULTIPLIER = 3
_DEFAULT_DEBUG_METRICS = False
_DEFAULT_DEBUG_FORCE_THINKING_SMALL_MODELS = False
_DEFAULT_FIRST_RAG_INIT_DONE = False
_DEFAULT_ONBOARDING_DONE_V1 = False
_DEFAULT_FEATURE_TOUR_DONE_V1 = False
# First-run tutorial: per-tab tips and the "Premiers pas" checklist.
_ONBOARDING_TIPS = ("library", "discovery", "drafting", "connections", "notebooks")
_ONBOARDING_MILESTONES = (
    "question-asked",
    "source-opened",
    "citation-inserted",
    "notebook-created",
)
_DEFAULT_RERANKER_ENABLED = True
_DEFAULT_RERANKER_PROFILE = "auto"
_DEFAULT_RERANKER_TRANSLATE_FRENCH = True
_DEFAULT_SELECTED_MODEL = ""
_DEFAULT_VISUAL_ANALYSIS_MODE = "off"
_DEFAULT_VISION_MODEL = ""
_DEFAULT_VISUAL_DESCRIPTION_MODEL = ""
_DEFAULT_VISUAL_ANALYSIS_AUTO_CHAT = False
_SUPPORTED_VISUAL_ANALYSIS_MODES = {"off", "on_demand", "index"}
_SUPPORTED_METADATA_SOURCES = {"none", "bibtex", "zotero"}
_SUPPORTED_UI_LANGUAGES = {"fr", "en"}
_SUPPORTED_RERANKER_PROFILES = {
    "auto",
    "qwen_gpu",
    "ettin_17m_cpu",
    "ettin_cpu",
    "ettin_68m_cpu",
    "ettin_150m_cpu",
    "ettin_400m_gpu",
    "ettin_1b_gpu",
    "gte_modernbert_cpu",
    "bge_base_gpu",
    "bge_v2_m3_gpu",
}
_APP_SETTINGS_LOCK = threading.RLock()
_APP_SETTINGS_CACHE_KEY: tuple[str, int, int] | None = None
_APP_SETTINGS_CACHE: dict[str, Any] = {}


def _app_settings_path() -> Path:
    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    return Path(config.STORAGE_DIR) / "app_settings.json"


def _load_app_settings_payload() -> dict[str, Any]:
    global _APP_SETTINGS_CACHE, _APP_SETTINGS_CACHE_KEY

    with _APP_SETTINGS_LOCK:
        path = _app_settings_path()
        if not path.exists():
            _APP_SETTINGS_CACHE_KEY = None
            _APP_SETTINGS_CACHE = {}
            return {}
        try:
            stat = path.stat()
            cache_key = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
            if cache_key == _APP_SETTINGS_CACHE_KEY:
                return dict(_APP_SETTINGS_CACHE)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, json.JSONDecodeError):
            return {}
        normalized = payload if isinstance(payload, dict) else {}
        _APP_SETTINGS_CACHE_KEY = cache_key
        _APP_SETTINGS_CACHE = dict(normalized)
        return dict(normalized)


def _save_app_settings_payload(payload: dict[str, Any]) -> dict[str, Any]:
    global _APP_SETTINGS_CACHE, _APP_SETTINGS_CACHE_KEY

    with _APP_SETTINGS_LOCK:
        path = _app_settings_path()
        current = _load_app_settings_payload()
        current.update(payload or {})
        atomic_write_json(path, current)
        stat = path.stat()
        _APP_SETTINGS_CACHE_KEY = (str(path), int(stat.st_mtime_ns), int(stat.st_size))
        _APP_SETTINGS_CACHE = dict(current)
        return current


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_threshold(value: Any, default: float = _DEFAULT_THRESHOLD) -> float:
    try:
        threshold = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, min(1.0, threshold))


def _normalize_max_chunks(value: Any, default: int = _DEFAULT_MAX_CHUNKS) -> int:
    try:
        max_chunks = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(1, min(20, max_chunks))


def _normalize_discovery_multiplier(value: Any, default: int) -> int:
    try:
        multiplier = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(1, min(8, multiplier))


def _normalize_text(value: Any, default: str = "") -> str:
    return str(value if value is not None else default).strip()


def _normalize_ui_language(value: Any, default: str = "") -> str:
    language = str(value if value is not None else default).strip().lower()
    return language if language in _SUPPORTED_UI_LANGUAGES else ""


def _normalize_visual_analysis_mode(value: Any, default: str = "off") -> str:
    mode = str(value if value is not None else default).strip().lower()
    return mode if mode in _SUPPORTED_VISUAL_ANALYSIS_MODES else _DEFAULT_VISUAL_ANALYSIS_MODE


def _normalize_onboarding_progress(value: Any, *, legacy_tour_done: bool = False) -> dict[str, Any]:
    """Keep only known tips/milestones so the frontend cannot grow this blob.

    Installs that finished the old six-tab tour predate the tutorial: they
    already know the app, so tips and the checklist start out dismissed.
    """
    if not isinstance(value, dict):
        return {
            "tutorial_done": legacy_tour_done,
            "tips_seen": list(_ONBOARDING_TIPS) if legacy_tour_done else [],
            "milestones": [],
            "checklist_hidden": legacy_tour_done,
        }

    def _known(items: Any, allowed: tuple[str, ...]) -> list[str]:
        values = {str(item).strip() for item in items} if isinstance(items, list) else set()
        return [item for item in allowed if item in values]

    return {
        "tutorial_done": _normalize_bool(value.get("tutorial_done"), False),
        "tips_seen": _known(value.get("tips_seen"), _ONBOARDING_TIPS),
        "milestones": _known(value.get("milestones"), _ONBOARDING_MILESTONES),
        "checklist_hidden": _normalize_bool(value.get("checklist_hidden"), False),
    }


def _normalize_optional_bool(value: Any) -> bool | None:
    """Tri-state consent: ``None`` until the user answered."""
    return None if value is None else _normalize_bool(value)


def _normalize_reranker_profile(value: Any, default: str = "auto") -> str:
    profile = str(value if value is not None else default).strip().lower()
    return profile if profile in _SUPPORTED_RERANKER_PROFILES else _DEFAULT_RERANKER_PROFILE


def _infer_metadata_source(path: Any) -> str:
    normalized = _normalize_text(path).lower()
    if not normalized:
        return "none"
    # A .json file is a records snapshot from an index import: a file source too.
    return "bibtex" if normalized.endswith((".bib", ".bibtex", ".txt", ".json")) else "zotero"


def _normalize_metadata_source(value: Any, path: Any = "") -> str:
    source = _normalize_text(value).lower()
    return source if source in _SUPPORTED_METADATA_SOURCES else _infer_metadata_source(path)


def _data_path_configured(persisted: Any) -> bool:
    """False while the PDF folder is only Bibliobrain's own, still empty default."""

    if _normalize_text(persisted) or str(os.getenv("DATA_PATH", "") or "").strip():
        return True
    try:
        with os.scandir(config.DATA_PATH) as entries:
            return any(True for _ in entries)
    except OSError:
        return False


def get_app_settings() -> dict[str, Any]:
    payload = _load_app_settings_payload()
    zotero_path = _normalize_text(payload.get("zotero_path"), config.ZOTERO_DB_PATH or "")
    metadata_source = _normalize_metadata_source(
        payload.get("metadata_source"),
        zotero_path,
    )
    allow_external_api_requests = _normalize_bool(
        payload.get("allow_external_api_requests", payload.get("allow_external_discovery")),
        _DEFAULT_ALLOW_EXTERNAL_API_REQUESTS,
    )
    return {
        "llm_thinking": _normalize_bool(payload.get("llm_thinking"), _DEFAULT_THINKING),
        "threshold": _normalize_threshold(payload.get("threshold"), _DEFAULT_THRESHOLD),
        "max_chunks": _normalize_max_chunks(payload.get("max_chunks"), _DEFAULT_MAX_CHUNKS),
        "zotero_path": zotero_path,
        "metadata_source": metadata_source,
        "data_path": _normalize_text(payload.get("data_path"), config.DATA_PATH or ""),
        "data_path_configured": _data_path_configured(payload.get("data_path")),
        "auto_enrich_pdf_metadata": (
            metadata_source != "zotero"
            and _normalize_bool(
                payload.get("auto_enrich_pdf_metadata"),
                _DEFAULT_AUTO_ENRICH_PDF_METADATA,
            )
        ),
        "allow_external_api_requests": allow_external_api_requests,
        # Legacy alias kept for old frontend/backend callers.
        "allow_external_discovery": allow_external_api_requests,
        "research_scope_enabled": _normalize_bool(
            payload.get("research_scope_enabled"),
            _DEFAULT_RESEARCH_SCOPE_ENABLED,
        ),
        "discovery_query_planner": _normalize_bool(
            payload.get("discovery_query_planner"),
            _DEFAULT_DISCOVERY_QUERY_PLANNER,
        ),
        "discovery_dense_enabled": _normalize_bool(
            payload.get("discovery_dense_enabled"),
            _DEFAULT_DISCOVERY_DENSE_ENABLED,
        ),
        "index_standalone_pdfs": _normalize_bool(
            payload.get("index_standalone_pdfs"),
            _DEFAULT_INDEX_STANDALONE_PDFS,
        ),
        "discovery_candidate_multiplier": _normalize_discovery_multiplier(
            payload.get("discovery_candidate_multiplier"),
            _DEFAULT_DISCOVERY_CANDIDATE_MULTIPLIER,
        ),
        "discovery_llm_validation_multiplier": _normalize_discovery_multiplier(
            payload.get("discovery_llm_validation_multiplier"),
            _DEFAULT_DISCOVERY_LLM_VALIDATION_MULTIPLIER,
        ),
        "unpaywall_email": _normalize_text(payload.get("unpaywall_email"), ""),
        "debug_metrics": _normalize_bool(payload.get("debug_metrics"), _DEFAULT_DEBUG_METRICS),
        "debug_force_thinking_small_models": _normalize_bool(
            payload.get("debug_force_thinking_small_models"),
            _DEFAULT_DEBUG_FORCE_THINKING_SMALL_MODELS,
        ),
        "first_rag_init_done": _normalize_bool(
            payload.get("first_rag_init_done"),
            _DEFAULT_FIRST_RAG_INIT_DONE,
        ),
        "onboarding_done_v1": _normalize_bool(
            payload.get("onboarding_done_v1"),
            _DEFAULT_ONBOARDING_DONE_V1,
        ),
        "feature_tour_done_v1": _normalize_bool(
            payload.get("feature_tour_done_v1"),
            _DEFAULT_FEATURE_TOUR_DONE_V1,
        ),
        "onboarding_progress": _normalize_onboarding_progress(
            payload.get("onboarding_progress"),
            legacy_tour_done=_normalize_bool(
                payload.get("feature_tour_done_v1"),
                _DEFAULT_FEATURE_TOUR_DONE_V1,
            ),
        ),
        "reranker_enabled": _normalize_bool(
            payload.get("reranker_enabled"),
            _DEFAULT_RERANKER_ENABLED,
        ),
        "reranker_profile": _normalize_reranker_profile(
            payload.get("reranker_profile"),
            _DEFAULT_RERANKER_PROFILE,
        ),
        "reranker_translate_french": _normalize_bool(
            payload.get("reranker_translate_french"),
            _DEFAULT_RERANKER_TRANSLATE_FRENCH,
        ),
        "selected_model": _normalize_text(
            payload.get("selected_model"),
            _DEFAULT_SELECTED_MODEL,
        ),
        "visual_analysis_mode": _normalize_visual_analysis_mode(
            payload.get("visual_analysis_mode"),
            _DEFAULT_VISUAL_ANALYSIS_MODE,
        ),
        "vision_model": _normalize_text(payload.get("vision_model"), _DEFAULT_VISION_MODEL),
        "visual_description_model": _normalize_text(
            payload.get("visual_description_model"),
            _DEFAULT_VISUAL_DESCRIPTION_MODEL,
        ),
        "visual_analysis_auto_chat": _normalize_bool(
            payload.get("visual_analysis_auto_chat"),
            _DEFAULT_VISUAL_ANALYSIS_AUTO_CHAT,
        ),
        "ui_language": _normalize_ui_language(payload.get("ui_language"), ""),
        # None = not asked yet: src/update_check.py sends nothing until True.
        "update_check_enabled": _normalize_optional_bool(payload.get("update_check_enabled")),
        "ollama_context_length": get_ollama_context_length(),
        **get_generation_settings(),
    }


def set_app_settings(
    *,
    llm_thinking: Any | None = None,
    threshold: Any | None = None,
    max_chunks: Any | None = None,
    zotero_path: Any | None = None,
    metadata_source: Any | None = None,
    data_path: Any | None = None,
    auto_enrich_pdf_metadata: Any | None = None,
    allow_external_api_requests: Any | None = None,
    allow_external_discovery: Any | None = None,
    research_scope_enabled: Any | None = None,
    discovery_query_planner: Any | None = None,
    discovery_dense_enabled: Any | None = None,
    index_standalone_pdfs: Any | None = None,
    discovery_candidate_multiplier: Any | None = None,
    discovery_llm_validation_multiplier: Any | None = None,
    unpaywall_email: Any | None = None,
    debug_metrics: Any | None = None,
    debug_force_thinking_small_models: Any | None = None,
    first_rag_init_done: Any | None = None,
    onboarding_done_v1: Any | None = None,
    feature_tour_done_v1: Any | None = None,
    onboarding_progress: Any | None = None,
    reranker_enabled: Any | None = None,
    reranker_profile: Any | None = None,
    reranker_translate_french: Any | None = None,
    selected_model: Any | None = None,
    visual_analysis_mode: Any | None = None,
    vision_model: Any | None = None,
    visual_description_model: Any | None = None,
    visual_analysis_auto_chat: Any | None = None,
    ui_language: Any | None = None,
    update_check_enabled: Any | None = None,
    temperature: Any | None = None,
    repeat_penalty: Any | None = None,
    top_p: Any | None = None,
    ollama_context_length: Any | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if llm_thinking is not None:
        payload["llm_thinking"] = _normalize_bool(llm_thinking, _DEFAULT_THINKING)
    if threshold is not None:
        payload["threshold"] = _normalize_threshold(threshold, _DEFAULT_THRESHOLD)
    if max_chunks is not None:
        payload["max_chunks"] = _normalize_max_chunks(max_chunks, _DEFAULT_MAX_CHUNKS)
    if zotero_path is not None:
        payload["zotero_path"] = _normalize_text(zotero_path, "")
    if metadata_source is not None:
        metadata_path = (
            payload.get("zotero_path")
            if "zotero_path" in payload
            else _load_app_settings_payload().get("zotero_path", config.ZOTERO_DB_PATH or "")
        )
        payload["metadata_source"] = _normalize_metadata_source(
            metadata_source,
            metadata_path,
        )
    if data_path is not None:
        payload["data_path"] = _normalize_text(data_path, config.DATA_PATH or "")
    current_payload = _load_app_settings_payload()
    effective_metadata_source = payload.get("metadata_source") or _normalize_metadata_source(
        current_payload.get("metadata_source"),
        payload.get("zotero_path", current_payload.get("zotero_path", config.ZOTERO_DB_PATH or "")),
    )
    if auto_enrich_pdf_metadata is not None:
        payload["auto_enrich_pdf_metadata"] = _normalize_bool(
            auto_enrich_pdf_metadata,
            _DEFAULT_AUTO_ENRICH_PDF_METADATA,
        )
    if effective_metadata_source == "zotero":
        payload["auto_enrich_pdf_metadata"] = False
    external_api_value = (
        allow_external_api_requests
        if allow_external_api_requests is not None
        else allow_external_discovery
    )
    if external_api_value is not None:
        normalized_external_api = _normalize_bool(
            external_api_value,
            _DEFAULT_ALLOW_EXTERNAL_API_REQUESTS,
        )
        payload["allow_external_api_requests"] = normalized_external_api
        # Legacy mirror for compatibility with older saved-state readers.
        payload["allow_external_discovery"] = normalized_external_api
    if research_scope_enabled is not None:
        payload["research_scope_enabled"] = _normalize_bool(
            research_scope_enabled,
            _DEFAULT_RESEARCH_SCOPE_ENABLED,
        )
    if discovery_query_planner is not None:
        payload["discovery_query_planner"] = _normalize_bool(
            discovery_query_planner,
            _DEFAULT_DISCOVERY_QUERY_PLANNER,
        )
    if discovery_dense_enabled is not None:
        payload["discovery_dense_enabled"] = _normalize_bool(
            discovery_dense_enabled,
            _DEFAULT_DISCOVERY_DENSE_ENABLED,
        )
    if index_standalone_pdfs is not None:
        payload["index_standalone_pdfs"] = _normalize_bool(
            index_standalone_pdfs,
            _DEFAULT_INDEX_STANDALONE_PDFS,
        )
    if discovery_candidate_multiplier is not None:
        payload["discovery_candidate_multiplier"] = _normalize_discovery_multiplier(
            discovery_candidate_multiplier,
            _DEFAULT_DISCOVERY_CANDIDATE_MULTIPLIER,
        )
    if discovery_llm_validation_multiplier is not None:
        payload["discovery_llm_validation_multiplier"] = _normalize_discovery_multiplier(
            discovery_llm_validation_multiplier,
            _DEFAULT_DISCOVERY_LLM_VALIDATION_MULTIPLIER,
        )
    if unpaywall_email is not None:
        payload["unpaywall_email"] = _normalize_text(unpaywall_email, "")
    if debug_metrics is not None:
        payload["debug_metrics"] = _normalize_bool(debug_metrics, _DEFAULT_DEBUG_METRICS)
    if debug_force_thinking_small_models is not None:
        payload["debug_force_thinking_small_models"] = _normalize_bool(
            debug_force_thinking_small_models,
            _DEFAULT_DEBUG_FORCE_THINKING_SMALL_MODELS,
        )
    if first_rag_init_done is not None:
        payload["first_rag_init_done"] = _normalize_bool(
            first_rag_init_done,
            _DEFAULT_FIRST_RAG_INIT_DONE,
        )
    if onboarding_done_v1 is not None:
        payload["onboarding_done_v1"] = _normalize_bool(
            onboarding_done_v1,
            _DEFAULT_ONBOARDING_DONE_V1,
        )
    if feature_tour_done_v1 is not None:
        payload["feature_tour_done_v1"] = _normalize_bool(
            feature_tour_done_v1,
            _DEFAULT_FEATURE_TOUR_DONE_V1,
        )
    if onboarding_progress is not None:
        payload["onboarding_progress"] = _normalize_onboarding_progress(onboarding_progress)
    if reranker_enabled is not None:
        payload["reranker_enabled"] = _normalize_bool(
            reranker_enabled,
            _DEFAULT_RERANKER_ENABLED,
        )
    if reranker_profile is not None:
        payload["reranker_profile"] = _normalize_reranker_profile(
            reranker_profile,
            _DEFAULT_RERANKER_PROFILE,
        )
    if reranker_translate_french is not None:
        payload["reranker_translate_french"] = _normalize_bool(
            reranker_translate_french,
            _DEFAULT_RERANKER_TRANSLATE_FRENCH,
        )
    if selected_model is not None:
        payload["selected_model"] = _normalize_text(
            selected_model,
            _DEFAULT_SELECTED_MODEL,
        )
    if visual_analysis_mode is not None:
        payload["visual_analysis_mode"] = _normalize_visual_analysis_mode(
            visual_analysis_mode,
            _DEFAULT_VISUAL_ANALYSIS_MODE,
        )
    if vision_model is not None:
        payload["vision_model"] = _normalize_text(vision_model, _DEFAULT_VISION_MODEL)
    if visual_description_model is not None:
        payload["visual_description_model"] = _normalize_text(
            visual_description_model,
            _DEFAULT_VISUAL_DESCRIPTION_MODEL,
        )
    if visual_analysis_auto_chat is not None:
        payload["visual_analysis_auto_chat"] = _normalize_bool(
            visual_analysis_auto_chat,
            _DEFAULT_VISUAL_ANALYSIS_AUTO_CHAT,
        )
    if ui_language is not None:
        payload["ui_language"] = _normalize_ui_language(ui_language, "")
    if update_check_enabled is not None:
        payload["update_check_enabled"] = _normalize_bool(update_check_enabled)
    if temperature is not None or repeat_penalty is not None or top_p is not None:
        set_generation_settings(
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            top_p=top_p,
        )
    if ollama_context_length is not None:
        set_ollama_context_length(ollama_context_length)
    _save_app_settings_payload(payload)
    return get_app_settings()


def index_standalone_pdfs_enabled() -> bool:
    """Whether Zotero PDFs without a parent record are indexed too."""
    try:
        return bool(get_app_settings().get("index_standalone_pdfs", _DEFAULT_INDEX_STANDALONE_PDFS))
    except (OSError, TypeError, ValueError):
        return _DEFAULT_INDEX_STANDALONE_PDFS
