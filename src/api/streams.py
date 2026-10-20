"""Cancellation registry for streamed generations and discovery requests.

Each SSE generation registers an abort signal under a request id, so the
``/api/stream/abort`` route (or a page exit) can stop it and, when closing the
stream is not enough, unload the local model it was using. This module is the
single owner of that registry.
"""

import asyncio
import logging
import threading
import time
import uuid

from src.llm_provider import StreamAbortSignal, is_remote_provider, unload_ollama_model

_logger = logging.getLogger(__name__)

# Stopping a chat answer closes its stream, which ends Ollama's generation, so
# the model stays loaded for the next question. Leaving the page, and the
# drafting and notebook stops (some of their calls are not streamed), still
# unload it.
_STOPS_KEEPING_MODEL = {"user_stop", "client_disconnect", "context_switch", "chat_deleted"}

_stream_abort_lock = threading.Lock()
_stream_abort_signals: dict[str, dict[str, object]] = {}
_discovery_abort_lock = threading.Lock()
_discovery_abort_signals: set[StreamAbortSignal] = set()

_STREAM_ABORT_REASONS = {
    "user_stop",
    "page_exit",
    "chat_deleted",
    "context_switch",
    "notebook_stop",
    "drafting_stop",
    "client_disconnect",
    "unknown",
}


def normalize_stream_abort_reason(reason: str | None) -> str:
    normalized = str(reason or "").strip().lower()
    return normalized if normalized in _STREAM_ABORT_REASONS else "unknown"


def stream_abort_reason(signal: StreamAbortSignal, fallback: str = "unknown") -> str:
    reason = signal.abort_reason() if hasattr(signal, "abort_reason") else ""
    return normalize_stream_abort_reason(reason or fallback)


def stream_interruption_text(reason: str) -> str:
    normalized = normalize_stream_abort_reason(reason)
    if normalized == "user_stop":
        return "[Réponse interrompue par l'utilisateur]"
    if normalized == "page_exit":
        return "[Réponse interrompue : page fermée ou rechargée]"
    if normalized == "chat_deleted":
        return "[Réponse interrompue : conversation supprimée]"
    if normalized == "context_switch":
        return "[Réponse interrompue : changement de contexte]"
    if normalized == "client_disconnect":
        return "[Réponse interrompue : connexion avec l'interface fermée]"
    return "[Réponse interrompue]"


def register_stream_abort_signal(
    model_name: str | None = None,
    request_id: str | None = None,
) -> tuple[str, StreamAbortSignal]:
    request_id = str(request_id or "").strip()[:128] or uuid.uuid4().hex
    signal = StreamAbortSignal()
    with _stream_abort_lock:
        _stream_abort_signals[request_id] = {
            "signal": signal,
            "model": str(model_name or "").strip(),
        }
    return request_id, signal


def _record_signal(record: dict[str, object] | None) -> StreamAbortSignal | None:
    signal = (record or {}).get("signal")
    return signal if isinstance(signal, StreamAbortSignal) else None


def abort_stream_request(request_id: str, reason: str = "unknown") -> bool:
    key = str(request_id or "").strip()
    if not key:
        return False
    with _stream_abort_lock:
        record = _stream_abort_signals.get(key)
    signal = _record_signal(record)
    if signal is None:
        return False
    signal.abort(normalize_stream_abort_reason(reason))
    return True


def abort_stream_requests_for_model(model_name: str | None, reason: str = "unknown") -> int:
    normalized = str(model_name or "").strip()
    if not normalized:
        return 0
    with _stream_abort_lock:
        signals = [
            signal
            for record in _stream_abort_signals.values()
            if str(record.get("model") or "").strip() == normalized
            for signal in [_record_signal(record)]
            if signal is not None
        ]
    for abort_signal in signals:
        abort_signal.abort(normalize_stream_abort_reason(reason))
    return len(signals)


def release_stream_abort_signal(request_id: str) -> None:
    key = str(request_id or "").strip()
    if not key:
        return
    with _stream_abort_lock:
        _stream_abort_signals.pop(key, None)


def register_discovery_abort_signal() -> StreamAbortSignal:
    signal = StreamAbortSignal()
    with _discovery_abort_lock:
        _discovery_abort_signals.add(signal)
    return signal


def release_discovery_abort_signal(signal: StreamAbortSignal | None) -> None:
    if signal is None:
        return
    with _discovery_abort_lock:
        _discovery_abort_signals.discard(signal)


def abort_discovery_requests() -> int:
    with _discovery_abort_lock:
        signals = list(_discovery_abort_signals)
    for abort_signal in signals:
        try:
            abort_signal.abort()
        except Exception:
            _logger.debug("Unable to abort a discovery request.", exc_info=True)
    return len(signals)


def stop_local_ollama_model(model_name: str | None) -> bool:
    normalized = str(model_name or "").strip()
    if not normalized or is_remote_provider():
        return False
    stopped = False
    for attempt in range(2):
        stopped = unload_ollama_model(normalized, timeout_seconds=2) or stopped
        if stopped:
            break
        if attempt == 0:
            time.sleep(0.15)
    return stopped


async def stop_local_ollama_model_async(model_name: str | None) -> bool:
    """Unload Ollama without blocking the server event loop."""
    return await asyncio.to_thread(stop_local_ollama_model, model_name)


def stop_needs_unload(reason: str | None) -> bool:
    """Whether this stop must unload the model to end its generation."""
    return normalize_stream_abort_reason(reason) not in _STOPS_KEEPING_MODEL


async def release_model_after_stop_async(model_name: str | None, reason: str | None) -> bool:
    """Unload the model after a stop only when closing the stream is not enough."""
    if not stop_needs_unload(reason):
        return False
    return await stop_local_ollama_model_async(model_name)
