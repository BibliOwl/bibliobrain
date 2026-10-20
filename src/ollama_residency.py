"""Keep the chat model loaded, with one context window sized to the GPU.

Ollama reloads a model (about 3 s for a 4B model) whenever a request asks for
another ``num_ctx`` than the one it was loaded with, and a request without
``num_ctx`` gets Ollama's own default (65 536 on Ollama 0.40: 5.3 GB instead of
3.3 GB for qwen3.5:4b). So every call of a chat model uses one window per
model: the largest that fits next to the app's other GPU users. It is measured
once per model and GPU by loading the model at two sizes and reading its
memory from ``/api/ps``, then kept in the provider state.

Ollama unloads an idle model after 5 minutes; chat calls ask it to keep the
model for :func:`keep_alive_seconds` instead, and :func:`start_warm_up` loads
the chat and embedding models before the first question.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# A chat answer needs 6000 tokens of prompt plus its output.
CONTEXT_FLOOR = 8192
_CONTEXT_STEP = 4096
_CONTEXT_CEILING = 262144
_GIB = 1024**3
# Desktop, CUDA contexts and the embedding model.
_RESERVE_BYTES = int(1.5 * _GIB)
# Qwen reranker weights plus the activations of a deep candidate pool.
_GPU_RERANKER_RESERVE_BYTES = int(2.5 * _GIB)
_CALIBRATION_STATE_KEY = "chat_context_calibration"
_DEFAULT_KEEP_ALIVE_SECONDS = 1800

# A question asked while its model is being measured waits this long at most.
_MEASURE_WAIT_SECONDS = 30.0

_lock = threading.RLock()
_calibrations: dict[str, dict[str, Any]] | None = None
_model_locks: dict[str, threading.Lock] = {}
_warm_up_threads: dict[str, threading.Thread] = {}
# Set when no measure of that model is running.
_measures_done: dict[str, threading.Event] = {}


def _provider() -> Any:
    from src import llm_provider

    return llm_provider


def keep_alive_seconds() -> int:
    """How long Ollama keeps a chat or embedding model after its last request."""
    raw = os.getenv("OLLAMA_KEEP_ALIVE_SECONDS", "").strip()
    try:
        value = int(raw) if raw else _DEFAULT_KEEP_ALIVE_SECONDS
    except ValueError:
        value = _DEFAULT_KEEP_ALIVE_SECONDS
    return max(60, min(86400, value))


def _model_key(model_name: str) -> str:
    name = str(model_name or "").strip().lower()
    return name if ":" in name else f"{name}:latest"


def _loaded_calibrations() -> dict[str, dict[str, Any]]:
    global _calibrations
    with _lock:
        if _calibrations is None:
            stored = _provider()._load_provider_state().get(_CALIBRATION_STATE_KEY)
            _calibrations = dict(stored) if isinstance(stored, dict) else {}
        return _calibrations


def _remember(model_name: str, entry: dict[str, Any]) -> None:
    with _lock:
        calibrations = _loaded_calibrations()
        calibrations[_model_key(model_name)] = entry
        snapshot = dict(calibrations)
    _provider()._save_provider_state({_CALIBRATION_STATE_KEY: snapshot})


def calibrated_context_length(model_name: str | None) -> int:
    entry = _loaded_calibrations().get(_model_key(str(model_name or "")))
    try:
        return int((entry or {}).get("num_ctx") or 0)
    except (TypeError, ValueError):
        return 0


def _provisional_context_length(model_name: str) -> int:
    """Window used until the model is measured: the VRAM tier, never below the floor."""
    provider = _provider()
    value = max(CONTEXT_FLOOR, provider._vram_context_cap())
    model_max = provider._model_advertised_context_length(model_name)
    return min(value, model_max) if model_max else value


def pinned_context_length(model_name: str | None) -> int:
    """The ``num_ctx`` every call of this chat model uses; 0 leaves the call alone.

    An explicit window chosen in the Settings wins, as before: it already
    reaches every call. Embedding and OCR models are not chat models. While
    the model is being measured the call waits for the result: sent at once,
    it would reload the model between two measures and spoil them (seen on
    a first launch: 45 s of reloads instead of an 8 s measure).
    """
    provider = _provider()
    model = str(model_name or "").strip()
    if not model or provider.get_ollama_context_length():
        return 0
    if not provider._is_chat_model_candidate(model):
        return 0
    with _lock:
        measure_done = _measures_done.get(_model_key(model))
    if measure_done is not None:
        measure_done.wait(timeout=_MEASURE_WAIT_SECONDS)
    return calibrated_context_length(model) or _provisional_context_length(model)


@dataclass(frozen=True)
class _Footprint:
    context_length: int
    size: int
    size_vram: int
    digest: str

    @property
    def on_gpu(self) -> bool:
        return self.size_vram >= self.size


def _load_at(model_name: str, context_length: int) -> _Footprint | None:
    """Load the model with this window and read what it occupies.

    Goes around the request helper on purpose: it would replace the window
    being measured with the pinned one.
    """
    provider = _provider()
    base_url = provider._ollama_base_url()
    payload = {
        "model": model_name,
        "prompt": "",
        "stream": False,
        "keep_alive": keep_alive_seconds(),
        "options": {"num_ctx": int(context_length)},
    }
    wanted = _model_key(model_name)
    # A chat request arriving meanwhile can reload the model at another
    # window before /api/ps is read: such a reading measures nothing.
    for _attempt in range(2):
        with provider._open_ollama_url(
            provider._join_url(base_url, "/api/generate"),
            method="POST",
            json_payload=payload,
            timeout=provider._ollama_timeout(),
        ) as response:
            response.read()
        for entry in provider._read_ollama_json(base_url, "/api/ps", timeout=5).get("models", []):
            if _model_key(str(entry.get("name") or entry.get("model") or "")) != wanted:
                continue
            try:
                footprint = _Footprint(
                    context_length=int(entry.get("context_length") or 0),
                    size=int(entry.get("size") or 0),
                    size_vram=int(entry.get("size_vram") or 0),
                    digest=str(entry.get("digest") or ""),
                )
            except (TypeError, ValueError):
                break
            if footprint.context_length == int(context_length) and footprint.size > 0:
                return footprint
            break
    return None


def _round_down(value: float) -> int:
    return int(value // _CONTEXT_STEP) * _CONTEXT_STEP


def _reserved_bytes() -> int:
    reserve = _RESERVE_BYTES
    try:
        from src.rag.reranker import get_reranker_status

        status = get_reranker_status()
        if status.get("enabled") and str(status.get("device") or "").startswith("cuda"):
            reserve += _GPU_RERANKER_RESERVE_BYTES
    except Exception:
        logger.debug("Reranker device unknown; no GPU reserve kept for it.", exc_info=True)
    return reserve


def _fitting_context_length(
    small: _Footprint, large: _Footprint, *, budget: int, ceiling: int
) -> int:
    """Largest window whose memory, extrapolated from two loads, fits ``budget``."""
    grown = large.context_length - small.context_length
    per_token = (large.size - small.size) / grown if grown > 0 else 0.0
    if per_token <= 0:
        return min(large.context_length, ceiling)
    fixed = small.size - per_token * small.context_length
    return max(CONTEXT_FLOOR, min(ceiling, _round_down((budget - fixed) / per_token)))


def calibrate(model_name: str) -> int:
    """Measure the largest window that keeps the model on the GPU, and keep it.

    Costs three to six model loads, once per model and GPU. Without an NVIDIA
    GPU (no VRAM figure), with an Ollama on another computer, or when a
    measure fails, the provisional window is used and nothing is kept, so the
    next warm-up measures again. Calls of this model wait meanwhile.
    """
    model = str(model_name or "").strip()
    key = _model_key(model)
    done = threading.Event()
    with _lock:
        _measures_done[key] = done
    try:
        return _measure(model)
    finally:
        done.set()
        with _lock:
            if _measures_done.get(key) is done:
                del _measures_done[key]


def _measure(model: str) -> int:
    provider = _provider()
    total = provider._detected_gpu_vram_bytes()
    if total <= 0 or not provider._is_this_computer_url(provider._ollama_base_url()):
        provisional = _provisional_context_length(model)
        _load_at(model, provisional)
        return provisional
    ceiling = min(
        provider._model_advertised_context_length(model) or _CONTEXT_CEILING, _CONTEXT_CEILING
    )
    small = _load_at(model, CONTEXT_FLOOR)
    if small is None:
        return _provisional_context_length(model)
    chosen = CONTEXT_FLOOR
    loaded = CONTEXT_FLOOR
    if small.on_gpu and ceiling > CONTEXT_FLOOR:
        loaded = min(2 * CONTEXT_FLOOR, ceiling)
        large = _load_at(model, loaded)
        if large is None:
            return _provisional_context_length(model)
        if large.on_gpu:
            estimate = _fitting_context_length(
                small, large, budget=total - _reserved_bytes(), ceiling=ceiling
            )
            if estimate <= large.context_length:
                chosen = estimate
            else:
                # The estimate ignores what other programs hold: check it, and
                # bisect between the last window that fits and the first that spills.
                fits, spills, candidate = large.context_length, 0, estimate
                for _attempt in range(4):
                    footprint = _load_at(model, candidate)
                    loaded = candidate
                    if footprint is not None and footprint.on_gpu:
                        fits = candidate
                    else:
                        spills = candidate
                    if not spills:
                        break
                    candidate = _round_down((fits + spills) / 2)
                    if candidate <= fits:
                        break
                chosen = fits
    if loaded != chosen:
        _load_at(model, chosen)
    _remember(
        model,
        {
            "num_ctx": chosen,
            "digest": small.digest,
            "vram_total": total,
            "calibrated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    logger.info("Chat context for %s: %d tokens (VRAM %.1f GiB)", model, chosen, total / _GIB)
    return chosen


def _calibration_still_valid(model_name: str) -> bool:
    entry = _loaded_calibrations().get(_model_key(model_name)) or {}
    return bool(entry.get("num_ctx")) and entry.get("vram_total") == (
        _provider()._detected_gpu_vram_bytes()
    )


def _warm_embedding_model() -> None:
    provider = _provider()
    if provider.get_embedding_provider_mode() != "ollama":
        return
    model = provider.required_embedding_model_name()
    if not model:
        return
    with provider._open_ollama_url(
        provider._join_url(provider._embedding_ollama_base_url(), "/api/embed"),
        method="POST",
        json_payload={"model": model, "input": "warm-up", "keep_alive": keep_alive_seconds()},
        timeout=60,
    ) as response:
        response.read()


def warm_up(model_name: str) -> dict[str, Any]:
    """Load the chat model with its window, measuring the window the first time."""
    provider = _provider()
    model = str(model_name or "").strip()
    if (
        not model
        or not provider.is_ollama_provider()
        or not provider._is_chat_model_candidate(model)
    ):
        return {"ok": False, "skipped": "not_a_local_chat_model"}
    with _lock:
        model_lock = _model_locks.setdefault(_model_key(model), threading.Lock())
    with model_lock:
        started = time.perf_counter()
        explicit = provider.get_ollama_context_length()
        calibrated_now = False
        if explicit:
            context_length = explicit
            _load_at(model, explicit)
        elif _calibration_still_valid(model):
            context_length = calibrated_context_length(model)
            footprint = _load_at(model, context_length)
            stored_digest = (_loaded_calibrations().get(_model_key(model)) or {}).get("digest")
            # A re-pulled model or a busier GPU: measure again.
            if footprint is None or not footprint.on_gpu or footprint.digest != stored_digest:
                context_length = calibrate(model)
                calibrated_now = True
        else:
            context_length = calibrate(model)
            calibrated_now = True
        try:
            _warm_embedding_model()
        except Exception:
            logger.debug("Embedding warm-up failed.", exc_info=True)
    return {
        "ok": True,
        "model": model,
        "num_ctx": context_length,
        "calibrated": calibrated_now,
        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def start_warm_up(model_name: str, *, after: list[threading.Thread | None] | None = None) -> bool:
    """Warm the model in the background; ``after`` are threads to wait for first.

    At startup the reranker loads first, so its GPU check still sees the
    memory the chat model is about to take.
    """
    model = str(model_name or "").strip()
    if not model:
        return False
    key = _model_key(model)
    with _lock:
        running = _warm_up_threads.get(key)
        if running is not None and running.is_alive():
            return False

        def _run() -> None:
            for thread in after or []:
                if thread is not None:
                    thread.join(timeout=180)
            try:
                result = warm_up(model)
                logger.info("Chat model warm-up: %s", result)
            except Exception:
                logger.info("Chat model warm-up failed for %s", model, exc_info=True)

        thread = threading.Thread(target=_run, name=f"bibliobrain-warm-up-{key}", daemon=True)
        _warm_up_threads[key] = thread
        thread.start()
    return True
