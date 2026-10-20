"""Application setup and shared server state.

Feature routers import this module; it never imports those routers. Lifecycle,
stream cancellation and model warmup each have one owner across requests.
"""

import contextlib
import logging
import os
import signal
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import (
    JSONResponse,
)

from src import config  # noqa: F401 — suppression warnings pypdf
from src.api.streams import stop_local_ollama_model
from src.app_settings import (
    get_app_settings,
    set_app_settings,
)
from src.http_security import (
    CacheControlStaticFiles,
    CrossOriginGuardMiddleware,
    LocalHostRegistry,
    LocalNetworkHostMiddleware,
    RateLimiter,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    client_ip_for,
    max_request_bytes,
    max_upload_bytes,
    parse_allowed_origins,
)
from src.llm_provider import (
    public_provider_info,
    required_embedding_model_name,
    requires_local_ollama_embeddings,
)
from src.logging_setup import configure_logging
from src.network_security import (
    install_uvicorn_access_query_redaction,
)
from src.ollama_utils import (
    check_ollama_health,
    is_local_ollama_url,
    start_ollama_server,
)

# Console + rotating file logging under the Bibliobrain home. Idempotent, so the
# managed launcher and a direct uvicorn import both end up with one setup.
configure_logging(config.LOG_DIR)


async def discover_articles(**kwargs):
    from src.article_discovery import discover_articles as _discover_articles

    return await _discover_articles(**kwargs)


async def download_and_store_paper_pdf(**kwargs):
    from src.article_discovery import (
        download_and_store_paper_pdf as _download_and_store_paper_pdf,
    )

    return await _download_and_store_paper_pdf(**kwargs)


def clear_runtime_state():
    from src.rag.state import clear_runtime_state as _clear_runtime_state

    return _clear_runtime_state()


def get_runtime_status():
    from src.rag.state import get_runtime_status as _get_runtime_status

    return _get_runtime_status()


def pause_vector_loading():
    from src.rag.state import pause_vector_loading as _pause_vector_loading

    return _pause_vector_loading()


def set_vector_loading_suspended(suspended: bool) -> None:
    from src.rag import state as rag_state

    if suspended:
        rag_state.suspend_vector_loading()
    else:
        rag_state.resume_vector_loading()


def get_vector_database_status():
    from src.rag.vector_store import get_vector_database_status as _get_vector_database_status

    return _get_vector_database_status()


def get_vector_reload_preview(*args, **kwargs):
    from src.rag.vector_store import get_vector_reload_preview as _get_vector_reload_preview

    return _get_vector_reload_preview(*args, **kwargs)


def reset_vector_database():
    from src.rag.vector_store import reset_vector_database as _reset_vector_database

    return _reset_vector_database()


def adopt_equivalent_vector_index(**kwargs):
    from src.rag.embedding_equivalence import (
        adopt_equivalent_vector_index as _adopt_equivalent_vector_index,
    )

    return _adopt_equivalent_vector_index(**kwargs)


def set_active_vector_index_profile(**kwargs):
    from src.rag.vector_store import (
        set_active_vector_index_profile as _set_active_vector_index_profile,
    )

    return _set_active_vector_index_profile(**kwargs)


def init_rag(*args, **kwargs):
    from src.rag.query_engine import init_rag as _init_rag

    return _init_rag(*args, **kwargs)


def stream_chat_api(*args, **kwargs):
    from src.rag.query_engine import stream_chat_api as _stream_chat_api

    return _stream_chat_api(*args, **kwargs)


def _ensure_local_ollama_started() -> dict:
    """Reuse a healthy local Ollama API or start it when the app needs it."""
    if not requires_local_ollama_embeddings():
        return {
            "ok": False,
            "started": False,
            "code": "not_required",
            "msg": "Ollama n'est pas requis par la configuration actuelle.",
        }

    provider_info = public_provider_info()
    url = str(provider_info.get("embedding_base_url") or "http://localhost:11434").strip()
    if not is_local_ollama_url(url):
        return {
            "ok": False,
            "started": False,
            "code": "non_local_url",
            "msg": "Le démarrage automatique est disponible uniquement pour un Ollama local.",
            "url": url,
        }

    health = check_ollama_health(
        url,
        required_embedding_model=required_embedding_model_name(),
        required=True,
    )
    if health.get("ollama_ok"):
        return {
            "ok": True,
            "started": False,
            "code": "already_running",
            "msg": "Ollama est déjà démarré.",
            "url": url,
        }

    result = start_ollama_server()
    result["url"] = url
    return result


_ollama_startup_lock = threading.Lock()
_ollama_startup_thread: threading.Thread | None = None


def _start_local_ollama_background() -> bool:
    """Check/start local Ollama without delaying ASGI application readiness."""
    global _ollama_startup_thread

    with _ollama_startup_lock:
        if _ollama_startup_thread is not None and _ollama_startup_thread.is_alive():
            return False

        def _run() -> None:
            try:
                ollama_startup = _ensure_local_ollama_started()
                if ollama_startup.get("ok"):
                    logger.info("%s", ollama_startup.get("msg") or "Ollama prêt.")
                elif ollama_startup.get("code") not in {"not_required", "non_local_url"}:
                    logger.warning(
                        "Démarrage automatique d'Ollama impossible : %s",
                        ollama_startup.get("msg")
                        or ollama_startup.get("code")
                        or "erreur inconnue",
                    )
            except Exception:
                # Ollama reste réparable depuis l'interface ; son absence ne doit pas
                # empêcher le backend et les providers distants de démarrer.
                logger.exception("Échec du contrôle automatique d'Ollama au démarrage.")

        _ollama_startup_thread = threading.Thread(
            target=_run,
            name="bibliobrain-ollama-startup",
            daemon=True,
        )
        _ollama_startup_thread.start()
        return True


@contextlib.asynccontextmanager
async def _app_lifespan(_app: FastAPI):  # pragma: no cover - executed at runtime
    # Les helpers sont définis plus bas dans le module ; résolus à l'exécution.
    _local_hosts.start_background_resolution()
    _start_local_ollama_background()
    _start_reranker_warmup_background()
    _start_chat_warmup_background()
    from src.update_check import start_background_checks

    # Sends nothing unless the user enabled update checks; see src/update_check.py.
    start_background_checks()
    from src import zotero_outbox

    # PDFs dropped while Zotero was not answering, before the last shutdown.
    zotero_outbox.resume()
    if _managed_server:
        with _browser_session_lock:
            # Stop a managed server that was launched without any browser window.
            # Once a window registers, only an explicit window-close notification
            # can arm automatic shutdown; background tabs never expire on a TTL.
            _arm_browser_shutdown_locked(120.0)
    try:
        yield
    finally:
        with _browser_session_lock:
            _cancel_browser_shutdown_locked()


app = FastAPI(title="BIBLIOBRAIN API", lifespan=_app_lifespan)
# HTTP hardening lives in src/http_security.py. Middleware order matters: the
# last one added is the outermost. Host validation is registered first so it
# runs innermost, after the access and cross-origin guards.
_local_hosts = LocalHostRegistry()
app.add_middleware(LocalNetworkHostMiddleware, registry=_local_hosts)
logger = logging.getLogger(__name__)
install_uvicorn_access_query_redaction()
# httpx logs full outgoing URLs at INFO, including OpenAlex `search` parameters.
# Keep those transport logs below the application threshold so names and other
# search terms never appear in normal Bibliobrain logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
_managed_server = os.getenv("BIBLIOBRAIN_MANAGED_SERVER", "").strip().lower() in {
    "1",
    "true",
    "yes",
}
_BROWSER_SHUTDOWN_GRACE_SECONDS = 5.0
_browser_session_lock = threading.RLock()
_browser_sessions: set[str] = set()
_browser_shutdown_timer: threading.Timer | None = None
_managed_active_operations = 0
_rag_init_lock = threading.Lock()


def _cancel_browser_shutdown_locked() -> None:
    global _browser_shutdown_timer
    if _browser_shutdown_timer is not None:
        _browser_shutdown_timer.cancel()
    _browser_shutdown_timer = None


def _arm_browser_shutdown_locked(delay_seconds: float) -> None:
    global _browser_shutdown_timer
    if not _managed_server:
        return
    _cancel_browser_shutdown_locked()
    timer = threading.Timer(max(0.1, delay_seconds), _evaluate_browser_sessions)
    timer.daemon = True
    _browser_shutdown_timer = timer
    timer.start()


def _evaluate_browser_sessions() -> None:
    """Stop only when no explicitly opened window or operation remains."""
    should_shutdown = False
    with _browser_session_lock:
        _cancel_browser_shutdown_locked()
        if not _browser_sessions and not _managed_active_operations and _managed_server:
            should_shutdown = True
    if should_shutdown:
        _schedule_process_shutdown(0.1, require_inactive=True)


def _register_browser_session(session_id: str) -> None:
    if not _managed_server:
        return
    with _browser_session_lock:
        _browser_sessions.add(session_id)
        _cancel_browser_shutdown_locked()


def _close_browser_session(session_id: str) -> None:
    if not _managed_server:
        return
    with _browser_session_lock:
        _browser_sessions.discard(session_id)
        if _browser_sessions or _managed_active_operations:
            _cancel_browser_shutdown_locked()
        else:
            # A short grace period lets a refresh register its replacement page.
            _arm_browser_shutdown_locked(_BROWSER_SHUTDOWN_GRACE_SECONDS)


def _begin_managed_operation() -> bool:
    global _managed_active_operations
    if not _managed_server:
        return False
    with _browser_session_lock:
        _managed_active_operations += 1
        _cancel_browser_shutdown_locked()
    return True


def _end_managed_operation(active: bool = True) -> None:
    global _managed_active_operations
    if not active:
        return
    with _browser_session_lock:
        _managed_active_operations = max(0, _managed_active_operations - 1)
        if _browser_sessions or _managed_active_operations:
            _cancel_browser_shutdown_locked()
        else:
            _arm_browser_shutdown_locked(_BROWSER_SHUTDOWN_GRACE_SECONDS)


@contextlib.contextmanager
def _managed_operation():
    """Keep the packaged server alive while a user action is still running."""
    active = _begin_managed_operation()
    try:
        yield
    finally:
        _end_managed_operation(active)


def _remove_managed_pid_file() -> None:
    pid_path = Path(config.RUNTIME_DIR) / "server.pid"
    try:
        if pid_path.is_file() and pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pid_path.unlink()
    except Exception:
        logger.debug("Unable to remove the managed server PID file.", exc_info=True)


def _shutdown_runtime_and_process(*, require_inactive: bool = False) -> None:
    if require_inactive:
        with _browser_session_lock:
            if _browser_sessions or _managed_active_operations:
                return
    try:
        settings = get_app_settings()
        stop_local_ollama_model(str(settings.get("selected_model") or "").strip())
    except Exception:
        logger.debug("Unable to unload the active Ollama model during shutdown.", exc_info=True)
    _remove_managed_pid_file()
    logger.info("Stopping the managed Bibliobrain server.")
    os.kill(os.getpid(), signal.SIGTERM)


def _schedule_process_shutdown(
    delay_seconds: float,
    *,
    require_inactive: bool = False,
) -> None:
    timer = threading.Timer(
        max(0.0, delay_seconds),
        _shutdown_runtime_and_process,
        kwargs={"require_inactive": require_inactive},
    )
    timer.daemon = True
    timer.start()


_ALLOWED_ORIGINS = parse_allowed_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)


app.add_middleware(CrossOriginGuardMiddleware, allowed_origins=_ALLOWED_ORIGINS)


app.add_middleware(
    RequestBodyLimitMiddleware,
    max_bytes=max_request_bytes(),
    path_limits={"/api/library/upload": max_upload_bytes()},
)


app.add_middleware(SecurityHeadersMiddleware)

_reranker_warmup_lock = threading.Lock()
_reranker_warmup_thread: threading.Thread | None = None


def _start_reranker_warmup_background() -> dict[str, Any]:
    """Start reranker warmup without blocking the HTTP/UI startup path."""
    global _reranker_warmup_thread
    from src.rag.reranker import get_reranker_status, warmup_reranker

    status = get_reranker_status()
    if not status.get("enabled"):
        return {**status, "warmup_started": False, "warmup_skipped": "reranker is disabled"}
    if status.get("loaded"):
        return {**status, "warmup_started": False, "warmup_skipped": "already loaded"}
    if status.get("unload_after_use"):
        return {
            **status,
            "warmup_started": False,
            "warmup_skipped": "unload-after-use is enabled",
        }
    if status.get("loading"):
        return {**status, "warmup_started": False, "warmup_running": True}

    with _reranker_warmup_lock:
        if _reranker_warmup_thread is not None and _reranker_warmup_thread.is_alive():
            return {**get_reranker_status(), "warmup_started": False, "warmup_running": True}

        def _run() -> None:
            try:
                warmup_reranker()
            except Exception:
                # Reranker loading failures are reflected through get_reranker_status().
                logger.debug("Recovered from an error in _run.", exc_info=True)

        _reranker_warmup_thread = threading.Thread(
            target=_run,
            name="bibliobrain-reranker-warmup",
            daemon=True,
        )
        _reranker_warmup_thread.start()

    return {**get_reranker_status(), "warmup_started": True, "warmup_running": True}


def _start_chat_warmup_background(model_name: str | None = None) -> dict[str, Any]:
    """Load the chat model with its context window, after Ollama and the reranker.

    The reranker goes first so its free-VRAM check runs before the chat model
    takes its share. Without a model name, the one last selected is used.
    """
    from src.llm_provider import is_ollama_provider
    from src.ollama_residency import start_warm_up

    model = str(model_name or get_app_settings().get("selected_model") or "").strip()
    if not model:
        return {"ok": False, "warmup_started": False, "warmup_skipped": "no_model"}
    if not is_ollama_provider():
        return {"ok": False, "warmup_started": False, "warmup_skipped": "not_ollama"}
    started = start_warm_up(model, after=[_ollama_startup_thread, _reranker_warmup_thread])
    return {"ok": True, "model": model, "warmup_started": started}


# Per-client sliding-window limits (RATE_LIMIT_CHAT_PER_MINUTE, RATE_LIMIT_GENERAL_PER_MINUTE).
_rate_limiter = RateLimiter.from_env()


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Middleware de rate limiting appliqué aux endpoints sensibles."""
    # Les endpoints SSE/chat sont les plus critiques
    path = request.url.path
    if request.method == "OPTIONS" or path == "/" or path.startswith("/static/"):
        return await call_next(request)
    if request.method == "GET" and path == "/api/notebooks":
        # A read-only listing must not consume the scarce generation/SSE quota.
        return await call_next(request)

    if path == "/api/chat" or path.endswith("/chat"):
        # Endpoint chat (SSE) - limite stricte
        max_req = _rate_limiter.chat_per_minute
        group = "chat"
    else:
        # Autres endpoints - limite générale
        max_req = _rate_limiter.general_per_minute
        group = "general"

    client_ip = client_ip_for(request)

    if not _rate_limiter.check(client_ip, group, max_req):
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "msg": f"Trop de requetes. Maximum {max_req} requetes par minute pour cet endpoint.",
            },
            headers={"Retry-After": "60"},
        )

    response = await call_next(request)
    return response


STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
app.mount("/static", CacheControlStaticFiles(directory=STATIC_DIR), name="static")

_persisted_app_settings = get_app_settings()
if _persisted_app_settings.get("data_path"):
    config.set_data_path(str(_persisted_app_settings.get("data_path")))
if _persisted_app_settings.get("metadata_source") != "none" and _persisted_app_settings.get(
    "zotero_path"
):
    config.set_zotero_path(str(_persisted_app_settings.get("zotero_path")))


def _persist_app_settings_best_effort(**kwargs: Any) -> str:
    try:
        set_app_settings(**kwargs)
        return ""
    except Exception as exc:
        logger.warning("Unable to persist app settings: %s", exc)
        return " Parametres non sauvegardes."
