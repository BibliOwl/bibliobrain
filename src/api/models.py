"""Models HTTP routes and feature-specific helpers."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src import config
from src.api import application
from src.api.common import api_error
from src.api_models import (
    SetDocumentToolsSourceRequest,
    SetEmbeddingModelRequest,
    SetEmbeddingSourceRequest,
    SetOllamaUrlRequest,
    SetProviderRequest,
    VisionModelInstallRequest,
    VisualAnalysisRequest,
)
from src.app_settings import get_app_settings
from src.file_access import resolve_pdf_path
from src.llm_provider import (
    can_use_remote,
    current_embedding_profile_label,
    disable_runtime_remote_embeddings,
    embedding_base_url,
    ensure_internal_embedding_model,
    get_embedding_provider_mode,
    is_lmstudio_provider,
    is_remote_provider,
    list_local_lmstudio_models,
    list_local_ollama_models,
    list_remote_embedding_models,
    public_provider_info,
    recommend_local_embedding_model,
    required_embedding_model_name,
    requires_local_ollama_embeddings,
    set_runtime_document_tools_source,
    set_runtime_embedding_model,
    set_runtime_embedding_source,
    set_runtime_lmstudio_base_url,
    set_runtime_mode,
    set_runtime_ollama_base_url,
    validate_local_embedding_model,
    validate_remote_embedding_model,
)
from src.ollama_utils import (
    check_ollama_health,
    fetch_gpu_status,
    fetch_ollama_models,
    fetch_ollama_runtime_status,
)
from src.rag_settings import get_persisted_chunk_size
from src.version import get_app_version
from src.visual_analysis import (
    VisualAnalysisError,
    analyze_pdf_visuals,
    get_cached_pdf_visuals,
    install_recommended_vision_model,
    list_installed_vision_models,
    results_as_markdown,
)

_logger = logging.getLogger(__name__)

router = APIRouter()


def _current_embedding_health(url: str) -> dict:
    provider_mode = get_embedding_provider_mode()
    required_embedding_model = required_embedding_model_name()
    if provider_mode == "remote":
        return {
            "required": False,
            "ok": True,
            "provider": "remote",
            "embedding_ok": True,
            "embedding_model": required_embedding_model,
            "msg": "Embeddings distants actifs cote serveur.",
        }
    ok, resolved, msg = validate_local_embedding_model(
        required_embedding_model,
        url,
        probe_timeout_seconds=3,
    )
    provider_label = "LM Studio" if provider_mode == "lmstudio" else "Ollama"
    if not required_embedding_model:
        return {
            "required": True,
            "ok": False,
            "provider": provider_mode,
            "embedding_ok": False,
            "embedding_model": "",
            "msg": f"Aucun modele d'embedding configure pour {provider_label}.",
        }
    if ok:
        success_msg = (
            f"{provider_label} OK + embedding {resolved or required_embedding_model} detecte."
        )
        return {
            "required": True,
            "ok": True,
            "provider": provider_mode,
            "embedding_ok": True,
            "embedding_model": resolved or required_embedding_model,
            "msg": msg or success_msg,
        }
    return {
        "required": True,
        "ok": False,
        "provider": provider_mode,
        "embedding_ok": False,
        "embedding_model": resolved or required_embedding_model,
        "msg": msg or f"Verification embeddings {provider_label} impossible.",
    }


async def _reuse_equivalent_index(embedding_model: str) -> None:
    """Same model under another name or service: point it at the existing index."""
    try:
        await asyncio.to_thread(
            application.adopt_equivalent_vector_index,
            chunk_size=get_persisted_chunk_size(),
            embedding_model=current_embedding_profile_label(embedding_model),
        )
    except Exception:
        # A failed check only means the index is built as before.
        _logger.warning("Equivalent index check failed", exc_info=True)


def _configured_ollama_request_url(
    requested_url: str | None,
    *,
    provider: str = "ollama",
) -> str:
    """Accept diagnostics only for a server-configured local model endpoint."""
    info = public_provider_info()
    url_key = "lmstudio_base_url" if provider == "lmstudio" else "ollama_base_url"
    fallback = "http://127.0.0.1:1234" if provider == "lmstudio" else "http://localhost:11434"
    configured = str(info.get(url_key) or fallback).strip().rstrip("/")
    requested = str(requested_url or configured).strip().rstrip("/")
    if requested != configured:
        raise HTTPException(
            status_code=400,
            detail="Configurez d'abord cette URL Ollama dans les parametres.",
        )
    return configured


def _configured_embedding_request_url(requested_url: str | None, *, provider: str) -> str:
    """Accept diagnostics only for the service that computes the index."""
    if provider == "lmstudio":
        return _configured_ollama_request_url(requested_url, provider="lmstudio")
    configured = embedding_base_url().strip().rstrip("/")
    requested = str(requested_url or configured).strip().rstrip("/")
    if requested != configured:
        raise HTTPException(
            status_code=400,
            detail="Configurez d'abord cette URL Ollama dans les parametres.",
        )
    return configured


@router.get("/api/models")
async def api_models(url: str | None = None, refresh: bool = False):
    if is_remote_provider():
        chat_url = None
    else:
        chat_provider = "lmstudio" if is_lmstudio_provider() else "ollama"
        chat_url = await asyncio.to_thread(
            _configured_ollama_request_url,
            url,
            provider=chat_provider,
        )

    # The index has its own service: its health never depends on the chat address.
    embedding_url = "" if get_embedding_provider_mode() == "remote" else embedding_base_url()

    model_result, health = await asyncio.gather(
        asyncio.to_thread(
            fetch_ollama_models,
            chat_url,
            force_refresh=refresh,
        ),
        asyncio.to_thread(_current_embedding_health, embedding_url),
    )
    models, status = model_result
    return {
        "models": models,
        "status": status,
        "health": health,
    }


@router.get("/api/provider-info")
async def api_provider_info():
    return public_provider_info()


@router.get("/api/capabilities")
async def api_capabilities():
    return {
        "version": get_app_version(),
        "rag_connections": True,
        "rag_retrieval_graph": True,
    }


@router.get("/api/ollama-local-models")
def api_ollama_local_models(url: str | None = None):
    provider_mode = get_embedding_provider_mode()
    if provider_mode == "remote":
        models: list[str] = []
        status = "Embeddings distants actifs cote serveur."
    else:
        configured_url = _configured_embedding_request_url(url, provider=provider_mode)
        if provider_mode == "lmstudio":
            models, status = list_local_lmstudio_models(
                configured_url,
                embeddings_only=True,
            )
        else:
            models, status = list_local_ollama_models(
                configured_url,
                embeddings_only=True,
            )
    return {
        "models": models,
        "status": status,
        "provider": provider_mode,
        "recommended_embedding_model": recommend_local_embedding_model(models),
        "required_embedding_model": required_embedding_model_name(),
    }


@router.get("/api/remote-embedding-models")
def api_remote_embedding_models():
    if not is_remote_provider():
        return {
            "models": [],
            "status": "Le provider distant doit etre actif pour utiliser ses embeddings.",
            "provider": "remote",
            "required_embedding_model": "",
        }
    models, status = list_remote_embedding_models()
    return {
        "models": models,
        "status": status,
        "provider": "remote",
        "required_embedding_model": (
            required_embedding_model_name() if get_embedding_provider_mode() == "remote" else ""
        ),
    }


@router.get("/api/vision-models")
async def api_vision_models():
    return await asyncio.to_thread(list_installed_vision_models)


@router.post("/api/vision-models/install")
async def api_install_vision_model(req: VisionModelInstallRequest):
    try:
        return await asyncio.to_thread(
            install_recommended_vision_model,
            req.model,
            confirmed=req.confirmed,
        )
    except VisualAnalysisError as exc:
        code = str(exc)
        status = 400 if code in {"confirmation_required", "model_not_allowed"} else 503
        return JSONResponse(status_code=status, content={"ok": False, "error": code})


@router.post("/api/visual-analysis")
async def api_visual_analysis(req: VisualAnalysisRequest):
    settings = get_app_settings()
    mode = str(settings.get("visual_analysis_mode") or "off").strip().lower()
    if mode == "off":
        return JSONResponse(
            status_code=409,
            content={"ok": False, "error": "visual_analysis_disabled"},
        )
    model = str(req.model or settings.get("vision_model") or "").strip()
    analysis_model = str(
        req.analysis_model or settings.get("visual_description_model") or ""
    ).strip()
    if not model and not analysis_model:
        return JSONResponse(
            status_code=409,
            content={"ok": False, "error": "vision_model_required"},
        )
    full = resolve_pdf_path(config.DATA_PATH, req.path)
    if full is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "pdf_not_found"})
    try:
        payload = await asyncio.to_thread(
            analyze_pdf_visuals,
            str(full),
            model=model,
            analysis_model=analysis_model,
        )
    except Exception as exc:
        _logger.debug("Recovered from an error in api_visual_analysis.", exc_info=True)
        code = str(exc) if isinstance(exc, VisualAnalysisError) else "visual_analysis_failed"
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": code},
        )
    return {**payload, "markdown": results_as_markdown(payload)}


@router.get("/api/visual-analysis-cached")
async def api_visual_analysis_cached(
    path: str,
    model: str | None = None,
    analysis_model: str | None = None,
):
    settings = get_app_settings()
    selected_model = str(model or settings.get("vision_model") or "").strip()
    selected_analysis_model = str(
        analysis_model or settings.get("visual_description_model") or ""
    ).strip()
    if not selected_model and not selected_analysis_model:
        return {"ok": True, "found": False, "markdown": ""}
    full = resolve_pdf_path(config.DATA_PATH, path)
    if full is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "pdf_not_found"})
    try:
        payload = await asyncio.to_thread(
            get_cached_pdf_visuals,
            str(full),
            model=selected_model,
            analysis_model=selected_analysis_model,
        )
    except Exception as exc:
        _logger.debug("Recovered from an error in api_visual_analysis_cached.", exc_info=True)
        code = str(exc) if isinstance(exc, VisualAnalysisError) else "visual_analysis_cache_failed"
        return JSONResponse(status_code=503, content={"ok": False, "error": code})
    if payload is None:
        return {"ok": True, "found": False, "markdown": ""}
    return {
        **payload,
        "ok": True,
        "found": True,
        "markdown": results_as_markdown(payload),
    }


@router.post("/api/set-ollama-url")
async def api_set_ollama_url(req: SetOllamaUrlRequest):
    provider = (req.provider or "").strip().lower()
    use_lmstudio = provider == "lmstudio" or is_lmstudio_provider()
    info_before = public_provider_info()
    current_url = (
        str(info_before.get("lmstudio_base_url" if use_lmstudio else "ollama_base_url") or "")
        .strip()
        .rstrip("/")
    )
    try:
        if use_lmstudio:
            url = set_runtime_lmstudio_base_url(req.url)
        else:
            url = set_runtime_ollama_base_url(req.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    changed = current_url != str(url or "").strip().rstrip("/")
    if changed:
        application.clear_runtime_state()
    return {
        "ok": True,
        "changed": changed,
        "ollama_base_url": url,
        "url": url,
        "info": public_provider_info(),
    }


@router.post("/api/set-provider")
async def api_set_provider(req: SetProviderRequest):
    mode = req.mode.strip().lower()
    if mode == "local":
        mode = "ollama"
    if mode not in {"ollama", "lmstudio", "remote", "openai", "openai_compatible"}:
        return api_error(
            "invalid_provider_mode",
            status_code=422,
            message=(f"Mode invalide : {req.mode}. Valeurs acceptées : remote, ollama, lmstudio."),
        )
    if mode == "remote" and not can_use_remote():
        return api_error(
            "remote_provider_not_configured",
            status_code=409,
            message=(
                "Provider distant non configuré. Configurez son URL et sa clé "
                "dans Paramètres > Assistant IA > Connexion."
            ),
        )
    requested_url = str(req.url or "").strip()
    try:
        if mode == "lmstudio" and requested_url:
            set_runtime_lmstudio_base_url(requested_url)
        elif mode == "ollama" and requested_url:
            set_runtime_ollama_base_url(requested_url)
        elif mode == "ollama" and req.use_default_ollama_url:
            set_runtime_ollama_base_url(None)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    set_runtime_mode(mode)
    embedding_model, embedding_msg = await asyncio.to_thread(ensure_internal_embedding_model)
    # Réinitialise la chaîne RAG pour qu'elle soit reconstruite avec le bon provider
    application.clear_runtime_state()
    info = public_provider_info()
    effective_mode = str(info.get("effective_mode") or info.get("mode") or "").strip().lower()
    if effective_mode == "local":
        effective_mode = "ollama"
    requested_mode = "remote" if mode in {"remote", "openai", "openai_compatible"} else mode
    return {
        "ok": True,
        "mode": mode,
        "effective_mode": effective_mode,
        "applied": effective_mode == requested_mode,
        "embedding_model": embedding_model,
        "embedding_message": embedding_msg,
        "info": info,
    }


@router.post("/api/set-embedding-model")
async def api_set_embedding_model(req: SetEmbeddingModelRequest):
    model = (req.model or "").strip()
    requested_provider = (req.provider or "").strip().lower() or get_embedding_provider_mode()
    if requested_provider == "remote" and req.use_local:
        disable_runtime_remote_embeddings()
        normalized = required_embedding_model_name()
        await _reuse_equivalent_index(normalized)
        selected_profile = application.set_active_vector_index_profile(
            chunk_size=get_persisted_chunk_size(),
            embedding_model=current_embedding_profile_label(normalized),
        )
        application.clear_runtime_state()
        profile_payload = selected_profile if isinstance(selected_profile, dict) else {}
        return {
            "ok": True,
            "model": normalized,
            "info": public_provider_info(),
            "index_profile": profile_payload,
            "index_reused": bool(profile_payload.get("ready")),
            "index_action": "reuse" if profile_payload.get("ready") else "build",
        }
    if not model:
        return api_error(
            "invalid_embedding_model",
            status_code=422,
            message="Modele d'embedding invalide.",
        )
    if requested_provider == "remote":
        if not is_remote_provider():
            return api_error(
                "remote_provider_inactive",
                status_code=409,
                message="Activez le provider distant avant de selectionner son modele d'embedding.",
            )
        ok, resolved, msg = await asyncio.to_thread(validate_remote_embedding_model, model)
    else:
        ok, resolved, msg = await asyncio.to_thread(validate_local_embedding_model, model)
    if not ok:
        return api_error(
            "embedding_model_unavailable",
            status_code=422,
            message=str(msg or "Modele d'embedding indisponible."),
        )
    normalized = set_runtime_embedding_model(
        resolved,
        provider_mode=requested_provider,
    )
    await _reuse_equivalent_index(normalized)
    selected_profile = application.set_active_vector_index_profile(
        chunk_size=get_persisted_chunk_size(),
        embedding_model=current_embedding_profile_label(normalized),
    )
    application.clear_runtime_state()
    profile_payload = selected_profile if isinstance(selected_profile, dict) else {}
    return {
        "ok": True,
        "model": normalized,
        "info": public_provider_info(),
        "index_profile": profile_payload,
        "index_reused": bool(profile_payload.get("ready")),
        "index_action": "reuse" if profile_payload.get("ready") else "build",
    }


@router.post("/api/set-document-tools-source")
async def api_set_document_tools_source(req: SetDocumentToolsSourceRequest):
    from src.rag import ocr_ingestion

    # Pages already read are filed under the model of the Ollama that read them.
    if ocr_ingestion.status()["active"]:
        return api_error(
            "ocr_active",
            status_code=409,
            message="Mettez la lecture des pages scannées en pause avant de changer ce réglage.",
        )
    try:
        source = set_runtime_document_tools_source(req.source)
    except ValueError:
        return api_error(
            "invalid_document_tools_source",
            status_code=422,
            message="Réglage invalide. Valeurs acceptées : local, chat.",
        )
    return {"ok": True, "source": source, "info": public_provider_info()}


@router.post("/api/set-embedding-source")
async def api_set_embedding_source(req: SetEmbeddingSourceRequest):
    try:
        source = set_runtime_embedding_source(req.source)
    except ValueError:
        return api_error(
            "invalid_embedding_source",
            status_code=422,
            message="Source d'indexation invalide. Valeurs acceptées : local, chat.",
        )
    embedding_model, embedding_msg = await asyncio.to_thread(ensure_internal_embedding_model)
    await _reuse_equivalent_index(embedding_model)
    selected_profile = application.set_active_vector_index_profile(
        chunk_size=get_persisted_chunk_size(),
        embedding_model=current_embedding_profile_label(embedding_model),
    )
    application.clear_runtime_state()
    profile_payload = selected_profile if isinstance(selected_profile, dict) else {}
    return {
        "ok": True,
        "source": source,
        "model": embedding_model,
        "embedding_message": embedding_msg,
        "info": public_provider_info(),
        "index_profile": profile_payload,
        "index_reused": bool(profile_payload.get("ready")),
        "index_action": "reuse" if profile_payload.get("ready") else "build",
    }


@router.get("/api/ollama-health")
def api_ollama_health(url: str | None = None):
    provider_mode = get_embedding_provider_mode()
    if provider_mode == "remote":
        return _current_embedding_health("")
    url = _configured_embedding_request_url(url, provider=provider_mode)
    if requires_local_ollama_embeddings():
        return check_ollama_health(
            url,
            required_embedding_model=required_embedding_model_name(),
            required=True,
        )
    return _current_embedding_health(url)


@router.post("/api/ollama/start")
def api_start_ollama():
    """Start the local Ollama service when it is required and currently offline."""
    with application._managed_operation():
        return application._ensure_local_ollama_started()


@router.get("/api/gpu")
def api_gpu(url: str | None = None):
    provider_mode = "lmstudio" if is_lmstudio_provider() else "ollama"
    url = _configured_ollama_request_url(url, provider=provider_mode)
    return {"status": fetch_gpu_status(url)}


@router.get("/api/ollama-runtime")
def api_ollama_runtime(model: str = "", url: str | None = None):
    provider_mode = "lmstudio" if is_lmstudio_provider() else "ollama"
    url = _configured_ollama_request_url(url, provider=provider_mode)
    return fetch_ollama_runtime_status(url, model)
