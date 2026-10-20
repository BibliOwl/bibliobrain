"""Persistent, non-secret machine configuration for Bibliobrain.

The file lives at a fixed per-user location so path overrides can be resolved
before ``STORAGE_DIR`` is known. Secret values are deliberately rejected here
and belong in :mod:`src.api_secrets` instead.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from src.atomic_io import atomic_write_json

CONFIGURATION_VERSION = 1
CONFIGURATION_FILENAME = "configuration.json"

# These names replace the non-secret entries that historically lived in .env.
# Values are kept in their familiar uppercase form to make manual migration and
# support diagnostics straightforward.
MACHINE_SETTING_NAMES = frozenset(
    {
        "BIBLIOBRAIN_HOME",
        "BIBLIOBRAIN_UPDATE_MANIFEST_URL",
        "DATA_PATH",
        "STORAGE_DIR",
        "CHROMA_PATH",
        "ZOTERO_DB_PATH",
        "ALLOWED_ORIGINS",
        "BIBLIOBRAIN_ALLOWED_HOSTS",
        "BIBLIOBRAIN_BIND_HOST",
        "BIBLIOBRAIN_TRUST_PROXY_HEADERS",
        "MAX_REQUEST_BYTES",
        "MAX_UPLOAD_BYTES",
        "RATE_LIMIT_CHAT_PER_MINUTE",
        "RATE_LIMIT_GENERAL_PER_MINUTE",
        "BIBLIOBRAIN_LOG_LEVEL",
        "LLM_PROVIDER",
        "OLLAMA_BASE_URL",
        "OLLAMA_EMBEDDING_MODEL",
        "OLLAMA_MODELS",
        "OLLAMA_MAX_OUTPUT_TOKENS",
        "OLLAMA_TIMEOUT_SECONDS",
        "OLLAMA_WATCHDOG_SECONDS",
        "OLLAMA_KEEP_ALIVE_SECONDS",
        "OLLAMA_AUTH_SCHEME",
        "OLLAMA_SSL_VERIFY",
        "NODE_EXTRA_CA_CERTS",
        "LMSTUDIO_BASE_URL",
        "LMSTUDIO_EMBEDDING_MODEL",
        "UNPAYWALL_EMAIL",
        "ZOTERO_CONNECTOR_URL",
        "DISCOVERY_LLM_TIMEOUT_SECONDS",
        "DISCOVERY_TOTAL_BUDGET_SECONDS",
        "REMOTE_EMBEDDING_MODEL",
        "REMOTE_TIMEOUT_SECONDS",
        "REMOTE_STREAMING",
        "REMOTE_MODEL_LIST_TIMEOUT_SECONDS",
        "MODEL_LIST_CACHE_TTL_SECONDS",
        "REMOTE_SSL_VERIFY",
        "BIBLIOBRAIN_ERROR_REPORTING_ENABLED",
        "BIBLIOBRAIN_ERROR_TRACKING_ENVIRONMENT",
        "RERANKER_ENABLED",
        "RERANKER_TRANSLATE_FRENCH",
        "RERANKER_MODEL",
        "RERANKER_QUICK_MAX_TEXT_CHARS",
        "RERANKER_CANDIDATE_MULT",
        "RERANKER_DEVICE",
        "RERANKER_UNLOAD_AFTER_USE",
        "RAG_QUICK_LEXICAL_SCAN_LIMIT",
        "RAG_LEXICAL_SCAN_LIMIT",
        "CHAT_CONTEXT_MESSAGE_LIMIT",
    }
)

SECRET_ENVIRONMENT_NAMES = frozenset(
    {
        "REMOTE_LLM_BASE_URL",
        "REMOTE_LLM_API_KEY",
        "OPENALEX_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
        "S2_API_KEY",
        "OLLAMA_API_KEY",
        "OLLAMA_AUTH_HEADER",
        "HF_TOKEN",
        "BIBLIOBRAIN_BUGSINK_DSN",
    }
)

INTERNAL_ENVIRONMENT_NAMES = frozenset(
    {
        "BIBLIOBRAIN_DOCUMENT_ISOLATION",
        "BIBLIOBRAIN_MANAGED_SERVER",
        "BIBLIOBRAIN_MAX_DOCUMENT_MB",
        "BIBLIOBRAIN_PARSER_MEMORY_MB",
        "BIBLIOBRAIN_PARSER_TIMEOUT_SECONDS",
        # A diagnostic override for one process only. Persisted, it would outlive
        # every update: the .env template of 0.6.x shipped BIBLIOBRAIN_VERSION=0.6.x,
        # and a configuration.json that still holds it from an earlier migration
        # is ignored on load.
        "BIBLIOBRAIN_VERSION",
    }
)

_LOCK = threading.RLock()


class MachineConfigError(RuntimeError):
    """Raised when the per-user non-secret configuration is invalid."""


def local_app_data_dir() -> Path:
    raw = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / "AppData" / "Local"


def configuration_path(local_app_data: str | os.PathLike[str] | None = None) -> Path:
    base = Path(local_app_data) if local_app_data is not None else local_app_data_dir()
    return base / "bibliobrain" / CONFIGURATION_FILENAME


def _stringify_setting(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value).strip()


def load_machine_settings(
    path: str | os.PathLike[str] | None = None,
) -> dict[str, str]:
    target = Path(path) if path is not None else configuration_path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MachineConfigError("La configuration locale est illisible ou corrompue.") from exc
    if not isinstance(payload, dict) or payload.get("version") != CONFIGURATION_VERSION:
        raise MachineConfigError("La version de la configuration locale n'est pas prise en charge.")
    raw_settings = payload.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise MachineConfigError("La section 'settings' de la configuration locale est invalide.")
    return {
        str(name): _stringify_setting(value)
        for name, value in raw_settings.items()
        if name in MACHINE_SETTING_NAMES and _stringify_setting(value)
    }


def save_machine_settings(
    updates: dict[str, Any],
    *,
    path: str | os.PathLike[str] | None = None,
) -> Path:
    """Atomically merge allowed non-secret settings into configuration.json."""
    target = Path(path) if path is not None else configuration_path()
    with _LOCK:
        current = load_machine_settings(target)
        for name, value in updates.items():
            if name not in MACHINE_SETTING_NAMES:
                continue
            normalized = _stringify_setting(value)
            if normalized:
                current[name] = normalized
            else:
                current.pop(name, None)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            target,
            {"version": CONFIGURATION_VERSION, "settings": current},
        )
    return target


def activate_machine_settings(
    path: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Expose persisted settings to legacy readers without loading a .env file.

    Explicit process variables retain priority. This keeps command-line and test
    overrides possible while making configuration.json the persistent source.
    """
    target = Path(path) if path is not None else configuration_path()
    settings = load_machine_settings(target)
    for name, value in settings.items():
        if not str(os.environ.get(name) or "").strip():
            os.environ[name] = value
    return target.resolve() if target.exists() else None
