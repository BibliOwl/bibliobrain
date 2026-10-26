import base64
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel

from src import config
from src.atomic_io import atomic_write_json
from src.model_profiles import (
    needs_two_step_json,
    resolve_model_family,
    resolve_ollama_think_request,
)
from src.ollama_residency import keep_alive_seconds, pinned_context_length
from src.structured_chat import (
    STRUCTURED_OUTPUT_MAX_RETRIES,
    ChatAnswer,
    build_instructor_client,
)

logger = logging.getLogger(__name__)

_INVALID_MODEL_VALUES = {
    "",
    "-",
    "—",
    "none",
    "null",
    "undefined",
    "chargement...",
    "chargement…",
    "aucun modele",
    "erreur connexion",
}

_DEFAULT_TEMPERATURE = 0.1
# 1.2 until 2026-10: penalising recent tokens also penalised the figures and
# [Sx] labels the answer must copy. On the grounding benchmark (54 cases,
# temperature 0 and 0.1) 1.0 was never worse; qwen3.5:4b at 1.2 changed
# "0,9 Ic" into "1,9 Ic", interpolated a value absent from a table and wrote
# answers twice as long. No answer looped at 1.0 (270 answers).
_DEFAULT_REPEAT_PENALTY = 1.0
# A stored 1.2 without this marker is the old default the interface saved on
# every settings sync, not a choice: it reads as the new default.
_OLD_DEFAULT_REPEAT_PENALTY = 1.2
_GENERATION_DEFAULTS_VERSION_KEY = "generation_defaults_version"
_GENERATION_DEFAULTS_VERSION = 2
_DEFAULT_TOP_P = 0.5
_DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
_DEFAULT_LMSTUDIO_BASE_URL = "http://127.0.0.1:1234"
_DEFAULT_LMSTUDIO_API_KEY = "lm-studio"
_LMSTUDIO_PUBLIC_SELECTION_ENABLED = True
_DEFAULT_OLLAMA_MAX_OUTPUT_TOKENS = 5000
_DEFAULT_OLLAMA_CONTEXT_LENGTH = 0
_DEFAULT_OLLAMA_WATCHDOG_SECONDS = 90.0
_OLLAMA_USER_AGENT = "BIBLIOBRAIN/1.0"
_MODEL_LIST_CACHE_STATE_KEY = "model_list_cache"
DEFAULT_OLLAMA_EMBEDDING_MODEL = "embeddinggemma"
_REMOTE_EMBEDDINGS_DISABLED = "__local__"
_runtime_lock = threading.RLock()
_provider_state_lock = threading.RLock()

# ── Runtime provider override ─────────────────────────────────────────────────
# None  → utilise la variable d'env LLM_PROVIDER
# str   → surcharge explicite choisie par l'utilisateur ("ollama" | "remote")
_runtime_mode: str | None = None
_runtime_ollama_base_url: str | None = None
_runtime_lmstudio_base_url: str | None = None
_runtime_ollama_embedding_model: str | None = None
_runtime_lmstudio_embedding_model: str | None = None
_runtime_remote_embedding_model: str | None = None
_runtime_generation_settings: dict[str, float] | None = None
_runtime_ollama_context_length: int | None = None
_fallback_active: bool = False
_fallback_reason: str = ""


# Abort reason that closes the stream but keeps the Ollama model loaded.
SOFT_ABORT_REASON = "soft-timeout"


class StreamAbortSignal:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._callbacks: list[Callable[[], None]] = []
        self._reason = ""

    def is_aborted(self) -> bool:
        return self._event.is_set()

    def abort(self, reason: str | None = None) -> None:
        callbacks: list[Callable[[], None]] = []
        normalized_reason = str(reason or "").strip()[:80]
        with self._lock:
            if self._event.is_set():
                if normalized_reason and not self._reason:
                    self._reason = normalized_reason
                return
            self._reason = normalized_reason
            self._event.set()
            callbacks = list(self._callbacks)
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logger.debug("Recovered from an error in abort.", exc_info=True)
                continue

    def abort_reason(self) -> str:
        with self._lock:
            return self._reason

    def register_close_callback(self, callback) -> None:
        if not callable(callback):
            return
        should_call = False
        with self._lock:
            if self._event.is_set():
                should_call = True
            else:
                self._callbacks.append(callback)
        if should_call:
            try:
                callback()
            except Exception:
                logger.debug("Provider callback raised", exc_info=True)

    def throw_if_aborted(self) -> None:
        if self._event.is_set():
            raise InterruptedError("Generation aborted.")


def _provider_state_path() -> Path:
    os.makedirs(config.STORAGE_DIR, exist_ok=True)
    return Path(config.STORAGE_DIR) / "provider_state.json"


def _load_provider_state() -> dict[str, Any]:
    with _provider_state_lock:
        path = _provider_state_path()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.debug("Recovered from an error in _load_provider_state.", exc_info=True)
            return {}
        return payload if isinstance(payload, dict) else {}


def _save_provider_state(payload: dict[str, Any]) -> None:
    with _provider_state_lock:
        path = _provider_state_path()
        current = _load_provider_state()
        current.update(payload)
        atomic_write_json(path, current)


_EMBEDDING_SOURCE_KEY = "embedding_source"
# "local": the index is computed by Ollama on this computer, whatever answers the chat.
# "chat": the index follows the chat service (LM Studio, or Ollama on another computer).
EMBEDDING_SOURCE_LOCAL = "local"
EMBEDDING_SOURCE_CHAT = "chat"
_DOCUMENT_TOOLS_SOURCE_KEY = "document_tools_source"
# Scan reading and figure analysis always use Ollama. "local": the one on this
# computer. "chat": the Ollama address saved for the chat, on another computer.
DOCUMENT_TOOLS_SOURCE_LOCAL = "local"
DOCUMENT_TOOLS_SOURCE_CHAT = "chat"


def _normalize_embedding_source(value: Any) -> str | None:
    source = str(value or "").strip().lower()
    return source if source in {EMBEDDING_SOURCE_LOCAL, EMBEDDING_SOURCE_CHAT} else None


def _is_this_computer_url(url: str | None) -> bool:
    try:
        hostname = (urlsplit(str(url or "").strip()).hostname or "").lower()
    except ValueError:
        return False
    if hostname == "localhost" or hostname.endswith(".docker.internal"):
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _legacy_embedding_source(payload: dict[str, Any]) -> str:
    """Where the index was computed before the setting existed: with the chat service."""
    mode = str(payload.get("mode") or os.getenv("LLM_PROVIDER", "")).strip().lower()
    if mode == "lmstudio":
        return EMBEDDING_SOURCE_CHAT
    ollama_url = str(payload.get("ollama_base_url") or os.getenv("OLLAMA_BASE_URL", "")).strip()
    if ollama_url and not _is_this_computer_url(ollama_url):
        return EMBEDDING_SOURCE_CHAT
    return EMBEDDING_SOURCE_LOCAL


def _load_persisted_embedding_source() -> str:
    payload = _load_provider_state()
    return _normalize_embedding_source(
        payload.get(_EMBEDDING_SOURCE_KEY)
    ) or _legacy_embedding_source(payload)


def _legacy_document_tools_source(payload: dict[str, Any]) -> str:
    """Before the setting existed, figure analysis followed the saved Ollama address."""
    ollama_url = str(payload.get("ollama_base_url") or os.getenv("OLLAMA_BASE_URL", "")).strip()
    if ollama_url and not _is_this_computer_url(ollama_url):
        return DOCUMENT_TOOLS_SOURCE_CHAT
    return DOCUMENT_TOOLS_SOURCE_LOCAL


def _load_persisted_document_tools_source() -> str:
    payload = _load_provider_state()
    return _normalize_embedding_source(
        payload.get(_DOCUMENT_TOOLS_SOURCE_KEY)
    ) or _legacy_document_tools_source(payload)


def _pin_service_sources() -> None:
    """Record where the index and the document tools run before the chat service changes.

    Without a saved value they are read from the chat service, which is right for
    an older install but would move them along with the next change.
    """
    with _provider_state_lock:
        saved = _load_provider_state()
        missing = {
            key: value
            for key, value in (
                (_EMBEDDING_SOURCE_KEY, get_embedding_source()),
                (_DOCUMENT_TOOLS_SOURCE_KEY, get_document_tools_source()),
            )
            if key not in saved
        }
        if missing:
            _save_provider_state(missing)


def _normalize_internal_provider_mode(mode: str | None) -> str:
    value = str(mode or "").strip().lower()
    if value == "lmstudio":
        return "lmstudio"
    if value in {"remote", "openai", "openai_compatible"}:
        return "remote"
    return "ollama"


def _embedding_state_key(provider_mode: str | None) -> str:
    provider = _normalize_internal_provider_mode(provider_mode)
    if provider == "lmstudio":
        return "lmstudio_embedding_model"
    if provider == "remote":
        return "remote_embedding_model"
    return "ollama_embedding_model"


def _load_persisted_embedding_model(provider_mode: str | None = None) -> str | None:
    value = str(_load_provider_state().get(_embedding_state_key(provider_mode)) or "").strip()
    return value or None


def _normalize_temperature(value: Any, default: float = _DEFAULT_TEMPERATURE) -> float:
    try:
        temperature = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(0.0, min(2.0, temperature))


def _normalize_repeat_penalty(value: Any, default: float = _DEFAULT_REPEAT_PENALTY) -> float:
    try:
        repeat_penalty = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(1.0, min(2.0, repeat_penalty))


def _normalize_top_p(value: Any, default: float = _DEFAULT_TOP_P) -> float:
    try:
        top_p = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(0.05, min(1.0, top_p))


def _normalize_ollama_context_length(
    value: Any,
    default: int = _DEFAULT_OLLAMA_CONTEXT_LENGTH,
) -> int:
    """Normalize the requested Ollama context; zero delegates to Ollama auto mode."""
    try:
        context_length = int(value)
    except (TypeError, ValueError):
        return int(default)
    if context_length <= 0:
        return 0
    return max(2048, min(262144, context_length))


def _load_persisted_ollama_context_length() -> int:
    return _normalize_ollama_context_length(
        _load_provider_state().get("ollama_context_length"),
    )


def get_ollama_context_length() -> int:
    with _runtime_lock:
        value = _runtime_ollama_context_length
    if value is None:
        value = _load_persisted_ollama_context_length()
    return _normalize_ollama_context_length(value)


def set_ollama_context_length(value: Any, *, persist: bool = True) -> int:
    global _runtime_ollama_context_length
    normalized = _normalize_ollama_context_length(value)
    with _runtime_lock:
        _runtime_ollama_context_length = normalized
    if persist:
        _save_provider_state({"ollama_context_length": normalized})
    return normalized


_AUTO_CONTEXT_FLOOR = 4096
_AUTO_CONTEXT_STEP = 4096
_AUTO_CONTEXT_CAP = 16384
# Character-based token estimates run low on French prose; leave headroom.
_AUTO_CONTEXT_MARGIN = 1.10
_GIB = 1024**3

_vram_probe_lock = threading.Lock()
_vram_probe_cache: int | None = None
_model_context_lock = threading.Lock()
_model_context_cache: dict[str, int] = {}


def _detected_gpu_vram_bytes() -> int:
    """Total VRAM of the largest NVIDIA pool, probed once per process."""
    global _vram_probe_cache
    with _vram_probe_lock:
        if _vram_probe_cache is not None:
            return _vram_probe_cache
    try:
        from src.visual_analysis import _detect_total_gpu_vram

        detected = max(0, int(_detect_total_gpu_vram()[0]))
    except Exception:
        logger.debug("Recovered from an error in _detected_gpu_vram_bytes.", exc_info=True)
        detected = 0
    with _vram_probe_lock:
        _vram_probe_cache = detected
    return detected


def _vram_context_cap() -> int:
    """Ceiling for Auto context sizing, scaled down on small GPUs.

    ``nvidia-smi`` is the only probe available, so AMD / Intel / Apple report
    zero and fall back to the fixed cap; Ollama still spills to CPU there.
    """
    gib = _detected_gpu_vram_bytes() / _GIB
    if gib <= 0:
        return _AUTO_CONTEXT_CAP
    if gib < 6:
        return 4096
    if gib < 8:
        return 8192
    if gib < 12:
        return 12288
    return _AUTO_CONTEXT_CAP


def _model_advertised_context_length(model_name: str | None) -> int:
    """Largest ``*.context_length`` reported by ``/api/show``, cached per model."""
    normalized = _normalize_model_name(model_name)
    if not normalized:
        return 0
    with _model_context_lock:
        cached = _model_context_cache.get(normalized)
    if cached is not None:
        return cached
    detected = 0
    try:
        with _open_ollama_request(
            "/api/show",
            {"model": normalized},
            timeout_seconds=5,
        ) as response:
            payload = json.loads(response.read() or b"{}")
        model_info = payload.get("model_info")
        if isinstance(model_info, dict):
            values: list[int] = []
            for key, value in model_info.items():
                if not str(key).endswith(".context_length"):
                    continue
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    values.append(parsed)
            if values:
                detected = max(values)
    except Exception:
        logger.debug("Recovered from an error in _model_advertised_context_length.", exc_info=True)
        detected = 0
    with _model_context_lock:
        _model_context_cache[normalized] = detected
    return detected


def resolve_ollama_context_length(
    model_name: str | None = None,
    *,
    required_tokens: int = 0,
) -> int:
    """Context window to request for one call; ``0`` lets Ollama decide.

    An explicit user setting is authoritative and returned untouched. On Auto
    a chat model gets its pinned window (see :mod:`src.ollama_residency`), so
    every call finds it loaded as is; a prompt too long for it, and any other
    model, get a window grown to fit ``required_tokens`` -- Ollama otherwise
    defaults to 4096 and rejects longer prompts outright -- bounded both by
    what the model advertises and by :func:`_vram_context_cap`.
    """
    configured = get_ollama_context_length()
    if configured:
        return configured
    pinned = pinned_context_length(model_name)
    needed = int(required_tokens or 0)
    if needed <= 0:
        return pinned
    target = math.ceil(needed * _AUTO_CONTEXT_MARGIN / _AUTO_CONTEXT_STEP) * _AUTO_CONTEXT_STEP
    ceiling = _vram_context_cap()
    model_max = _model_advertised_context_length(model_name)
    if model_max:
        ceiling = min(ceiling, model_max)
    fitted = max(_AUTO_CONTEXT_FLOOR, min(target, max(_AUTO_CONTEXT_FLOOR, ceiling)))
    return max(fitted, pinned)


def _normalize_generation_settings_payload(
    payload: dict[str, Any] | None = None,
) -> dict[str, float]:
    source = payload if isinstance(payload, dict) else {}
    return {
        "temperature": _normalize_temperature(source.get("temperature"), _DEFAULT_TEMPERATURE),
        "repeat_penalty": _normalize_repeat_penalty(
            source.get("repeat_penalty"),
            _DEFAULT_REPEAT_PENALTY,
        ),
        "top_p": _normalize_top_p(source.get("top_p"), _DEFAULT_TOP_P),
    }


def _load_persisted_generation_settings() -> dict[str, float]:
    payload = _load_provider_state()
    repeat_penalty = payload.get("repeat_penalty")
    try:
        saved_before_new_defaults = (
            int(payload.get(_GENERATION_DEFAULTS_VERSION_KEY) or 0) < _GENERATION_DEFAULTS_VERSION
        )
    except (TypeError, ValueError):
        saved_before_new_defaults = True
    if saved_before_new_defaults and repeat_penalty == _OLD_DEFAULT_REPEAT_PENALTY:
        repeat_penalty = None
    return _normalize_generation_settings_payload(
        {
            "temperature": payload.get("temperature"),
            "repeat_penalty": repeat_penalty,
            "top_p": payload.get("top_p"),
        }
    )


def get_generation_settings() -> dict[str, float]:
    with _runtime_lock:
        settings = _runtime_generation_settings or _load_persisted_generation_settings()
    return _normalize_generation_settings_payload(settings)


def set_generation_settings(
    *,
    temperature: Any | None = None,
    repeat_penalty: Any | None = None,
    top_p: Any | None = None,
    persist: bool = True,
) -> dict[str, float]:
    global _runtime_generation_settings
    updated = dict(get_generation_settings())
    if temperature is not None:
        updated["temperature"] = temperature
    if repeat_penalty is not None:
        updated["repeat_penalty"] = repeat_penalty
    if top_p is not None:
        updated["top_p"] = top_p
    normalized = _normalize_generation_settings_payload(updated)
    with _runtime_lock:
        _runtime_generation_settings = normalized
    if persist:
        _save_provider_state(
            {**normalized, _GENERATION_DEFAULTS_VERSION_KEY: _GENERATION_DEFAULTS_VERSION}
        )
    return dict(normalized)


def _resolve_generation_settings(
    *,
    temperature: Any | None = None,
    repeat_penalty: Any | None = None,
    top_p: Any | None = None,
) -> dict[str, float]:
    resolved = dict(get_generation_settings())
    if temperature is not None:
        resolved["temperature"] = temperature
    if repeat_penalty is not None:
        resolved["repeat_penalty"] = repeat_penalty
    if top_p is not None:
        resolved["top_p"] = top_p
    return _normalize_generation_settings_payload(resolved)


def _openai_compatible_sampling_settings(settings: dict[str, float]) -> dict[str, float]:
    sampling = dict(settings)
    if float(sampling.get("temperature", _DEFAULT_TEMPERATURE)) <= 0.0:
        # Mistral rejects top_p < 1 for greedy sampling through OpenAI-compatible APIs.
        sampling["top_p"] = 1.0
    return sampling


def _build_ollama_generation_options(
    *,
    temperature: Any | None = None,
    repeat_penalty: Any | None = None,
    top_p: Any | None = None,
) -> dict[str, float | int]:
    settings = _resolve_generation_settings(
        temperature=temperature,
        repeat_penalty=repeat_penalty,
        top_p=top_p,
    )
    options: dict[str, float | int] = {
        "temperature": settings["temperature"],
        "repeat_penalty": settings["repeat_penalty"],
        "top_p": settings["top_p"],
        "num_predict": _ollama_max_output_tokens(),
    }
    context_length = get_ollama_context_length()
    if context_length:
        options["num_ctx"] = context_length
    return options


def _resolve_llm_model_name(llm: Any) -> str:
    return str(getattr(llm, "model", "") or "").strip()


def _stop_ollama_generation(
    model_name: str | None,
    *,
    abort_signal: StreamAbortSignal | None = None,
    timeout_seconds: float | int | None = 2,
) -> None:
    normalized = _normalize_model_name(model_name)
    if abort_signal is not None:
        abort_signal.abort()
        return
    if normalized:
        unload_ollama_model(normalized, timeout_seconds=timeout_seconds)


def _normalize_runtime_mode(mode: str | None) -> str | None:
    value = str(mode or "").strip().lower()
    if not value:
        return None
    if value == "local":
        return "ollama"
    if value in {"ollama", "lmstudio", "remote", "openai", "openai_compatible"}:
        return value
    return None


def _load_persisted_runtime_mode() -> str | None:
    payload = _load_provider_state()
    mode = _normalize_runtime_mode(payload.get("mode"))
    if mode == "lmstudio" and not _LMSTUDIO_PUBLIC_SELECTION_ENABLED:
        return None
    return mode


def _save_persisted_state_key(key: str, normalized: str | None) -> None:
    """Persiste (ou retire si None) une clé du provider state JSON."""
    with _provider_state_lock:
        if normalized is None:
            payload = _load_provider_state()
            payload.pop(key, None)
            if payload:
                atomic_write_json(_provider_state_path(), payload)
            else:
                try:
                    _provider_state_path().unlink(missing_ok=True)
                except Exception:
                    logger.debug("Unable to remove the provider state file", exc_info=True)
            return
        _save_provider_state({key: normalized})


def _save_persisted_runtime_mode(mode: str | None) -> None:
    _save_persisted_state_key("mode", _normalize_runtime_mode(mode))


def _normalize_base_url(url: str | None) -> str | None:
    value = str(url or "").strip()
    if not value:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("provider URL contains control characters")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("provider URL is malformed") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("provider URL must use http or https")
    if not parsed.hostname:
        raise ValueError("provider URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("provider URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("provider URL must not contain a query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("provider URL must not contain a path")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("provider URL port is invalid")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))


# Alias conservés : les deux providers partagent la même normalisation.
_normalize_ollama_base_url = _normalize_base_url
_normalize_lmstudio_base_url = _normalize_base_url


def _load_persisted_ollama_base_url() -> str | None:
    payload = _load_provider_state()
    return _normalize_base_url(payload.get("ollama_base_url"))


def _save_persisted_ollama_base_url(url: str | None) -> None:
    _save_persisted_state_key("ollama_base_url", _normalize_base_url(url))


def _load_persisted_lmstudio_base_url() -> str | None:
    payload = _load_provider_state()
    return _normalize_base_url(payload.get("lmstudio_base_url"))


def _save_persisted_lmstudio_base_url(url: str | None) -> None:
    _save_persisted_state_key("lmstudio_base_url", _normalize_base_url(url))


def set_runtime_mode(mode: str | None, *, persist: bool = True) -> None:
    """Surcharge le provider à chaud. mode=None réinitialise vers la variable d'env."""
    global _runtime_mode, _fallback_active, _fallback_reason
    with _runtime_lock:
        _runtime_mode = _normalize_runtime_mode(mode)
        _fallback_active = False
        _fallback_reason = ""
    if persist:
        _pin_service_sources()
        _save_persisted_runtime_mode(_runtime_mode)


def set_runtime_ollama_base_url(url: str | None, *, persist: bool = True) -> str:
    global _runtime_ollama_base_url
    normalized = _normalize_ollama_base_url(url) or _DEFAULT_OLLAMA_BASE_URL
    if persist:
        _pin_service_sources()
    with _runtime_lock:
        _runtime_ollama_base_url = normalized
    if persist:
        _save_persisted_ollama_base_url(normalized)
    return normalized


def set_runtime_lmstudio_base_url(url: str | None, *, persist: bool = True) -> str:
    global _runtime_lmstudio_base_url
    normalized = _normalize_lmstudio_base_url(url) or _DEFAULT_LMSTUDIO_BASE_URL
    with _runtime_lock:
        _runtime_lmstudio_base_url = normalized
    if persist:
        _save_persisted_lmstudio_base_url(normalized)
    return normalized


def set_runtime_embedding_model(
    model_name: str | None,
    *,
    persist: bool = True,
    provider_mode: str | None = None,
) -> str:
    global _runtime_ollama_embedding_model
    global _runtime_lmstudio_embedding_model
    global _runtime_remote_embedding_model
    target_provider = _normalize_internal_provider_mode(
        provider_mode or get_embedding_provider_mode()
    )
    normalized = _normalize_model_name(model_name) or required_embedding_model_name()
    with _runtime_lock:
        if target_provider == "lmstudio":
            _runtime_lmstudio_embedding_model = normalized
        elif target_provider == "remote":
            _runtime_remote_embedding_model = normalized
        else:
            _runtime_ollama_embedding_model = normalized
    if persist:
        _save_provider_state({_embedding_state_key(target_provider): normalized})
    return normalized


def get_embedding_source() -> str:
    with _runtime_lock:
        return _runtime_embedding_source


def set_runtime_embedding_source(source: str | None, *, persist: bool = True) -> str:
    global _runtime_embedding_source
    normalized = _normalize_embedding_source(source)
    if normalized is None:
        raise ValueError("embedding source must be 'local' or 'chat'")
    with _runtime_lock:
        _runtime_embedding_source = normalized
    if persist:
        _save_provider_state({_EMBEDDING_SOURCE_KEY: normalized})
    return normalized


def get_document_tools_source() -> str:
    with _runtime_lock:
        return _runtime_document_tools_source


def set_runtime_document_tools_source(source: str | None, *, persist: bool = True) -> str:
    global _runtime_document_tools_source
    normalized = _normalize_embedding_source(source)
    if normalized is None:
        raise ValueError("document tools source must be 'local' or 'chat'")
    with _runtime_lock:
        _runtime_document_tools_source = normalized
    if persist:
        _save_provider_state({_DOCUMENT_TOOLS_SOURCE_KEY: normalized})
    return normalized


def disable_runtime_remote_embeddings(*, persist: bool = True) -> None:
    global _runtime_remote_embedding_model
    with _runtime_lock:
        _runtime_remote_embedding_model = _REMOTE_EMBEDDINGS_DISABLED
    if persist:
        _save_provider_state({_embedding_state_key("remote"): _REMOTE_EMBEDDINGS_DISABLED})


def get_effective_mode() -> str:
    with _runtime_lock:
        runtime_mode = _runtime_mode
    return runtime_mode if runtime_mode is not None else _provider_raw()


def activate_fallback(reason: str) -> None:
    """Bascule en mode local suite à une erreur de connexion distante."""
    global _runtime_mode, _fallback_active, _fallback_reason
    with _runtime_lock:
        _runtime_mode = "ollama"
        _fallback_active = True
        _fallback_reason = reason


def get_fallback_status() -> dict:
    with _runtime_lock:
        return {"active": _fallback_active, "reason": _fallback_reason}


_runtime_mode = _load_persisted_runtime_mode()
_runtime_ollama_base_url = _load_persisted_ollama_base_url()
_runtime_lmstudio_base_url = _load_persisted_lmstudio_base_url()
_runtime_ollama_embedding_model = _load_persisted_embedding_model("ollama")
_runtime_lmstudio_embedding_model = _load_persisted_embedding_model("lmstudio")
_runtime_remote_embedding_model = _load_persisted_embedding_model("remote")
_runtime_embedding_source = _load_persisted_embedding_source()
_runtime_document_tools_source = _load_persisted_document_tools_source()
_runtime_generation_settings = _load_persisted_generation_settings()
_runtime_ollama_context_length = _load_persisted_ollama_context_length()


def can_use_remote() -> bool:
    """Vrai si l'URL et la clé API distantes sont configurées côté serveur."""
    return bool(_remote_base_url()) and bool(_remote_api_key())


def is_remote_connection_error(exc: Exception) -> bool:
    """Détecte si une exception est due à une connexion réseau échouée."""
    err = str(exc).lower()
    keywords = {
        "connection",
        "timeout",
        "refused",
        "unreachable",
        "network",
        "ssl",
        "certificate",
        "remote end closed",
        "connecttimeout",
        "connectionerror",
        "httperror",
        "nodename nor servname",
        "name or service not known",
        "failed to establish",
    }
    return any(kw in err for kw in keywords)


def _normalize_model_name(model_name: str | None) -> str:
    value = str(model_name or "").strip()
    lowered = value.lower().replace("…", "...")
    if lowered in _INVALID_MODEL_VALUES:
        return ""
    return value


def _provider_raw() -> str:
    return os.getenv("LLM_PROVIDER", "ollama").strip().lower() or "ollama"


def is_ollama_provider() -> bool:
    return get_effective_mode() == "ollama"


def is_lmstudio_provider() -> bool:
    return get_effective_mode() == "lmstudio"


def is_remote_provider() -> bool:
    return get_effective_mode() in {"remote", "openai", "openai_compatible"}


def _ollama_base_url() -> str:
    with _runtime_lock:
        runtime_ollama_base_url = _runtime_ollama_base_url
    if runtime_ollama_base_url:
        return runtime_ollama_base_url
    return (
        os.getenv("OLLAMA_BASE_URL", _DEFAULT_OLLAMA_BASE_URL).strip() or _DEFAULT_OLLAMA_BASE_URL
    ).rstrip("/")


def _local_ollama_base_url() -> str:
    """Ollama on this computer, even when the chat uses one on another computer."""
    chat_url = _ollama_base_url()
    if _is_this_computer_url(chat_url):
        return chat_url
    env_url = os.getenv("OLLAMA_BASE_URL", "").strip().rstrip("/")
    if env_url and _is_this_computer_url(env_url):
        return env_url
    return _DEFAULT_OLLAMA_BASE_URL


def _embedding_ollama_base_url() -> str:
    """Ollama that computes the index: this computer's unless indexing follows the chat."""
    if get_embedding_source() == EMBEDDING_SOURCE_CHAT:
        return _ollama_base_url()
    return _local_ollama_base_url()


def document_tools_ollama_base_url() -> str:
    """Ollama that reads scanned pages and figures: this computer's by default."""
    if get_document_tools_source() == DOCUMENT_TOOLS_SOURCE_CHAT:
        return _ollama_base_url()
    return _local_ollama_base_url()


def _lmstudio_base_url() -> str:
    with _runtime_lock:
        runtime_lmstudio_base_url = _runtime_lmstudio_base_url
    if runtime_lmstudio_base_url:
        return runtime_lmstudio_base_url
    return (
        os.getenv("LMSTUDIO_BASE_URL", _DEFAULT_LMSTUDIO_BASE_URL).strip()
        or _DEFAULT_LMSTUDIO_BASE_URL
    ).rstrip("/")


def _lmstudio_api_base_url(base_url: str | None = None) -> str:
    raw = str(base_url or _lmstudio_base_url()).strip().rstrip("/")
    if not raw:
        raw = _DEFAULT_LMSTUDIO_BASE_URL
    parts = urlsplit(raw)
    normalized_path = parts.path.rstrip("/")
    if normalized_path.lower().endswith("/v1"):
        return raw
    if not normalized_path:
        normalized_path = "/v1"
    else:
        normalized_path = normalized_path + "/v1"
    normalized = urlunsplit(
        (parts.scheme, parts.netloc, normalized_path, parts.query, parts.fragment)
    )
    _ensure_local_url_bypasses_proxy(normalized)
    return normalized


def _url_targets_local_network(url: str) -> bool:
    hostname = (urlsplit(str(url or "")).hostname or "").strip().lower()
    if not hostname:
        return False
    if hostname in {"localhost", "host.docker.internal", "gateway.docker.internal"}:
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname.endswith(".local")
    return bool(address.is_private or address.is_loopback or address.is_link_local)


def _ensure_local_url_bypasses_proxy(url: str) -> None:
    if not _url_targets_local_network(url):
        return
    hostname = (urlsplit(str(url or "")).hostname or "").strip()
    if not hostname:
        return
    for env_name in ("NO_PROXY", "no_proxy"):
        entries = [
            entry.strip()
            for entry in str(os.environ.get(env_name) or "").split(",")
            if entry.strip()
        ]
        lowered = {entry.lower() for entry in entries}
        if hostname.lower() not in lowered:
            entries.append(hostname)
            os.environ[env_name] = ",".join(entries)


def _open_request_with_local_proxy_bypass(
    request: urllib.request.Request,
    *,
    timeout: float | int,
    context: ssl.SSLContext | None = None,
):
    if _url_targets_local_network(request.full_url):
        handlers: list[Any] = [urllib.request.ProxyHandler({})]
        if urlsplit(request.full_url).scheme.lower() == "https":
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers).open(
            request,
            timeout=float(timeout),
        )
    return urllib.request.urlopen(
        request,
        timeout=float(timeout),
        context=context,
    )


_REMOTE_URL_BLOCKED_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
}

_REMOTE_URL_BLOCKED_PREFIXES = (
    "169.254.",
    "fe80:",
)


def _is_blocked_remote_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if not host:
        return True
    if host in _REMOTE_URL_BLOCKED_HOSTS:
        return True
    if host.startswith(_REMOTE_URL_BLOCKED_PREFIXES):
        return True
    return False


def _validate_remote_base_url(url: str) -> str:
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"REMOTE_LLM_BASE_URL must use http(s) scheme, got: {parsed.scheme!r}")
    if _is_blocked_remote_host(parsed.hostname or ""):
        raise ValueError(
            f"REMOTE_LLM_BASE_URL hostname not allowed (link-local/metadata): "
            f"{parsed.hostname!r}"
        )
    return raw


class _RedactedSecret(str):
    """A str subclass that hides its value in repr/exception messages."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "'***REDACTED***'"


def _remote_base_url() -> str:
    raw = os.getenv("REMOTE_LLM_BASE_URL", "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        return _validate_remote_base_url(raw)
    except ValueError:
        return ""


def _remote_api_key() -> _RedactedSecret:
    return _RedactedSecret(os.getenv("REMOTE_LLM_API_KEY", "").strip())


def _remote_embedding_model() -> str:
    with _runtime_lock:
        runtime_embedding_model = _runtime_remote_embedding_model
    if runtime_embedding_model == _REMOTE_EMBEDDINGS_DISABLED:
        return ""
    if runtime_embedding_model:
        return runtime_embedding_model
    persisted = _load_persisted_embedding_model("remote")
    if persisted == _REMOTE_EMBEDDINGS_DISABLED:
        return ""
    if persisted:
        return persisted
    return os.getenv("REMOTE_EMBEDDING_MODEL", "").strip()


def get_embedding_provider_mode() -> str:
    if is_remote_provider() and _remote_embedding_model():
        return "remote"
    if is_lmstudio_provider() and get_embedding_source() == EMBEDDING_SOURCE_CHAT:
        return "lmstudio"
    return "ollama"


def embedding_base_url() -> str:
    """Address of the service that computes the index."""
    provider_mode = get_embedding_provider_mode()
    if provider_mode == "remote":
        return _remote_base_url()
    if provider_mode == "lmstudio":
        return _lmstudio_base_url()
    return _embedding_ollama_base_url()


def current_embedding_profile_label(model_name: str | None = None) -> str:
    resolved = _normalize_model_name(model_name) or required_embedding_model_name()
    provider_mode = get_embedding_provider_mode()
    if provider_mode == "lmstudio":
        return f"lmstudio:{resolved}" if resolved else "lmstudio"
    if provider_mode == "remote":
        endpoint = _remote_base_url().strip().rstrip("/")
        endpoint_scope = hashlib.sha256(endpoint.lower().encode("utf-8")).hexdigest()[:10]
        identity = f"remote:{resolved}" if resolved else "remote"
        return f"{identity}@{endpoint_scope}" if endpoint else identity
    return resolved


def required_embedding_model_name() -> str:
    global _runtime_ollama_embedding_model, _runtime_lmstudio_embedding_model
    provider_mode = get_embedding_provider_mode()
    if provider_mode == "remote":
        return _remote_embedding_model()

    configured = ""
    if provider_mode == "lmstudio":
        with _runtime_lock:
            runtime_embedding_model = _runtime_lmstudio_embedding_model
        if runtime_embedding_model:
            configured = runtime_embedding_model
        else:
            persisted = _load_persisted_embedding_model("lmstudio")
            if persisted:
                configured = persisted
            else:
                configured = os.getenv("LMSTUDIO_EMBEDDING_MODEL", "").strip()
        return configured

    with _runtime_lock:
        runtime_embedding_model = _runtime_ollama_embedding_model
    if runtime_embedding_model:
        configured = runtime_embedding_model
    else:
        persisted = _load_persisted_embedding_model("ollama")
        if persisted:
            configured = persisted
        else:
            value = os.getenv(
                "OLLAMA_EMBEDDING_MODEL",
                DEFAULT_OLLAMA_EMBEDDING_MODEL,
            ).strip()
            configured = value or DEFAULT_OLLAMA_EMBEDDING_MODEL

    resolved = resolve_local_ollama_model_name(configured, embeddings_only=True)
    if resolved:
        if resolved != configured:
            with _runtime_lock:
                _runtime_ollama_embedding_model = resolved
            try:
                _save_provider_state({"ollama_embedding_model": resolved})
            except Exception:
                logger.debug("Unable to persist the resolved embedding model", exc_info=True)
        return resolved

    return configured


def _normalize_local_model_name(name: str | None) -> str:
    return str(name or "").strip().lower().split(":", 1)[0]


def _is_embedding_model_candidate(name: str | None) -> bool:
    value = _normalize_local_model_name(name)
    if not value:
        return False
    return bool(re.search(r"(embed|embedding|bge|e5|gte|minilm|nomic|mxbai)", value))


def recommend_local_embedding_model(models: Iterable[str]) -> str:
    """Return the best installed embedding model without overriding a saved choice."""
    candidates = [str(model or "").strip() for model in models]
    candidates = [model for model in candidates if model]
    for preferred in (
        "embeddinggemma",
        "qwen3-embedding",
        "nomic-embed-text-v2-moe",
        "nomic-embed-text",
    ):
        match = next(
            (model for model in candidates if _normalize_local_model_name(model) == preferred),
            "",
        )
        if match:
            return match
    return candidates[0] if candidates else ""


def _is_ocr_model_candidate(name: str | None) -> bool:
    """Return whether a model name identifies a dedicated OCR model."""
    return "ocr" in _normalize_local_model_name(name)


def _is_chat_model_candidate(name: str | None) -> bool:
    value = str(name or "").strip()
    return bool(
        value and not _is_embedding_model_candidate(value) and not _is_ocr_model_candidate(value)
    )


def _filter_chat_model_names(models: Iterable[str]) -> list[str]:
    return [str(model).strip() for model in models if _is_chat_model_candidate(model)]


def list_local_ollama_models(
    ollama_url: str | None = None,
    *,
    embeddings_only: bool = False,
) -> tuple[list[str], str]:
    default_url = _embedding_ollama_base_url() if embeddings_only else _ollama_base_url()
    target = (ollama_url or default_url).rstrip("/")
    try:
        data = _read_ollama_json(target, "/api/tags", timeout=4)
        raw = data.get("models", [])
        sorted_models = sorted(raw, key=lambda model: model.get("size", 0))
        models = [
            str(model.get("name", "")).strip()
            for model in sorted_models
            if isinstance(model, dict) and str(model.get("name", "")).strip()
        ]
        if embeddings_only:
            models = [model for model in models if _is_embedding_model_candidate(model)]
        else:
            models = _filter_chat_model_names(models)
        if not models:
            return [], (
                "Aucun modele d'embedding Ollama installe."
                if embeddings_only
                else "Aucun modele de chat Ollama installe."
            )
        label = (
            "modele(s) d'embedding Ollama detectes"
            if embeddings_only
            else "modele(s) de chat Ollama detectes"
        )
        return models, f"{len(models)} {label}."
    except Exception as exc:
        logger.debug("Recovered from an error in list_local_ollama_models.", exc_info=True)
        return [], _format_ollama_access_error(exc, path="/api/tags")


def list_local_lmstudio_models(
    lmstudio_url: str | None = None,
    *,
    embeddings_only: bool = False,
) -> tuple[list[str], str]:
    target = _lmstudio_api_base_url(lmstudio_url)
    return _list_openai_compatible_models(
        base_url=target,
        api_key="",
        timeout=10,
        ssl_verify=True,
        label="LM Studio",
        embeddings_only=embeddings_only,
    )


def list_remote_embedding_models() -> tuple[list[str], str]:
    ok, msg = _validate_remote_config()
    if not ok:
        return [], msg
    return _list_openai_compatible_models(
        base_url=_remote_base_url(),
        api_key=_remote_api_key(),
        timeout=_remote_model_list_timeout(),
        ssl_verify=_remote_httpx_verify(),
        label="API distante",
        embeddings_only=True,
    )


def resolve_local_lmstudio_model_name(
    model_name: str | None,
    lmstudio_url: str | None = None,
) -> str:
    requested = str(model_name or "").strip()
    if not requested:
        return ""
    models, _ = list_local_lmstudio_models(lmstudio_url, embeddings_only=False)
    if requested in models:
        return requested
    wanted = _normalize_local_model_name(requested)
    for model in models:
        if _normalize_local_model_name(model) == wanted:
            return model
    return ""


def resolve_local_ollama_model_name(
    model_name: str | None,
    ollama_url: str | None = None,
    *,
    embeddings_only: bool = False,
) -> str:
    requested = str(model_name or "").strip()
    if not requested:
        return ""
    models, _ = list_local_ollama_models(ollama_url, embeddings_only=embeddings_only)
    if requested in models:
        return requested
    wanted = _normalize_local_model_name(requested)
    for model in models:
        if _normalize_local_model_name(model) == wanted:
            return model
    return ""


def _is_transient_ollama_probe_error(detail: str | Exception | None) -> bool:
    value = str(detail or "").strip().lower()
    if not value:
        return False
    transient_markers = (
        "timed out",
        "timeout",
        "connection reset",
        "connection aborted",
        "connection refused",
        "remote end closed",
        "temporarily unavailable",
        "name or service not known",
        "failed to establish",
        "unreachable",
    )
    return any(marker in value for marker in transient_markers)


def validate_local_embedding_model(
    model_name: str | None,
    ollama_url: str | None = None,
    *,
    probe_timeout_seconds: float | int = 20,
) -> tuple[bool, str, str]:
    probe_timeout = max(2.0, min(60.0, float(probe_timeout_seconds)))
    if get_embedding_provider_mode() == "lmstudio":
        target = _lmstudio_api_base_url(ollama_url)
        requested = str(model_name or "").strip()
        resolved = resolve_local_lmstudio_model_name(requested, target)
        if not resolved:
            available_models, status = list_local_lmstudio_models(target, embeddings_only=True)
            if not available_models:
                return (
                    False,
                    "",
                    (
                        status
                        or "Aucun modele d'embedding LM Studio detecte. "
                        "Chargez un modele d'embedding dans la page Developer de LM Studio "
                        "ou avec `lms load`, puis selectionnez-le dans Bibliobrain."
                    ),
                )
            return (
                False,
                "",
                (
                    f"Modele d'embedding introuvable dans LM Studio: {requested}. "
                    "Chargez-le dans LM Studio ou selectionnez un modele detecte."
                ),
            )
        try:
            data = _open_url_json(
                _join_url(target, "/embeddings"),
                method="POST",
                json_payload={"model": resolved, "input": ["ping"]},
                headers=_openai_compatible_headers(_DEFAULT_LMSTUDIO_API_KEY),
                timeout=probe_timeout,
                ssl_verify=True,
            )
            items = data.get("data", [])
            if isinstance(items, list) and items:
                first = items[0] if isinstance(items[0], dict) else {}
                if isinstance(first.get("embedding"), list) and first.get("embedding"):
                    return True, resolved, ""
            return (
                False,
                resolved,
                f"Reponse embeddings invalide renvoyee par LM Studio pour {resolved}.",
            )
        except urllib.error.HTTPError as exc:
            detail = _extract_openai_compatible_error_message(_read_http_error_payload(exc))
            if "no models loaded" in detail.lower():
                return (
                    False,
                    resolved,
                    (
                        f"LM Studio a refuse la requete embeddings pour {resolved} (HTTP {exc.code}). "
                        "Aucun modele n'est actuellement charge pour l'API embeddings. "
                        "Chargez un modele d'embedding dans la page Developer de LM Studio "
                        "ou avec `lms load`, puis selectionnez-le dans Bibliobrain."
                    ),
                )
            return (
                False,
                resolved,
                (
                    f"LM Studio a refuse la requete embeddings pour {resolved} (HTTP {exc.code})."
                    + (f" Detail: {detail}" if detail else "")
                ),
            )
        except Exception as exc:
            logger.debug(
                "Recovered from an error in validate_local_embedding_model.", exc_info=True
            )
            detail = str(exc).strip() or "erreur inconnue"
            if _is_transient_ollama_probe_error(detail):
                return (
                    True,
                    resolved,
                    f"Validation LM Studio incertaine pour {resolved}. Detail: {detail}",
                )
            return (
                False,
                resolved,
                f"Validation LM Studio impossible pour {resolved}. Detail: {detail}",
            )

    target = (ollama_url or _embedding_ollama_base_url()).rstrip("/")
    resolved = resolve_local_ollama_model_name(
        model_name,
        target,
        embeddings_only=True,
    )
    if not resolved:
        requested = str(model_name or "").strip()
        return (
            False,
            "",
            (
                f"Modele d'embedding introuvable dans Ollama: {requested}. "
                "Installez-le ou selectionnez un modele d'embedding detecte."
            ),
        )

    attempts = [
        ("/api/embed", {"model": resolved, "input": ["ping"]}),
        ("/api/embeddings", {"model": resolved, "prompt": "ping"}),
    ]
    errors: list[str] = []
    transient_failures = 0
    hard_failures = 0
    for path, payload in attempts:
        try:
            with _open_ollama_url(
                _join_url(target, path),
                method="POST",
                json_payload=cast(dict[str, Any], payload),
                timeout=probe_timeout,
            ) as response:
                data = json.loads(response.read() or b"{}")
            if isinstance(data, dict) and (
                (isinstance(data.get("embeddings"), list) and data.get("embeddings"))
                or (isinstance(data.get("embedding"), list) and data.get("embedding"))
            ):
                return True, resolved, ""
            errors.append(f"{path}: reponse embed vide ou invalide")
            hard_failures += 1
        except urllib.error.HTTPError as exc:
            detail = _format_ollama_access_error(exc, path=path)
            errors.append(detail)
            if _is_transient_ollama_probe_error(detail):
                transient_failures += 1
            else:
                hard_failures += 1
        except Exception as exc:
            logger.debug(
                "Recovered from an error in validate_local_embedding_model.", exc_info=True
            )
            detail = f"{path}: {exc}"
            errors.append(detail)
            if _is_transient_ollama_probe_error(exc):
                transient_failures += 1
                break
            else:
                hard_failures += 1

    detail = " | ".join(part for part in errors if part) or "probe embedding echouee"
    if transient_failures > 0 and hard_failures == 0:
        return (
            True,
            resolved,
            (
                f"Validation Ollama incertaine pour le modele d'embedding {resolved}. "
                f"Detail: {detail}"
            ),
        )
    return (
        False,
        resolved,
        (
            f"Modele d'embedding detecte mais non utilisable via Ollama: {resolved}. "
            f"Detail: {detail}"
        ),
    )


def validate_remote_embedding_model(
    model_name: str | None,
    *,
    probe_timeout_seconds: float | int = 20,
) -> tuple[bool, str, str]:
    requested = str(model_name or "").strip()
    if not requested:
        return False, "", "Modele d'embedding distant invalide."
    ok, msg = _validate_remote_config()
    if not ok:
        return False, requested, msg
    probe_timeout = max(2.0, min(60.0, float(probe_timeout_seconds)))
    try:
        data = _open_url_json(
            _join_url(_remote_base_url(), "/embeddings"),
            method="POST",
            json_payload={"model": requested, "input": ["ping"]},
            headers=_remote_headers(),
            timeout=probe_timeout,
            ssl_verify=_remote_httpx_verify(),
        )
        items = data.get("data", [])
        if isinstance(items, list) and items:
            first = items[0] if isinstance(items[0], dict) else {}
            embedding = first.get("embedding")
            if isinstance(embedding, list) and embedding:
                return True, requested, ""
        return (
            False,
            requested,
            f"Reponse embeddings invalide renvoyee par l'API distante pour {requested}.",
        )
    except urllib.error.HTTPError as exc:
        detail = _extract_openai_compatible_error_message(_read_http_error_payload(exc))
        suffix = f" : {detail}" if detail else ""
        return (
            False,
            requested,
            f"Le modele {requested} ne peut pas fournir d'embeddings (HTTP {exc.code}){suffix}.",
        )
    except Exception as exc:
        logger.debug("Recovered from an error in validate_remote_embedding_model.", exc_info=True)
        return (
            False,
            requested,
            f"Verification du modele d'embedding distant impossible : {_format_llm_exception(exc)}",
        )


def ensure_internal_embedding_model() -> tuple[str, str]:
    provider_mode = get_embedding_provider_mode()
    if provider_mode == "remote":
        return _remote_embedding_model(), ""

    configured = required_embedding_model_name()
    ok, resolved, message = validate_local_embedding_model(configured)
    if ok and resolved:
        if resolved != configured:
            try:
                set_runtime_embedding_model(resolved)
            except Exception:
                logger.debug("Unable to apply the resolved embedding model", exc_info=True)
        return resolved, message

    if provider_mode == "lmstudio":
        models, status = list_local_lmstudio_models(embeddings_only=True)
    else:
        models, status = list_local_ollama_models(embeddings_only=True)
    if models:
        selected = recommend_local_embedding_model(models)
        try:
            selected = set_runtime_embedding_model(selected)
        except Exception:
            logger.debug("Unable to apply the selected embedding model", exc_info=True)
        return selected, status
    return configured, message or status


def requires_local_ollama_embeddings() -> bool:
    return get_embedding_provider_mode() == "ollama"


def _remote_ssl_verify() -> bool:
    return os.getenv("REMOTE_SSL_VERIFY", "true").strip().lower() not in {"false", "0", "no"}


def _remote_httpx_verify() -> str | bool:
    if not _remote_ssl_verify():
        return False
    custom_ca_path = os.environ.get("NODE_EXTRA_CA_CERTS", "").strip()
    if custom_ca_path and os.path.exists(custom_ca_path):
        return custom_ca_path
    return True


def get_secure_http_client():
    verify = _remote_httpx_verify()
    if verify is True:
        return None
    return httpx.Client(verify=verify)


def _remote_timeout() -> int:
    value = os.getenv("REMOTE_TIMEOUT_SECONDS", "45").strip()
    if value.isdigit():
        return max(10, min(300, int(value)))
    return 45


def _remote_model_list_timeout() -> int:
    value = os.getenv("REMOTE_MODEL_LIST_TIMEOUT_SECONDS", "5").strip()
    try:
        seconds = int(value)
    except ValueError:
        seconds = 5
    return max(2, min(_remote_timeout(), seconds))


def _model_list_cache_ttl_seconds() -> int:
    value = os.getenv("MODEL_LIST_CACHE_TTL_SECONDS", "300").strip()
    try:
        seconds = int(value)
    except ValueError:
        seconds = 300
    return max(0, min(86400, seconds))


def _model_list_cache_key(ollama_url: str | None = None) -> str:
    if is_remote_provider():
        return f"remote:{_remote_base_url()}"
    if is_lmstudio_provider():
        return f"lmstudio:{_lmstudio_api_base_url(ollama_url)}"
    return f"ollama:{(ollama_url or _ollama_base_url()).rstrip('/')}"


def _load_cached_model_list(
    cache_key: str,
    *,
    allow_stale: bool = False,
) -> tuple[list[str], str] | None:
    cache = _load_provider_state().get(_MODEL_LIST_CACHE_STATE_KEY)
    if not isinstance(cache, dict):
        return None
    entry = cache.get(cache_key)
    if not isinstance(entry, dict):
        return None
    models = [
        str(model or "").strip() for model in entry.get("models", []) if str(model or "").strip()
    ]
    # Old cache entries may predate chat-model classification. Filtering on
    # every read prevents an OCR/embedding model from leaking back into the UI
    # while the provider is temporarily unreachable.
    models = _filter_chat_model_names(models)
    if not models:
        return None
    try:
        cached_at = float(entry.get("cached_at") or 0.0)
    except (TypeError, ValueError):
        cached_at = 0.0
    if not allow_stale and time.time() - cached_at > _model_list_cache_ttl_seconds():
        return None
    status = str(entry.get("status") or "").strip()
    return models, status


def _save_cached_model_list(cache_key: str, models: list[str], status: str) -> None:
    if not models:
        return
    with _runtime_lock:
        state_payload = _load_provider_state()
        cache = state_payload.get(_MODEL_LIST_CACHE_STATE_KEY)
        if not isinstance(cache, dict):
            cache = {}
        cache[cache_key] = {
            "models": list(models),
            "status": str(status or ""),
            "cached_at": time.time(),
        }
        _save_provider_state({_MODEL_LIST_CACHE_STATE_KEY: cache})


def _cached_model_fallback(
    cache_key: str,
    status: str,
) -> tuple[list[str], str]:
    cached = _load_cached_model_list(cache_key, allow_stale=True)
    if cached is None:
        return [], status
    models, _ = cached
    return models, f"{status} Derniere liste connue utilisee."


def _ollama_timeout() -> int:
    value = os.getenv("OLLAMA_TIMEOUT_SECONDS", "600").strip()
    if value.isdigit():
        return max(30, min(3600, int(value)))
    return 600


def _ollama_max_output_tokens() -> int:
    value = str(
        os.getenv("OLLAMA_MAX_OUTPUT_TOKENS", str(_DEFAULT_OLLAMA_MAX_OUTPUT_TOKENS)) or ""
    ).strip()
    if value.isdigit():
        return max(32, min(4096, int(value)))
    return _DEFAULT_OLLAMA_MAX_OUTPUT_TOKENS


def _ollama_watchdog_seconds() -> float:
    value = str(
        os.getenv("OLLAMA_WATCHDOG_SECONDS", str(_DEFAULT_OLLAMA_WATCHDOG_SECONDS)) or ""
    ).strip()
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return _DEFAULT_OLLAMA_WATCHDOG_SECONDS
    return max(10.0, min(600.0, seconds))


def _ollama_request_watchdog_seconds() -> int:
    return max(15, min(_ollama_timeout(), int(round(_ollama_watchdog_seconds() + 15.0))))


def _join_url(base: str, path: str) -> str:
    return base.rstrip("/") + "/" + path.lstrip("/")


def _ollama_ssl_verify() -> bool:
    return os.getenv("OLLAMA_SSL_VERIFY", "true").strip().lower() not in {"false", "0", "no"}


def _ollama_httpx_verify() -> str | bool:
    custom_ca_path = os.environ.get("NODE_EXTRA_CA_CERTS", "").strip()
    if custom_ca_path:
        if not os.path.isfile(custom_ca_path):
            raise ValueError("NODE_EXTRA_CA_CERTS does not point to a readable CA file")
        return custom_ca_path
    return _ollama_ssl_verify()


def _ollama_ssl_context():
    verify_value = _ollama_httpx_verify()
    if verify_value is True:
        return None
    if isinstance(verify_value, str):
        return ssl.create_default_context(cafile=verify_value)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _ollama_auth_header() -> str:
    explicit = os.getenv("OLLAMA_AUTH_HEADER", "").strip()
    if explicit:
        return explicit
    api_key = os.getenv("OLLAMA_API_KEY", "").strip()
    if not api_key:
        return ""
    scheme = os.getenv("OLLAMA_AUTH_SCHEME", "Bearer").strip() or "Bearer"
    return f"{scheme} {api_key}"


def _ollama_headers(*, include_json_content_type: bool = False) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": _OLLAMA_USER_AGENT,
    }
    if include_json_content_type:
        headers["Content-Type"] = "application/json"
    auth_header = _ollama_auth_header()
    if auth_header:
        headers["Authorization"] = auth_header
    return headers


def _clean_ollama_url_and_extract_auth(url: str) -> tuple[str, dict[str, str]]:
    value = str(url or "").strip()
    if not value:
        return "", {}
    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc:
        return value.rstrip("/"), {}
    auth_headers: dict[str, str] = {}
    if parsed.username is not None:
        username = unquote(parsed.username)
        password = unquote(parsed.password or "")
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        auth_headers["Authorization"] = f"Basic {token}"
    hostname = parsed.hostname or ""
    if not hostname:
        return value.rstrip("/"), auth_headers
    netloc = hostname
    if ":" in hostname and not hostname.startswith("["):
        netloc = f"[{hostname}]"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    cleaned = urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    ).rstrip("/")
    return cleaned, auth_headers


def _format_ollama_access_error(exc: Exception, *, path: str = "") -> str:
    if isinstance(exc, urllib.error.HTTPError):
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            logger.debug("Recovered from an error in _format_ollama_access_error.", exc_info=True)
            detail = ""
        endpoint = f" sur {path}" if path else ""
        if exc.code in {401, 403}:
            hint = (
                "Le serveur HTTPS refuse l'acces a l'API Ollama native"
                f"{endpoint}. Verifiez l'authentification (OLLAMA_AUTH_HEADER, "
                "OLLAMA_API_KEY ou URL avec user:pass@host). "
                "Si ce serveur expose plutot une API OpenAI-compatible, utilisez "
                "REMOTE_LLM_BASE_URL en mode provider distant."
            )
            if detail:
                hint = f"{hint} Detail: {detail[:240]}"
            return f"Ollama inaccessible : HTTP {exc.code} ({exc.reason}). {hint}"
        if detail:
            return f"Ollama inaccessible : HTTP {exc.code} ({exc.reason}). Detail: {detail[:240]}"
        return f"Ollama inaccessible : HTTP {exc.code} ({exc.reason})"
    return f"Ollama inaccessible : {exc}"


def _open_ollama_url(
    url: str,
    *,
    method: str = "GET",
    json_payload: dict[str, Any] | None = None,
    timeout: float | int = 4,
):
    cleaned_url, url_auth_headers = _clean_ollama_url_and_extract_auth(url)
    headers = _ollama_headers(include_json_content_type=json_payload is not None)
    for key, value in url_auth_headers.items():
        headers.setdefault(key, value)
    request = urllib.request.Request(
        cleaned_url,
        data=(json.dumps(json_payload).encode("utf-8") if json_payload is not None else None),
        headers=headers,
        method=method.upper(),
    )
    context = _ollama_ssl_context()
    return _open_request_with_local_proxy_bypass(
        request,
        timeout=timeout,
        context=context,
    )


def _read_ollama_json(
    base_url: str,
    path: str,
    *,
    timeout: float | int = 4,
) -> dict[str, Any]:
    with _open_ollama_url(_join_url(base_url, path), timeout=timeout) as response:
        data = json.loads(response.read() or b"{}")
    return data if isinstance(data, dict) else {}


def _ollama_client_kwargs() -> dict[str, Any]:
    return {"headers": _ollama_headers()}


def strip_thinking_blocks(text: str | None) -> str:
    value = str(text or "")
    if not value:
        return ""
    value = re.sub(r"<think>[\s\S]*?</think>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<think>[\s\S]*$", "", value, flags=re.IGNORECASE)
    # A closing tag left without its opener means the draft was streamed as
    # plain content (seen with granite4.2 under think=false): keep the final part.
    value = re.sub(r"^[\s\S]*</think>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<\|?channel\|?>thought[\s\S]*?<channel\|>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<\|?channel\|?>thought[\s\S]*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"</?thinking>", "", value, flags=re.IGNORECASE)
    value = value.replace("<|think|>", "")
    value = value.replace("<channel|>", "")
    value = re.sub(r"\[\s*S\s*['’]\s*(\d+)\s*\]", r"[S\1]", value, flags=re.IGNORECASE)
    value = _strip_visible_drafting_sections(value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def _strip_visible_drafting_sections(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""
    final_polish = re.search(r"(?im)^\s*(?:\*+\s*)?Final\s+Polish\s*:\s*$", value)
    if final_polish:
        tail = value[final_polish.end() :].strip()
        if re.search(r"(?im)^\s*\**\s*Answer\s*:\s*\**", tail):
            value = tail

    draft_marker = re.search(
        r"(?im)^\s*(?:\*+\s*)?(?:Self[- ]Correction(?:\s+during\s+drafting)?|Refined\s+Explanation|"
        r"Check\s+citations|Check\s+structure|Check\s+language|Check\s+constraints)\s*:",
        value,
    )
    if draft_marker:
        value = value[: draft_marker.start()].strip()

    wait_marker = re.search(r"(?im)^\s*\*?\s*Wait\s*,", value)
    if wait_marker:
        value = value[: wait_marker.start()].strip()
    return value


def sanitize_assistant_content(
    text: str | None,
    *,
    thinking_enabled: bool,
) -> str:
    value = str(text or "")
    if not value:
        return ""
    return value if thinking_enabled else strip_thinking_blocks(value)


def resolve_chat_think_mode(
    model_name: str | None,
    thinking_enabled: bool,
) -> bool | str | None:
    return resolve_ollama_think_request(model_name, thinking_enabled)


def merge_thinking_and_content(thinking: str | None, content: str | None) -> str:
    thinking_text = str(thinking or "").strip()
    content_text = str(content or "").strip()
    if thinking_text and content_text:
        return f"<think>\n{thinking_text}\n</think>\n\n{content_text}"
    if thinking_text:
        return f"<think>\n{thinking_text}\n</think>"
    return content_text


def estimate_model_size_b(model_name: str | None) -> float | None:
    raw = str(model_name or "").strip().lower()
    if not raw:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*b\b", raw)
    if not match:
        return None
    try:
        return float(match.group(1))
    except Exception:
        logger.debug("Recovered from an error in estimate_model_size_b.", exc_info=True)
        return None


def is_tiny_model(model_name: str | None, *, threshold_b: float = 1.5) -> bool:
    size_b = estimate_model_size_b(model_name)
    return bool(size_b is not None and size_b <= float(threshold_b))


def _ollama_request_payload(
    payload: dict[str, Any],
    *,
    think: bool | str | None = None,
    options_override: dict[str, Any] | None = None,
) -> bytes:
    body = dict(payload)
    think_value = think
    model_name = str(body.get("model") or "").strip().lower()
    if think is True and model_name.startswith("gpt-oss"):
        think_value = "medium"
    if options_override:
        options = dict(body.get("options") or {})
        options.update(options_override)
        body["options"] = options
    if think_value is False:
        body["think"] = False
    elif think_value not in (None, ""):
        body["think"] = think_value
    _keep_chat_model_resident(body)
    return json.dumps(body).encode("utf-8")


def _keep_chat_model_resident(body: dict[str, Any]) -> None:
    """One window per chat model, and a long keep-alive, on every generation.

    A planner or title call without ``num_ctx`` used to load the model at
    Ollama's default window and the next answer reloaded it (about 3 s each
    way). Unload requests (``keep_alive: 0``) and non-generation calls such as
    ``/api/show`` are left alone.
    """
    if body.get("keep_alive") == 0 or ("messages" not in body and "prompt" not in body):
        return
    model = str(body.get("model") or "")
    pinned = pinned_context_length(model)
    if pinned:
        options = dict(body.get("options") or {})
        options["num_ctx"] = max(int(options.get("num_ctx") or 0), pinned)
        body["options"] = options
    if "keep_alive" not in body and _is_chat_model_candidate(model):
        body["keep_alive"] = keep_alive_seconds()


def _prepare_ollama_chat_messages_for_thinking(
    model_name: str | None,
    messages: list[dict[str, str]],
    think: bool | str | None,
) -> list[dict[str, str]]:
    # Ollama's native ``think`` request field is the source of truth. Injecting
    # model-specific control tokens can consume the output budget without ever
    # producing final content on otherwise compatible templates.
    prepared = [dict(message) for message in list(messages or [])]
    if resolve_model_family(model_name) != "qwen3.5" or think is not False:
        return prepared

    directive = "/no_think"
    compact_rules = (
        "Answer directly using only the supplied context. "
        "Reuse exact technical names. "
        "Keep source labels exactly as [S1], [S2], etc. "
        "When two sources support the answer, cite both. "
        "If the requested comparison is absent, say it cannot be determined."
    )
    for message in prepared:
        role = str(message.get("role") or "").strip().lower()
        content = str(message.get("content") or "")
        if role == "system" and "Contexte :" in content:
            context = content.rsplit("Contexte :", 1)[-1].strip()
            message["content"] = f"{compact_rules}\n\nContext:\n{context}"
        elif role == "user":
            for marker in (
                "\n\nStyle de reponse a respecter:",
                "\n\nResponse style rules:",
            ):
                if marker in content:
                    content = content.split(marker, 1)[0].rstrip()
                    break
            message["content"] = content

    for message in reversed(prepared):
        if str(message.get("role") or "").strip().lower() != "user":
            continue
        content = str(message.get("content") or "").rstrip()
        if directive not in content.lower():
            message["content"] = f"{content}\n\n{compact_rules}\n{directive}".strip()
        return prepared
    return [*prepared, {"role": "user", "content": directive}]


_CITATION_INFERENCE_STOPWORDS = frozenset(
    {
        "about", "after", "also", "avec", "been", "being", "between", "both", "cette", "comme",
        "context", "dans", "does", "each", "elle", "entre", "from", "have", "into", "leur",
        "leurs", "mais", "more", "most", "much", "nous", "only", "other", "plus", "pour",
        "same", "sont", "source", "sources", "such", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "those", "through", "under", "very", "were", "what",
        "when", "where", "which", "while", "with", "within", "would",
    }
)


def _citation_inference_terms(text: str) -> set[str]:
    return {
        term
        for term in re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split()
        if len(term) >= 4 and term not in _CITATION_INFERENCE_STOPWORDS
    }


def _qwen35_allowed_labels(messages: list[dict[str, str]]) -> set[int]:
    return {
        int(match.group(1))
        for message in messages
        for match in re.finditer(r"\[S(\d+)\]", str(message.get("content") or ""), re.IGNORECASE)
    }


def _repair_qwen35_citation_labels(value: str, allowed: set[int]) -> str:
    """``[1]``, ``[[S1]]`` and "source S1" become ``[S1]`` when S1 exists."""

    def _replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return f"[S{index}]" if index in allowed else match.group(0)

    value = re.sub(r"\[(\d+)\]", _replace, value)
    value = re.sub(r"\[\[S(\d+)\]\]", r"[S\1]", value, flags=re.IGNORECASE)
    return re.sub(
        r"\b(?:source|citation)\s+S(\d+)\b",
        lambda match: (
            f"[S{int(match.group(1))}]" if int(match.group(1)) in allowed else match.group(0)
        ),
        value,
        flags=re.IGNORECASE,
    )


def _normalize_qwen35_citation_labels(
    text: str | None,
    messages: list[dict[str, str]],
) -> str:
    value = str(text or "")
    allowed = _qwen35_allowed_labels(messages)
    if not allowed:
        return value
    value = _repair_qwen35_citation_labels(value, allowed)

    combined = "\n".join(str(message.get("content") or "") for message in messages)
    source_matches = list(
        re.finditer(
            r"\[S(\d+)\]\s*(.*?)(?=\n\s*\[S\d+\]|\Z)",
            combined,
            flags=re.IGNORECASE | re.DOTALL,
        )
    )
    answer_terms = _citation_inference_terms(value)
    terms_by_source = {
        int(match.group(1)): _citation_inference_terms(match.group(2)) for match in source_matches
    }
    inferred: list[str] = []
    for index, source_terms in terms_by_source.items():
        label = f"[S{index}]"
        if index not in allowed or re.search(re.escape(label), value, re.IGNORECASE):
            continue
        # Only words no other source has: two shared words used to be enough,
        # so any passage holding "which" and "magnetic" was cited.
        other_terms = set().union(
            *(terms for other, terms in terms_by_source.items() if other != index)
        )
        if len((source_terms - other_terms) & answer_terms) >= 2:
            inferred.append(label)
    if inferred:
        value = f"{value.rstrip()} {' '.join(dict.fromkeys(inferred))}".strip()
    return value


# A word that can still turn into a label once its number arrives.
_PENDING_CITATION_WORD_RE = re.compile(r"\b(?:source|citation)\s*$", re.IGNORECASE)


class _Qwen35CitationStream:
    """Streams qwen3.5 text with its citation labels repaired as they arrive.

    The repairs change the text ("[1]" becomes "[S1]"), so only what can no
    longer change is released: nothing from the first unclosed bracket on,
    and not the last word, which may still grow ("S1" into "S12") or be a
    "source" about to become a label. The labels inferred from the whole
    answer come last, in :meth:`finish`. Leading and trailing blanks are
    held back as well, so the released text stays a prefix of the final one.
    """

    def __init__(self, messages: list[dict[str, str]]) -> None:
        self.messages = messages
        self.allowed = _qwen35_allowed_labels(messages)
        self.raw = ""
        self.sent = ""

    def push(self, piece: str) -> str:
        self.raw += piece
        stable = self.raw[: self._stable_end()]
        if self.allowed:
            stable = _repair_qwen35_citation_labels(stable, self.allowed)
        return self._release(stable.strip())

    def finish(self) -> tuple[str, str]:
        """``(delta, full)``: the rest to send, and the whole repaired answer."""
        full = _normalize_qwen35_citation_labels(self.raw, self.messages).lstrip()
        return self._release(full), full

    def _stable_end(self) -> int:
        end = len(self.raw)
        open_brackets: list[int] = []
        for position, char in enumerate(self.raw):
            if char == "[":
                open_brackets.append(position)
            elif char == "]" and open_brackets:
                open_brackets.pop()
        if open_brackets:
            end = open_brackets[0]
        boundary = max(self.raw.rfind(" ", 0, end), self.raw.rfind("\n", 0, end))
        end = boundary + 1 if boundary >= 0 else 0
        pending = _PENDING_CITATION_WORD_RE.search(self.raw, 0, end)
        return pending.start() if pending else end

    def _release(self, text: str) -> str:
        if not text.startswith(self.sent):
            return ""
        delta = text[len(self.sent) :]
        self.sent = text
        return delta


def _open_ollama_request(
    path: str,
    payload: dict[str, Any],
    *,
    think: bool | str | None = None,
    options_override: dict[str, Any] | None = None,
    timeout_seconds: float | int | None = None,
    base_url: str | None = None,
):
    timeout_value = (
        _ollama_timeout() if timeout_seconds is None else max(5, min(3600, int(timeout_seconds)))
    )
    return _open_ollama_url(
        _join_url(base_url or _ollama_base_url(), path),
        method="POST",
        json_payload=json.loads(
            _ollama_request_payload(payload, think=think, options_override=options_override)
        ),
        timeout=timeout_value,
    )


def unload_ollama_model(
    model_name: str,
    *,
    timeout_seconds: float | int | None = 5,
    base_url: str | None = None,
) -> bool:
    """Free a model's memory on the chat's Ollama, or on ``base_url`` when given."""
    normalized = _normalize_model_name(model_name)
    if not normalized:
        return False
    payload = {
        "model": normalized,
        "prompt": "",
        "stream": False,
        "keep_alive": 0,
    }
    try:
        with _open_ollama_request(
            "/api/generate",
            payload,
            timeout_seconds=timeout_seconds,
            base_url=base_url,
        ) as response:
            response.read()
        return True
    except Exception:
        logger.debug("Recovered from an error in unload_ollama_model.", exc_info=True)
        return False


# A load this long means the model was (re)loaded, not just found in memory.
_RELOAD_THRESHOLD_MS = 500.0


def ollama_timings(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Load, prompt and decode figures of a finished Ollama call.

    Ollama reports them on its last message only; ``{}`` when they are absent
    (another provider, or a message that is not the last one).
    """
    data = payload if isinstance(payload, dict) else {}
    if "load_duration" not in data and "eval_count" not in data:
        return {}

    def _ms(key: str) -> float:
        try:
            return round(float(data.get(key) or 0) / 1e6, 1)
        except (TypeError, ValueError):
            return 0.0

    def _count(key: str) -> int:
        try:
            return int(data.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    load_ms = _ms("load_duration")
    decode_ms = _ms("eval_duration")
    output_tokens = _count("eval_count")
    return {
        "load_ms": load_ms,
        "reloaded": load_ms >= _RELOAD_THRESHOLD_MS,
        "prompt_tokens": _count("prompt_eval_count"),
        "prefill_ms": _ms("prompt_eval_duration"),
        "output_tokens": output_tokens,
        "decode_ms": decode_ms,
        "tokens_per_second": round(output_tokens * 1000 / decode_ms, 1) if decode_ms else 0.0,
    }


def stream_ollama_chat(
    model_name: str,
    messages: list[dict[str, str]],
    *,
    think: bool | str | None = None,
    temperature: float | None = None,
    repeat_penalty: float | None = None,
    top_p: float | None = None,
    abort_signal: StreamAbortSignal | None = None,
    options_override: dict[str, Any] | None = None,
    format_schema: dict[str, Any] | str | None = None,
    request_timeout_seconds: float | None = None,
) -> Iterable[dict[str, Any]]:
    """Stream an Ollama chat answer.

    ``request_timeout_seconds`` bounds the HTTP request. qwen3.5 answers get
    their citation labels repaired on the way (see
    :class:`_Qwen35CitationStream`); their last message carries
    ``content_replace`` in the unexpected case where the repaired whole
    answer does not extend what was already streamed.
    """
    normalized_model_name = _normalize_model_name(model_name)
    request_timeout = request_timeout_seconds or _ollama_request_watchdog_seconds()
    # They used to be fetched whole for this repair, so the first word only
    # showed once the answer was complete and Stop had to unload the model.
    citation_stream = (
        _Qwen35CitationStream(messages)
        if resolve_model_family(normalized_model_name) == "qwen3.5" and format_schema is None
        else None
    )

    prepared_messages = _prepare_ollama_chat_messages_for_thinking(
        normalized_model_name,
        messages,
        think,
    )
    payload = {
        "model": normalized_model_name,
        "messages": prepared_messages,
        "stream": True,
        "options": _build_ollama_generation_options(
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            top_p=top_p,
        ),
    }
    if format_schema is not None:
        payload["format"] = format_schema

    if abort_signal is not None:
        abort_signal.throw_if_aborted()
    response = _open_ollama_request(
        "/api/chat",
        payload,
        think=think,
        options_override=options_override,
        timeout_seconds=request_timeout,
    )
    watchdog_seconds = _ollama_watchdog_seconds()
    watchdog_deadline = time.monotonic() + watchdog_seconds
    if abort_signal is not None:

        def _close_stream() -> None:
            # Closing the connection is enough: Ollama stops generating
            # (measured on 0.40.1: the next request is served at once). The
            # model stays loaded, so the next question does not reload it.
            try:
                response.close()
            except Exception:
                logger.debug("Unable to close the Ollama streaming response", exc_info=True)

        abort_signal.register_close_callback(_close_stream)

    try:
        with response:
            while True:
                if abort_signal is not None:
                    abort_signal.throw_if_aborted()
                if time.monotonic() >= watchdog_deadline:
                    _stop_ollama_generation(normalized_model_name, abort_signal=abort_signal)
                    raise TimeoutError(
                        "Generation Ollama interrompue par guardrail "
                        f"(>{watchdog_seconds:.0f}s)."
                    )
                try:
                    raw = response.readline()
                except Exception:
                    if abort_signal is not None and abort_signal.is_aborted():
                        raise InterruptedError("Generation aborted.")
                    raise
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                chunk = json.loads(line)
                error = str(chunk.get("error") or "").strip()
                if error:
                    raise RuntimeError(error)

                message = chunk.get("message")
                content = ""
                thinking = ""
                if isinstance(message, dict):
                    content = str(message.get("content") or "")
                    thinking = str(message.get("thinking") or "")
                if not thinking:
                    thinking = str(chunk.get("thinking") or "")
                if not content:
                    content = str(chunk.get("response") or "")

                done = bool(chunk.get("done"))
                replace = None
                if citation_stream is not None:
                    content = citation_stream.push(content)
                    if done:
                        rest, full = citation_stream.finish()
                        content += rest
                        replace = full if citation_stream.sent != full else None
                        citation_stream = None
                event: dict[str, Any] = {"thinking": thinking, "content": content, "done": done}
                if done:
                    event["timings"] = ollama_timings(chunk)
                if replace is not None:
                    event["content_replace"] = replace
                yield event
            if citation_stream is not None:
                # The stream ended without its last message: send what was held back.
                rest, _full = citation_stream.finish()
                if rest:
                    yield {"thinking": "", "content": rest, "done": False}
    except InterruptedError:
        raise
    except Exception:
        if abort_signal is not None and abort_signal.is_aborted():
            raise InterruptedError("Generation aborted.")
        raise


def invoke_ollama_chat(
    model_name: str,
    messages: list[dict[str, str]],
    *,
    think: bool | str | None = None,
    temperature: float | None = None,
    repeat_penalty: float | None = None,
    top_p: float | None = None,
    options_override: dict[str, Any] | None = None,
    request_timeout_seconds: float | int | None = None,
) -> tuple[str, str, dict[str, Any]]:
    normalized_model_name = _normalize_model_name(model_name)
    payload = {
        "model": normalized_model_name,
        "messages": _prepare_ollama_chat_messages_for_thinking(
            normalized_model_name,
            messages,
            think,
        ),
        "stream": False,
        "options": _build_ollama_generation_options(
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            top_p=top_p,
        ),
    }

    with _open_ollama_request(
        "/api/chat",
        payload,
        think=think,
        options_override=options_override,
        timeout_seconds=request_timeout_seconds or _ollama_request_watchdog_seconds(),
    ) as response:
        data = json.loads(response.read() or b"{}")

    error = str(data.get("error") or "").strip()
    if error:
        raise RuntimeError(error)

    message = data.get("message")
    content = ""
    thinking = ""
    if isinstance(message, dict):
        content = str(message.get("content") or "")
        thinking = str(message.get("thinking") or "")
    if not thinking:
        thinking = str(data.get("thinking") or "")
    if not content:
        content = str(data.get("response") or "")
    if resolve_model_family(normalized_model_name) == "qwen3.5":
        content = _normalize_qwen35_citation_labels(content, messages)
    return thinking, content, data


def invoke_ollama_generate(
    model_name: str,
    prompt: str,
    *,
    think: bool | str | None = None,
    temperature: float | None = None,
    repeat_penalty: float | None = None,
    top_p: float | None = None,
    options_override: dict[str, Any] | None = None,
    request_timeout_seconds: float | int | None = None,
    timings: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """``timings``, when given, receives the call's :func:`ollama_timings`."""
    payload = {
        "model": _normalize_model_name(model_name),
        "prompt": prompt,
        "stream": False,
        "options": _build_ollama_generation_options(
            temperature=temperature,
            repeat_penalty=repeat_penalty,
            top_p=top_p,
        ),
    }

    with _open_ollama_request(
        "/api/generate",
        payload,
        think=think,
        options_override=options_override,
        timeout_seconds=request_timeout_seconds or _ollama_request_watchdog_seconds(),
    ) as response:
        data = json.loads(response.read() or b"{}")

    error = str(data.get("error") or "").strip()
    if error:
        raise RuntimeError(error)
    if timings is not None:
        timings.update(ollama_timings(data))

    thinking = str(data.get("thinking") or "")
    content = str(data.get("response") or "")
    if not thinking or not content:
        message = data.get("message")
        if isinstance(message, dict):
            if not thinking:
                thinking = str(message.get("thinking") or "")
            if not content:
                content = str(message.get("content") or "")
    return thinking, content


_TWO_STEP_EXTRACTION_PROMPT = (
    "Extract the following response into the requested structured JSON format. "
    "Do not change the meaning or citations.\n\nRaw text:\n{raw}"
)


def _build_openai_client_for_two_step(
    base_url: str,
    api_key: str,
    timeout: float,
    http_client: Any,
) -> Any:
    """Plain OpenAI client (no Instructor) used for the free-form generation step."""
    from openai import OpenAI  # noqa: PLC0415

    openai_kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key or "not-needed"}
    if timeout is not None:
        openai_kwargs["timeout"] = timeout
    if http_client is not None:
        openai_kwargs["http_client"] = http_client
    return OpenAI(**openai_kwargs)


def _remote_streaming_enabled() -> bool:
    """Stream remote answers so the connection is never idle.

    Gateways in front of remote models drop a request that sends no byte for
    a while (observed: ~60s) with "Server disconnected without sending a
    response". A non-streamed call is silent until the whole answer exists,
    so any long generation got cut. Streaming also turns the client timeout
    into a between-chunks limit instead of a whole-answer one.
    """
    if not is_remote_provider():
        return False
    return os.getenv("REMOTE_STREAMING", "true").strip().lower() not in {"0", "false", "no", "off"}


_STREAM_FINISH_REASONS = {"stop", "length", "tool_calls", "content_filter", "function_call"}


class IncompleteStreamError(RuntimeError):
    """An OpenAI-compatible stream ended before its final chunk.

    Such a stream closes with a chunk that carries ``finish_reason`` (then
    ``data: [DONE]``). A connection closed without it is a cut answer, even
    when the HTTP layer saw a clean end, and must not be kept as a complete one.
    """


INCOMPLETE_STREAM_MESSAGE = (
    "Réponse incomplète : le fournisseur a arrêté le flux avant son signal de fin."
)


def _uses_openai_compatible_stream(llm: Any) -> bool:
    # ChatOpenAI and its subclasses; matched by name to keep langchain_openai lazy.
    return any(cls.__name__ == "ChatOpenAI" for cls in type(llm).__mro__)


def _chunk_finish_reason(chunk: Any) -> str:
    metadata = getattr(chunk, "response_metadata", None)
    if isinstance(metadata, dict):
        return str(metadata.get("finish_reason") or "")
    return ""


def _stream_chat_completion(
    client: Any,
    create_kwargs: dict[str, Any],
    abort_signal: StreamAbortSignal | None = None,
) -> Any:
    """Run a chat completion as a stream and return it as one ``ChatCompletion``."""
    from openai.types.chat import ChatCompletion  # noqa: PLC0415

    parts: list[str] = []
    finish_reason = ""
    completion_id = ""
    model = str(create_kwargs.get("model") or "")
    stream = client.chat.completions.create(**create_kwargs, stream=True)
    try:
        for chunk in stream:
            if abort_signal is not None:
                abort_signal.throw_if_aborted()
            completion_id = completion_id or str(getattr(chunk, "id", "") or "")
            model = str(getattr(chunk, "model", "") or model)
            for choice in getattr(chunk, "choices", None) or []:
                if getattr(choice, "index", 0) != 0:
                    continue
                content = getattr(getattr(choice, "delta", None), "content", None)
                if content:
                    parts.append(content)
                if getattr(choice, "finish_reason", None):
                    finish_reason = str(choice.finish_reason)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()
    if not finish_reason:
        raise IncompleteStreamError(INCOMPLETE_STREAM_MESSAGE)
    return ChatCompletion.model_validate({
        "id": completion_id or "streamed",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "finish_reason": finish_reason if finish_reason in _STREAM_FINISH_REASONS else "stop",
            "message": {"role": "assistant", "content": "".join(parts)},
        }],
    })


def _invoke_structured_streaming(
    client: Any,
    request_kwargs: dict[str, Any],
    abort_signal: StreamAbortSignal | None = None,
) -> BaseModel:
    """Instructor's JSON-mode call, with the transport switched to streaming.

    Instructor builds the request and validates the answer exactly as in
    ``instructor_client.chat.completions.create``; only the wire changes.
    A validation failure is re-asked like Instructor does.
    """
    import instructor  # noqa: PLC0415
    from instructor.processing.response import handle_response_model  # noqa: PLC0415
    from pydantic import ValidationError  # noqa: PLC0415

    kwargs = dict(request_kwargs)
    response_model = kwargs.pop("response_model")
    retries = max(0, int(kwargs.pop("max_retries", 0) or 0))
    messages = list(kwargs.pop("messages") or [])
    last_error: Exception | None = None
    for _attempt in range(retries + 1):
        # handle_response_model inserts its schema prompt into the list it gets.
        schema_model, create_kwargs = handle_response_model(
            response_model, mode=instructor.Mode.JSON, messages=list(messages), **kwargs,
        )
        completion = _stream_chat_completion(client, create_kwargs, abort_signal)
        try:
            assert schema_model is not None
            return schema_model.from_response(completion, mode=instructor.Mode.JSON)
        except ValidationError as exc:
            last_error = exc
            messages = [
                *messages,
                {"role": "assistant", "content": completion.choices[0].message.content or ""},
                {
                    "role": "user",
                    "content": f"Correct your JSON ONLY RESPONSE, based on the following errors:\n{exc}",
                },
            ]
    assert last_error is not None
    raise last_error


def _clean_instructor_error_message(message: str) -> str:
    value = str(message or "").strip()
    if not value:
        return ""
    last_exception = re.search(
        r"<last_exception>\s*(.*?)\s*</last_exception>",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if last_exception:
        return re.sub(r"\s+", " ", last_exception.group(1)).strip()
    value = re.sub(
        r"<failed_attempts>.*?</failed_attempts>",
        "",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(r"</?[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


_CONTEXT_OVERFLOW_MARKERS = (
    "exceed_context_size",
    "exceeds the available context size",
)
_CONTEXT_OVERFLOW_PATTERN = re.compile(
    r"\((\d+)\s*tokens?\)\s*exceeds the available context size\s*\((\d+)",
    re.IGNORECASE,
)


def _humanize_llm_error(message: str) -> str:
    """Replace provider context-overflow noise with an actionable instruction."""
    value = str(message or "")
    if not any(marker in value.lower() for marker in _CONTEXT_OVERFLOW_MARKERS):
        return value
    advice = (
        "Augmentez \"Fenetre de contexte Ollama\" dans Parametres, "
        "ou choisissez un modele avec une fenetre plus large."
    )
    match = _CONTEXT_OVERFLOW_PATTERN.search(value)
    if match:
        return (
            f"Contexte trop court : la requete demande {match.group(1)} tokens "
            f"mais la fenetre disponible est de {match.group(2)}. {advice}"
        )
    return f"Contexte trop court : la requete depasse la fenetre du modele. {advice}"


def _format_llm_exception(exc: Exception) -> str:
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        message = _humanize_llm_error(_clean_instructor_error_message(str(current)))
        label = type(current).__name__
        if message and message not in parts:
            parts.append(message)
        elif label and label not in {"Exception", "RuntimeError"} and label not in parts:
            parts.append(label)
        current = current.__cause__ or current.__context__
    return " | ".join(parts) or type(exc).__name__


def _instructor_endpoint() -> tuple[str, str, float, Any]:
    if is_ollama_provider():
        base = _ollama_base_url().rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"
        return base, "ollama", float(_ollama_request_watchdog_seconds()), None
    if is_lmstudio_provider():
        return (
            _lmstudio_api_base_url(),
            _DEFAULT_LMSTUDIO_API_KEY,
            float(_remote_timeout()),
            None,
        )
    return (
        _remote_base_url(),
        _remote_api_key(),
        float(_remote_timeout()),
        get_secure_http_client(),
    )


_instructor_client_lock = threading.Lock()
_instructor_client_cache: tuple[tuple[str, str, float], Any, Any] | None = None


def _close_instructor_cache_entry(entry: tuple[tuple[str, str, float], Any, Any]) -> None:
    closed: set[int] = set()
    instructor_client = entry[1]
    candidates = (
        instructor_client,
        getattr(instructor_client, "client", None),
        getattr(instructor_client, "_client", None),
        entry[2],
    )
    for candidate in candidates:
        if candidate is None or id(candidate) in closed:
            continue
        close = getattr(candidate, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("Unable to close an LLM client", exc_info=True)
            closed.add(id(candidate))


def _get_cached_instructor_client() -> tuple[Any, str, str, float, Any]:
    """Reuse the instructor/OpenAI client while the endpoint stays the same.

    The cache key is the resolved endpoint tuple, so switching provider or
    credentials at runtime rebuilds the client automatically. The httpx
    client is cached alongside to avoid leaking one connection pool per call.
    """
    global _instructor_client_cache
    base_url, api_key, timeout, http_client = _instructor_endpoint()
    key = (base_url, api_key, timeout)
    with _instructor_client_lock:
        if _instructor_client_cache is None or _instructor_client_cache[0] != key:
            previous = _instructor_client_cache
            instructor_client = build_instructor_client(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
                http_client=http_client,
            )
            _instructor_client_cache = (
                key,
                instructor_client,
                http_client,
            )
            if previous is not None:
                _close_instructor_cache_entry(previous)
        else:
            cached_http_client = _instructor_client_cache[2]
            if http_client is not None and http_client is not cached_http_client:
                try:
                    http_client.close()
                except Exception:
                    logger.debug("Unable to close a stale HTTP client", exc_info=True)
            http_client = cached_http_client
        return _instructor_client_cache[1], base_url, api_key, timeout, http_client


def _structured_request_kwargs(
    *,
    model_name: str,
    messages: list[dict[str, str]],
    response_model: type[BaseModel],
    settings: dict[str, float],
) -> dict[str, Any]:
    sampling = settings if is_ollama_provider() else _openai_compatible_sampling_settings(settings)
    prepared_messages = (
        _prepare_ollama_chat_messages_for_thinking(model_name, messages, False)
        if is_ollama_provider()
        else list(messages or [])
    )
    request_kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": prepared_messages,
        "response_model": response_model,
        "max_retries": STRUCTURED_OUTPUT_MAX_RETRIES,
        "temperature": sampling["temperature"],
        "top_p": sampling["top_p"],
        "max_tokens": _ollama_max_output_tokens(),
    }
    if is_ollama_provider():
        ollama_options: dict[str, Any] = {
            "repeat_penalty": settings["repeat_penalty"],
            "num_predict": _ollama_max_output_tokens(),
        }
        # No num_ctx here: Ollama's /v1 layer discards the whole `options`
        # object, so the window has to be set on the native endpoint instead.
        # Ollama models are routed through _invoke_native_structured for that
        # reason; this branch only survives for OpenAI-compatible lookalikes.
        request_kwargs["extra_body"] = {
            "think": False,
            "options": ollama_options,
        }
    return request_kwargs


def _free_form_generate(
    *,
    model_name: str,
    messages: list[dict[str, str]],
    base_url: str,
    api_key: str,
    timeout: float,
    http_client: Any,
    settings: dict[str, float],
    abort_signal: StreamAbortSignal | None = None,
) -> str:
    """Step 1 of Read-then-Parse: plain text generation, no JSON schema pressure."""
    plain_client = _build_openai_client_for_two_step(base_url, api_key, timeout, http_client)
    sampling = settings if is_ollama_provider() else _openai_compatible_sampling_settings(settings)
    prepared_messages = (
        _prepare_ollama_chat_messages_for_thinking(model_name, messages, False)
        if is_ollama_provider()
        else list(messages or [])
    )
    create_kwargs: dict[str, Any] = {
        "model": model_name,
        "messages": prepared_messages,
        "temperature": sampling["temperature"],
        "top_p": sampling["top_p"],
        "max_tokens": _ollama_max_output_tokens(),
    }
    if is_ollama_provider():
        ollama_options: dict[str, Any] = {
            "repeat_penalty": settings["repeat_penalty"],
            "num_predict": _ollama_max_output_tokens(),
        }
        # No num_ctx here: Ollama's /v1 layer discards the whole `options`
        # object, so the window has to be set on the native endpoint instead.
        # Ollama models are routed through _invoke_native_structured for that
        # reason; this branch only survives for OpenAI-compatible lookalikes.
        create_kwargs["extra_body"] = {
            "think": False,
            "options": ollama_options,
        }
    try:
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        if _remote_streaming_enabled():
            completion = _stream_chat_completion(plain_client, create_kwargs, abort_signal)
        else:
            completion = plain_client.chat.completions.create(**create_kwargs)
    except Exception as exc:
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        raise RuntimeError(
            f"Echec generation libre (step 1/2): {_format_llm_exception(exc)}"
        ) from exc

    if abort_signal is not None:
        abort_signal.throw_if_aborted()
    choices = getattr(completion, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    return _extract_text(message)


def _estimate_structured_prompt_tokens(
    messages: list[dict[str, str]],
    response_model: type[BaseModel] | None = None,
) -> int:
    """Rough prompt size, so Auto can pick a window that actually fits.

    Deliberately crude: it only has to be close enough to land on the right
    4096-token step, and :func:`resolve_ollama_context_length` adds margin.
    """
    chars = sum(len(str(message.get("content") or "")) for message in messages or [])
    tokens = math.ceil(chars / 3.2)
    if response_model is not None:
        try:
            tokens += math.ceil(len(json.dumps(response_model.model_json_schema())) / 3.2)
        except Exception:
            logger.debug(
                "Recovered from an error in _estimate_structured_prompt_tokens.", exc_info=True
            )
    return tokens + 256


def _free_form_generate_native(
    *,
    model_name: str,
    messages: list[dict[str, str]],
    settings: dict[str, float],
    context_length: int = 0,
    abort_signal: StreamAbortSignal | None = None,
) -> str:
    """Step 1 of Read-then-Parse over Ollama's native chat endpoint."""
    options_override: dict[str, Any] = {"num_predict": _ollama_max_output_tokens()}
    if context_length:
        options_override["num_ctx"] = int(context_length)
    try:
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        _thinking, content, _data = invoke_ollama_chat(
            model_name,
            messages,
            think=False,
            temperature=settings["temperature"],
            repeat_penalty=settings["repeat_penalty"],
            top_p=settings["top_p"],
            options_override=options_override,
            request_timeout_seconds=_ollama_request_watchdog_seconds(),
        )
    except Exception as exc:
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        raise RuntimeError(
            f"Echec generation libre (step 1/2): {_format_llm_exception(exc)}"
        ) from exc
    if abort_signal is not None:
        abort_signal.throw_if_aborted()
    return str(content or "")


def _structured_value_has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_structured_value_has_content(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_structured_value_has_content(item) for item in value)
    return True


def _normalize_qwen35_structured_value(
    value: Any,
    messages: list[dict[str, str]],
) -> Any:
    if isinstance(value, str):
        return _normalize_qwen35_citation_labels(value, messages)
    if isinstance(value, dict):
        return {
            key: _normalize_qwen35_structured_value(item, messages) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_normalize_qwen35_structured_value(item, messages) for item in value]
    return value


def _invoke_native_structured(
    *,
    model_name: str,
    messages: list[dict[str, str]],
    response_model: type[BaseModel],
    settings: dict[str, float],
    context_length: int = 0,
    normalize_value: Callable[[Any, list[dict[str, str]]], Any] | None = None,
    normalize_text: Callable[[str, list[dict[str, str]]], str] | None = None,
    abort_signal: StreamAbortSignal | None = None,
) -> BaseModel:
    """Structured output through Ollama's native ``/api/generate`` schema mode.

    Ollama's OpenAI-compatible ``/v1`` layer silently discards the ``options``
    object, so ``num_ctx`` never reaches the model there: long prompts are
    truncated or rejected at Ollama's 4096-token default no matter what the
    user picked in Settings. ``/api/generate`` honours it, which is why every
    Ollama structured call routes through here.
    """
    schema = response_model.model_json_schema()
    prepared = _prepare_ollama_chat_messages_for_thinking(model_name, messages, False)
    transcript = "\n\n".join(
        f"{str(message.get('role') or 'user').upper()}:\n{str(message.get('content') or '').strip()}"
        for message in prepared
        if str(message.get("content") or "").strip()
    )
    base_prompt = (
        "Perform the requested task from the conversation below. "
        "Return only one JSON object matching the supplied schema. "
        "Populate every required field and every relevant field from the evidence. "
        "Never return an empty object or leave all fields empty when the conversation contains an answer. "
        "Preserve technical names and citation labels exactly.\n\n"
        f"CONVERSATION:\n{transcript}"
    )
    context_options: dict[str, Any] = {"num_ctx": int(context_length)} if context_length else {}
    last_error = ""
    for attempt in range(2):
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        prompt = base_prompt
        if attempt and last_error:
            prompt += (
                "\n\nThe previous JSON was invalid or empty. Correct it now. "
                f"Validation detail: {last_error}"
            )
        payload = {
            "model": model_name,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "options": {
                **_build_ollama_generation_options(
                    temperature=settings["temperature"],
                    repeat_penalty=settings["repeat_penalty"],
                    top_p=settings["top_p"],
                ),
                "num_predict": min(_ollama_max_output_tokens(), 2048),
                **context_options,
            },
        }
        try:
            with _open_ollama_request(
                "/api/generate",
                payload,
                think=False,
                timeout_seconds=_ollama_request_watchdog_seconds(),
            ) as response:
                data = json.loads(response.read() or b"{}")
        except Exception:
            if abort_signal is not None:
                abort_signal.throw_if_aborted()
            raise
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        error = str(data.get("error") or "").strip()
        if error:
            raise RuntimeError(_humanize_llm_error(error))
        raw = str(data.get("response") or "").strip()
        try:
            parsed = response_model.model_validate_json(raw)
        except Exception as exc:
            logger.debug("Recovered from an error in _invoke_native_structured.", exc_info=True)
            last_error = _format_llm_exception(exc)
            continue
        normalized_payload = parsed.model_dump()
        if normalize_value is not None:
            normalized_payload = normalize_value(normalized_payload, messages)
            parsed = response_model.model_validate(normalized_payload)
        if _structured_value_has_content(normalized_payload):
            return parsed
        last_error = "all structured fields were empty"

    answer_field = response_model.model_fields.get("answer")
    if answer_field is not None and answer_field.annotation is str:
        _thinking, fallback_text, _data = invoke_ollama_chat(
            model_name,
            messages,
            think=False,
            temperature=settings["temperature"],
            repeat_penalty=settings["repeat_penalty"],
            top_p=settings["top_p"],
            options_override={
                "num_predict": min(_ollama_max_output_tokens(), 1024),
                **context_options,
            },
            request_timeout_seconds=_ollama_request_watchdog_seconds(),
        )
        if normalize_text is not None:
            fallback_text = normalize_text(fallback_text, messages)
        fallback_text = str(fallback_text or "").strip()
        if fallback_text:
            return response_model.model_validate({"answer": fallback_text})

    raise RuntimeError(
        "Echec structured output via Ollama natif: "
        f"{last_error or 'reponse vide ou invalide'}"
    )


def _invoke_qwen35_native_structured(
    *,
    model_name: str,
    messages: list[dict[str, str]],
    response_model: type[BaseModel],
    settings: dict[str, float],
    context_length: int = 0,
    abort_signal: StreamAbortSignal | None = None,
) -> BaseModel:
    """Qwen 3.5 additionally needs its citation labels repaired."""
    return _invoke_native_structured(
        model_name=model_name,
        messages=messages,
        response_model=response_model,
        settings=settings,
        context_length=context_length,
        normalize_value=_normalize_qwen35_structured_value,
        normalize_text=_normalize_qwen35_citation_labels,
        abort_signal=abort_signal,
    )


def _create_structured(
    instructor_client: Any,
    request_kwargs: dict[str, Any],
    abort_signal: StreamAbortSignal | None = None,
) -> BaseModel:
    if _remote_streaming_enabled():
        return _invoke_structured_streaming(instructor_client.client, request_kwargs, abort_signal)
    return instructor_client.chat.completions.create(**request_kwargs)


def invoke_structured_chat(
    model_name: str,
    messages: list[dict[str, str]],
    response_model: type[BaseModel] = ChatAnswer,
    *,
    temperature: float | None = None,
    repeat_penalty: float | None = None,
    top_p: float | None = None,
    context_tokens_hint: int = 0,
    abort_signal: StreamAbortSignal | None = None,
) -> BaseModel:
    """Run a structured-output chat call and return the parsed Pydantic object.

    Routing:
      - Ollama goes through the native ``/api/generate`` schema mode, the only
        transport that honours ``num_ctx`` (its /v1 layer drops ``options``).
      - LM Studio and remote providers use Instructor over OpenAI-compatible
        HTTP, with models flagged by ``needs_two_step_json`` (small models
        prone to "task collapse" on RAG + strict JSON) running a two-step
        Read-then-Parse pipeline: free-form generation, then JSON extraction.

    On Ollama Auto the requested window grows to fit the prompt -- otherwise
    Ollama falls back to 4096 and long prompts fail outright. The size is
    estimated from the messages; ``context_tokens_hint`` lets a caller that
    already knows its prompt budget supply a better figure.

    Native Ollama "thinking" is incompatible with strict structured output, so
    this function does not expose a thinking mode — callers that need a CoT
    trace must use the streaming Ollama path directly.
    """
    if abort_signal is not None:
        abort_signal.throw_if_aborted()
    normalized_model_name = _normalize_model_name(model_name)
    if not normalized_model_name:
        raise RuntimeError(
            "Nom de modele invalide. Selectionnez un modele dans la liste puis reessayez."
        )

    settings = _resolve_generation_settings(
        temperature=temperature,
        repeat_penalty=repeat_penalty,
        top_p=top_p,
    )
    if is_ollama_provider():
        required_tokens = max(
            int(context_tokens_hint or 0),
            _estimate_structured_prompt_tokens(messages, response_model),
        ) + min(_ollama_max_output_tokens(), 2048)
        context_length = resolve_ollama_context_length(
            normalized_model_name,
            required_tokens=required_tokens,
        )
        if resolve_model_family(normalized_model_name) == "qwen3.5":
            return _invoke_qwen35_native_structured(
                model_name=normalized_model_name,
                messages=messages,
                response_model=response_model,
                settings=settings,
                context_length=context_length,
                abort_signal=abort_signal,
            )
        structured_messages = messages
        if needs_two_step_json(normalized_model_name):
            raw_text = _free_form_generate_native(
                model_name=normalized_model_name,
                messages=messages,
                settings=settings,
                context_length=context_length,
                abort_signal=abort_signal,
            )
            structured_messages = [
                {
                    "role": "user",
                    "content": _TWO_STEP_EXTRACTION_PROMPT.format(raw=raw_text),
                }
            ]
        return _invoke_native_structured(
            model_name=normalized_model_name,
            messages=structured_messages,
            response_model=response_model,
            settings=settings,
            context_length=context_length,
            abort_signal=abort_signal,
        )

    instructor_client, base_url, api_key, timeout, http_client = _get_cached_instructor_client()

    if needs_two_step_json(normalized_model_name):
        raw_text = _free_form_generate(
            model_name=normalized_model_name,
            messages=messages,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
            http_client=http_client,
            settings=settings,
            abort_signal=abort_signal,
        )
        extraction_messages = [
            {
                "role": "user",
                "content": _TWO_STEP_EXTRACTION_PROMPT.format(raw=raw_text),
            }
        ]
        request_kwargs = _structured_request_kwargs(
            model_name=normalized_model_name,
            messages=extraction_messages,
            response_model=response_model,
            settings=settings,
        )
        try:
            result = _create_structured(instructor_client, request_kwargs, abort_signal)
            if abort_signal is not None:
                abort_signal.throw_if_aborted()
            return result
        except Exception as exc:
            if abort_signal is not None:
                abort_signal.throw_if_aborted()
            raise RuntimeError(
                f"Echec extraction structuree (step 2/2): {_format_llm_exception(exc)}"
            ) from exc

    request_kwargs = _structured_request_kwargs(
        model_name=normalized_model_name,
        messages=messages,
        response_model=response_model,
        settings=settings,
    )
    try:
        result = _create_structured(instructor_client, request_kwargs, abort_signal)
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        return result
    except Exception as exc:
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        raise RuntimeError(f"Echec appel instructor: {_format_llm_exception(exc)}") from exc


def _validate_remote_config() -> tuple[bool, str]:
    if not _remote_base_url():
        return (
            False,
            "Provider distant active mais REMOTE_LLM_BASE_URL est vide.",
        )
    if not _remote_api_key():
        return (
            False,
            "Provider distant active mais REMOTE_LLM_API_KEY est vide.",
        )
    return True, "Configuration distante OK."


def _validate_lmstudio_config(base_url: str | None = None) -> tuple[bool, str]:
    target = str(base_url or _lmstudio_base_url()).strip()
    if not target:
        return False, "Provider LM Studio actif mais l'URL LM Studio est vide."
    return True, "Configuration LM Studio OK."


def _remote_headers() -> dict[str, str]:
    return _openai_compatible_headers(_remote_api_key())


def _openai_compatible_headers(api_key: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": _OLLAMA_USER_AGENT,
    }
    token = str(api_key or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _open_url_json(
    url: str,
    *,
    method: str = "GET",
    json_payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | int = 10,
    ssl_verify: str | bool = True,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=(json.dumps(json_payload).encode("utf-8") if json_payload is not None else None),
        headers=headers or {},
        method=method.upper(),
    )
    context = None
    if isinstance(ssl_verify, str):
        context = ssl.create_default_context(cafile=ssl_verify)
    elif not ssl_verify:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    with _open_request_with_local_proxy_bypass(
        request,
        timeout=timeout,
        context=context,
    ) as response:
        data = json.loads(response.read() or b"{}")
    return data if isinstance(data, dict) else {}


def _read_http_error_payload(exc: urllib.error.HTTPError) -> dict[str, Any]:
    try:
        raw = exc.read()
    except Exception:
        logger.debug("Recovered from an error in _read_http_error_payload.", exc_info=True)
        return {}
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _extract_openai_compatible_error_message(payload: dict[str, Any] | None) -> str:
    source = payload if isinstance(payload, dict) else {}
    error = source.get("error")
    if isinstance(error, str):
        return error.strip()
    if isinstance(error, dict):
        for key in ("message", "detail", "code", "type"):
            value = str(error.get(key) or "").strip()
            if value:
                return value
    for key in ("message", "detail", "error"):
        value = str(source.get(key) or "").strip()
        if value:
            return value
    return ""


def _openai_compatible_model_supports_embeddings(item: dict[str, Any]) -> bool:
    model_id = str(item.get("id") or "").strip()
    if _is_embedding_model_candidate(model_id):
        return True

    capability_values: list[str] = []
    for key in (
        "type",
        "task",
        "tasks",
        "capability",
        "capabilities",
        "supported_endpoints",
        "endpoints",
    ):
        value = item.get(key)
        if isinstance(value, dict):
            for name, enabled in value.items():
                if enabled:
                    capability_values.append(str(name))
        elif isinstance(value, (list, tuple, set)):
            capability_values.extend(str(entry) for entry in value)
        elif value is not None:
            capability_values.append(str(value))
    joined = " ".join(capability_values).lower()
    return bool(re.search(r"\b(embed|embedding|embeddings|feature-extraction)\b", joined))


def _list_openai_compatible_models(
    *,
    base_url: str,
    api_key: str | None = None,
    timeout: float | int = 10,
    ssl_verify: str | bool = True,
    label: str = "API",
    embeddings_only: bool = False,
) -> tuple[list[str], str]:
    try:
        data = _open_url_json(
            _join_url(base_url, "/models"),
            headers=_openai_compatible_headers(api_key),
            timeout=timeout,
            ssl_verify=ssl_verify,
        )
        raw = data.get("data", [])
        candidates = [
            item for item in raw if isinstance(item, dict) and str(item.get("id", "")).strip()
        ]
        if embeddings_only:
            candidates = [
                item for item in candidates if _openai_compatible_model_supports_embeddings(item)
            ]
        else:
            candidates = [
                item for item in candidates if _is_chat_model_candidate(str(item.get("id") or ""))
            ]
        models = sorted({str(item.get("id", "")).strip() for item in candidates})
        if not models:
            qualifier = "d'embedding " if embeddings_only else ""
            return [], f"Aucun modele {qualifier}detecte sur {label}."
        return models, f"{len(models)} modele(s) detecte(s) sur {label}."
    except urllib.error.HTTPError as exc:
        return [], f"{label} inaccessible (HTTP {exc.code})."
    except Exception as exc:
        logger.debug("Recovered from an error in _list_openai_compatible_models.", exc_info=True)
        return [], f"{label} inaccessible : {exc}"


def _extract_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload

    content = getattr(payload, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)

    if isinstance(payload, dict):
        for key in ("text", "content", "answer"):
            value = payload.get(key)
            if isinstance(value, str):
                return value

    return str(payload)


def list_available_models(
    ollama_url: str | None = None,
    *,
    force_refresh: bool = False,
) -> tuple[list[str], str]:
    cache_key = _model_list_cache_key(ollama_url)
    if not force_refresh:
        cached = _load_cached_model_list(cache_key)
        if cached is not None:
            models, status = cached
            return models, f"{status} (cache)"

    if is_remote_provider():
        ok, msg = _validate_remote_config()
        if not ok:
            return [], msg
        models, status = _list_openai_compatible_models(
            base_url=_remote_base_url(),
            api_key=_remote_api_key(),
            timeout=_remote_model_list_timeout(),
            ssl_verify=_remote_httpx_verify(),
            label="API distante",
            embeddings_only=False,
        )
        if models:
            _save_cached_model_list(cache_key, models, status)
            return models, status
        return _cached_model_fallback(cache_key, status)

    if is_lmstudio_provider():
        ok, msg = _validate_lmstudio_config(ollama_url)
        if not ok:
            return [], msg
        models, status = list_local_lmstudio_models(ollama_url, embeddings_only=False)
        if models:
            _save_cached_model_list(cache_key, models, status)
            return models, status
        return _cached_model_fallback(cache_key, status)

    target = (ollama_url or _ollama_base_url()).rstrip("/")
    try:
        data = _read_ollama_json(target, "/api/tags", timeout=4)
        raw = data.get("models", [])
        sorted_models = sorted(raw, key=lambda model: model.get("size", 0))
        models = _filter_chat_model_names(
            model["name"]
            for model in sorted_models
            if isinstance(model, dict) and str(model.get("name") or "").strip()
        )
        if not models:
            return [], "Aucun modele de chat installe (ollama pull <modele>)"
        status = f"{len(models)} modele(s) | defaut : {models[0]}"
        _save_cached_model_list(cache_key, models, status)
        return models, status
    except Exception as exc:
        logger.debug("Recovered from an error in list_available_models.", exc_info=True)
        status = _format_ollama_access_error(exc, path="/api/tags")
        return _cached_model_fallback(cache_key, status)


def fetch_gpu_status(ollama_url: str | None = None) -> str:
    if is_remote_provider():
        return "Statut GPU indisponible en mode provider distant."
    if is_lmstudio_provider():
        return "Statut GPU indisponible avec LM Studio."

    target = (ollama_url or _ollama_base_url()).rstrip("/")
    try:
        data = _read_ollama_json(target, "/api/ps", timeout=4)
    except Exception as exc:
        logger.debug("Recovered from an error in fetch_gpu_status.", exc_info=True)
        return _format_ollama_access_error(exc, path="/api/ps")

    models = data.get("models", [])
    if not models:
        return "Aucun modele charge en memoire"

    def _fmt(byte_count: int) -> str:
        return f"{byte_count / 1e9:.2f} Go" if byte_count >= 1e9 else f"{byte_count / 1e6:.0f} Mo"

    lines: list[str] = []
    for model in models:
        name = model.get("name", "?")
        size_total = int(model.get("size", 0))
        size_vram = int(model.get("size_vram", 0))
        size_ram = size_total - size_vram

        if size_vram > 0 and size_ram == 0:
            location, icon = f"GPU uniquement ({_fmt(size_vram)} VRAM)", "GPU"
        elif size_vram > 0:
            location, icon = f"GPU+CPU ({_fmt(size_vram)} VRAM + {_fmt(size_ram)} RAM)", "GPU+CPU"
        else:
            location, icon = f"CPU uniquement ({_fmt(size_ram)} RAM)", "CPU"

        lines.append(f"[{icon}] {name} - {location}")

    return "\n".join(lines)


def create_chat_llm(
    model_name: str,
    *,
    temperature: float | None = None,
    repeat_penalty: float | None = None,
    top_p: float | None = None,
    max_output_tokens: int | None = None,
    request_timeout_seconds: float | None = None,
    max_retries: int | None = None,
    context_tokens_hint: int = 0,
    json_mode: bool = False,
    thinking_enabled: bool = False,
):
    """Build a LangChain chat model for the active provider.

    ``context_tokens_hint`` is the prompt + output size the caller expects. On
    Ollama Auto it sizes ``num_ctx`` like structured calls do; without it,
    Ollama falls back to 4096 tokens and silently truncates long prompts.

    ``json_mode`` constrains Ollama to a JSON answer with reasoning off.
    Reasoning models otherwise think aloud in the answer ("Let me analyze...")
    and can spend the whole output budget before writing any JSON.

    ``thinking_enabled`` sets Ollama's reasoning explicitly: Ollama turns it on
    whenever the field is absent, and some models (granite4.2) then write
    their reasoning straight into the answer text.
    """
    model_name = _normalize_model_name(model_name)
    if not model_name:
        raise RuntimeError(
            "Nom de modele invalide. Selectionnez un modele dans la liste puis reessayez."
        )

    settings = _resolve_generation_settings(
        temperature=temperature,
        repeat_penalty=repeat_penalty,
        top_p=top_p,
    )

    if is_remote_provider():
        ok, msg = _validate_remote_config()
        if not ok:
            raise RuntimeError(msg)
        try:
            from langchain_openai import ChatOpenAI
        except Exception as exc:
            raise RuntimeError(
                "Support provider distant indisponible. Installez `langchain-openai`."
            ) from exc

        sampling = _openai_compatible_sampling_settings(settings)
        remote_kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": cast(Any, _remote_api_key()),
            "base_url": _remote_base_url(),
            "timeout": _remote_timeout(),
            "temperature": sampling["temperature"],
            "top_p": sampling["top_p"],
            "http_client": get_secure_http_client(),
        }
        if max_output_tokens is not None:
            remote_kwargs["max_tokens"] = max(1, int(max_output_tokens))
        if request_timeout_seconds is not None:
            remote_kwargs["timeout"] = request_timeout_seconds
        if max_retries is not None:
            remote_kwargs["max_retries"] = max_retries
        if _remote_streaming_enabled():
            # invoke() then aggregates the streamed chunks itself.
            remote_kwargs["streaming"] = True
        return ChatOpenAI(
            **remote_kwargs,
        )

    if is_lmstudio_provider():
        ok, msg = _validate_lmstudio_config()
        if not ok:
            raise RuntimeError(msg)
        try:
            from langchain_openai import ChatOpenAI
        except Exception as exc:
            raise RuntimeError(
                "Support LM Studio indisponible. Installez `langchain-openai`."
            ) from exc

        sampling = _openai_compatible_sampling_settings(settings)
        lmstudio_kwargs: dict[str, Any] = {
            "model": model_name,
            "api_key": cast(Any, _DEFAULT_LMSTUDIO_API_KEY),
            "base_url": _lmstudio_api_base_url(),
            "timeout": _remote_timeout(),
            "temperature": sampling["temperature"],
            "top_p": sampling["top_p"],
        }
        if max_output_tokens is not None:
            lmstudio_kwargs["max_tokens"] = max(1, int(max_output_tokens))
        if request_timeout_seconds is not None:
            lmstudio_kwargs["timeout"] = request_timeout_seconds
        if max_retries is not None:
            lmstudio_kwargs["max_retries"] = max_retries
        return ChatOpenAI(**lmstudio_kwargs)

    shared_client_kwargs = _ollama_client_kwargs()
    shared_sync_kwargs: dict[str, Any] = {
        "timeout": _ollama_request_watchdog_seconds(),
        "verify": _ollama_httpx_verify(),
    }
    if request_timeout_seconds is not None:
        shared_sync_kwargs["timeout"] = request_timeout_seconds
    from langchain_ollama import OllamaLLM

    output_tokens = (
        max(1, int(max_output_tokens))
        if max_output_tokens is not None
        else _ollama_max_output_tokens()
    )
    ollama_kwargs: dict[str, Any] = {
        "model": model_name,
        "base_url": _ollama_base_url(),
        "temperature": settings["temperature"],
        "repeat_penalty": settings["repeat_penalty"],
        "top_p": settings["top_p"],
        "num_predict": output_tokens,
        "keep_alive": keep_alive_seconds(),
        "client_kwargs": shared_client_kwargs,
        "sync_client_kwargs": shared_sync_kwargs,
        "async_client_kwargs": dict(shared_sync_kwargs),
    }
    context_length = resolve_ollama_context_length(
        model_name,
        required_tokens=int(context_tokens_hint or 0),
    )
    if context_length:
        ollama_kwargs["num_ctx"] = context_length
    if json_mode:
        ollama_kwargs["format"] = "json"
        ollama_kwargs["reasoning"] = False
    else:
        think = resolve_ollama_think_request(model_name, thinking_enabled)
        if think is not None:
            # OllamaLLM takes a bool; a reasoning level ("medium") means on.
            ollama_kwargs["reasoning"] = bool(think)
    return OllamaLLM(**ollama_kwargs)


def _remote_api_is_openai() -> bool:
    return (urlsplit(_remote_base_url()).hostname or "").lower() == "api.openai.com"


def create_embeddings(local_embedding_model: str):
    embedding_provider = get_embedding_provider_mode()
    if embedding_provider == "remote":
        ok, msg = _validate_remote_config()
        if not ok:
            raise RuntimeError(msg)
        try:
            from langchain_openai import OpenAIEmbeddings
        except Exception as exc:
            raise RuntimeError(
                "Embeddings distants indisponibles. Installez `langchain-openai`."
            ) from exc

        return OpenAIEmbeddings(
            model=_remote_embedding_model(),
            api_key=cast(Any, _remote_api_key()),
            base_url=_remote_base_url(),
            timeout=_remote_timeout(),
            http_client=get_secure_http_client(),
            # LangChain otherwise sends OpenAI token ids instead of the text. Only
            # OpenAI's own models read them: another API rejects them, or embeds
            # them as if they were its own tokens.
            check_embedding_ctx_length=_remote_api_is_openai(),
        )

    if embedding_provider == "lmstudio":
        ok, msg = _validate_lmstudio_config()
        if not ok:
            raise RuntimeError(msg)
        try:
            from langchain_openai import OpenAIEmbeddings
        except Exception as exc:
            raise RuntimeError(
                "Embeddings LM Studio indisponibles. Installez `langchain-openai`."
            ) from exc

        return OpenAIEmbeddings(
            model=local_embedding_model,
            api_key=cast(Any, _DEFAULT_LMSTUDIO_API_KEY),
            base_url=_lmstudio_api_base_url(),
            timeout=_remote_timeout(),
            # LM Studio's OpenAI-compatible endpoint accepts raw strings only.
            # LangChain otherwise tokenizes long inputs client-side and sends
            # arrays of token IDs, which LM Studio rejects with HTTP 400.
            check_embedding_ctx_length=False,
        )

    resolved_model = (
        resolve_local_ollama_model_name(local_embedding_model, embeddings_only=True)
        or local_embedding_model
    )
    if resolved_model != local_embedding_model:
        try:
            set_runtime_embedding_model(resolved_model)
        except Exception:
            logger.debug("Unable to apply the resolved embedding model", exc_info=True)
    shared_client_kwargs = _ollama_client_kwargs()
    client_options = {
        "verify": _ollama_httpx_verify(),
        "timeout": _ollama_request_watchdog_seconds(),
    }
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(
        model=resolved_model,
        base_url=_embedding_ollama_base_url(),
        # Ollama's 5-minute default made the first question after a pause
        # reload the embedding model (1.6 s) before searching.
        keep_alive=keep_alive_seconds(),
        client_kwargs=shared_client_kwargs,
        sync_client_kwargs=client_options,
        async_client_kwargs=dict(client_options),
    )


# Task prompts from the EmbeddingGemma model cards (v1 and v2 share them).
# Ollama, LM Studio and APIs send the text as is, so the app adds them.
EMBEDDINGGEMMA_PROMPTS = "embeddinggemma"
_EMBEDDING_PROMPT_PREFIXES = {
    EMBEDDINGGEMMA_PROMPTS: ("task: search result | query: ", "title: none | text: "),
}


def default_embedding_prompts(model_name: str | None) -> str:
    """Prompt scheme a new index built with ``model_name`` uses ("" = raw text)."""
    compact = re.sub(r"[^a-z0-9]", "", str(model_name or "").lower())
    return EMBEDDINGGEMMA_PROMPTS if "embeddinggemma" in compact else ""


class PromptedEmbeddings:
    """Prefix queries and passages with a model's task prompts."""

    def __init__(self, inner: Any, scheme: str) -> None:
        self.inner = inner
        self.scheme = scheme
        self._query_prefix, self._document_prefix = _EMBEDDING_PROMPT_PREFIXES[scheme]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed_documents([self._document_prefix + text for text in texts])

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(self._query_prefix + text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.inner.aembed_documents([self._document_prefix + text for text in texts])

    async def aembed_query(self, text: str) -> list[float]:
        return await self.inner.aembed_query(self._query_prefix + text)


def with_embedding_prompts(embeddings: Any, scheme: str | None) -> Any:
    """Wrap ``embeddings`` in the prompt ``scheme``; unknown or empty schemes stay raw.

    An index keeps the scheme it was built with (see ``index_manifest.json``):
    vectors of prefixed and raw text differ, so queries must match the passages.
    """
    if not scheme or scheme not in _EMBEDDING_PROMPT_PREFIXES:
        return embeddings
    return PromptedEmbeddings(embeddings, scheme)


def invoke_text(llm: Any, prompt: str) -> str:
    model_name = _resolve_llm_model_name(llm)
    try:
        return _extract_text(llm.invoke(prompt))
    except Exception:
        if model_name and is_ollama_provider():
            unload_ollama_model(model_name, timeout_seconds=2)
        raise


def stream_text(
    llm: Any,
    prompt: Any,
    *,
    abort_signal: StreamAbortSignal | None = None,
) -> Iterable[str]:
    model_name = _resolve_llm_model_name(llm)
    watchdog_seconds = _ollama_watchdog_seconds()
    deadline = time.monotonic() + watchdog_seconds if model_name and is_ollama_provider() else None
    check_finish = _uses_openai_compatible_stream(llm)
    finish_reason = ""
    try:
        for chunk in llm.stream(prompt):
            if abort_signal is not None and abort_signal.is_aborted():
                _stop_ollama_generation(model_name, abort_signal=abort_signal)
                raise InterruptedError("Generation aborted.")
            if deadline is not None and time.monotonic() >= deadline:
                _stop_ollama_generation(model_name, abort_signal=abort_signal)
                raise TimeoutError(
                    "Generation Ollama interrompue par guardrail " f"(>{watchdog_seconds:.0f}s)."
                )
            if check_finish:
                finish_reason = _chunk_finish_reason(chunk) or finish_reason
            text = _extract_text(chunk)
            if text:
                yield text
        if check_finish and not finish_reason:
            raise IncompleteStreamError(INCOMPLETE_STREAM_MESSAGE)
    except InterruptedError:
        raise
    except Exception:
        if model_name and is_ollama_provider():
            unload_ollama_model(model_name, timeout_seconds=2)
        raise


def provider_runtime_label(local_embedding_model: str) -> str:
    embedding_provider = get_embedding_provider_mode()
    if is_ollama_provider() and _embedding_ollama_base_url() == _ollama_base_url():
        return f"ollama (chat+embeddings, {_ollama_base_url()})"
    if is_lmstudio_provider() and embedding_provider == "lmstudio":
        return f"lmstudio (chat+embeddings, {_lmstudio_base_url()})"
    if embedding_provider == "remote":
        return f"distant (chat+embeddings, {_remote_base_url()})"
    if is_remote_provider():
        chat = f"distant (chat={_remote_base_url()})"
    elif is_lmstudio_provider():
        chat = f"lmstudio (chat={_lmstudio_base_url()})"
    else:
        chat = f"ollama (chat={_ollama_base_url()})"
    return (
        f"{chat} + "
        f"ollama embeddings ({_embedding_ollama_base_url()}, model={local_embedding_model})"
    )


def public_provider_info() -> dict[str, Any]:
    fallback = get_fallback_status()
    remote_available = can_use_remote()
    local_embedding_model = required_embedding_model_name()
    embedding_provider = get_embedding_provider_mode()
    local_embeddings_required = embedding_provider in {"ollama", "lmstudio"}
    persisted_mode = _load_persisted_runtime_mode()
    persisted_ollama_base_url = _load_persisted_ollama_base_url()
    persisted_lmstudio_base_url = _load_persisted_lmstudio_base_url()
    env_mode = _provider_raw()
    env_ollama_base_url = (
        os.getenv("OLLAMA_BASE_URL", _DEFAULT_OLLAMA_BASE_URL).strip() or _DEFAULT_OLLAMA_BASE_URL
    ).rstrip("/")
    env_lmstudio_base_url = (
        os.getenv("LMSTUDIO_BASE_URL", _DEFAULT_LMSTUDIO_BASE_URL).strip()
        or _DEFAULT_LMSTUDIO_BASE_URL
    ).rstrip("/")
    effective_ollama_base_url = _ollama_base_url()
    effective_lmstudio_base_url = _lmstudio_base_url()
    if is_remote_provider():
        ok, msg = _validate_remote_config()
        return {
            "provider": env_mode,
            "mode": "remote",
            "effective_mode": get_effective_mode(),
            "persisted_mode": persisted_mode,
            "env_mode": env_mode,
            "ollama_base_url": effective_ollama_base_url,
            "persisted_ollama_base_url": persisted_ollama_base_url,
            "env_ollama_base_url": env_ollama_base_url,
            "lmstudio_base_url": effective_lmstudio_base_url,
            "persisted_lmstudio_base_url": persisted_lmstudio_base_url,
            "env_lmstudio_base_url": env_lmstudio_base_url,
            "provider_url": _remote_base_url(),
            "configured": ok,
            "message": msg,
            "base_url": _remote_base_url(),
            "has_api_key": bool(_remote_api_key()),
            "remote_embedding_model": _remote_embedding_model() or "",
            "required_embedding_model": local_embedding_model,
            "embedding_provider": embedding_provider,
            "embedding_base_url": embedding_base_url(),
            "embedding_source": get_embedding_source(),
            "document_tools_source": get_document_tools_source(),
            "document_tools_base_url": document_tools_ollama_base_url(),
            "local_embeddings_required": local_embeddings_required,
            "can_use_remote": remote_available,
            "lmstudio_public_selection_enabled": _LMSTUDIO_PUBLIC_SELECTION_ENABLED,
            "fallback": fallback,
        }
    if is_lmstudio_provider():
        ok, msg = _validate_lmstudio_config()
        return {
            "provider": "lmstudio",
            "mode": "lmstudio",
            "effective_mode": get_effective_mode(),
            "persisted_mode": persisted_mode,
            "env_mode": env_mode,
            "ollama_base_url": effective_ollama_base_url,
            "persisted_ollama_base_url": persisted_ollama_base_url,
            "env_ollama_base_url": env_ollama_base_url,
            "lmstudio_base_url": effective_lmstudio_base_url,
            "persisted_lmstudio_base_url": persisted_lmstudio_base_url,
            "env_lmstudio_base_url": env_lmstudio_base_url,
            "provider_url": effective_lmstudio_base_url,
            "configured": ok,
            "message": "Provider local LM Studio actif." if ok else msg,
            "base_url": _lmstudio_api_base_url(),
            "has_api_key": False,
            "remote_embedding_model": "",
            "required_embedding_model": local_embedding_model,
            "embedding_provider": embedding_provider,
            "embedding_base_url": embedding_base_url(),
            "embedding_source": get_embedding_source(),
            "document_tools_source": get_document_tools_source(),
            "document_tools_base_url": document_tools_ollama_base_url(),
            "local_embeddings_required": local_embeddings_required,
            "can_use_remote": remote_available,
            "lmstudio_public_selection_enabled": _LMSTUDIO_PUBLIC_SELECTION_ENABLED,
            "fallback": fallback,
        }
    return {
        "provider": "ollama",
        "mode": "local",
        "effective_mode": get_effective_mode(),
        "persisted_mode": persisted_mode,
        "env_mode": env_mode,
        "ollama_base_url": effective_ollama_base_url,
        "persisted_ollama_base_url": persisted_ollama_base_url,
        "env_ollama_base_url": env_ollama_base_url,
        "lmstudio_base_url": effective_lmstudio_base_url,
        "persisted_lmstudio_base_url": persisted_lmstudio_base_url,
        "env_lmstudio_base_url": env_lmstudio_base_url,
        "provider_url": effective_ollama_base_url,
        "configured": True,
        "message": "Provider local Ollama actif.",
        "base_url": effective_ollama_base_url,
        "has_api_key": False,
        "remote_embedding_model": "",
        "required_embedding_model": local_embedding_model,
        "embedding_provider": embedding_provider,
        "embedding_base_url": embedding_base_url(),
        "embedding_source": get_embedding_source(),
        "document_tools_source": get_document_tools_source(),
        "document_tools_base_url": document_tools_ollama_base_url(),
        "local_embeddings_required": local_embeddings_required,
        "can_use_remote": remote_available,
        "lmstudio_public_selection_enabled": _LMSTUDIO_PUBLIC_SELECTION_ENABLED,
        "fallback": fallback,
    }
