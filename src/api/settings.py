"""Settings HTTP routes and feature-specific helpers."""

from fastapi import APIRouter, HTTPException, Request

from src import config
from src.api.common import is_loopback_request
from src.api_models import (
    ApiSecretsRequest,
    AppSettingsRequest,
    RagSettingsRequest,
)
from src.api_secrets import ApiSecretStoreError, api_config_status, update_api_config
from src.app_settings import get_app_settings, set_app_settings
from src.llm_provider import _validate_remote_base_url
from src.rag_settings import get_persisted_chunk_size, get_rag_settings, set_persisted_chunk_size

router = APIRouter()


@router.get("/api/rag-settings")
def api_rag_settings():
    return get_rag_settings()


@router.post("/api/rag-settings")
def api_set_rag_settings(req: RagSettingsRequest):
    chunk_size = get_persisted_chunk_size()
    if req.chunk_size is not None:
        chunk_size = set_persisted_chunk_size(req.chunk_size)
    return {"ok": True, "chunk_size": int(chunk_size)}


@router.get("/api/api-secrets")
def api_api_secrets(request: Request):
    if not is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail="La gestion des clés API est limitée à cet appareil.",
        )
    return api_config_status(config.STORAGE_DIR)


@router.post("/api/api-secrets")
def api_set_api_secrets(req: ApiSecretsRequest, request: Request):
    if not is_loopback_request(request):
        raise HTTPException(
            status_code=403,
            detail="La gestion des clés API est limitée à cet appareil.",
        )

    updates: dict[str, str] = {}
    remote_url = str(req.remote_llm_base_url or "").strip()
    if remote_url:
        try:
            updates["remote_llm_base_url"] = _validate_remote_base_url(remote_url)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=(
                    "L’URL du fournisseur doit être une URL HTTP(S) valide et ne peut "
                    "pas cibler un endpoint de métadonnées système."
                ),
            ) from exc

    for field_name in (
        "remote_llm_api_key",
        "openalex_api_key",
        "semantic_scholar_api_key",
        "ollama_api_key",
        "ollama_auth_header",
        "huggingface_token",
        "bugsink_dsn",
    ):
        secret = getattr(req, field_name)
        if secret is not None:
            value = secret.get_secret_value().strip()
            if value:
                updates[field_name] = value

    try:
        update_api_config(
            config.STORAGE_DIR,
            updates=updates,
            clear=req.clear,
        )
    except ApiSecretStoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    return {"ok": True, **api_config_status(config.STORAGE_DIR)}


@router.get("/api/app-settings")
def api_app_settings():
    settings = get_app_settings()
    return settings


@router.post("/api/app-settings")
def api_set_app_settings(req: AppSettingsRequest):
    settings = set_app_settings(
        llm_thinking=req.llm_thinking,
        threshold=req.threshold,
        max_chunks=req.max_chunks,
        temperature=req.temperature,
        repeat_penalty=req.repeat_penalty,
        top_p=req.top_p,
        ollama_context_length=req.ollama_context_length,
        zotero_path=req.zotero_path,
        metadata_source=req.metadata_source,
        data_path=req.data_path,
        auto_enrich_pdf_metadata=req.auto_enrich_pdf_metadata,
        allow_external_api_requests=req.allow_external_api_requests,
        allow_external_discovery=req.allow_external_discovery,
        research_scope_enabled=req.research_scope_enabled,
        discovery_query_planner=req.discovery_query_planner,
        discovery_dense_enabled=req.discovery_dense_enabled,
        index_standalone_pdfs=req.index_standalone_pdfs,
        discovery_candidate_multiplier=req.discovery_candidate_multiplier,
        discovery_llm_validation_multiplier=req.discovery_llm_validation_multiplier,
        unpaywall_email=req.unpaywall_email,
        debug_metrics=req.debug_metrics,
        debug_force_thinking_small_models=req.debug_force_thinking_small_models,
        first_rag_init_done=req.first_rag_init_done,
        onboarding_done_v1=req.onboarding_done_v1,
        feature_tour_done_v1=req.feature_tour_done_v1,
        onboarding_progress=req.onboarding_progress,
        reranker_enabled=req.reranker_enabled,
        reranker_profile=req.reranker_profile,
        reranker_translate_french=req.reranker_translate_french,
        selected_model=req.selected_model,
        visual_analysis_mode=req.visual_analysis_mode,
        vision_model=req.vision_model,
        visual_description_model=req.visual_description_model,
        visual_analysis_auto_chat=req.visual_analysis_auto_chat,
        chat_answer_length=req.chat_answer_length,
        ui_language=req.ui_language,
        update_check_enabled=req.update_check_enabled,
    )
    if req.research_scope_enabled is False:
        from src.citation_graph import clear_bibliographic_search_cache

        clear_bibliographic_search_cache()
    if req.metadata_source is not None and settings.get("metadata_source") == "none":
        from src.library import clear_metadata_library
        from src.rag import state as rag_state

        clear_metadata_library()
        with rag_state._state_lock:
            rag_state._zotero_library = {}
    return {"ok": True, **settings}
