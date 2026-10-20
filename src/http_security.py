"""HTTP hardening shared by the Bibliobrain API.

Everything here is transport-level policy that does not depend on the
application routes: Host-header validation against DNS rebinding, the
cross-origin guard, request body limits, security response headers,
static-file caching and the per-client rate limiter.

``app_api`` wires these into the FastAPI application; keeping them in one
module makes the policy reviewable in isolation and testable without loading
the whole API.
"""

from __future__ import annotations

import collections
import ipaddress
import logging
import os
import socket
import threading
import time
from collections.abc import Iterable

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.common import env_bool, env_int

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Host header validation (DNS rebinding protection)
# ──────────────────────────────────────────────────────────────────────────────


def host_name_from_header(value: str) -> str:
    """Extract the bare host name from a ``Host`` header value."""
    host = str(value or "").strip().lower().rstrip(".")
    if host.startswith("["):
        closing = host.find("]")
        return host[1:closing] if closing > 0 else ""
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


class LocalHostRegistry:
    """Names under which this machine may be addressed, resolved lazily."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._names: set[str] = {
            str(name or "").strip().lower().rstrip(".")
            for name in (socket.gethostname(),)
            if str(name or "").strip()
        }
        self._thread: threading.Thread | None = None

    @property
    def names(self) -> set[str]:
        with self._lock:
            return set(self._names)

    @property
    def resolution_thread(self) -> threading.Thread | None:
        return self._thread

    def reset_resolution(self) -> None:
        """Forget a finished background resolution so it can run again (tests)."""
        with self._lock:
            self._thread = None

    def start_background_resolution(self) -> bool:
        """Resolve the machine FQDN without delaying the first HTTP response."""
        with self._lock:
            if self._thread is not None:
                return False

            def _run() -> None:
                try:
                    fqdn = str(socket.getfqdn() or "").strip().lower().rstrip(".")
                    if fqdn:
                        with self._lock:
                            self._names.add(fqdn)
                except Exception:
                    logger.debug("Local FQDN resolution failed", exc_info=True)

            self._thread = threading.Thread(
                target=_run,
                name="bibliobrain-hostname-resolution",
                daemon=True,
            )
            self._thread.start()
            return True

    def is_allowed_request_host(self, value: str) -> bool:
        host = host_name_from_header(value)
        if not host:
            return False

        allowed_names = {"localhost", "testserver", *self.names}
        allowed_names.update(
            name.strip().lower().rstrip(".")
            for name in os.getenv("BIBLIOBRAIN_ALLOWED_HOSTS", "").split(",")
            if name.strip()
        )
        if host in allowed_names:
            return True

        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        if address.is_unspecified or address.is_multicast:
            return False
        return address.is_loopback or address.is_private or address.is_link_local


class LocalNetworkHostMiddleware:
    """Block DNS-rebinding hostnames while allowing literal private-LAN addresses."""

    def __init__(self, app: ASGIApp, *, registry: LocalHostRegistry) -> None:
        self.app = app
        self.registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            headers = dict(scope.get("headers") or [])
            host = headers.get(b"host", b"").decode("latin-1", errors="ignore")
            if not self.registry.is_allowed_request_host(host):
                response = Response("Invalid host header", status_code=400)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ──────────────────────────────────────────────────────────────────────────────
# Static assets
# ──────────────────────────────────────────────────────────────────────────────


class CacheControlStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
        return response


# ──────────────────────────────────────────────────────────────────────────────
# CORS — only explicit local origins; wildcards are rejected
# ──────────────────────────────────────────────────────────────────────────────

DEFAULT_ALLOWED_ORIGINS = "http://localhost:7860,http://127.0.0.1:7860"


def is_safe_origin(origin: str) -> bool:
    """Valide qu'une origine CORS est strictement locale et bien formée."""
    origin = origin.strip().lower()
    if not origin:
        return False
    # Rejeter les wildcards
    if "*" in origin:
        return False
    # Autoriser uniquement les schèmes http avec localhost / 127.0.0.1
    if origin.startswith("http://"):
        host = origin[len("http://") :]
        # Retirer le port pour vérifier l'hôte
        host_part = host.split(":")[0] if ":" in host else host
        if host_part in {"localhost", "127.0.0.1"}:
            return True
    return False


def parse_allowed_origins(raw: str | None = None) -> list[str]:
    """Return the safe origins from ``ALLOWED_ORIGINS`` (or ``raw``), never empty."""
    if raw is None:
        raw = os.getenv("ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS)
    candidates = [origin.strip() for origin in str(raw or "").split(",") if origin.strip()]
    allowed = [origin for origin in candidates if is_safe_origin(origin)]
    for rejected in candidates:
        if rejected not in allowed:
            logger.warning("CORS: origine rejetee (non-locale ou wildcard): %s", rejected)
    return allowed or ["http://localhost:7860"]


# ──────────────────────────────────────────────────────────────────────────────
# Cross-origin guard for requests that skip the CORS preflight
# ──────────────────────────────────────────────────────────────────────────────


class CrossOriginGuardMiddleware:
    """Reject cross-origin state changes that do not require a CORS preflight."""

    _SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self._allowed_origins = {origin.lower().rstrip("/") for origin in allowed_origins}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http" and scope.get("method") not in self._SAFE_METHODS:
            headers = {
                name.decode("latin-1").lower(): value.decode("latin-1")
                for name, value in scope.get("headers", [])
            }
            fetch_site = headers.get("sec-fetch-site", "").strip().lower()
            origin = headers.get("origin", "").strip().lower().rstrip("/")
            host = headers.get("host", "").strip().lower().rstrip("/")
            same_request_origin = bool(host) and origin == f"http://{host}"
            if fetch_site and fetch_site not in {"same-origin", "none"}:
                response = Response("Cross-site request blocked", status_code=403)
                await response(scope, receive, send)
                return
            if origin and origin not in self._allowed_origins and not same_request_origin:
                response = Response("Invalid origin", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ──────────────────────────────────────────────────────────────────────────────
# Request body limit
# ──────────────────────────────────────────────────────────────────────────────


# Global request body size limit (default 10 MiB; override with MAX_REQUEST_BYTES).
def max_request_bytes() -> int:
    return env_int("MAX_REQUEST_BYTES", 10 * 1024 * 1024, minimum=1024)


# A dropped document is a whole file, not a JSON payload (default 200 MiB;
# override with MAX_UPLOAD_BYTES).
def max_upload_bytes() -> int:
    return env_int("MAX_UPLOAD_BYTES", 200 * 1024 * 1024, minimum=1024)


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject oversized request bodies, including chunked bodies without a length header."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        path_limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        # Exact paths that take a file instead of JSON, with their own limit.
        self.path_limits = dict(path_limits or {})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        max_bytes = self.path_limits.get(str(scope.get("path") or ""), self.max_bytes)
        content_lengths = [
            value for name, value in scope.get("headers", []) if name.lower() == b"content-length"
        ]
        if content_lengths:
            try:
                declared_size = int(content_lengths[-1])
            except (TypeError, ValueError):
                response = JSONResponse(
                    {
                        "ok": False,
                        "error": "invalid_content_length",
                        "msg": "Invalid Content-Length",
                    },
                    status_code=400,
                )
                await response(scope, receive, send)
                return
            if declared_size < 0:
                response = JSONResponse(
                    {
                        "ok": False,
                        "error": "invalid_content_length",
                        "msg": "Invalid Content-Length",
                    },
                    status_code=400,
                )
                await response(scope, receive, send)
                return
            if declared_size > max_bytes:
                response = JSONResponse(
                    {
                        "ok": False,
                        "error": "request_body_too_large",
                        "msg": "Request body too large",
                    },
                    status_code=413,
                )
                await response(scope, receive, send)
                return

        received_size = 0

        async def limited_receive() -> Message:
            nonlocal received_size
            message = await receive()
            if message["type"] == "http.request":
                received_size += len(message.get("body", b""))
                if received_size > max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            response = JSONResponse(
                {
                    "ok": False,
                    "error": "request_body_too_large",
                    "msg": "Request body too large",
                },
                status_code=413,
            )
            await response(scope, receive, send)


# ──────────────────────────────────────────────────────────────────────────────
# Security response headers
# ──────────────────────────────────────────────────────────────────────────────


class SecurityHeadersMiddleware:
    _HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        (b"cross-origin-opener-policy", b"same-origin"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        (
            b"content-security-policy",
            b"default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
            b"script-src 'self'; "
            b"script-src-elem 'self'; "
            b"script-src-attr 'none'; "
            b"style-src 'self' 'unsafe-inline'; "
            b"font-src 'self' data:; "
            b"img-src 'self' data: blob:; connect-src 'self'",
        ),
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def add_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                existing = {name.lower() for name, _value in message.get("headers", [])}
                headers = list(message.get("headers", []))
                headers.extend(header for header in self._HEADERS if header[0] not in existing)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, add_headers)


# ──────────────────────────────────────────────────────────────────────────────
# Rate limiter — sliding window per client address
# ──────────────────────────────────────────────────────────────────────────────


def client_ip_for(request: Request) -> str:
    """Return the peer IP; trust proxy headers only when explicitly enabled."""
    if env_bool("BIBLIOBRAIN_TRUST_PROXY_HEADERS", False):
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


class RateLimiter:
    """Sliding-window request counter keyed by (client address, endpoint group)."""

    WINDOW_SECONDS = 60
    MAX_KEYS = 1024  # Memory cap: many client addresses must not grow the dict unbounded.

    def __init__(self, *, chat_per_minute: int, general_per_minute: int) -> None:
        self.chat_per_minute = int(chat_per_minute)
        self.general_per_minute = int(general_per_minute)
        # Structure: { (client_ip, endpoint_group): deque[timestamps] }
        self.windows: dict[tuple[str, str], collections.deque] = {}
        self._lock = threading.Lock()
        self._last_evict = 0.0

    @classmethod
    def from_env(cls) -> RateLimiter:
        return cls(
            chat_per_minute=env_int("RATE_LIMIT_CHAT_PER_MINUTE", 10, minimum=1, maximum=60),
            general_per_minute=env_int(
                "RATE_LIMIT_GENERAL_PER_MINUTE", 240, minimum=1, maximum=300
            ),
        )

    def check(self, client_ip: str, endpoint_group: str, max_requests: int) -> bool:
        """Return True when the request is allowed, False when the window is full."""
        now = time.monotonic()
        cutoff = now - self.WINDOW_SECONDS
        key = (client_ip, endpoint_group)

        with self._lock:
            # Periodically evict keys whose windows are entirely expired.
            if (now - self._last_evict) > self.WINDOW_SECONDS or len(self.windows) > self.MAX_KEYS:
                stale = [k for k, w in self.windows.items() if not w or w[-1] < cutoff]
                for k in stale:
                    self.windows.pop(k, None)
                self._last_evict = now

            if key not in self.windows:
                while len(self.windows) >= self.MAX_KEYS:
                    oldest_key = next(iter(self.windows), None)
                    if oldest_key is None:
                        break
                    self.windows.pop(oldest_key, None)
                self.windows[key] = collections.deque()

            window = self.windows[key]
            while window and window[0] < cutoff:
                window.popleft()

            if len(window) >= max_requests:
                return False

            window.append(now)
            return True
