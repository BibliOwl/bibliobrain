"""Validated request schemas for the Bibliobrain HTTP API."""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

from src.visual_analysis import RECOMMENDED_OCR_MODEL

_PATH_MAX_LEN = 4096
_SENSITIVE_PATH_PREFIXES_POSIX = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
    "/var/lib",
)
_PATH_QUOTE_PAIRS = {
    '"': '"',
    "'": "'",
    "\u201c": "\u201d",
    "\u2018": "\u2019",
    "\u00ab": "\u00bb",
}


def _validate_user_path(value: str, *, allow_empty: bool = False) -> str:
    raw = str(value or "").strip()
    while len(raw) >= 2 and _PATH_QUOTE_PAIRS.get(raw[0]) == raw[-1]:
        raw = raw[1:-1].strip()
    if not raw:
        if allow_empty:
            return ""
        raise ValueError("path must not be empty")
    if len(raw) > _PATH_MAX_LEN:
        raise ValueError("path too long")
    if "\x00" in raw:
        raise ValueError("path contains null byte")
    real = os.path.realpath(raw)
    real_norm = real.replace("\\", "/").lower()
    for prefix in _SENSITIVE_PATH_PREFIXES_POSIX:
        if real_norm == prefix or real_norm.startswith(prefix + "/"):
            raise ValueError(f"path resolves to a restricted location: {prefix}")
    return raw


class InitRequest(BaseModel):
    # Empty: prepare the documents before any chat model is installed.
    model: str = ""
    threshold: float = 0.3
    force_reload: bool = False
    max_chunks: int = 6
    chunk_size: int | None = None

class RagSettingsRequest(BaseModel):
    chunk_size: int | None = Field(default=None, ge=1)


class ArticleTaxonomyRegenerateRequest(BaseModel):
    model: str | None = None

class AppSettingsRequest(BaseModel):
    llm_thinking: bool | None = None
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    max_chunks: int | None = Field(default=None, ge=1, le=20)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    repeat_penalty: float | None = Field(default=None, ge=1.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.05, le=1.0)
    ollama_context_length: int | None = Field(default=None, ge=0, le=262144)
    zotero_path: str | None = None
    metadata_source: Literal["none", "bibtex", "zotero"] | None = None
    data_path: str | None = None
    auto_enrich_pdf_metadata: bool | None = None
    allow_external_api_requests: bool | None = None
    allow_external_discovery: bool | None = None
    research_scope_enabled: bool | None = None
    discovery_query_planner: bool | None = None
    discovery_dense_enabled: bool | None = None
    discovery_candidate_multiplier: int | None = Field(default=None, ge=1, le=8)
    discovery_llm_validation_multiplier: int | None = Field(default=None, ge=1, le=8)
    unpaywall_email: str | None = None
    debug_metrics: bool | None = None
    # Tri-state consent: None = not asked yet (src/update_check.py sends nothing).
    update_check_enabled: bool | None = None
    debug_force_thinking_small_models: bool | None = None
    first_rag_init_done: bool | None = None
    onboarding_done_v1: bool | None = None
    feature_tour_done_v1: bool | None = None
    onboarding_progress: dict[str, Any] | None = None
    reranker_enabled: bool | None = None
    reranker_profile: Literal[
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
    ] | None = None
    reranker_translate_french: bool | None = None
    selected_model: str | None = None
    visual_analysis_mode: Literal["off", "on_demand", "index"] | None = None
    vision_model: str | None = Field(default=None, max_length=200)
    visual_description_model: str | None = Field(default=None, max_length=200)
    visual_analysis_auto_chat: bool | None = None
    ui_language: str | None = Field(default=None, pattern="^(fr|en)$")


class ApiSecretsRequest(BaseModel):
    remote_llm_base_url: str | None = Field(default=None, max_length=2048)
    remote_llm_api_key: SecretStr | None = None
    openalex_api_key: SecretStr | None = None
    semantic_scholar_api_key: SecretStr | None = None
    ollama_api_key: SecretStr | None = None
    ollama_auth_header: SecretStr | None = None
    huggingface_token: SecretStr | None = None
    bugsink_dsn: SecretStr | None = None
    clear: list[
        Literal[
            "remote_llm_base_url",
            "remote_llm_api_key",
            "openalex_api_key",
            "semantic_scholar_api_key",
            "ollama_api_key",
            "ollama_auth_header",
            "huggingface_token",
            "bugsink_dsn",
        ]
    ] = Field(default_factory=list, max_length=8)


class VisionModelInstallRequest(BaseModel):
    model: str = Field(default=RECOMMENDED_OCR_MODEL, max_length=200)
    confirmed: bool = False

class VisualAnalysisRequest(BaseModel):
    path: str = Field(min_length=1, max_length=2000)
    model: str | None = Field(default=None, max_length=200)
    analysis_model: str | None = Field(default=None, max_length=200)

class BugReportRequest(BaseModel):
    description: str = Field(min_length=10, max_length=5000)
    steps: str = Field(default="", max_length=5000)
    contact: str = Field(default="", max_length=320)
    include_diagnostics: bool = True
    include_chat: bool = False
    active_chat_id: str = Field(default="", max_length=120)
    current_model: str = Field(default="", max_length=200)
    screenshot_data_url: str = Field(default="", max_length=425_000)
    client_logs: list[dict[str, Any]] = Field(default_factory=list, max_length=30)
    client_context: dict[str, Any] = Field(default_factory=dict)

    @field_validator("description")
    @classmethod
    def _description_must_have_content(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) < 10:
            raise ValueError("description must contain at least 10 non-space characters")
        return normalized

class ChatRequest(BaseModel):
    message: str
    model: str
    threshold: float = 0.3
    tags: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)
    chat_id: str | None = None
    thinking: bool = False
    force_thinking_small_models: bool = False
    privacy_ack: bool = False
    external_search_confirmed: bool = False
    article_search_scope: str = ""
    article_path: str = Field(default="", max_length=2048)
    article_title: str = Field(default="", max_length=500)
    article_paths: list[str] = Field(default_factory=list, max_length=200)
    article_scope_once: bool = False
    depth: str = "auto"
    hint_action: str = Field(default="", max_length=40)
    force_visual_analysis: bool = False

class LiteratureReviewRequest(BaseModel):
    message: str
    model: str
    threshold: float = 0.3
    tags: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)
    target_chunks: int = Field(default=24, ge=8, le=40)
    chat_id: str | None = None
    request_id: str = Field(default="", max_length=128)

class AbortStreamRequest(BaseModel):
    request_id: str = ""
    model: str | None = None
    reason: str = Field(default="user_stop", max_length=40)

class BrowserSessionRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=128)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
            raise ValueError("invalid browser session id")
        return normalized

class ChatCreateRequest(BaseModel):
    title: str = ""

class ChatTitleRequest(BaseModel):
    title: str = ""

class ChatAutoTitleRequest(BaseModel):
    model: str = ""


class ChatWarmupRequest(BaseModel):
    model: str = ""

class SummaryRequest(BaseModel):
    rel_path: str
    analysis_type: str
    model: str
    thinking: bool = False
    force_thinking_small_models: bool = False

class AnalysisPromptRequest(BaseModel):
    analysis_type: str
    prompt: str = ""
    language: str = ""

class LibraryExportRequest(BaseModel):
    export_format: Literal["markdown", "bibtex"] = "markdown"
    paths: list[str] | None = None

class RagExclusionRequest(BaseModel):
    paths: list[str] = Field(..., min_length=1, max_length=10000)
    excluded: bool = True

class DraftingRequest(BaseModel):
    paragraph: str
    model: str | None = None
    max_suggestions: int = 5
    exclude_citation_keys: list[str] = Field(default_factory=list)
    user_hint: str | None = None
    # Draft text around a selection or a retried sentence: context, not analysed.
    context_before: str = Field("", max_length=4000)
    context_after: str = Field("", max_length=4000)

class DraftingReviewRequest(BaseModel):
    text: str
    model: str | None = None
    max_suggestions: int = 4
    request_id: str = ""
    argument_kind: Literal[
        "any",
        "mechanism",
        "counter_argument",
        "limitation",
        "context",
        "implication",
    ] = "any"

class DraftingExportRequest(BaseModel):
    content: str
    export_format: Literal["markdown", "latex"] = "markdown"
    title: str = ""
    bibliography: list[dict[str, Any]] = Field(default_factory=list)

class ZoteroLoadRequest(BaseModel):
    path: str

    @field_validator("path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        return _validate_user_path(v)

class SetDataPathRequest(BaseModel):
    path: str

    @field_validator("path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        return _validate_user_path(v)

class DirectoryPickerRequest(BaseModel):
    path: str = ""

    @field_validator("path")
    @classmethod
    def _check_path(cls, v: str) -> str:
        return _validate_user_path(v, allow_empty=True)

class NotebookCreateRequest(BaseModel):
    title: str = "Nouveau carnet"

class NotebookTitleRequest(BaseModel):
    title: str

class NotebookNotesRequest(BaseModel):
    notes: str

class NotebookSourceAddRequest(BaseModel):
    title: str
    content: str
    source_path: str = ""

    @field_validator("source_path")
    @classmethod
    def _check_source_path(cls, v: str) -> str:
        return _validate_user_path(v, allow_empty=True)

class NotebookImportPathsRequest(BaseModel):
    notebook_id: str | None = None
    title: str = "Nouveau carnet (graphe)"
    paths: list[str] = Field(default_factory=list)
    max_pages: int = Field(default=15, ge=1, le=500)
    max_chars: int = Field(default=20000, ge=1, le=2_000_000)

    @field_validator("paths")
    @classmethod
    def _check_paths(cls, v: list[str]) -> list[str]:
        if len(v) > 1000:
            raise ValueError("too many paths (max 1000)")
        cleaned: list[str] = []
        for item in v:
            raw = str(item or "").strip()
            if not raw:
                continue
            if len(raw) > _PATH_MAX_LEN or "\x00" in raw or ".." in raw.replace("\\", "/").split("/"):
                raise ValueError("invalid path entry")
            # Reject absolute paths: POSIX (/etc/passwd) and Windows (C:\..., \\server\share)
            if raw.startswith(("/", "\\")) or (len(raw) >= 2 and raw[1] == ":"):
                raise ValueError("absolute path not allowed")
            cleaned.append(raw)
        return cleaned

class NotebookImportZoteroCollectionRequest(BaseModel):
    notebook_id: str | None = None
    collection: str
    title: str = ""
    include_descendants: bool = True
    max_pages: int = Field(default=15, ge=1, le=40)
    max_chars: int = Field(default=20000, ge=2000, le=120000)

class NotebookAppendAnalysisRequest(BaseModel):
    notebook_id: str | None = None
    title: str = "Nouveau carnet"
    text: str = ""

class NotebookChatRequest(BaseModel):
    message: str
    model: str
    thinking: bool = False
    force_thinking_small_models: bool = False
    append_to_notes: bool = False
    persist_history: bool = True
    read_only: bool = False

class ArticleDiscoveryRequest(BaseModel):
    mode: str = "search"
    input_text: str = ""
    model: str | None = None
    notebook_id: str | None = None
    max_results: int = 10
    include_unpaywall: bool = True
    unpaywall_email: str | None = None
    query_planner: bool | None = None
    privacy_ack: bool = False
    debug: bool = False
    # "Search again" on a saved search: its results are replaced in place.
    history_id: str | None = Field(default=None, max_length=64)


class DiscoveryHistoryRenameRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ResearchTrendTopic(BaseModel):
    label: str = Field(default="", max_length=100)
    query: str = Field(min_length=2, max_length=500)


ResearchWorkType = Literal[
    "article", "review", "preprint", "book-chapter", "dissertation", "report"
]


def _default_research_work_types() -> list[ResearchWorkType]:
    return ["article"]


class ResearchTrendsRequest(BaseModel):
    topics: list[ResearchTrendTopic] = Field(min_length=1, max_length=3)
    from_year: int = Field(ge=1800, le=2200)
    to_year: int = Field(ge=1800, le=2200)
    work_types: list[ResearchWorkType] = Field(
        default_factory=_default_research_work_types,
        max_length=6,
    )
    open_access_only: bool = False
    privacy_ack: bool = False

    @model_validator(mode="after")
    def validate_year_range(self) -> ResearchTrendsRequest:
        if self.from_year > self.to_year:
            raise ValueError("from_year must be less than or equal to to_year")
        if self.to_year - self.from_year + 1 > 200:
            raise ValueError("publication trend ranges are limited to 200 years")
        return self


class ArticleDiscoveryAbortRequest(BaseModel):
    model: str | None = None

class CitationEntitySearchRequest(BaseModel):
    entity_type: Literal["author", "institution"]
    query: str = Field(min_length=2, max_length=200)
    limit: int = Field(default=8, ge=1, le=20)

class CitationScopeGraphRequest(BaseModel):
    entity_type: Literal["author", "institution"]
    openalex_id: str = Field(pattern=r"^[AI]\d+$", min_length=2, max_length=32)
    from_year: int | None = Field(default=None, ge=1800, le=2200)
    to_year: int | None = Field(default=None, ge=1800, le=2200)
    max_works: int = Field(default=80, ge=10, le=150)
    strategy: Literal["balanced", "most_cited", "recent"] = "balanced"


class BibliographicSearchGraphRequest(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    from_year: int | None = Field(default=None, ge=1800, le=2200)
    to_year: int | None = Field(default=None, ge=1800, le=2200)
    max_works: int = Field(default=80, ge=10, le=150)
    strategy: Literal["balanced", "most_cited", "recent"] = "balanced"

class ArticleDiscoveryDownloadRequest(BaseModel):
    title: str
    doi: str = ""
    pdf_url: str = ""
    semantic_url: str = ""
    citation_count: int = 0
    influential_citation_count: int = 0
    add_to_zotero: bool = True
    privacy_ack: bool = False

class ChatDiscoverRequest(BaseModel):
    """Request pour decouvrir des articles a partir du contexte d'une conversation."""
    chat_id: str = ""
    max_results: int = Field(default=8, ge=1, le=50)
    privacy_ack: bool = False
    model: str = ""
    include_unpaywall: bool = True
    debug: bool = False

class SetOllamaUrlRequest(BaseModel):
    provider: str | None = None
    url: str = ""

class SetProviderRequest(BaseModel):
    mode: str  # "remote" | "local"
    use_default_ollama_url: bool = False
    # Address of the Ollama/LM Studio server to use with this mode (this computer or another one).
    url: str | None = None

class SetDocumentToolsSourceRequest(BaseModel):
    source: str  # "local" (Ollama on this computer) | "chat" (the chat's Ollama address)


class SetEmbeddingSourceRequest(BaseModel):
    source: str  # "local" (Ollama on this computer) | "chat" (same service as the chat)


class SetEmbeddingModelRequest(BaseModel):
    model: str
    provider: str | None = None
    use_local: bool = False

class FullResetRequest(BaseModel):
    confirm: bool = False
    remove_discovery_imports: bool = True

class ConfirmRequest(BaseModel):
    confirm: bool = False


class IndexTransferTokenRequest(BaseModel):
    # Server-issued by /api/index-transfer/inspect; never a path.
    token: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class IndexTransferPullRequest(IndexTransferTokenRequest):
    # Downloads only after the user accepted the proposal in the UI.
    confirm: bool = False


class IndexTransferImportRequest(IndexTransferTokenRequest):
    confirm: bool = False
    adopt_settings: bool = True
