from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import os
import platform
import re
import threading
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import urlparse

import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import HttpTransport
from sentry_sdk.types import Event

from src.common import env_bool as _env_bool
from src.version import get_app_version

_MAX_BACKEND_LOGS = 200
_MAX_CLIENT_LOGS = 30
_MAX_CHAT_MESSAGES = 20
_MAX_SCREENSHOT_BYTES = 300 * 1024
_MAX_SCREENSHOT_DATA_URL_CHARS = 425_000
_SCREENSHOT_PREFIX = "data:image/jpeg;base64,"
_LOG_HANDLER_MARKER = "_bibliobrain_bug_report_buffer"
_DEFAULT_BUGSINK_DSN = "https://2fabb47a5a884f299b8baa53d84692df@report.bibliobrain.org/1"
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+")
_AUTHORIZATION_RE = re.compile(
    r"(?i)((?:authorization|proxy-authorization|x-api-key|cookie|set-cookie)"
    r"\s*[:=]\s*)[^\r\n]+"
)
_SECRET_RE = re.compile(
    r"""(?ix)(
        ["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|
        passphrase|client[_-]?secret|private[_-]?key|secret)["']?\s*[:=]\s*|
        --(?:api-key|access-token|token|password|secret)(?:=|\s+)
    )"""
    r"""(?:"[^"]*"|'[^']*'|[^\s,;]+)"""
)
_DSN_AUTH_RE = re.compile(r"(?i)(https?://)[^/@\s]+@")
_KNOWN_TOKEN_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:sk-[a-z0-9_-]{16,}|hf_[a-z0-9]{16,}|"
    r"gh[pousr]_[a-z0-9_]{20,}|xox[baprs]-[a-z0-9-]{10,}|AIza[a-z0-9_-]{20,})"
)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
    r"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
)
_URL_QUERY_RE = re.compile(r"(?i)(https?://[^\s?#]+)\?[^\s#]*")
_URL_FRAGMENT_RE = re.compile(r"(?i)(https?://[^\s#]+)#[^\s]*")
_FILE_URI_RE = re.compile(r"""(?i)\bfile:///[^\r\n\s"'<>]+""")
_QUOTED_LOCAL_PATH_RE = re.compile(
    r"""(?i)(["'])(?:[a-z]:[\\/]|\\\\|/(?:home|users|private|tmp|var/tmp)/).*?\1"""
)
_WINDOWS_PATH_RE = re.compile(r"""(?i)(?<![\w])(?:[a-z]:[\\/]|\\\\)[^\r\n\s"'<>|]+""")
_UNIX_LOCAL_PATH_RE = re.compile(
    r"""(?i)(?<![\w])/(?:home|users|private|tmp|var/tmp)/[^\r\n\s"'<>]+"""
)
_DOCUMENT_NAME_RE = re.compile(
    r"""(?i)(?<![\w.-])[^\\/\s"'<>|:]+\.(?:pdf|docx?|odt|rtf|tex|bib)(?![\w])"""
)
_EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b")
_IPV4_RE = re.compile(
    r"(?<![\w.])(?:25[0-5]|2[0-4]\d|1?\d?\d)"
    r"(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\w.])"
)
_IPV6_RE = re.compile(r"(?i)(?<![\w:])(?:[0-9a-f]{1,4}:){2,7}[0-9a-f]{0,4}(?![\w:])|(?<!:)::1(?!:)")
_PRIVATE_CONTENT_RE = re.compile(
    r"""(?im)(\b["']?(?:system[_ -]?prompt|user[_ -]?prompt|prompt|conversation|chat[_ -]?history|"""
    r"page[_ -]?content|document[_ -]?(?:text|content)|pdf[_ -]?text|llm[_ -]?(?:input|output)|"
    r"""completion)["']?\s*[:=]\s*).*$"""
)
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)(?:^|[_-])(?:authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|"
    r"api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|passphrase|"
    r"client[_-]?secret|private[_-]?key|secret|dsn|session[_-]?id|csrf[^_-]*)(?:$|[_-])"
)
_PRIVATE_CONTENT_FIELD_RE = re.compile(
    r"(?i)^(?:prompt|system_prompt|user_prompt|messages|conversation|chat_history|"
    r"page_content|document_text|document_content|pdf_text|full_text|request_body|"
    r"response_body|local_variables|vars|llm_input|llm_output|completion)$"
)
_SDK_LOCK = threading.Lock()
_SDK_INITIALIZED_DSN = ""
_RATE_LIMIT_DROP_REASONS = {"status_429", "self_rate_limits"}
# How long the user is kept waiting for a report. The SDK only gives up on a silent
# server after HttpTransport.TIMEOUT (30 s), so a report can still be in flight
# past this: its delivery is then unconfirmed, neither a success nor a failure.
_DELIVERY_WAIT_SECONDS = 15.0
_FLUSH_TIMEOUT_SECONDS = 5.0


class _DropRecorder:
    """Remembers what the SDK transport did with events, since capture_event never raises.

    Outcomes are kept per event id, and only for the reports under delivery. The SDK
    also sends envelopes of its own (client reports), and a transport replaced after
    a DSN change can still be finishing a request: neither says anything about the
    report being sent now.

    A report is settled once the transport is done with it, whether it was dropped
    or delivered. Until then nothing is known: no recorded drop is not a delivery.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reasons: dict[str, list[str]] = {}
        self._settled: dict[str, threading.Event] = {}

    def watch(self, event_id: str) -> None:
        with self._lock:
            self._reasons[event_id] = []
            self._settled[event_id] = threading.Event()

    def forget(self, event_id: str) -> None:
        with self._lock:
            self._reasons.pop(event_id, None)
            self._settled.pop(event_id, None)

    def record(self, event_id: str | None, reason: str) -> None:
        with self._lock:
            if not event_id or event_id not in self._reasons:
                return
            self._reasons[event_id].append(reason)
            # A drop is final, including the one of a full queue, which never
            # reaches the worker thread.
            self._settled[event_id].set()

    def settle(self, event_id: str | None) -> None:
        with self._lock:
            settled = self._settled.get(event_id) if event_id else None
        if settled is not None:
            settled.set()

    def wait(self, event_id: str, timeout: float) -> bool:
        """Wait for the transport to be done with a report; False if it still is not."""
        with self._lock:
            settled = self._settled.get(event_id)
        return settled is not None and settled.wait(timeout)

    def failure(self, event_id: str) -> str | None:
        with self._lock:
            reasons = list(self._reasons.get(event_id, ()))
        if not reasons:
            return None
        if any(reason in _RATE_LIMIT_DROP_REASONS for reason in reasons):
            return "bug_report_rate_limited"
        return "bug_report_delivery_failed"


_DROPS = _DropRecorder()
_HANDLED_ENVELOPE = threading.local()


@contextmanager
def _handling(envelope: Envelope) -> Iterator[None]:
    """Mark the envelope this thread is handling, so a drop can be traced to its event."""
    previous = getattr(_HANDLED_ENVELOPE, "event_id", None)
    _HANDLED_ENVELOPE.event_id = envelope.headers.get("event_id")
    try:
        yield
    finally:
        _HANDLED_ENVELOPE.event_id = previous


class _RecordingTransport(HttpTransport):
    def capture_envelope(self, envelope: Envelope) -> None:
        # A full queue is reported here, on the calling thread.
        with _handling(envelope):
            super().capture_envelope(envelope)

    def _send_envelope(self, envelope: Envelope) -> None:
        # Every other drop is reported while the worker thread sends the envelope.
        event_id = envelope.headers.get("event_id")
        with _handling(envelope):
            try:
                super()._send_envelope(envelope)
            except BaseException:
                # The SDK swallows this one; without a drop it would read as delivered.
                _DROPS.record(event_id, "send_error")
                raise
            # Returning without a drop means the server accepted the envelope.
            _DROPS.settle(event_id)

    def on_dropped_event(self, reason: str) -> None:
        _DROPS.record(getattr(_HANDLED_ENVELOPE, "event_id", None), reason)
        super().on_dropped_event(reason)


def redact_text(
    value: Any,
    *,
    max_length: int = 2000,
    preserve_email: bool = False,
) -> str:
    """Remove credentials, personal identifiers, and local document details."""
    text = str(value or "")
    home = os.path.expanduser("~").rstrip("\\/")
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", text)
    text = _SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _DSN_AUTH_RE.sub(r"\1[REDACTED]@", text)
    text = _KNOWN_TOKEN_RE.sub("[REDACTED]", text)
    text = _JWT_RE.sub("[REDACTED]", text)
    text = _URL_QUERY_RE.sub(r"\1?[QUERY_REDACTED]", text)
    text = _URL_FRAGMENT_RE.sub(r"\1#[FRAGMENT_REDACTED]", text)
    text = _FILE_URI_RE.sub("[LOCAL_PATH]", text)
    text = _QUOTED_LOCAL_PATH_RE.sub(r"\1[LOCAL_PATH]\1", text)
    text = _WINDOWS_PATH_RE.sub("[LOCAL_PATH]", text)
    text = _UNIX_LOCAL_PATH_RE.sub("[LOCAL_PATH]", text)
    if len(home) > 3 and home not in {"/", "\\"}:
        text = re.sub(re.escape(home), "[USER_HOME]", text, flags=re.IGNORECASE)
    text = _DOCUMENT_NAME_RE.sub("[DOCUMENT]", text)
    text = _PRIVATE_CONTENT_RE.sub(r"\1[CONTENT OMITTED]", text)
    if not preserve_email:
        text = _EMAIL_RE.sub("[EMAIL]", text)
    text = _IPV4_RE.sub("[IP_ADDRESS]", text)
    text = _IPV6_RE.sub("[IP_ADDRESS]", text)
    return text[:max_length]


class RecentLogHandler(logging.Handler):
    """Thread-safe in-memory log tail used only for opt-in bug reports."""

    def __init__(self, capacity: int = _MAX_BACKEND_LOGS) -> None:
        super().__init__(level=logging.WARNING)
        self._records: deque[dict[str, str]] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        setattr(self, _LOG_HANDLER_MARKER, True)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            item = {
                "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
                "level": record.levelname,
                "logger": redact_text(record.name, max_length=120),
                "message": redact_text(self.format(record)),
            }
            with self._lock:
                self._records.append(item)
        except Exception:
            self.handleError(record)

    def snapshot(self, limit: int = 100) -> list[dict[str, str]]:
        bounded = max(0, min(int(limit), _MAX_BACKEND_LOGS))
        with self._lock:
            return list(self._records)[-bounded:] if bounded else []


def install_log_capture() -> RecentLogHandler:
    root = logging.getLogger()
    for handler in root.handlers:
        if getattr(handler, _LOG_HANDLER_MARKER, False) and isinstance(handler, RecentLogHandler):
            return handler
    handler = RecentLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    uvicorn_error = logging.getLogger("uvicorn.error")
    if not any(getattr(item, _LOG_HANDLER_MARKER, False) for item in uvicorn_error.handlers):
        uvicorn_error.addHandler(handler)
    return handler


_recent_logs = install_log_capture()


def _validated_dsn() -> str:
    if not _env_bool("BIBLIOBRAIN_ERROR_REPORTING_ENABLED", True):
        raise ValueError("bug_report_not_configured")
    dsn = os.getenv("BIBLIOBRAIN_BUGSINK_DSN", "").strip() or _DEFAULT_BUGSINK_DSN
    parsed = urlparse(dsn)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.username
        or not parsed.path.strip("/")
    ):
        raise ValueError("bug_report_invalid_dsn")
    return dsn


def bug_report_status() -> dict[str, Any]:
    try:
        _validated_dsn()
    except ValueError:
        return {"configured": False, "provider": "bugsink"}
    return {"configured": True, "provider": "bugsink"}


def _scrub_event_value(value: Any, *, field_name: str = "") -> Any:
    if field_name == "screenshot_data_url":
        try:
            return validate_screenshot_data_url(value)
        except ValueError:
            return "[INVALID SCREENSHOT OMITTED]"
    if field_name and _SENSITIVE_FIELD_RE.search(field_name):
        return "[REDACTED]"
    if field_name and _PRIVATE_CONTENT_FIELD_RE.match(field_name):
        return "[CONTENT OMITTED]"
    if isinstance(value, dict):
        return {
            str(key)[:120]: _scrub_event_value(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_scrub_event_value(item) for item in value]
    if isinstance(value, str):
        return redact_text(
            value,
            max_length=5000,
            preserve_email=field_name.lower() == "contact",
        )
    return value


def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Last-chance recursive scrub before an event leaves the user's machine."""
    cleaned = _scrub_event_value(event)
    if isinstance(cleaned, dict):
        cleaned.pop("user", None)
        # The computer name often contains its owner's first name.
        cleaned.pop("server_name", None)
        request = cleaned.get("request")
        if isinstance(request, dict):
            for field in ("cookies", "data", "headers", "query_string", "env"):
                if field in request:
                    request[field] = "[REDACTED]"
    return cleaned


def _app_version() -> str:
    return get_app_version()


def validate_screenshot_data_url(value: Any) -> str:
    """Accept only a small JPEG data URL suitable for a Bugsink event payload."""
    data_url = str(value or "").strip()
    if not data_url:
        return ""
    if len(data_url) > _MAX_SCREENSHOT_DATA_URL_CHARS or not data_url.startswith(
        _SCREENSHOT_PREFIX
    ):
        raise ValueError("bug_report_invalid_screenshot")
    encoded = data_url[len(_SCREENSHOT_PREFIX) :]
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("bug_report_invalid_screenshot") from exc
    if len(decoded) > _MAX_SCREENSHOT_BYTES or not decoded.startswith(b"\xff\xd8\xff"):
        raise ValueError("bug_report_invalid_screenshot")
    return data_url


def _sanitize_attached_chat(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    entries: list[dict[str, Any]] = []
    raw_entries = value.get("entries")
    if isinstance(raw_entries, list):
        for item in raw_entries[-_MAX_CHAT_MESSAGES:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role not in {"user", "assistant"}:
                continue
            entry: dict[str, Any] = {
                "role": role,
                "content": redact_text(item.get("content"), max_length=4000),
                "content_truncated": len(str(item.get("content") or "")) > 4000,
                "original_content_length": len(str(item.get("content") or "")),
                "model": redact_text(item.get("model"), max_length=200),
                "created_at": redact_text(item.get("created_at"), max_length=80),
            }
            debug_metrics = item.get("debug_metrics")
            if isinstance(debug_metrics, dict):
                entry["debug_metrics"] = _scrub_event_value(debug_metrics)
            if item.get("interrupted"):
                entry["interrupted"] = True
                entry["interruption_reason"] = redact_text(
                    item.get("interruption_reason"), max_length=40
                )
                try:
                    entry["interruption_elapsed_ms"] = max(
                        0.0, float(item.get("interruption_elapsed_ms") or 0.0)
                    )
                except (TypeError, ValueError):
                    pass
                entry["answer_origin"] = redact_text(item.get("answer_origin"), max_length=80)
                entry["mode"] = redact_text(item.get("mode"), max_length=80)
            entries.append(entry)
    return {
        "message_count": min(max(int(value.get("message_count") or len(entries)), 0), 100_000),
        "included_count": len(entries),
        "entries": entries,
    }


def _ensure_sdk_initialized(dsn: str) -> None:
    global _SDK_INITIALIZED_DSN
    if _SDK_INITIALIZED_DSN == dsn:
        return
    with _SDK_LOCK:
        if _SDK_INITIALIZED_DSN == dsn:
            return
        sentry_sdk.init(
            dsn=dsn,
            release=_app_version(),
            environment=(
                os.getenv("BIBLIOBRAIN_ERROR_TRACKING_ENVIRONMENT", "production").strip()
                or "production"
            ),
            send_default_pii=False,
            # Empty rather than None: None makes the SDK fill in the computer name.
            server_name="",
            include_local_variables=False,
            traces_sample_rate=0.0,
            enable_logs=False,
            max_breadcrumbs=150,
            shutdown_timeout=2.0,
            transport=_RecordingTransport,
            default_integrations=False,
            auto_enabling_integrations=False,
            before_send=cast(Any, _before_send),
        )
        _SDK_INITIALIZED_DSN = dsn


def _sanitize_client_logs(values: list[dict[str, Any]]) -> list[dict[str, str]]:
    cleaned: list[dict[str, str]] = []
    for value in values[-_MAX_CLIENT_LOGS:]:
        if not isinstance(value, dict):
            continue
        cleaned.append(
            {
                "timestamp": redact_text(value.get("timestamp"), max_length=50),
                "type": redact_text(value.get("type"), max_length=40),
                "message": redact_text(value.get("message"), max_length=1000),
                "source": redact_text(value.get("source"), max_length=300),
                "location": redact_text(value.get("location"), max_length=80),
            }
        )
    return cleaned


def _breadcrumb_level(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"fatal", "critical"}:
        return "fatal"
    if normalized in {"error", "window.error", "unhandledrejection"}:
        return "error"
    if normalized in {"warning", "warn"}:
        return "warning"
    return "info"


def _build_breadcrumbs(
    backend_logs: list[dict[str, str]],
    client_logs: list[dict[str, str]],
) -> dict[Literal["values"], list[dict[str, Any]]]:
    values: list[dict[str, Any]] = []
    for item in backend_logs:
        values.append(
            {
                "timestamp": item.get("timestamp", ""),
                "type": "default",
                "category": f"backend.{item.get('logger', 'app')}",
                "level": _breadcrumb_level(item.get("level")),
                "message": item.get("message", ""),
            }
        )
    for item in client_logs:
        values.append(
            {
                "timestamp": item.get("timestamp", ""),
                "type": "default",
                "category": f"frontend.{item.get('type', 'error')}",
                "level": _breadcrumb_level(item.get("type")),
                "message": item.get("message", ""),
                "data": {
                    "source": item.get("source", ""),
                    "location": item.get("location", ""),
                },
            }
        )
    return {"values": values[-150:]}


async def deliver_bug_report(
    *,
    description: str,
    steps: str = "",
    contact: str = "",
    include_diagnostics: bool = True,
    client_logs: list[dict[str, Any]] | None = None,
    client_context: dict[str, Any] | None = None,
    attached_chat: dict[str, Any] | None = None,
    model_info: dict[str, Any] | None = None,
    debug_info: dict[str, Any] | None = None,
    screenshot_data_url: str = "",
) -> dict[str, str]:
    dsn = _validated_dsn()
    report_id = str(uuid.uuid4())
    app_version = _app_version()
    screenshot = validate_screenshot_data_url(screenshot_data_url)
    diagnostics: dict[str, Any] | None = None
    if include_diagnostics:
        context = client_context or {}
        backend_logs = _recent_logs.snapshot(100)
        sanitized_client_logs = _sanitize_client_logs(client_logs or [])
        diagnostics = {
            "app_version": app_version,
            "platform": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "client": {
                "language": redact_text(context.get("language"), max_length=20),
                "view": redact_text(context.get("view"), max_length=80),
                "user_agent": redact_text(context.get("user_agent"), max_length=500),
            },
            "backend_logs": backend_logs,
            "client_logs": sanitized_client_logs,
        }

    event: Event = {
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC),
        "platform": "python",
        "level": "error",
        "logger": "bibliobrain.user_report",
        "message": redact_text(description.strip(), max_length=5000),
        "tags": {
            "source": "bibliobrain-desktop",
            "report_kind": "manual",
            "schema_version": "2",
            "report_id": report_id,
        },
        "extra": {
            "report_id": report_id,
            "steps_to_reproduce": redact_text(steps.strip(), max_length=5000),
            "contact": redact_text(contact.strip(), max_length=320, preserve_email=True),
        },
    }
    sanitized_chat = _sanitize_attached_chat(attached_chat)
    if sanitized_chat and sanitized_chat["entries"]:
        event["extra"]["attached_conversation"] = sanitized_chat
    if model_info:
        event["extra"]["models"] = _scrub_event_value(model_info)
    if include_diagnostics and debug_info:
        event["extra"]["debug"] = _scrub_event_value(debug_info)
    if screenshot:
        event["extra"]["screenshot_data_url"] = screenshot
        event["extra"]["screenshot_format"] = "image/jpeg"
        event["extra"]["screenshot_bytes"] = len(
            base64.b64decode(screenshot[len(_SCREENSHOT_PREFIX) :])
        )
    if diagnostics is not None:
        event["contexts"] = {
            "bibliobrain": {
                "type": "app",
                "app_version": diagnostics["app_version"],
                "active_view": diagnostics["client"]["view"],
                "ui_language": diagnostics["client"]["language"],
            },
            "runtime": {
                "type": "runtime",
                "name": "Python",
                "version": diagnostics["python"],
            },
            "os": {
                "type": "os",
                "name": platform.system(),
                "version": platform.release(),
            },
            "client": {
                "type": "client",
                "user_agent": diagnostics["client"]["user_agent"],
            },
        }
        event["breadcrumbs"] = _build_breadcrumbs(
            diagnostics["backend_logs"], diagnostics["client_logs"]
        )

    def _capture() -> tuple[str, bool]:
        watched_id = str(event["event_id"])
        _DROPS.watch(watched_id)
        try:
            _ensure_sdk_initialized(dsn)
            event_id = sentry_sdk.capture_event(event)
            if not event_id:
                raise RuntimeError("bug_report_delivery_failed")
            # Wait on the report itself. A flush only says whether the queue emptied
            # in time, and gives up long before the SDK does on a slow request.
            settled = _DROPS.wait(watched_id, _DELIVERY_WAIT_SECONDS)
            if settled:
                # Lets the SDK's own envelopes out; behind a request still in
                # flight it could only time out.
                sentry_sdk.flush(timeout=_FLUSH_TIMEOUT_SECONDS)
            failure = _DROPS.failure(watched_id)
            if failure:
                raise RuntimeError(failure)
            return str(event_id), settled
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("bug_report_delivery_failed") from exc
        finally:
            _DROPS.forget(watched_id)

    remote_id, confirmed = await asyncio.to_thread(_capture)
    return {
        "report_id": report_id,
        "remote_id": remote_id,
        # "unconfirmed": the request was still in flight when the wait ended. It may
        # yet succeed or fail, and nobody is told either way.
        "delivery": "confirmed" if confirmed else "unconfirmed",
    }


__all__ = [
    "RecentLogHandler",
    "bug_report_status",
    "deliver_bug_report",
    "install_log_capture",
    "redact_text",
    "validate_screenshot_data_url",
]
