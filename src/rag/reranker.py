"""Cross-encoder reranker for retrieval results.

Soft dependency on ``sentence-transformers``/``torch`` at runtime. The
project ships ``sentence-transformers`` in ``requirements.txt``, but
PyTorch itself remains installed via machine-specific requirement files
because the correct wheel depends on CPU vs CUDA support. If the stack is
missing (or the model fails to load), the rerank functions become no-ops
and the caller transparently falls back to the original order.

Public API:
    - is_reranker_enabled() -> bool
    - rerank_texts(query, texts, top_k) -> list[tuple[int, float]]
    - rerank_documents(query, docs, top_k) -> list[Document]
    - rerank_scored_docs(query, scored, top_k) -> list[tuple[Document, float]]
    - get_reranker_status() -> dict (for /api/reranker-status)

Configuration is read from environment + app_settings:
    - RERANKER_ENABLED          (env: "1"/"true"/"yes"/"on", default: true)
    - reranker_profile          (setting: one of the reviewed model profiles)
    - reranker_translate_french (setting: local FR->EN translation for Ettin)
    - RERANKER_CANDIDATE_MULT   (env int, default 4) — how many candidates
                                  to fetch per final slot before reranking
    - RERANKER_DEVICE           (legacy headless override for the auto profile)

The app_settings flag ``reranker_enabled`` overrides the env var when set.
"""

from __future__ import annotations

import gc
import logging
import os
import re
import threading
import time
from typing import Any

from langchain_core.documents import Document

_logger = logging.getLogger(__name__)

# The automatic profile deliberately uses two different models: Qwen is
# multilingual and strong enough to justify GPU inference, while Ettin is tiny
# and fast on CPU but English-only. French queries can therefore be translated
# once before Ettin scores an English candidate set.
_GPU_MODEL = "Qwen/Qwen3-Reranker-0.6B"
_CPU_MODEL = "cross-encoder/ettin-reranker-32m-v1"
_ETTIN_17M_MODEL = "cross-encoder/ettin-reranker-17m-v1"
_ETTIN_68M_MODEL = "cross-encoder/ettin-reranker-68m-v1"
_ETTIN_150M_MODEL = "cross-encoder/ettin-reranker-150m-v1"
_ETTIN_400M_MODEL = "cross-encoder/ettin-reranker-400m-v1"
_ETTIN_1B_MODEL = "cross-encoder/ettin-reranker-1b-v1"
_GTE_MODERNBERT_MODEL = "Alibaba-NLP/gte-reranker-modernbert-base"
_BGE_BASE_MODEL = "BAAI/bge-reranker-base"
_BGE_V2_M3_MODEL = "BAAI/bge-reranker-v2-m3"
_TRANSLATION_MODEL = "Helsinki-NLP/opus-mt-fr-en"
_DEFAULT_DEEP_MODEL = _BGE_V2_M3_MODEL
_PROFILE_TARGETS: dict[str, tuple[str, str]] = {
    "qwen_gpu": (_GPU_MODEL, "cuda"),
    "ettin_17m_cpu": (_ETTIN_17M_MODEL, "cpu"),
    "ettin_cpu": (_CPU_MODEL, "cpu"),
    "ettin_68m_cpu": (_ETTIN_68M_MODEL, "cpu"),
    "ettin_150m_cpu": (_ETTIN_150M_MODEL, "cpu"),
    "ettin_400m_gpu": (_ETTIN_400M_MODEL, "cuda"),
    "ettin_1b_gpu": (_ETTIN_1B_MODEL, "cuda"),
    "gte_modernbert_cpu": (_GTE_MODERNBERT_MODEL, "cpu"),
    "bge_base_gpu": (_BGE_BASE_MODEL, "cuda"),
    "bge_v2_m3_gpu": (_BGE_V2_M3_MODEL, "cuda"),
}
_ENGLISH_ONLY_MODELS = {
    _ETTIN_17M_MODEL,
    _CPU_MODEL,
    _ETTIN_68M_MODEL,
    _ETTIN_150M_MODEL,
    _ETTIN_400M_MODEL,
    _ETTIN_1B_MODEL,
    _GTE_MODERNBERT_MODEL,
}
_PINNED_MODEL_REVISIONS = {
    _GPU_MODEL: "e61197ed45024b0ed8a2d74b80b4d909f1255473",
    _ETTIN_17M_MODEL: "9e4aa35321a6dd1a43ca313f500c4b4f7cfb5cc6",
    _CPU_MODEL: "1a9b74b929899cfdbadfe6bbdd2a9ac76f33aeef",
    _ETTIN_68M_MODEL: "d166fa88ddde3c42bc3ee92f7df476d941c8204a",
    _ETTIN_150M_MODEL: "025501c4e0f9bbeb4c5b198318e0089ff061cc14",
    _ETTIN_400M_MODEL: "5dca36282a5d85f368d2544002513a29159b4c9e",
    _ETTIN_1B_MODEL: "7d20e9baad17016fdf5549c08f69a2d7ca3e60c3",
    _GTE_MODERNBERT_MODEL: "f7481e6055501a30fb19d090657df9ec1f79ab2c",
    _BGE_BASE_MODEL: "2cfc18c9415c912f9d8155881c133215df768a70",
    _BGE_V2_M3_MODEL: "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e",
    _TRANSLATION_MODEL: "c4aed37b318c763fd177aa449b44e3b783cc6c02",
}
_DEFAULT_CANDIDATE_MULT = 4
_MAX_TEXT_CHARS = 1800
_QUICK_MAX_TEXT_CHARS = 700
# A 96-candidate deep pool peaks near 4 GiB reserved on top of the CUDA
# context. Below that, Windows spills into shared system memory and scoring
# becomes minutes instead of seconds.
_MIN_QWEN_GPU_MEMORY_BYTES = 5 * 1024**3
# Qwen ships bf16 weights; older GPUs emulate bf16 very slowly.
_MIN_QWEN_CUDA_CAPABILITY = (8, 0)
# A healthy GPU scores a deep pool in about 2s. Past this, the auto profile
# stops trusting the GPU for the rest of the session.
_SLOW_GPU_FALLBACK_MS = 30_000.0
_PREDICT_QUEUE_TIMEOUT_SECONDS = 1.0

_model_lock = threading.Lock()
_predict_lock = threading.Lock()
_model_state: dict[str, Any] = {
    "model": None,
    "name": "",
    "device": "",
    "profile": "",
    "load_error": "",
    "predict_error": "",
    "tried": False,
    "loading": False,
    "load_started_at": 0.0,
    "last_load_duration_ms": 0.0,
}

# Static facts about CUDA device 0, filled by ``_cuda_can_run_qwen`` so status
# polls and bug reports can say which GPU was judged without touching CUDA.
_gpu_probe: dict[str, Any] = {}
# Set once a GPU prediction proved pathologically slow; the auto profile then
# routes to the CPU model until restart.
_slow_gpu_state: dict[str, str] = {"reason": ""}

_translation_lock = threading.Lock()
_translation_state: dict[str, Any] = {
    "model": None,
    "tokenizer": None,
    "load_error": "",
    "tried": False,
}

# Secondary models keyed by name (e.g. the deep-tier model). Kept separate from
# the primary ``_model_state`` so the existing warmup/status machinery — which
# the frontend polls — keeps operating on the primary model untouched.
_alt_lock = threading.Lock()
_alt_models: dict[str, dict[str, Any]] = {}


def _alt_entry(name: str) -> dict[str, Any]:
    with _alt_lock:
        entry = _alt_models.get(name)
        if entry is None:
            entry = {
                "model": None,
                "name": name,
                "load_error": "",
                "predict_error": "",
                "tried": False,
                "loading": False,
                "load_started_at": 0.0,
                "last_load_duration_ms": 0.0,
                "lock": threading.Lock(),
            }
            _alt_models[name] = entry
        return entry

# Per-thread metrics from the most recent rerank call. Read by ``stream_chat_api``
# to emit a ``pipeline_metrics`` event for the debug panel.
_last_metrics_local = threading.local()


def _record_metrics(payload: dict[str, Any]) -> None:
    _last_metrics_local.value = dict(payload)


def consume_last_metrics() -> dict[str, Any] | None:
    """Return and clear the last rerank metrics for this thread."""
    payload = getattr(_last_metrics_local, "value", None)
    if payload is None:
        return None
    _last_metrics_local.value = None
    return payload


class _RerankerBusy(Exception):
    """A busy reranker should preserve retrieval order, not queue indefinitely."""


def _predict_scores(
    model: Any, pairs: list[tuple[str, str]], state: dict[str, Any]
) -> tuple[Any, dict[str, Any]]:
    runtime = {
        "model_name": state.get("name", ""),
        "device": str(getattr(model, "device", state.get("device", ""))),
        "dtype": str(getattr(model, "dtype", "")),
        "profile": state.get("profile", ""),
        "candidates_in": len(pairs),
        "max_query_chars": max((len(query) for query, _ in pairs), default=0),
        "max_candidate_chars": max((len(text) for _, text in pairs), default=0),
    }
    queued_at = time.perf_counter()
    if not _predict_lock.acquire(timeout=_PREDICT_QUEUE_TIMEOUT_SECONDS):
        _record_metrics({
            **runtime, "enabled": True, "applied": False,
            "skipped_reason": "reranker_busy",
            "queue_duration_ms": round((time.perf_counter() - queued_at) * 1000, 1),
        })
        raise _RerankerBusy
    started = time.perf_counter()
    try:
        scores = model.predict(pairs)
    finally:
        _predict_lock.release()
    runtime["queue_duration_ms"] = round((started - queued_at) * 1000, 1)
    runtime["predict_duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
    if runtime["predict_duration_ms"] >= 10000:
        _logger.warning(
            "Slow reranker: model=%s device=%s dtype=%s candidates=%s predict_ms=%s queue_ms=%s",
            runtime["model_name"], runtime["device"], runtime["dtype"], len(pairs),
            runtime["predict_duration_ms"], runtime["queue_duration_ms"],
        )
    if (
        runtime["predict_duration_ms"] >= _SLOW_GPU_FALLBACK_MS
        and runtime["device"].startswith("cuda")
        and state.get("profile") == "auto"
        and not _slow_gpu_state["reason"]
    ):
        _slow_gpu_state["reason"] = (
            f"{len(pairs)} candidates took {runtime['predict_duration_ms'] / 1000:.0f}s on "
            f"{_gpu_probe.get('name') or runtime['device']}"
        )
        _logger.warning(
            "GPU reranker too slow (%s); automatic profile switches to %s on CPU",
            _slow_gpu_state["reason"], _CPU_MODEL,
        )
    return scores, runtime


def is_reranker_loaded() -> bool:
    name, device = _resolve_runtime_target()
    return bool(
        _model_state["model"] is not None
        and _model_state.get("name") == name
        and _model_state.get("device") == device
    )


def is_reranker_loading() -> bool:
    return bool(_model_state.get("loading"))


def has_reranker_predict_error() -> bool:
    return bool(str(_model_state.get("predict_error") or "").strip())


def _release_model(state: dict[str, Any]) -> None:
    """Drop a model reference and ask Python/PyTorch to free VRAM."""
    state["model"] = None
    gc.collect()
    try:
        import torch

        if hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.empty_cache()
            try:
                torch.cuda.ipc_collect()
            except Exception:
                _logger.debug("torch.cuda.ipc_collect failed", exc_info=True)
    except Exception:
        _logger.debug("Unable to release CUDA memory after reranking", exc_info=True)


def _release_model_memory() -> None:
    """Drop the primary reranker model reference and free VRAM."""
    _release_model(_model_state)


def is_model_loaded(name: str | None = None) -> bool:
    """Whether the model for ``name`` (or the primary, when None) is loaded."""
    if not name or name == _resolve_model_name():
        return is_reranker_loaded()
    with _alt_lock:
        entry = _alt_models.get(name)
    return bool(entry and entry["model"] is not None)


def trigger_load(name: str | None = None) -> dict[str, Any]:
    """Synchronously trigger a model load. Returns post-load status."""
    if not name or name == _resolve_model_name():
        _load_model()
    else:
        _load_alt_model(name)
    return get_reranker_status(name)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _enabled_from_settings() -> bool:
    """Read the user-facing toggle. app_settings overrides the env var."""
    try:
        from src.app_settings import get_app_settings

        settings = get_app_settings()
        if "reranker_enabled" in settings:
            return bool(settings.get("reranker_enabled"))
    except Exception:
        _logger.debug("Unable to read reranker_enabled from app settings", exc_info=True)
    return _bool_env("RERANKER_ENABLED", default=True)


def _reranker_settings() -> dict[str, Any]:
    try:
        from src.app_settings import get_app_settings

        return get_app_settings()
    except Exception:
        _logger.debug("Recovered from an error in _reranker_settings.", exc_info=True)
        return {}


def _resolve_profile() -> str:
    profile = str(_reranker_settings().get("reranker_profile") or "auto").strip().lower()
    return profile if profile == "auto" or profile in _PROFILE_TARGETS else "auto"


def _cuda_can_run_qwen() -> bool:
    """Return whether CUDA can run the 0.6B bf16 model at full speed."""
    try:
        import torch

        if not torch.cuda.is_available():
            return False
        properties = torch.cuda.get_device_properties(0)
        capability = (int(properties.major), int(properties.minor))
        _gpu_probe.update({
            "name": str(getattr(properties, "name", "")),
            "total_mb": int(getattr(properties, "total_memory", 0)) // 2**20,
            "capability": f"{capability[0]}.{capability[1]}",
        })
        if capability < _MIN_QWEN_CUDA_CAPABILITY:
            return False
        if int(getattr(properties, "total_memory", 0)) < _MIN_QWEN_GPU_MEMORY_BYTES:
            return False
        try:
            free_bytes, _ = torch.cuda.mem_get_info(0)
            _gpu_probe["free_mb_at_probe"] = int(free_bytes) // 2**20
            return int(free_bytes) >= _MIN_QWEN_GPU_MEMORY_BYTES
        except Exception:
            _logger.debug("Recovered from an error in _cuda_can_run_qwen.", exc_info=True)
            return True
    except Exception:
        _logger.debug("Recovered from an error in _cuda_can_run_qwen.", exc_info=True)
        return False


def _resolve_runtime_target() -> tuple[str, str]:
    """Resolve the selected ``(model, device)`` without loading model weights."""
    profile = _resolve_profile()
    if profile in _PROFILE_TARGETS:
        return _PROFILE_TARGETS[profile]

    if _slow_gpu_state["reason"]:
        return _CPU_MODEL, "cpu"

    # Once an automatic target is loaded, keep it stable. Re-checking free CUDA
    # memory after loading Qwen would otherwise count Qwen's own allocation and
    # could incorrectly flip the next status poll back to CPU.
    if (
        _model_state.get("model") is not None
        and _model_state.get("profile") == "auto"
        and (_model_state.get("name"), _model_state.get("device"))
        in {(_GPU_MODEL, "cuda"), (_CPU_MODEL, "cpu")}
    ):
        return str(_model_state["name"]), str(_model_state["device"])

    # Keep the legacy device override useful for headless installations while
    # letting the UI's automatic profile choose sensible hardware by default.
    legacy_device = os.getenv("RERANKER_DEVICE", "auto").strip().lower()
    if legacy_device == "cpu":
        return _CPU_MODEL, "cpu"
    if legacy_device == "cuda":
        return _GPU_MODEL, "cuda"
    return (_GPU_MODEL, "cuda") if _cuda_can_run_qwen() else (_CPU_MODEL, "cpu")


def _translation_enabled() -> bool:
    settings = _reranker_settings()
    if "reranker_translate_french" in settings:
        return bool(settings.get("reranker_translate_french"))
    return _bool_env("RERANKER_TRANSLATE_FRENCH", default=True)


def is_reranker_enabled() -> bool:
    return _enabled_from_settings()


def _resolve_model_name() -> str:
    """Primary model selected by the hardware/profile settings."""
    return _resolve_runtime_target()[0]


def _resolve_deep_model_name() -> str:
    """Legacy explicit model accessor; automatic depth routing no longer uses it."""
    return os.getenv("RERANKER_MODEL", "").strip() or _DEFAULT_DEEP_MODEL


def resolve_model_name_for_depth(depth: str) -> str | None:
    """Use the hardware-selected primary model for every retrieval depth."""
    # Retrieval depth still controls candidate breadth, but hardware profile now
    # controls the reranker. Returning None routes every tier to the primary.
    return None


def quick_text_chars() -> int:
    raw = os.getenv("RERANKER_QUICK_MAX_TEXT_CHARS", "").strip()
    try:
        value = int(raw) if raw else _QUICK_MAX_TEXT_CHARS
    except ValueError:
        value = _QUICK_MAX_TEXT_CHARS
    return max(200, min(_MAX_TEXT_CHARS, value))


def _resolve_device() -> str:
    return _resolve_runtime_target()[1]


def candidate_multiplier() -> int:
    raw = os.getenv("RERANKER_CANDIDATE_MULT", "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_CANDIDATE_MULT
    except ValueError:
        value = _DEFAULT_CANDIDATE_MULT
    return max(2, min(20, value))


def should_unload_after_use() -> bool:
    return _bool_env("RERANKER_UNLOAD_AFTER_USE", default=False)


def _model_revision(name: str) -> str | None:
    """Pin bundled Hugging Face models while allowing explicit custom models."""
    return _PINNED_MODEL_REVISIONS.get(name)


def _load_model() -> Any | None:
    """Lazy-load the cross-encoder. Returns None on failure.

    Sets ``_model_state['loading']`` to True during the actual load so the
    frontend can poll ``get_reranker_status()`` and show a banner. Holding
    ``_model_lock`` for the entire load serializes concurrent callers.
    """
    with _model_lock:
        name, device = _resolve_runtime_target()
        target_changed = bool(
            _model_state.get("name")
            and (
                _model_state.get("name") != name
                or _model_state.get("device") != device
            )
        )
        if target_changed:
            _release_model(_model_state)
            _model_state["tried"] = False
            _model_state["load_error"] = ""
            _model_state["predict_error"] = ""
        if _model_state["model"] is not None:
            return _model_state["model"]
        if _model_state["tried"] and _model_state["load_error"]:
            return None
        _model_state["name"] = name
        _model_state["device"] = device
        _model_state["profile"] = _resolve_profile()
        _model_state["tried"] = True
        _model_state["loading"] = True
        _model_state["load_started_at"] = time.time()
        try:
            try:
                # Lazy import to keep the module importable without the dep.
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                _model_state["load_error"] = (
                    "reranker dependencies missing. "
                    "Install a torch profile (requirements-torch-*.txt), "
                    "then reinstall requirements.txt."
                )
                _logger.info("Reranker disabled: %s", exc)
                return None

            try:
                kwargs: dict[str, Any] = {
                    "device": device,
                    "trust_remote_code": False,
                    "model_kwargs": {"use_safetensors": True},
                }
                revision = _model_revision(name)
                if revision:
                    kwargs["revision"] = revision
                model = CrossEncoder(name, **kwargs)
            except Exception as exc:
                _model_state["load_error"] = f"failed to load reranker '{name}': {exc}"
                _logger.warning("Reranker model load failed (%s): %s", name, exc)
                return None

            _model_state["model"] = model
            _model_state["load_error"] = ""
            _model_state["predict_error"] = ""
            duration_ms = (time.time() - _model_state["load_started_at"]) * 1000.0
            _model_state["last_load_duration_ms"] = round(duration_ms, 1)
            _logger.info("Reranker loaded in %.1fs: %s", duration_ms / 1000.0, name)
            return model
        finally:
            _model_state["loading"] = False


def _load_alt_model(name: str) -> Any | None:
    """Lazy-load a non-primary cross-encoder (e.g. the deep-tier model)."""
    entry = _alt_entry(name)
    with entry["lock"]:
        if entry["model"] is not None:
            return entry["model"]
        if entry["tried"] and entry["load_error"]:
            return None
        entry["tried"] = True
        entry["loading"] = True
        entry["load_started_at"] = time.time()
        try:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                entry["load_error"] = (
                    "reranker dependencies missing. "
                    "Install a torch profile (requirements-torch-*.txt), "
                    "then reinstall requirements.txt."
                )
                _logger.info("Reranker disabled: %s", exc)
                return None

            device = _resolve_device()
            try:
                kwargs: dict[str, Any] = {
                    "device": device,
                    "trust_remote_code": False,
                    "model_kwargs": {"use_safetensors": True},
                }
                revision = _model_revision(name)
                if revision:
                    kwargs["revision"] = revision
                model = CrossEncoder(name, **kwargs)
            except Exception as exc:
                entry["load_error"] = f"failed to load reranker '{name}': {exc}"
                _logger.warning("Reranker model load failed (%s): %s", name, exc)
                return None

            entry["model"] = model
            entry["device"] = device
            entry["load_error"] = ""
            entry["predict_error"] = ""
            duration_ms = (time.time() - entry["load_started_at"]) * 1000.0
            entry["last_load_duration_ms"] = round(duration_ms, 1)
            _logger.info("Reranker (deep) loaded in %.1fs: %s", duration_ms / 1000.0, name)
            return model
        finally:
            entry["loading"] = False


def _acquire_model(model_name: str | None) -> tuple[Any, dict[str, Any], bool, float]:
    """Resolve ``(model, state, triggered_load, load_duration_ms)`` for a tier.

    ``model_name`` of None (or equal to the primary) uses the warmed primary
    model; any other name routes to the alt-model registry. ``state`` is the
    dict where predict errors / unload are recorded for that model.
    """
    if not model_name or model_name == _resolve_model_name():
        was_loaded = is_reranker_loaded()
        started = time.time()
        model = _load_model()
        return model, _model_state, (not was_loaded and model is not None), (time.time() - started) * 1000.0
    was_loaded = is_model_loaded(model_name)
    started = time.time()
    model = _load_alt_model(model_name)
    return model, _alt_entry(model_name), (not was_loaded and model is not None), (time.time() - started) * 1000.0


def get_reranker_status(model_name: str | None = None) -> dict[str, Any]:
    if model_name and model_name != _resolve_model_name():
        entry = _alt_entry(model_name)
        loading = bool(entry.get("loading"))
        started_at = float(entry.get("load_started_at") or 0.0)
        elapsed = max(0.0, time.time() - started_at) if loading and started_at else 0.0
        return {
            "enabled": _enabled_from_settings(),
            "model_name": entry.get("name") or model_name,
            "device": entry.get("device", ""),
            "dtype": str(getattr(entry.get("model"), "dtype", "")),
            "loaded": entry.get("model") is not None,
            "loading": loading,
            "load_elapsed_seconds": round(elapsed, 1),
            "load_error": entry.get("load_error", ""),
            "predict_error": entry.get("predict_error", ""),
            "candidate_multiplier": candidate_multiplier(),
            "unload_after_use": should_unload_after_use(),
            "tier": "deep",
        }
    enabled = _enabled_from_settings()
    selected_name, selected_device = _resolve_runtime_target()
    loaded = is_reranker_loaded()
    state_matches_target = (
        _model_state.get("name") == selected_name
        and _model_state.get("device") == selected_device
    )
    loading = bool(_model_state.get("loading")) and state_matches_target
    started_at = float(_model_state.get("load_started_at") or 0.0)
    elapsed_seconds = max(0.0, time.time() - started_at) if loading and started_at else 0.0
    return {
        "enabled": enabled,
        "profile": _resolve_profile(),
        "model_name": selected_name,
        "device": selected_device,
        "dtype": str(getattr(_model_state.get("model"), "dtype", "")) if loaded else "",
        "loaded": loaded,
        "loading": loading,
        "load_elapsed_seconds": round(elapsed_seconds, 1),
        "load_error": _model_state["load_error"] if state_matches_target else "",
        "predict_error": _model_state.get("predict_error", "") if state_matches_target else "",
        "candidate_multiplier": candidate_multiplier(),
        "unload_after_use": should_unload_after_use(),
        "translation_enabled": _translation_enabled(),
        "translation_model": _TRANSLATION_MODEL,
        "translation_loaded": _translation_state["model"] is not None,
        "translation_error": _translation_state["load_error"],
        "gpu": dict(_gpu_probe),
        "slow_gpu_fallback": _slow_gpu_state["reason"],
    }


def warmup_reranker() -> dict[str, Any]:
    """Force-load the reranker model. Safe to call multiple times.

    Returns the post-load status dict so the caller can show success/failure.
    Blocks for the duration of the load (30-60s on first call). Callers
    should run this in a background thread / async-to-thread context.
    """
    if not _enabled_from_settings():
        return {**get_reranker_status(), "warmup_skipped": "reranker is disabled"}
    if should_unload_after_use():
        return {**get_reranker_status(), "warmup_skipped": "unload-after-use is enabled"}
    _load_model()
    return get_reranker_status()


_WORD_RE = re.compile(r"[a-zà-öø-ÿ]+", re.IGNORECASE)
_FRENCH_MARKERS = {
    "avec", "cette", "comment", "dans", "des", "est", "et", "je", "les",
    "leur", "mais", "pour", "quels", "quelle", "quelles", "sont", "une",
}
_ENGLISH_MARKERS = {
    "and", "are", "for", "from", "how", "in", "is", "of", "the", "this",
    "to", "using", "what", "which", "with",
}


def _language_marker_counts(text: str) -> tuple[int, int]:
    words = {word.lower() for word in _WORD_RE.findall(str(text or ""))}
    return len(words & _FRENCH_MARKERS), len(words & _ENGLISH_MARKERS)


def _looks_french(text: str) -> bool:
    value = str(text or "")
    french, english = _language_marker_counts(value)
    has_french_diacritic = bool(re.search(r"[àâçéèêëîïôùûüÿœ]", value, re.IGNORECASE))
    return (has_french_diacritic and french >= 1) or (french >= 2 and french > english)


def _mostly_english(texts: list[str]) -> bool:
    english = 0
    french = 0
    sample = texts[:12]
    for text in sample:
        fr_count, en_count = _language_marker_counts(text)
        if en_count > fr_count:
            english += 1
        elif fr_count > en_count:
            french += 1
    return english > french and english >= max(1, len(sample) // 2 + 1)


def _load_translation_model() -> tuple[Any, Any] | None:
    with _translation_lock:
        if _translation_state["model"] is not None:
            return _translation_state["tokenizer"], _translation_state["model"]
        if _translation_state["tried"] and _translation_state["load_error"]:
            return None
        _translation_state["tried"] = True
        try:
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

            revision = _PINNED_MODEL_REVISIONS[_TRANSLATION_MODEL]
            tokenizer = AutoTokenizer.from_pretrained(
                _TRANSLATION_MODEL,
                revision=revision,
                trust_remote_code=False,
            )
            model = AutoModelForSeq2SeqLM.from_pretrained(
                _TRANSLATION_MODEL,
                revision=revision,
                trust_remote_code=False,
                use_safetensors=True,
            ).to("cpu")
            model.eval()
        except Exception as exc:
            _translation_state["load_error"] = str(exc)
            _logger.warning("French query translation unavailable: %s", exc)
            return None
        _translation_state.update(
            {"model": model, "tokenizer": tokenizer, "load_error": ""}
        )
        return tokenizer, model


def _translate_french_query(query: str) -> str:
    loaded = _load_translation_model()
    if loaded is None:
        return query
    tokenizer, model = loaded
    try:
        import torch

        inputs = tokenizer(
            query,
            return_tensors="pt",
            truncation=True,
            max_length=256,
        )
        with torch.inference_mode():
            output_ids = model.generate(**inputs, max_length=256, num_beams=1)
        translated = str(tokenizer.decode(output_ids[0], skip_special_tokens=True) or "").strip()
        return translated or query
    except Exception as exc:
        _translation_state["load_error"] = str(exc)
        _logger.warning("French query translation failed: %s", exc)
        return query


def translate_french_to_english(text: str) -> str:
    """English version of a text known to be French, or "" (translation off or unchanged).

    For callers that know the language from the conversation: short topics
    such as "pompes de flux" carry no marker for :func:`_looks_french`.
    """
    if not _translation_enabled() or not str(text or "").strip():
        return ""
    translated = _translate_french_query(text).strip()
    return "" if translated.casefold() == str(text).strip().casefold() else translated


def needs_english_retrieval_query(query: str) -> bool:
    """Whether :func:`english_retrieval_query` will translate ``query`` (no model call)."""
    return _translation_enabled() and _looks_french(query)


def english_retrieval_query(query: str) -> str:
    """English version of a French question, or "" when not applicable.

    Used to search an English library with a French question: lexical search
    only matches the question's own words ("écrantage"), so a French question
    retrieved the few French documents instead of the relevant papers.
    """
    if not _translation_enabled() or not _looks_french(query):
        return ""
    translated = _translate_french_query(query).strip()
    if not translated or translated.casefold() == str(query or "").strip().casefold():
        return ""
    return translated


def _prepare_query_for_reranking(
    query: str,
    candidate_texts: list[str],
    state: dict[str, Any],
) -> tuple[str, bool, float]:
    if (
        not _translation_enabled()
        or state.get("name") not in _ENGLISH_ONLY_MODELS
        or not _looks_french(query)
        or not _mostly_english(candidate_texts)
    ):
        return query, False, 0.0
    started = time.time()
    translated = _translate_french_query(query)
    elapsed_ms = (time.time() - started) * 1000.0
    return translated, translated.strip() != query.strip(), elapsed_ms


def _doc_text_for_scoring(doc: Document, max_chars: int | None = None) -> str:
    limit = max_chars if max_chars and max_chars > 0 else _MAX_TEXT_CHARS
    text = str(getattr(doc, "page_content", "") or "")
    if len(text) > limit:
        text = text[:limit]
    metadata = getattr(doc, "metadata", {}) or {}
    title = str(metadata.get("zotero_title", "") or metadata.get("title", "") or "").strip()
    if title:
        return f"{title}\n\n{text}"
    return text


def _plain_text_for_scoring(text: str, max_chars: int | None = None) -> str:
    limit = max_chars if max_chars and max_chars > 0 else _MAX_TEXT_CHARS
    value = str(text or "")
    if len(value) > limit:
        value = value[:limit]
    return value


def rerank_texts(
    query: str,
    texts: list[str],
    top_k: int | None = None,
) -> list[tuple[int, float]]:
    """Return ``(original_index, reranker_score)`` pairs ordered by relevance.

    This generic variant lets non-LangChain pipelines reuse the same lazy
    cross-encoder stack. If the reranker is disabled or unavailable, it returns
    the original order with neutral ``0.0`` scores.
    """
    if not texts:
        return []
    limit = top_k if top_k is not None else len(texts)
    if limit <= 0:
        return []

    fallback = [(idx, 0.0) for idx in range(min(len(texts), limit))]
    enabled = _enabled_from_settings()
    if not enabled:
        _record_metrics({"enabled": False, "applied": False, "candidates_in": len(texts)})
        return fallback

    was_loaded = is_reranker_loaded()
    load_started = time.time()
    model = _load_model()
    load_duration_ms = (time.time() - load_started) * 1000.0
    triggered_load = not was_loaded and model is not None

    if model is None:
        _record_metrics(
            {
                "enabled": True,
                "applied": False,
                "load_failed": True,
                "load_error": _model_state.get("load_error", ""),
                "candidates_in": len(texts),
                "input_type": "text",
            }
        )
        return fallback

    if has_reranker_predict_error():
        _record_metrics(
            {
                "enabled": True,
                "applied": False,
                "predict_error": _model_state.get("predict_error", ""),
                "candidates_in": len(texts),
                "skipped_reason": "previous_predict_error",
                "input_type": "text",
            }
        )
        return fallback

    try:
        candidate_texts = [_plain_text_for_scoring(text) for text in texts]
        scoring_query, translated, translation_ms = _prepare_query_for_reranking(
            query, candidate_texts, _model_state
        )
        pairs = [(scoring_query, text) for text in candidate_texts]
        scores, prediction_metrics = _predict_scores(model, pairs, _model_state)
    except _RerankerBusy:
        return fallback
    except Exception as exc:
        _model_state["predict_error"] = str(exc)
        _logger.warning("Reranker predict failed, returning original order: %s", exc)
        _release_model_memory()
        _record_metrics(
            {
                "enabled": True,
                "applied": False,
                "predict_error": str(exc),
                "candidates_in": len(texts),
                "input_type": "text",
            }
        )
        return fallback

    indexed = [(idx, float(scores[idx])) for idx in range(len(texts))]
    indexed.sort(key=lambda item: item[1], reverse=True)
    result = indexed[:limit]

    unload_after_use = should_unload_after_use()
    _record_metrics(
        {
            "enabled": True,
            "applied": True,
            "model_name": _model_state.get("name", ""),
            "candidates_in": len(texts),
            "top_k": len(result),
            **prediction_metrics,
            "triggered_load": triggered_load,
            "load_duration_ms": round(load_duration_ms, 1) if triggered_load else 0.0,
            "unloaded_after_use": unload_after_use,
            "input_type": "text",
            "query_translated": translated,
            "translation_duration_ms": round(translation_ms, 1),
        }
    )
    if unload_after_use:
        _release_model_memory()
    return result


def rerank_documents(
    query: str,
    docs: list[Document],
    top_k: int | None = None,
    *,
    model_name: str | None = None,
    max_text_chars: int | None = None,
) -> list[Document]:
    """Return ``docs`` reordered by reranker score, truncated to ``top_k``.

    ``model_name`` selects the cross-encoder (None = primary/quick model; a name
    routes to the deep-tier model). ``max_text_chars`` truncates each candidate
    before scoring. If reranker is disabled or fails, returns ``docs[:top_k]``.
    """
    if not docs:
        return []
    limit = top_k if top_k is not None else len(docs)
    if limit <= 0:
        return []

    enabled = _enabled_from_settings()
    if not enabled:
        _record_metrics({"enabled": False, "applied": False, "candidates_in": len(docs)})
        return list(docs)[:limit]

    model, state, triggered_load, load_duration_ms = _acquire_model(model_name)

    if model is None:
        _record_metrics(
            {
                "enabled": True,
                "applied": False,
                "load_failed": True,
                "load_error": state.get("load_error", ""),
                "candidates_in": len(docs),
                "model_name": state.get("name", ""),
            }
        )
        return list(docs)[:limit]

    if str(state.get("predict_error") or "").strip():
        _record_metrics(
            {
                "enabled": True,
                "applied": False,
                "predict_error": state.get("predict_error", ""),
                "candidates_in": len(docs),
                "skipped_reason": "previous_predict_error",
                "model_name": state.get("name", ""),
            }
        )
        return list(docs)[:limit]

    try:
        candidate_texts = [_doc_text_for_scoring(d, max_text_chars) for d in docs]
        scoring_query, translated, translation_ms = _prepare_query_for_reranking(
            query, candidate_texts, state
        )
        pairs = [(scoring_query, text) for text in candidate_texts]
        scores, prediction_metrics = _predict_scores(model, pairs, state)
    except _RerankerBusy:
        return list(docs)[:limit]
    except Exception as exc:
        state["predict_error"] = str(exc)
        _logger.warning("Reranker predict failed, returning original order: %s", exc)
        _release_model(state)
        _record_metrics(
            {
                "enabled": True,
                "applied": False,
                "predict_error": str(exc),
                "candidates_in": len(docs),
                "model_name": state.get("name", ""),
            }
        )
        return list(docs)[:limit]

    indexed = list(enumerate(docs))
    indexed.sort(key=lambda item: float(scores[item[0]]), reverse=True)
    result = [doc for _, doc in indexed[:limit]]

    unload_after_use = should_unload_after_use()
    _record_metrics(
        {
            "enabled": True,
            "applied": True,
            "model_name": state.get("name", ""),
            "candidates_in": len(docs),
            "top_k": len(result),
            **prediction_metrics,
            "triggered_load": triggered_load,
            "load_duration_ms": round(load_duration_ms, 1) if triggered_load else 0.0,
            "unloaded_after_use": unload_after_use,
            "query_translated": translated,
            "translation_duration_ms": round(translation_ms, 1),
        }
    )
    if unload_after_use:
        _release_model(state)
    return result


def rerank_scored_docs(
    query: str,
    scored: list[tuple[Document, float]],
    top_k: int | None = None,
) -> list[tuple[Document, float]]:
    """Variant that preserves original retrieval scores alongside reranker order."""
    if not scored:
        return []
    limit = top_k if top_k is not None else len(scored)
    if limit <= 0:
        return []

    if not _enabled_from_settings():
        return list(scored)[:limit]

    model = _load_model()
    if model is None:
        return list(scored)[:limit]
    if has_reranker_predict_error():
        return list(scored)[:limit]

    docs = [doc for doc, _ in scored]
    try:
        candidate_texts = [_doc_text_for_scoring(d) for d in docs]
        scoring_query, _, _ = _prepare_query_for_reranking(
            query, candidate_texts, _model_state
        )
        pairs = [(scoring_query, text) for text in candidate_texts]
        rerank_scores, prediction_metrics = _predict_scores(model, pairs, _model_state)
    except _RerankerBusy:
        return list(scored)[:limit]
    except Exception as exc:
        _model_state["predict_error"] = str(exc)
        _logger.warning("Reranker predict failed, returning original order: %s", exc)
        _release_model_memory()
        return list(scored)[:limit]

    enriched = [
        (scored[i][0], scored[i][1], float(rerank_scores[i])) for i in range(len(scored))
    ]
    enriched.sort(key=lambda item: item[2], reverse=True)
    _record_metrics({
        **prediction_metrics, "enabled": True, "applied": True,
        "top_k": min(len(enriched), limit), "input_type": "scored_documents",
    })
    if should_unload_after_use():
        _release_model_memory()
    return [(doc, original_score) for doc, original_score, _ in enriched[:limit]]


__all__ = [
    "candidate_multiplier",
    "consume_last_metrics",
    "get_reranker_status",
    "has_reranker_predict_error",
    "is_model_loaded",
    "is_reranker_enabled",
    "is_reranker_loaded",
    "is_reranker_loading",
    "quick_text_chars",
    "rerank_documents",
    "rerank_scored_docs",
    "rerank_texts",
    "resolve_model_name_for_depth",
    "should_unload_after_use",
    "trigger_load",
    "warmup_reranker",
]
