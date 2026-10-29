from __future__ import annotations

import asyncio
import importlib
import io
import json
import threading
import time
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

import src.config as config


def _reload_module(name: str) -> Any:
    module = cast(Any, importlib.import_module(name))
    return cast(Any, importlib.reload(module))


class _ApiTestModules:
    """Keep API assertions and dependency patches valid across feature modules.

    Production imports each feature explicitly. Tests patch every imported copy
    of a shared dependency, just as they did with the previous single module.
    """

    def __init__(self, modules: list[Any]) -> None:
        object.__setattr__(self, "_modules", modules)

    def __getattr__(self, name: str) -> Any:
        for module in self._modules:
            if hasattr(module, name):
                return getattr(module, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        owners = [module for module in self._modules if hasattr(module, name)]
        if not owners:
            raise AttributeError(name)
        for module in owners:
            setattr(module, name, value)


def _load_app_api_module(
    storage_dir: Path,
    data_dir: Path,
) -> Any:
    """Recharge l'API avec des chemins locaux au test pour eviter les effets de bord globaux."""
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    config.STORAGE_DIR = str(storage_dir)
    config.DATA_PATH = str(data_dir)

    for name in [
        "src.analysis_cache_db",
        "src.chat_history_db",
        "src.discovery_history_db",
        "src.app_settings",
        "src.rag_settings",
        "src.rag.zotero_integration",
        "src.rag.vector_store",
        "src.rag",
        "src.library",
        "src.library_taxonomy",
        "src.notebooks",
    ]:
        _reload_module(name)

    common = _reload_module("src.api.common")
    streams = _reload_module("src.api.streams")
    application = _reload_module("src.api.application")
    feature_names = (
        "chat",
        "graphs",
        "bug_reports",
        "notebooks",
        "drafting",
        "discovery",
        "library",
        "models",
        "settings",
        "lifecycle",
        "files",
    )
    features = [_reload_module(f"src.api.{name}") for name in feature_names]
    entry = _reload_module("app_api")
    api = _ApiTestModules([entry, application, common, streams, *features])
    # Switching the embedding model re-embeds probe passages with the new
    # service: over the network here, up to a minute of timeouts. The check
    # itself is covered in tests/test_embedding_equivalence.py.
    api.adopt_equivalent_vector_index = lambda **_kwargs: None
    return api


def _load_llm_provider_module(storage_dir: Path) -> Any:
    storage_dir.mkdir(parents=True, exist_ok=True)
    config.STORAGE_DIR = str(storage_dir)
    return _reload_module("src.llm_provider")


def test_managed_browser_sessions_shutdown_only_after_last_window_closes(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_MANAGED_SERVER", "1")
    app_api = _load_app_api_module(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    scheduled_delays: list[float] = []
    scheduled_shutdowns: list[tuple[float, bool]] = []
    monkeypatch.setattr(
        app_api,
        "_arm_browser_shutdown_locked",
        lambda delay: scheduled_delays.append(float(delay)),
    )
    monkeypatch.setattr(
        app_api,
        "_schedule_process_shutdown",
        lambda delay, require_inactive=False: scheduled_shutdowns.append(
            (float(delay), bool(require_inactive))
        ),
    )
    client = TestClient(app_api.app)

    opened = client.post(
        "/api/app/window/open",
        json={"session_id": "browser-window-1"},
    )
    assert opened.status_code == 200
    assert opened.json()["auto_shutdown"] is True
    assert "browser-window-1" in app_api._browser_sessions
    assert scheduled_delays == []

    # Kept for compatibility with cached frontends, without TTL semantics.
    heartbeat = client.post(
        "/api/app/window/heartbeat",
        json={"session_id": "browser-window-1"},
    )
    assert heartbeat.status_code == 200
    assert scheduled_delays == []
    app_api._evaluate_browser_sessions()
    assert "browser-window-1" in app_api._browser_sessions
    assert scheduled_shutdowns == []

    second_opened = client.post(
        "/api/app/window/open",
        json={"session_id": "browser-window-2"},
    )
    assert second_opened.status_code == 200

    closed = client.post(
        "/api/app/window/close",
        json={"session_id": "browser-window-1"},
    )
    assert closed.status_code == 200
    assert "browser-window-1" not in app_api._browser_sessions
    assert "browser-window-2" in app_api._browser_sessions
    assert scheduled_delays == []

    last_closed = client.post(
        "/api/app/window/close",
        json={"session_id": "browser-window-2"},
    )
    assert last_closed.status_code == 200
    assert scheduled_delays[-1] == app_api._BROWSER_SHUTDOWN_GRACE_SECONDS


def test_managed_operation_defers_automatic_shutdown(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_MANAGED_SERVER", "1")
    app_api = _load_app_api_module(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    armed_delays: list[float] = []
    shutdowns: list[tuple[float, bool]] = []
    monkeypatch.setattr(
        app_api,
        "_arm_browser_shutdown_locked",
        lambda delay: armed_delays.append(float(delay)),
    )
    monkeypatch.setattr(
        app_api,
        "_schedule_process_shutdown",
        lambda delay, require_inactive=False: shutdowns.append(
            (float(delay), bool(require_inactive))
        ),
    )

    with app_api._managed_operation():
        assert app_api._managed_active_operations == 1
        app_api._evaluate_browser_sessions()
        assert shutdowns == []
        assert armed_delays == []

    assert app_api._managed_active_operations == 0
    assert armed_delays[-1] == app_api._BROWSER_SHUTDOWN_GRACE_SECONDS


def test_rag_init_is_counted_as_a_managed_operation(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_MANAGED_SERVER", "1")
    app_api = _load_app_api_module(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(app_api, "_arm_browser_shutdown_locked", lambda _delay: None)

    def fake_init_rag(*_args: Any, **_kwargs: Any) -> tuple[bool, str]:
        started.set()
        assert release.wait(timeout=2)
        return True, "ready"

    monkeypatch.setattr(app_api, "init_rag", fake_init_rag)

    async def consume_init_stream() -> str:
        response = await app_api.api_init(app_api.InitRequest(model="demo"))
        assert started.wait(timeout=1)
        assert app_api._managed_active_operations == 1
        concurrent = await app_api.api_init(app_api.InitRequest(model="demo"))
        assert concurrent.status_code == 409
        assert json.loads(concurrent.body)["ok"] is False
        release.set()
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        return "".join(chunks)

    body = asyncio.run(consume_init_stream())

    assert '"type": "done"' in body
    assert '"ok": true' in body
    assert app_api._managed_active_operations == 0


def test_explicit_shutdown_requires_managed_local_server(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_MANAGED_SERVER", "1")
    app_api = _load_app_api_module(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    scheduled: list[tuple[float, bool]] = []
    monkeypatch.setattr(app_api, "is_loopback_request", lambda _request: True)
    monkeypatch.setattr(
        app_api,
        "_schedule_process_shutdown",
        lambda delay, require_inactive=False: scheduled.append(
            (float(delay), bool(require_inactive))
        ),
    )

    response = TestClient(app_api.app).post("/api/app/shutdown")

    assert response.status_code == 200
    assert response.json()["stopping"] is True
    assert scheduled == [(0.4, False)]


def test_rate_limiter_skips_root_and_static_assets(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api._rate_limiter.windows.clear()
    app_api._rate_limiter.general_per_minute = 1

    client = TestClient(app_api.app)

    assert client.get("/").status_code == 200
    assert client.get("/static/js/app.js").status_code == 200
    assert client.get("/api/analysis-types").status_code == 200
    assert client.get("/api/analysis-types").status_code == 429


def test_notebook_listing_does_not_consume_generation_rate_limit(
    workspace_tmp_dir: Path,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    app_api._rate_limiter.windows.clear()
    app_api._rate_limiter.chat_per_minute = 1
    app_api._rate_limiter.general_per_minute = 2
    client = TestClient(app_api.app)

    assert client.get("/api/notebooks").status_code == 200
    assert client.get("/api/notebooks").status_code == 200
    assert client.get("/api/notebooks").status_code == 200


def test_cross_origin_guard_blocks_state_changes(workspace_tmp_dir: Path) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    client = TestClient(app_api.app)

    foreign_origin = client.post(
        "/api/app/shutdown",
        headers={"Origin": "https://attacker.example"},
    )
    cross_site = client.post(
        "/api/app/shutdown",
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    same_lan_origin = client.post(
        "/api/app/shutdown",
        headers={
            "Host": "192.168.1.42:7860",
            "Origin": "http://192.168.1.42:7860",
            "Sec-Fetch-Site": "same-origin",
        },
    )

    assert foreign_origin.status_code == 403
    assert cross_site.status_code == 403
    assert same_lan_origin.status_code == 409


def test_heavy_async_handlers_delegate_sync_work(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    delegated: list[Any] = []

    async def fake_to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        delegated.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(app_api.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(app_api, "get_vector_reload_preview", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(app_api, "build_article_taxonomy_payload", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(app_api, "suggest_citations_for_draft", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(app_api, "suggest_argument_improvements", lambda **_kwargs: {"ok": True})
    monkeypatch.setattr(
        app_api,
        "get_conversation_messages",
        lambda *_args, **_kwargs: [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Réponse"},
        ],
    )
    monkeypatch.setattr(app_api, "is_remote_provider", lambda: False)
    monkeypatch.setattr(app_api, "is_lmstudio_provider", lambda: False)
    monkeypatch.setattr(
        app_api, "invoke_ollama_generate", lambda *_args, **_kwargs: ("", "Titre utile")
    )
    monkeypatch.setattr(app_api, "stop_local_ollama_model", lambda _model: True)
    monkeypatch.setattr(app_api, "rename_conversation", lambda *_args: True)

    async def run_handlers() -> None:
        await app_api.api_rag_reload_preview()
        await app_api.api_knowledge_graph(engine="taxonomy")
        await app_api.api_drafting_suggestions(app_api.DraftingRequest(paragraph="Texte"))
        await app_api.api_drafting_argument_suggestions(app_api.DraftingReviewRequest(text="Texte"))
        result = await app_api.api_autotitle_chat(
            "chat-1", app_api.ChatAutoTitleRequest(model="demo")
        )
        assert result == {"ok": True, "title": "Titre utile"}
        assert await app_api.stop_local_ollama_model_async("demo") is True

    asyncio.run(run_handlers())

    assert app_api.get_vector_reload_preview in delegated
    assert app_api.build_article_taxonomy_payload in delegated
    assert app_api.suggest_citations_for_draft in delegated
    assert app_api.suggest_argument_improvements in delegated
    assert app_api.invoke_ollama_generate in delegated
    assert app_api.stop_local_ollama_model in delegated


@pytest.mark.parametrize(
    ("result_status", "expected_http"),
    [
        ("invalid_request", 422),
        ("model_incompatible", 422),
        ("generation_invalid", 502),
        ("verification_failed", 502),
        ("error", 503),
    ],
)
def test_argument_suggestion_api_maps_failed_states_to_http_errors(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
    result_status: str,
    expected_http: int,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    monkeypatch.setattr(
        app_api,
        "suggest_argument_improvements",
        lambda **_kwargs: {
            "ok": False,
            "status": result_status,
            "summary": "Explicit failure.",
            "suggestions": [],
            "review_candidates": [],
            "claims": [],
            "sources": [],
            "model": {},
        },
    )

    response = asyncio.run(
        app_api.api_drafting_argument_suggestions(
            app_api.DraftingReviewRequest(text="Texte", model="model:8b")
        )
    )

    assert response.status_code == expected_http
    assert json.loads(response.body)["status"] == result_status


@pytest.mark.parametrize("result_status", ["success", "no_sources", "verification_rejected", "empty"])
def test_argument_suggestion_api_keeps_business_results_as_http_200(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
    result_status: str,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    monkeypatch.setattr(
        app_api,
        "suggest_argument_improvements",
        lambda **_kwargs: {"ok": True, "status": result_status, "suggestions": []},
    )

    response = asyncio.run(
        app_api.api_drafting_argument_suggestions(
            app_api.DraftingReviewRequest(text="Texte", model="model:8b")
        )
    )

    assert response["ok"] is True
    assert response["status"] == result_status
    assert response["suggestions"] == []
    assert response["request_id"]


def test_argument_suggestion_api_returns_cancelled_with_request_id(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")

    def cancelled(**_kwargs):
        raise InterruptedError("cancelled")

    monkeypatch.setattr(app_api, "suggest_argument_improvements", cancelled)
    response = asyncio.run(
        app_api.api_drafting_argument_suggestions(
            app_api.DraftingReviewRequest(
                text="Texte",
                model="model:8b",
                request_id="argument-request-1",
            )
        )
    )

    assert response.status_code == 409
    payload = json.loads(response.body)
    assert payload["ok"] is False
    assert payload["status"] == "cancelled"
    assert payload["request_id"] == "argument-request-1"


def test_argument_suggestion_api_passes_requested_kind(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    captured: dict[str, Any] = {}

    def fake_suggest_argument_improvements(**kwargs):
        captured.update(kwargs)
        return {"ok": True, "status": "success", "suggestions": []}

    monkeypatch.setattr(app_api, "suggest_argument_improvements", fake_suggest_argument_improvements)
    response = asyncio.run(
        app_api.api_drafting_argument_suggestions(
            app_api.DraftingReviewRequest(
                text="Texte",
                model="model:8b",
                argument_kind="counter_argument",
            )
        )
    )

    assert response["ok"] is True
    assert captured["argument_kind"] == "counter_argument"


def test_api_rejects_untrusted_host(workspace_tmp_dir: Path) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")

    response = TestClient(app_api.app).get("/", headers={"host": "attacker.example"})

    assert response.status_code == 400


def test_api_accepts_private_lan_ip_host(workspace_tmp_dir: Path) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")

    response = TestClient(app_api.app).get(
        "/api/capabilities", headers={"host": "192.168.1.42:7860"}
    )

    assert response.status_code == 200


def test_api_rejects_public_ip_host(workspace_tmp_dir: Path) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")

    response = TestClient(app_api.app).get("/api/capabilities", headers={"host": "8.8.8.8:7860"})

    assert response.status_code == 400


def test_api_accepts_explicit_custom_hostname(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    monkeypatch.setenv("BIBLIOBRAIN_ALLOWED_HOSTS", "bibliobrain.home")

    response = TestClient(app_api.app).get(
        "/api/capabilities", headers={"host": "bibliobrain.home:7860"}
    )

    assert response.status_code == 200


def test_request_body_limit_rejects_chunked_body_without_content_length(
    workspace_tmp_dir: Path,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    request_messages = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": False},
        ]
    )
    response_messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(request_messages)

    async def send(message: dict[str, Any]) -> None:
        response_messages.append(message)

    async def consume_body(scope: Any, receive_body: Any, send_response: Any) -> None:
        while True:
            message = await receive_body()
            if not message.get("more_body", False):
                break

    middleware = app_api.RequestBodyLimitMiddleware(consume_body, max_bytes=6)
    asyncio.run(
        middleware(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 1234),
                "server": ("127.0.0.1", 7860),
            },
            receive,
            send,
        )
    )

    assert response_messages[0]["type"] == "http.response.start"
    assert response_messages[0]["status"] == 413


def test_api_responses_include_browser_security_headers(workspace_tmp_dir: Path) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")

    response = TestClient(app_api.app).get("/api/capabilities")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    csp = response.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self';" in csp
    assert "script-src-elem 'self';" in csp
    assert "script-src-attr 'none';" in csp


def test_rate_limiter_ignores_untrusted_forwarded_for(workspace_tmp_dir: Path) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    app_api._rate_limiter.windows.clear()
    app_api._rate_limiter.general_per_minute = 1
    client = TestClient(app_api.app)

    assert (
        client.get("/api/analysis-types", headers={"x-forwarded-for": "198.51.100.1"}).status_code
        == 200
    )
    assert (
        client.get("/api/analysis-types", headers={"x-forwarded-for": "198.51.100.2"}).status_code
        == 429
    )


def test_ollama_diagnostics_reject_unconfigured_url(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    monkeypatch.setattr(
        app_api,
        "public_provider_info",
        lambda: {"ollama_base_url": "http://127.0.0.1:11434"},
    )
    monkeypatch.setattr(
        app_api,
        "fetch_gpu_status",
        lambda _url: (_ for _ in ()).throw(AssertionError("network call must be blocked")),
    )

    response = TestClient(app_api.app).get("/api/gpu", params={"url": "https://attacker.example"})

    assert response.status_code == 400


def test_notebook_import_rejects_parent_traversal(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    data_dir.mkdir(parents=True)
    (workspace_tmp_dir / "secret.pdf").write_bytes(b"%PDF-secret")
    app_api = _load_app_api_module(storage_dir, data_dir)

    response = TestClient(app_api.app).post(
        "/api/notebooks/import-paths",
        json={"title": "Traversal", "paths": ["../secret.pdf"]},
    )

    assert response.status_code == 422


def test_notebook_path_import_indexes_figure_analysis(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    pdf_path = data_dir / "papers" / "figures.pdf"
    pdf_path.parent.mkdir(parents=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")
    app_api = _load_app_api_module(storage_dir, data_dir)

    class FakeLoader:
        def __init__(self, path: str) -> None:
            self.path = path

        def load(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(page_content="Voir Figure 2 et sa courbe rouge.")]

    import langchain_community.document_loaders as document_loaders

    monkeypatch.setattr(document_loaders, "PyPDFLoader", FakeLoader)
    monkeypatch.setattr(
        app_api,
        "visual_analysis_model_settings",
        lambda: ("index", "vision:test", ""),
    )
    monkeypatch.setattr(
        app_api,
        "analyze_pdf_visuals",
        lambda *_args, **_kwargs: {
            "model": "vision:test",
            "results": [{"page": 1, "text": "La courbe rouge culmine à 12 T."}],
        },
    )

    response = TestClient(app_api.app).post(
        "/api/notebooks/import-paths",
        json={"title": "Figures", "paths": ["papers/figures.pdf"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["added"] == 1
    source = app_api.get_notebook(payload["notebook_id"])["sources"][0]
    assert source["visual_model"] == "vision:test"
    assert source["visual_pages"] == [1]
    assert "12 T" in source["visual_content"]


def test_bug_report_endpoint_relays_server_side_context(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    async def fake_deliver_bug_report(**kwargs: Any) -> dict[str, str]:
        captured.update(kwargs)
        return {"report_id": "local-1", "remote_id": "remote-1"}

    app_api.deliver_bug_report = fake_deliver_bug_report
    app_api._build_bug_report_chat = lambda *_args, **_kwargs: {
        "message_count": 1,
        "entries": [{"role": "user", "content": "Message", "model": "model-chat"}],
    }
    app_api._build_bug_report_runtime_context = lambda **_kwargs: (
        {"chat_models": ["model-chat"]},
        {"runtime": {"ready": True}},
    )
    client = TestClient(app_api.app)
    response = client.post(
        "/api/bug-reports",
        headers={"User-Agent": "Bibliobrain-Test/1"},
        json={
            "description": "Le bouton ne répond plus.",
            "steps": "Ouvrir puis cliquer.",
            "include_diagnostics": True,
            "include_chat": True,
            "active_chat_id": "chat-1",
            "current_model": "model-chat",
            "client_logs": [{"type": "window.error", "message": "boom"}],
            "client_context": {"language": "fr", "view": "tab-chat"},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "report_id": "local-1", "remote_id": "remote-1"}
    assert captured["client_context"]["user_agent"] == "Bibliobrain-Test/1"
    assert captured["client_context"]["view"] == "tab-chat"
    assert captured["client_logs"][0]["message"] == "boom"
    assert captured["attached_chat"]["entries"][0]["content"] == "Message"
    assert captured["model_info"]["chat_models"] == ["model-chat"]
    assert captured["debug_info"]["runtime"]["ready"] is True


def test_bug_report_endpoint_rejects_invalid_screenshot(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    client = TestClient(app_api.app)
    response = client.post(
        "/api/bug-reports",
        json={
            "description": "Un problème suffisamment détaillé.",
            "screenshot_data_url": "data:image/png;base64,iVBORw0KGgo=",
        },
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "bug_report_invalid_screenshot"}


def test_bug_report_endpoint_does_not_confirm_an_unconfirmed_delivery(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    async def still_in_flight(**_kwargs: Any) -> dict[str, str]:
        return {"report_id": "local-1", "remote_id": "remote-1", "delivery": "unconfirmed"}

    app_api.deliver_bug_report = still_in_flight
    client = TestClient(app_api.app)
    response = client.post(
        "/api/bug-reports",
        json={"description": "Un problème suffisamment détaillé."},
    )

    assert response.status_code == 202
    assert response.json() == {
        "ok": True,
        "report_id": "local-1",
        "remote_id": "remote-1",
        "delivery": "unconfirmed",
    }


def test_bug_report_chat_includes_interruption_diagnostics(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.get_conversation_with_messages = lambda *_args, **_kwargs: {
        "chat": {"message_count": 1},
        "messages": [
            {
                "role": "assistant",
                "content": "[Réponse interrompue]",
                "model": "demo-model",
                "ts": "2026-07-21T16:30:09Z",
                "meta": {
                    "interrupted": True,
                    "interruption_reason": "page_exit",
                    "interruption_elapsed_ms": 6034.2,
                    "answer_origin": "paper_list",
                    "mode": "paper_list",
                },
            }
        ],
    }

    payload = app_api._build_bug_report_chat("chat-1", include_debug=True)

    assert payload is not None
    entry = payload["entries"][0]
    assert entry["interrupted"] is True
    assert entry["interruption_reason"] == "page_exit"
    assert entry["interruption_elapsed_ms"] == 6034.2
    assert entry["answer_origin"] == "paper_list"
    assert entry["mode"] == "paper_list"


def test_bug_report_endpoint_reports_missing_configuration(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    async def unavailable(**_kwargs: Any) -> dict[str, str]:
        raise ValueError("bug_report_not_configured")

    app_api.deliver_bug_report = unavailable
    client = TestClient(app_api.app)
    response = client.post(
        "/api/bug-reports",
        json={"description": "Un problème suffisamment détaillé."},
    )

    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "bug_report_not_configured"}


def test_summary_stream_reports_generation_failure_as_error_event(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    def broken_summary(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("Échec contrôlé")

    app_api.generate_summary = broken_summary
    client = TestClient(app_api.app)
    response = client.post(
        "/api/summary",
        json={
            "rel_path": "paper.pdf",
            "analysis_type": "Résumé rapide",
            "model": "granite4.1:8b",
        },
    )

    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert response.status_code == 200
    assert events[-1] == {"type": "error", "msg": "summary_generation_failed"}
    assert not any(event.get("type") == "done" for event in events)


def test_removed_wiki_is_not_exposed(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    monkeypatch.setenv("BIBLIOBRAIN_ENABLE_LEGACY_WIKI", "true")
    legacy_file = storage_dir / "wiki_kb" / "papers.jsonl"
    legacy_file.parent.mkdir(parents=True)
    legacy_data = '{"paper_id":"kept","title":"Existing paper"}\n'
    legacy_file.write_text(legacy_data, encoding="utf-8")
    app_api = _load_app_api_module(storage_dir, data_dir)
    client = TestClient(app_api.app)

    capabilities = client.get("/api/capabilities")
    assert capabilities.status_code == 200
    assert "legacy_wiki" not in capabilities.json()
    assert capabilities.json()["rag_retrieval_graph"] is True
    assert client.get("/api/wiki/inspect").status_code == 404
    assert client.get("/api/wiki_kb/status").status_code == 404
    assert client.get("/api/wiki-settings").status_code == 404
    assert client.get("/api/concept_graph/view").status_code == 404
    assert client.post("/api/wiki/build", json={}).status_code == 404
    assert client.post("/api/wiki/reset").status_code == 404
    assert client.post("/api/wiki_rag/rebind").status_code == 404
    assert legacy_file.read_text(encoding="utf-8") == legacy_data
    response = client.post("/api/app-settings", json={"use_wiki_rag": True})
    assert response.status_code == 200
    assert "use_wiki_rag" not in response.json()


def test_vision_models_endpoint_returns_detected_models(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    expected = {
        "ok": True,
        "models": [{"name": "vision-demo:latest", "size": "2.0 GB"}],
        "recommended": "glm-ocr:q8_0",
    }
    app_api.list_installed_vision_models = lambda: expected

    response = TestClient(app_api.app).get("/api/vision-models")

    assert response.status_code == 200
    assert response.json() == expected


def test_vision_model_install_requires_explicit_confirmation(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    def reject_unconfirmed(model: str, *, confirmed: bool) -> dict[str, Any]:
        assert model == "glm-ocr:q8_0"
        assert confirmed is False
        raise app_api.VisualAnalysisError("confirmation_required")

    app_api.install_recommended_vision_model = reject_unconfirmed
    response = TestClient(app_api.app).post(
        "/api/vision-models/install",
        json={"model": "glm-ocr:q8_0", "confirmed": False},
    )

    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "confirmation_required"}


def test_visual_analysis_endpoint_refuses_when_opt_in_is_disabled(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.get_app_settings = lambda: {
        "visual_analysis_mode": "off",
        "vision_model": "",
    }
    app_api.analyze_pdf_visuals = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("visual analysis must not start")
    )

    response = TestClient(app_api.app).post(
        "/api/visual-analysis",
        json={"path": "papers/demo.pdf"},
    )

    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "visual_analysis_disabled"}


def test_visual_analysis_endpoint_uses_explicitly_selected_model(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    pdf_path = data_dir / "papers" / "demo.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")
    captured: dict[str, str] = {}
    app_api.get_app_settings = lambda: {
        "visual_analysis_mode": "on_demand",
        "vision_model": "saved-vision:latest",
    }

    def fake_analysis(path: str, *, model: str, analysis_model: str) -> dict[str, Any]:
        captured.update(path=path, model=model, analysis_model=analysis_model)
        return {
            "ok": True,
            "model": model,
            "ocr_model": model,
            "visual_analysis_model": analysis_model,
            "cached": False,
            "pages": [2],
            "results": [{"page": 2, "text": "Un graphique croissant."}],
        }

    app_api.analyze_pdf_visuals = fake_analysis
    response = TestClient(app_api.app).post(
        "/api/visual-analysis",
        json={
            "path": "papers/demo.pdf",
            "model": "chosen-vision:latest",
            "analysis_model": "chosen-analysis:latest",
        },
    )

    assert response.status_code == 200
    assert captured == {
        "path": str(pdf_path),
        "model": "chosen-vision:latest",
        "analysis_model": "chosen-analysis:latest",
    }
    assert "Page 2" in response.json()["markdown"]


def test_visual_analysis_cached_endpoint_restores_without_running_models(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    pdf_path = data_dir / "papers" / "demo.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")
    captured: dict[str, str] = {}
    app_api.get_app_settings = lambda: {
        "visual_analysis_mode": "off",
        "vision_model": "saved-vision:latest",
        "visual_description_model": "saved-analysis:latest",
    }
    app_api.analyze_pdf_visuals = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("visual analysis must not start")
    )

    def fake_cached(path: str, *, model: str, analysis_model: str) -> dict[str, Any]:
        captured.update(path=path, model=model, analysis_model=analysis_model)
        return {
            "ok": True,
            "model": model,
            "ocr_model": model,
            "visual_analysis_model": analysis_model,
            "cached": True,
            "pages": [2],
            "results": [{"page": 2, "text": "Un graphique mémorisé."}],
        }

    app_api.get_cached_pdf_visuals = fake_cached
    response = TestClient(app_api.app).get(
        "/api/visual-analysis-cached",
        params={"path": "papers/demo.pdf"},
    )

    assert response.status_code == 200
    assert captured == {
        "path": str(pdf_path),
        "model": "saved-vision:latest",
        "analysis_model": "saved-analysis:latest",
    }
    assert response.json()["found"] is True
    assert "graphique mémorisé" in response.json()["markdown"]


def test_visual_analysis_cached_endpoint_reports_cache_miss(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    pdf_path = data_dir / "demo.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n")
    app_api.get_app_settings = lambda: {
        "visual_analysis_mode": "on_demand",
        "vision_model": "vision:latest",
    }
    app_api.get_cached_pdf_visuals = lambda *_args, **_kwargs: None

    response = TestClient(app_api.app).get(
        "/api/visual-analysis-cached",
        params={"path": "demo.pdf"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "found": False, "markdown": ""}


def test_app_api_app_settings_endpoints_round_trip(workspace_tmp_dir: Path) -> None:
    """Les endpoints GET/POST de settings doivent exposer et renvoyer les valeurs normalisees."""
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    captured: dict[str, Any] = {}

    def fake_get_app_settings() -> dict[str, Any]:
        return {
            "threshold": 0.4,
            "max_chunks": 8,
            "debug_metrics": False,
            "discovery_query_planner": False,
            "discovery_candidate_multiplier": 4,
            "discovery_llm_validation_multiplier": 3,
            "selected_model": "saved-model",
            "ui_language": "fr",
        }

    def fake_set_app_settings(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "threshold": kwargs["threshold"],
            "max_chunks": kwargs["max_chunks"],
            "debug_metrics": True,
            "discovery_query_planner": kwargs["discovery_query_planner"],
            "discovery_candidate_multiplier": kwargs["discovery_candidate_multiplier"],
            "discovery_llm_validation_multiplier": kwargs["discovery_llm_validation_multiplier"],
            "reranker_profile": kwargs["reranker_profile"],
            "reranker_translate_french": kwargs["reranker_translate_french"],
            "selected_model": kwargs["selected_model"],
            "visual_analysis_auto_chat": kwargs["visual_analysis_auto_chat"],
            "ui_language": kwargs["ui_language"],
            "feature_tour_done_v1": kwargs["feature_tour_done_v1"],
        }

    app_api.get_app_settings = fake_get_app_settings
    app_api.set_app_settings = fake_set_app_settings

    client = TestClient(app_api.app)

    response = client.get("/api/app-settings")
    assert response.status_code == 200
    assert response.json()["threshold"] == 0.4
    assert response.json()["selected_model"] == "saved-model"

    response = client.post(
        "/api/app-settings",
        json={
            "threshold": 0.7,
            "max_chunks": 4,
            "debug_metrics": True,
            "discovery_query_planner": True,
            "discovery_candidate_multiplier": 6,
            "discovery_llm_validation_multiplier": 5,
            "reranker_profile": "ettin_cpu",
            "reranker_translate_french": False,
            "selected_model": "qwen3:8b",
            "visual_analysis_mode": "on_demand",
            "vision_model": "vision:2b",
            "visual_description_model": "vision:9b",
            "visual_analysis_auto_chat": True,
            "ui_language": "en",
            "feature_tour_done_v1": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["debug_metrics"] is True
    assert response.json()["discovery_query_planner"] is True
    assert response.json()["discovery_candidate_multiplier"] == 6
    assert response.json()["discovery_llm_validation_multiplier"] == 5
    assert response.json()["reranker_profile"] == "ettin_cpu"
    assert response.json()["reranker_translate_french"] is False
    assert response.json()["selected_model"] == "qwen3:8b"
    assert response.json()["visual_analysis_auto_chat"] is True
    assert response.json()["ui_language"] == "en"
    assert response.json()["feature_tour_done_v1"] is True
    assert captured["threshold"] == 0.7
    assert captured["max_chunks"] == 4
    assert captured["debug_metrics"] is True
    assert captured["allow_external_api_requests"] is None
    assert captured["auto_enrich_pdf_metadata"] is None
    assert captured["discovery_query_planner"] is True
    assert captured["discovery_candidate_multiplier"] == 6
    assert captured["discovery_llm_validation_multiplier"] == 5
    assert captured["reranker_profile"] == "ettin_cpu"
    assert captured["reranker_translate_french"] is False
    assert captured["selected_model"] == "qwen3:8b"
    assert captured["visual_analysis_mode"] == "on_demand"
    assert captured["vision_model"] == "vision:2b"
    assert captured["visual_description_model"] == "vision:9b"
    assert captured["visual_analysis_auto_chat"] is True
    assert captured["ui_language"] == "en"
    assert captured["feature_tour_done_v1"] is True


@pytest.mark.parametrize(
    "remote_url",
    ["https://api.example.com/v1", "http://192.168.1.20:1234/v1"],
)
def test_api_secret_endpoint_accepts_masked_fields_without_returning_values(
    workspace_tmp_dir: Path,
    remote_url: str,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_update_api_config(
        storage_path: str,
        *,
        updates: dict[str, str],
        clear: list[str],
    ) -> None:
        captured.update(storage_path=storage_path, updates=updates, clear=clear)

    safe_status = {
        "available": True,
        "backend": "windows_dpapi",
        "secrets": {
            "remote_llm_api_key": {"configured": True, "source": "secure_store"},
            "openalex_api_key": {"configured": True, "source": "secure_store"},
            "semantic_scholar_api_key": {"configured": False, "source": None},
            "ollama_api_key": {"configured": False, "source": None},
            "ollama_auth_header": {"configured": False, "source": None},
            "huggingface_token": {"configured": False, "source": None},
            "bugsink_dsn": {"configured": False, "source": None},
        },
        "remote_llm_base_url": {
            "configured": True,
            "source": "secure_store",
            "value": remote_url,
        },
    }
    app_api.update_api_config = fake_update_api_config
    app_api.api_config_status = lambda _storage_path: safe_status

    response = TestClient(app_api.app).post(
        "/api/api-secrets",
        json={
            "remote_llm_base_url": remote_url,
            "remote_llm_api_key": "remote-super-secret",
            "openalex_api_key": "openalex-super-secret",
            "huggingface_token": "hf-super-secret",
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["storage_path"] == str(storage_dir)
    assert captured["updates"] == {
        "remote_llm_base_url": remote_url,
        "remote_llm_api_key": "remote-super-secret",
        "openalex_api_key": "openalex-super-secret",
        "huggingface_token": "hf-super-secret",
    }
    assert "remote-super-secret" not in response.text
    assert "openalex-super-secret" not in response.text
    assert "hf-super-secret" not in response.text


def test_analysis_prompt_api_prefers_explicit_client_language(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    captured_languages: list[str] = []

    def fake_prompt_payloads(language: str = "fr") -> list[dict[str, Any]]:
        captured_languages.append(language)
        return [
            {
                "type": "Résumé rapide",
                "label": "Quick summary" if language == "en" else "Résumé rapide",
                "prompt": "English prompt" if language == "en" else "Prompt français",
            }
        ]

    monkeypatch.setattr(app_api, "get_app_settings", lambda: {"ui_language": "fr"})
    monkeypatch.setattr(app_api, "list_analysis_prompt_payloads", fake_prompt_payloads)
    client = TestClient(app_api.app)

    response = client.get("/api/analysis-prompts?language=en")
    assert response.status_code == 200
    assert response.json()["language"] == "en"
    assert response.json()["prompts"][0]["label"] == "Quick summary"

    response = client.get("/api/analysis-types?language=en")
    assert response.status_code == 200
    assert response.json()["type_options"] == [{"type": "Résumé rapide", "label": "Quick summary"}]
    assert captured_languages == ["en", "en"]


def test_app_api_zotero_load_returns_rows_when_settings_save_fails(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    monkeypatch.setattr(app_api, "init_zotero", lambda path: "Zotero OK")
    monkeypatch.setattr(
        app_api,
        "set_app_settings",
        lambda **_kwargs: (_ for _ in ()).throw(PermissionError("settings locked")),
    )
    monkeypatch.setattr(
        app_api,
        "list_library",
        lambda indexed_only=True: ([["Paper", "Author", "2024"]], ["papers/demo.pdf"]),
    )
    monkeypatch.setattr(app_api, "get_zotero_facets", lambda: {"stats": {"items": 1}})
    monkeypatch.setattr(app_api, "suggest_zotero_pdf_folder", lambda _path: "")

    client = TestClient(app_api.app)
    response = client.post("/api/zotero-load", json={"path": "C:/Zotero/zotero.sqlite"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["rows"][0][0] == "Paper"
    assert payload["paths"] == ["papers/demo.pdf"]
    assert "Parametres non sauvegardes" in payload["status"]


def test_app_api_zotero_load_strips_wrapping_path_quotes(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    source_path = workspace_tmp_dir / "library export.txt"
    source_path.write_text("@article{Demo, title={Demo}}", encoding="utf-8")
    captured: dict[str, str] = {}

    monkeypatch.setattr(
        app_api,
        "init_zotero",
        lambda path: captured.setdefault("path", path) and "BibTeX OK - 1 entree chargee.",
    )
    monkeypatch.setattr(app_api, "list_library", lambda indexed_only=True: ([], []))
    monkeypatch.setattr(app_api, "get_zotero_facets", lambda: {})
    monkeypatch.setattr(app_api, "_persist_app_settings_best_effort", lambda **_kwargs: "")

    client = TestClient(app_api.app)
    response = client.post("/api/zotero-load", json={"path": f"\u201c{source_path}\u201d"})

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["path"] == str(source_path)
    assert captured["path"] == str(source_path)


def test_app_api_zotero_load_marks_loader_errors_as_failures(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    persisted: list[dict[str, str]] = []

    monkeypatch.setattr(app_api, "init_zotero", lambda _path: "Erreur BibTeX : fichier invalide")
    monkeypatch.setattr(app_api, "list_library", lambda indexed_only=True: ([], []))
    monkeypatch.setattr(app_api, "get_zotero_facets", lambda: {})

    def persist_settings(**kwargs: str) -> str:
        persisted.append(kwargs)
        return ""

    monkeypatch.setattr(app_api, "_persist_app_settings_best_effort", persist_settings)
    monkeypatch.setattr(app_api, "is_bibtex_source", lambda _path: True)

    client = TestClient(app_api.app)
    response = client.post("/api/zotero-load", json={"path": "invalid.txt"})

    assert response.status_code == 422
    assert response.json()["ok"] is False
    assert response.json()["error"] == "metadata_source_load_failed"
    assert persisted == []


def test_app_api_set_data_path_returns_rows_when_settings_save_fails(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    source_dir = workspace_tmp_dir / "zotero-storage"
    source_dir.mkdir()
    monkeypatch.setattr(
        app_api,
        "set_app_settings",
        lambda **_kwargs: (_ for _ in ()).throw(PermissionError("settings locked")),
    )
    monkeypatch.setattr(
        app_api,
        "list_library",
        lambda indexed_only=True: ([["Paper", "Author", "2024"]], ["papers/demo.pdf"]),
    )
    monkeypatch.setattr(app_api, "get_zotero_facets", lambda: {"stats": {"items": 1}})

    client = TestClient(app_api.app)
    response = client.post("/api/set-data-path", json={"path": str(source_dir)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["rows"][0][0] == "Paper"
    assert payload["paths"] == ["papers/demo.pdf"]
    assert "Parametres non sauvegardes" in payload["msg"]


def test_app_api_set_data_path_counts_documents_on_disk(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    source_dir = workspace_tmp_dir / "papers"
    (source_dir / "nested").mkdir(parents=True)
    (source_dir / "a.pdf").write_bytes(b"%PDF-1.4")
    (source_dir / "nested" / "b.PDF").write_bytes(b"%PDF-1.4")
    (source_dir / "notes.xyz").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(app_api, "list_library", lambda indexed_only=True: ([], []))

    client = TestClient(app_api.app)
    response = client.post("/api/set-data-path", json={"path": str(source_dir)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["paths"] == []
    assert payload["document_count"] == 2
    assert payload["indexed_count"] == 0
    # Before the first preparation "0 indexed" read as "nothing found".
    assert "2 documents trouves, sous-dossiers compris ; 0 deja prepares" in payload["msg"]


def test_app_api_select_data_directory_returns_selected_path_without_saving(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    selected_dir = workspace_tmp_dir / "selected-pdfs"
    selected_dir.mkdir()
    captured: dict[str, str] = {}

    def fake_choose_directory(*, initial_path: str, title: str) -> str:
        captured.update(initial_path=initial_path, title=title)
        return str(selected_dir)

    monkeypatch.setattr(app_api, "choose_directory", fake_choose_directory)
    client = TestClient(app_api.app)
    response = client.post(
        "/api/select-data-directory",
        json={"path": str(data_dir)},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "cancelled": False,
        "path": str(selected_dir),
    }
    assert captured == {
        "initial_path": str(data_dir),
        "title": "Choisir le dossier contenant les PDF",
    }
    assert config.DATA_PATH == str(data_dir)


def test_app_api_select_data_directory_rejects_remote_requests(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    app_api = _load_app_api_module(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    monkeypatch.setattr(app_api, "is_loopback_request", lambda _request: False)
    monkeypatch.setattr(
        app_api,
        "choose_directory",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("dialog must not open")),
    )
    client = TestClient(app_api.app)

    response = client.post("/api/select-data-directory", json={"path": ""})

    assert response.status_code == 403
    assert response.json()["error"] == "local_request_required"


def test_app_api_select_metadata_file_returns_selected_path_without_saving(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    app_api = _load_app_api_module(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    selected_file = workspace_tmp_dir / "references.bib"
    selected_file.write_text("@article{demo}", encoding="utf-8")
    captured: dict[str, str] = {}

    def fake_choose_file(*, initial_path: str, title: str) -> str:
        captured.update(initial_path=initial_path, title=title)
        return str(selected_file)

    monkeypatch.setattr(app_api, "choose_file", fake_choose_file)
    client = TestClient(app_api.app)
    response = client.post(
        "/api/select-metadata-file",
        json={"path": str(workspace_tmp_dir)},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "cancelled": False,
        "path": str(selected_file),
    }
    assert captured == {
        "initial_path": str(workspace_tmp_dir),
        "title": "Choisir un fichier Zotero ou BibTeX",
    }


def test_library_export_endpoint_returns_download(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_build(export_format: str, paths: list[str] | None):
        captured["format"] = export_format
        captured["paths"] = paths
        return {
            "content": "@article{Demo,\n  title = {Demo}\n}\n",
            "count": 1,
            "filename": "bibliobrain-articles.bib",
            "media_type": "application/x-bibtex; charset=utf-8",
        }

    monkeypatch.setattr(app_api, "build_library_export", fake_build)
    client = TestClient(app_api.app)
    response = client.post(
        "/api/library/export",
        json={"export_format": "bibtex", "paths": ["papers/demo.pdf"]},
    )

    assert response.status_code == 200
    assert captured == {"format": "bibtex", "paths": ["papers/demo.pdf"]}
    assert response.headers["content-disposition"] == (
        'attachment; filename="bibliobrain-articles.bib"'
    )
    assert response.headers["x-exported-items"] == "1"
    assert response.text.startswith("@article{Demo")


def test_library_endpoint_keeps_filter_metadata_aligned_with_pagination(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    app_api = _load_app_api_module(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    rows = [["First"], ["Second"], ["Third"]]
    paths = ["first.pdf", "Folder\\SECOND.pdf", "third.pdf"]
    captured_paths: list[list[str]] = []

    monkeypatch.setattr(
        app_api,
        "list_library",
        lambda indexed_only=True, include_stats=False: (
            rows,
            paths,
            {"documents": 3},
        ),
    )

    def fake_metadata(page_paths: list[str]) -> list[dict[str, Any]]:
        captured_paths.append(page_paths)
        return [{"collections": ["Projet / Sous-dossier"]}]

    monkeypatch.setattr(app_api, "get_library_filter_metadata", fake_metadata)
    monkeypatch.setattr(
        app_api,
        "_library_theme_paths",
        lambda: {"folder/second.pdf": ["Physique", "Supraconductivité"]},
    )
    monkeypatch.setattr(app_api, "get_zotero_facets", lambda: {"collections": []})

    response = TestClient(app_api.app).get(
        "/api/library", params={"limit": 1, "offset": 1}
    )

    assert response.status_code == 200
    payload = response.json()
    assert captured_paths == [["Folder\\SECOND.pdf"]]
    assert payload["rows"] == [["Second"]]
    assert payload["paths"] == ["Folder\\SECOND.pdf"]
    assert payload["metadata"] == [
        {
            "collections": ["Projet / Sous-dossier"],
            "theme_path": ["Physique", "Supraconductivité"],
            "rag_excluded": False,
        }
    ]
    assert payload["pagination"] == {
        "limit": 1,
        "offset": 1,
        "total": 3,
        "has_more": True,
    }


def test_notebook_detail_endpoint_returns_light_source_previews(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    client = TestClient(app_api.app)

    created = client.post("/api/notebooks", json={"title": "Carnet lourd"})
    assert created.status_code == 200
    nb_id = created.json()["id"]
    full_content = "A" * 2500

    added = client.post(
        f"/api/notebooks/{nb_id}/sources",
        json={"title": "Long PDF", "content": full_content, "source_path": "paper.pdf"},
    )
    assert added.status_code == 200
    added_source = added.json()["source"]
    assert added_source["content_length"] == len(full_content)
    assert len(added_source["content"]) < len(full_content)

    response = client.get(f"/api/notebooks/{nb_id}")
    assert response.status_code == 200
    source = response.json()["sources"][0]
    assert source["content_length"] == len(full_content)
    assert source["content_truncated"] is True
    assert len(source["content"]) < len(full_content)
    assert app_api.get_notebook(nb_id)["sources"][0]["content"] == full_content


def test_stream_abort_endpoint_falls_back_to_model_when_request_id_missing(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    request_id, signal = app_api.register_stream_abort_signal("qwen3.5:4b")
    unload_calls: list[tuple[str, int | float]] = []

    monkeypatch.setattr(app_api, "is_remote_provider", lambda: False)

    def fake_unload(model: str, timeout_seconds: int | float = 2) -> bool:
        unload_calls.append((model, timeout_seconds))
        return True

    monkeypatch.setattr(
        app_api,
        "unload_ollama_model",
        fake_unload,
    )

    client = TestClient(app_api.app)
    try:
        response = client.post(
            "/api/stream/abort",
            json={"request_id": "", "model": "qwen3.5:4b", "reason": "page_exit"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["stream_aborted"] is True
        assert payload["aborted_requests"] == 1
        assert payload["model_unloaded"] is True
        assert payload["reason"] == "page_exit"
        assert signal.is_aborted() is True
        assert signal.abort_reason() == "page_exit"
        assert unload_calls == [("qwen3.5:4b", 2)]
    finally:
        app_api.release_stream_abort_signal(request_id)


def test_stream_abort_endpoint_does_not_fall_back_from_stale_request_id(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    active_request_id, active_signal = app_api.register_stream_abort_signal("shared-model")
    unload_calls: list[str] = []

    monkeypatch.setattr(app_api, "is_remote_provider", lambda: False)

    def fake_unload(model: str, timeout_seconds: int | float = 2) -> bool:
        del timeout_seconds
        unload_calls.append(model)
        return True

    monkeypatch.setattr(
        app_api,
        "unload_ollama_model",
        fake_unload,
    )

    try:
        response = TestClient(app_api.app).post(
            "/api/stream/abort",
            json={
                "request_id": "already-finished-request",
                "model": "shared-model",
                "reason": "user_stop",
            },
        )

        assert response.status_code == 200
        assert response.json() == {
            "ok": False,
            "stream_aborted": False,
            "aborted_requests": 0,
            "model_unloaded": False,
            "reason": "user_stop",
        }
        assert active_signal.is_aborted() is False
        assert unload_calls == []
    finally:
        app_api.release_stream_abort_signal(active_request_id)


def test_drafting_proofread_registers_client_abort_request_id(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_proofread_draft(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        assert kwargs["abort_signal"].is_aborted() is False
        return {
            "ok": True,
            "corrected_text": "Corrected.",
            "issues": [],
            "summary": "Done.",
        }

    monkeypatch.setattr(app_api, "proofread_draft", fake_proofread_draft)

    client = TestClient(app_api.app)
    response = client.post(
        "/api/drafting-proofread",
        json={"text": "Corrected.", "model": "qwen3.5:4b", "request_id": "draft-proofread-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["request_id"] == "draft-proofread-1"
    assert captured["text"] == "Corrected."
    assert captured["model_name"] == "qwen3.5:4b"
    assert app_api.abort_stream_request("draft-proofread-1") is False


def test_drafting_proofread_streams_progress_and_result(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    def fake_stream_proofread_draft(**kwargs: Any):
        assert kwargs["text"] == "Needs correction."
        assert kwargs["model_name"] == "fast-model"
        yield {"type": "progress", "percent": 42, "msg": "Relecture en cours…"}
        yield {
            "type": "result",
            "percent": 100,
            "payload": {
                "ok": True,
                "corrected_text": "Corrected.",
                "issues": [],
                "summary": "Done.",
            },
        }

    monkeypatch.setattr(app_api, "stream_proofread_draft", fake_stream_proofread_draft)

    response = TestClient(app_api.app).post(
        "/api/drafting-proofread",
        headers={"Accept": "text/event-stream"},
        json={"text": "Needs correction.", "model": "fast-model", "request_id": "proof-stream-1"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "progress"' in response.text
    assert '"percent": 42' in response.text
    assert '"corrected_text": "Corrected."' in response.text
    assert '"request_id": "proof-stream-1"' in response.text
    assert app_api.abort_stream_request("proof-stream-1") is False


def test_article_discovery_requires_global_external_api_permission(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.get_app_settings = lambda: {"allow_external_api_requests": False}

    client = TestClient(app_api.app)
    response = client.post(
        "/api/article-discovery",
        json={
            "mode": "search",
            "input_text": "HTS AC losses",
            "model": "demo-model",
            "privacy_ack": True,
        },
    )

    assert response.status_code == 403
    payload = response.json()
    assert payload["ok"] is False
    assert payload["error"] == "external_api_permission_required"
    assert "API externes" in payload["msg"]


def test_citation_graph_expand_route_is_registered(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.get_app_settings = lambda: {"allow_external_api_requests": False}

    response = TestClient(app_api.app).get(
        "/api/citation-graph/expand", params={"openalex_id": "W1", "limit": 3}
    )

    assert response.status_code == 403
    assert response.json()["ok"] is False
    assert response.json()["error"] == "external_api_permission_required"
    assert "API externes" in response.json()["msg"]


def test_papers_citation_graph_route_validates_and_requires_external_permission(
    workspace_tmp_dir: Path,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    app_api.get_app_settings = lambda: {"allow_external_api_requests": False}
    client = TestClient(app_api.app)

    refused = client.post("/api/library/citation-graph", json={"paths": ["papers/a.pdf"]})
    empty = client.post("/api/library/citation-graph", json={"paths": []})
    too_many = client.post(
        "/api/library/citation-graph",
        json={"paths": [f"papers/{index}.pdf" for index in range(61)]},
    )

    assert refused.status_code == 403
    assert refused.json()["error"] == "external_api_permission_required"
    assert empty.status_code == 422
    assert too_many.status_code == 422


def test_research_scope_citation_routes_require_external_permission(
    workspace_tmp_dir: Path,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    app_api.get_app_settings = lambda: {"allow_external_api_requests": False}
    client = TestClient(app_api.app)

    search_response = client.post(
        "/api/citation-graph/entities",
        json={"entity_type": "author", "query": "Ada"},
    )
    graph_response = client.post(
        "/api/citation-graph/scope",
        json={
            "entity_type": "author",
            "openalex_id": "A42",
            "max_works": 80,
            "strategy": "balanced",
        },
    )
    bibliographic_response = client.post(
        "/api/citation-graph/search",
        json={"query": "supraconductivité", "max_works": 80},
    )

    assert search_response.status_code == 403
    assert graph_response.status_code == 403
    assert bibliographic_response.status_code == 403
    assert search_response.json()["error"] == "external_api_permission_required"
    assert graph_response.json()["error"] == "external_api_permission_required"
    assert bibliographic_response.json()["error"] == "external_api_permission_required"


def test_research_scope_feature_flag_blocks_server_routes(
    workspace_tmp_dir: Path,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    app_api.get_app_settings = lambda: {
        "allow_external_api_requests": True,
        "research_scope_enabled": False,
    }
    client = TestClient(app_api.app)

    search_response = client.post(
        "/api/citation-graph/entities",
        json={"entity_type": "author", "query": "Ada Lovelace"},
    )
    graph_response = client.post(
        "/api/citation-graph/scope",
        json={
            "entity_type": "author",
            "openalex_id": "A42",
            "max_works": 80,
            "strategy": "balanced",
        },
    )

    assert search_response.status_code == 403
    assert graph_response.status_code == 403
    assert search_response.json()["error"] == "experimental_research_scope_disabled"
    assert graph_response.json()["error"] == "experimental_research_scope_disabled"


def test_research_entity_search_does_not_log_personal_query(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
    caplog: Any,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    app_api.get_app_settings = lambda: {
        "allow_external_api_requests": True,
        "research_scope_enabled": True,
    }

    async def fail_search(_entity_type: str, query: str, *, limit: int = 8):
        raise RuntimeError(f"provider failed for {query} ({limit})")

    import src.citation_graph as citation_graph

    monkeypatch.setattr(citation_graph, "search_research_entities", fail_search)
    caplog.set_level("ERROR")
    private_query = "Ada Lovelace private probe"

    response = TestClient(app_api.app).post(
        "/api/citation-graph/entities",
        json={"entity_type": "author", "query": private_query},
    )

    assert response.status_code == 502
    assert private_query not in caplog.text
    assert "OpenAlex research entity search failed" in caplog.text


def test_research_scope_citation_request_is_bounded(workspace_tmp_dir: Path) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    client = TestClient(app_api.app)

    response = client.post(
        "/api/citation-graph/scope",
        json={
            "entity_type": "author",
            "openalex_id": "A42",
            "max_works": 151,
            "strategy": "balanced",
        },
    )

    assert response.status_code == 422

    bibliographic_response = client.post(
        "/api/citation-graph/search",
        json={"query": "supraconductivité", "max_works": 151},
    )
    assert bibliographic_response.status_code == 422


def test_startup_status_reports_loaded_rag_without_reinit(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    app_api.get_vector_database_status = lambda: {"status": "present", "warning": ""}
    app_api.get_runtime_status = lambda: {
        "ready": True,
        "model": "demo-model",
        "threshold": 0.3,
        "chunk_size": 1000,
        "embedding_model": "demo-embed",
        "db_path": str(storage_dir / "chroma"),
        "provider": "ollama",
        "ready_at": "2026-04-30T00:00:00+00:00",
        "message": "Pret | modele : demo-model",
    }
    app_api.get_app_settings = lambda: {
        "first_rag_init_done": True,
        "debug_metrics": False,
        "feature_tour_done_v1": True,
    }
    app_api.get_library_stats = lambda: {"indexed_pdf_count": 2}

    client = TestClient(app_api.app)
    response = client.get("/api/startup-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["rag"]["ready"] is True
    assert payload["rag"]["model"] == "demo-model"
    assert payload["library_count"] == 2
    assert payload["library_configured"] is True
    assert payload["recommended_action"] == "ready"
    assert payload["settings"]["feature_tour_done_v1"] is True


def test_startup_status_reports_empty_configured_library_as_configured(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "configured-library"
    app_api = _load_app_api_module(storage_dir, data_dir)

    app_api.get_app_settings = lambda: {
        "data_path": str(data_dir),
        "metadata_source": "none",
    }
    app_api.get_library_stats = lambda: {"indexed_pdf_count": 0}

    client = TestClient(app_api.app)
    response = client.get("/api/startup-status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["library_count"] == 0
    assert payload["library_configured"] is True


def test_startup_status_offloads_blocking_snapshot_reads(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    offloaded: list[Any] = []

    async def recording_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        offloaded.append(func)
        return func(*args, **kwargs)

    monkeypatch.setattr(app_api.asyncio, "to_thread", recording_to_thread)

    payload = asyncio.run(app_api.api_startup_status())

    assert payload["ok"] is True
    assert app_api.get_vector_database_status in offloaded
    assert app_api.get_runtime_status in offloaded
    assert app_api.get_app_settings in offloaded
    assert app_api.public_provider_info in offloaded


def test_models_endpoint_offloads_blocking_provider_probe(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    def slow_models(_url: str, *, force_refresh: bool = False):
        time.sleep(0.15)
        return ["demo-model"], "ok"

    monkeypatch.setattr(app_api, "fetch_ollama_models", slow_models)
    monkeypatch.setattr(
        app_api,
        "_current_embedding_health",
        lambda _url: {"required": False, "ok": True},
    )

    async def run_probe() -> dict:
        started_at = time.perf_counter()
        task = asyncio.create_task(app_api.api_models(refresh=True))
        await asyncio.sleep(0.02)
        assert time.perf_counter() - started_at < 0.10
        return await task

    payload = asyncio.run(run_probe())

    assert payload["models"] == ["demo-model"]
    assert payload["health"]["ok"] is True


def test_app_api_remote_models_do_not_validate_remote_url_as_ollama(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(app_api, "is_remote_provider", lambda: True)
    monkeypatch.setattr(app_api, "get_embedding_provider_mode", lambda: "remote")

    def fake_models(url: str | None, *, force_refresh: bool = False):
        captured["chat_url"] = url
        captured["force_refresh"] = force_refresh
        return ["remote-chat-model"], "ok"

    def fake_health(url: str) -> dict[str, Any]:
        captured["embedding_url"] = url
        return {"required": False, "ok": True}

    monkeypatch.setattr(app_api, "fetch_ollama_models", fake_models)
    monkeypatch.setattr(app_api, "_current_embedding_health", fake_health)
    monkeypatch.setattr(
        app_api,
        "_configured_ollama_request_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("remote chat URL must not be validated as Ollama")
        ),
    )

    payload = asyncio.run(app_api.api_models(url="https://models.example.test/v1", refresh=True))

    assert payload["models"] == ["remote-chat-model"]
    assert captured == {
        "chat_url": None,
        "force_refresh": True,
        "embedding_url": "",
    }


def test_app_api_remote_embeddings_ignore_remote_chat_url(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    monkeypatch.setattr(app_api, "get_embedding_provider_mode", lambda: "remote")
    monkeypatch.setattr(
        app_api,
        "_configured_ollama_request_url",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("remote embedding mode has no local URL to validate")
        ),
    )

    response = TestClient(app_api.app).get(
        "/api/ollama-local-models",
        params={"url": "https://models.example.test/v1"},
    )

    assert response.status_code == 200
    assert response.json()["provider"] == "remote"
    assert response.json()["models"] == []


def test_app_api_start_ollama_starts_local_service(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    monkeypatch.setattr(app_api, "requires_local_ollama_embeddings", lambda: True)
    monkeypatch.setattr(
        app_api,
        "public_provider_info",
        lambda: {"embedding_base_url": "http://127.0.0.1:11434"},
    )
    monkeypatch.setattr(
        app_api,
        "check_ollama_health",
        lambda *_args, **_kwargs: {"required": True, "ollama_ok": False},
    )
    monkeypatch.setattr(
        app_api,
        "start_ollama_server",
        lambda: {"ok": True, "started": True, "code": "started", "pid": 123},
    )

    response = TestClient(app_api.app).post("/api/ollama/start")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "started": True,
        "code": "started",
        "pid": 123,
        "url": "http://127.0.0.1:11434",
    }


def test_app_api_start_ollama_refuses_non_local_url(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    monkeypatch.setattr(app_api, "requires_local_ollama_embeddings", lambda: True)
    monkeypatch.setattr(
        app_api,
        "public_provider_info",
        lambda: {"embedding_base_url": "https://ollama.example.org"},
    )
    monkeypatch.setattr(
        app_api,
        "start_ollama_server",
        lambda: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    response = TestClient(app_api.app).post("/api/ollama/start")

    assert response.status_code == 200
    assert response.json()["code"] == "non_local_url"
    assert response.json()["started"] is False


def test_app_api_start_ollama_skips_process_when_already_running(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    monkeypatch.setattr(app_api, "requires_local_ollama_embeddings", lambda: True)
    monkeypatch.setattr(
        app_api,
        "public_provider_info",
        lambda: {"ollama_base_url": "http://localhost:11434"},
    )
    monkeypatch.setattr(
        app_api,
        "check_ollama_health",
        lambda *_args, **_kwargs: {"required": True, "ollama_ok": True},
    )
    monkeypatch.setattr(
        app_api,
        "start_ollama_server",
        lambda: (_ for _ in ()).throw(AssertionError("must not start")),
    )

    response = TestClient(app_api.app).post("/api/ollama/start")

    assert response.status_code == 200
    assert response.json()["code"] == "already_running"
    assert response.json()["started"] is False


def test_app_lifespan_starts_local_ollama_without_blocking_http_startup(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(
        workspace_tmp_dir / "storage",
        workspace_tmp_dir / "data",
    )
    startup_started = threading.Event()
    release_startup = threading.Event()

    def ensure_local_ollama_started() -> dict[str, object]:
        startup_started.set()
        release_startup.wait(timeout=2)
        return {
            "ok": True,
            "started": False,
            "code": "already_running",
            "msg": "Ollama est déjà démarré.",
        }

    monkeypatch.setattr(app_api, "_ensure_local_ollama_started", ensure_local_ollama_started)
    monkeypatch.setattr(app_api, "_start_reranker_warmup_background", lambda: {})
    monkeypatch.setattr(app_api, "_managed_server", False)

    async def run_lifespan() -> None:
        started_at = time.perf_counter()
        async with app_api._app_lifespan(app_api.app):
            assert time.perf_counter() - started_at < 0.10
            assert startup_started.wait(timeout=1)
            assert app_api._ollama_startup_thread is not None
            assert app_api._ollama_startup_thread.is_alive()
            release_startup.set()

    asyncio.run(run_lifespan())

    assert app_api._ollama_startup_thread is not None
    app_api._ollama_startup_thread.join(timeout=1)
    assert not app_api._ollama_startup_thread.is_alive()


def test_local_hostname_resolution_does_not_block_server_startup(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    resolution_started = threading.Event()
    release_resolution = threading.Event()

    def slow_getfqdn() -> str:
        resolution_started.set()
        release_resolution.wait(timeout=2)
        return "bibliobrain.local.example"

    monkeypatch.setattr("socket.getfqdn", slow_getfqdn)
    app_api._local_hosts.reset_resolution()

    started_at = time.perf_counter()
    assert app_api._local_hosts.start_background_resolution() is True
    assert time.perf_counter() - started_at < 0.10
    assert resolution_started.wait(timeout=1)
    resolution_thread = app_api._local_hosts.resolution_thread
    assert resolution_thread is not None
    assert resolution_thread.is_alive()

    release_resolution.set()
    resolution_thread.join(timeout=1)
    assert not resolution_thread.is_alive()
    assert "bibliobrain.local.example" in app_api._local_hosts.names


def test_rag_reload_preview_endpoint_reports_diagnostic(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_preview(*, chunk_size: int | None = None) -> dict[str, Any]:
        captured["chunk_size"] = chunk_size
        return {
            "ok": True,
            "mode": "incremental",
            "summary": "Mise a jour incrementale: 1 nouveaux, 0 modifies, 0 supprimes.",
            "full_rebuild_required": False,
            "new_count": 1,
            "modified_count": 0,
            "removed_count": 0,
        }

    app_api.get_vector_reload_preview = fake_preview

    client = TestClient(app_api.app)
    response = client.get("/api/rag/reload-preview", params={"chunk_size": 1200})

    assert response.status_code == 200
    payload = response.json()
    assert captured["chunk_size"] == 1200
    assert payload["mode"] == "incremental"
    assert payload["new_count"] == 1


def _write_deck(path: Path, extra_member: str = "", extra_size: int = 0) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>Bobine</a:t></a:r></a:p></p:sld>',
        )
        if extra_member:
            archive.writestr(extra_member, bytes(extra_size))


def test_rag_reload_preview_survives_unreadable_slide_decks(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    """Two testers' 0.8.0 reports: "Preview RAG impossible (500)".

    One bad .pptx in the library made the reload preview fail. The endpoint
    runs the real file scan here; only the online metadata lookup is stubbed.
    """
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    import src.pdf_metadata as pdf_metadata

    monkeypatch.setattr(pdf_metadata, "enrich_pdf_metadata", lambda item, **_kwargs: item)
    monkeypatch.setenv("BIBLIOBRAIN_MAX_DOCUMENT_MB", "1")

    mib = 1024 * 1024
    decks = data_dir / "Présentations"
    decks.mkdir()
    (data_dir / "notes.md").write_text("# Notes\n\nTexte lisible.", encoding="utf-8")
    # From the logs: an embedded video, an embedded spreadsheet object.
    _write_deck(decks / "video.pptx", "ppt/media/media4.mp4", 30 * mib)
    _write_deck(decks / "objet.pptx", "ppt/embeddings/oleObject26.bin", 30 * mib)
    # Still refused when read, but must not break the scan either.
    _write_deck(decks / "texte-enorme.pptx", "ppt/slides/slide2.xml", 30 * mib)
    with zipfile.ZipFile(decks / "trop-gros.pptx", "w") as archive:
        archive.writestr("ppt/media/clip.mp4", bytes(2 * mib))  # over the 1 MB guard
    (decks / "corrompu.pptx").write_bytes(b"PK\x03\x04" + b"\x00" * 64)

    client = TestClient(app_api.app)
    response = client.get("/api/rag/reload-preview")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["mode"] == "initial_index"
    # Every document is listed: unreadable ones are skipped when indexing.
    assert payload["new_count"] == 6


def test_validate_local_embedding_model_lmstudio_reports_unloaded_embeddings(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    llm_provider = _load_llm_provider_module(storage_dir)
    llm_provider.set_runtime_mode("lmstudio", persist=False)
    llm_provider.set_runtime_embedding_source("chat", persist=False)

    def fake_open_url_json(
        url: str,
        *,
        method: str = "GET",
        json_payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | int = 10,
        ssl_verify: bool = True,
    ) -> dict[str, Any]:
        del method, json_payload, headers, timeout, ssl_verify
        if url.endswith("/models"):
            return {"data": [{"id": "gemma-4-26b-a4b-it"}]}
        if url.endswith("/embeddings"):
            raise urllib.error.HTTPError(
                url,
                400,
                "Bad Request",
                hdrs=Message(),
                fp=io.BytesIO(
                    b'{"error":{"message":"No models loaded. Please load a model in the developer page or use the \\"lms load\\" command."}}'
                ),
            )
        raise AssertionError(f"URL inattendue: {url}")

    llm_provider._open_url_json = fake_open_url_json

    ok, resolved, msg = llm_provider.validate_local_embedding_model("gemma-4-26b-a4b-it")

    assert ok is False
    assert resolved == "gemma-4-26b-a4b-it"
    assert "Aucun modele n'est actuellement charge" in msg
    assert "lms load" in msg


def test_app_api_embedding_models_endpoint_filters_lmstudio_for_embeddings(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    app_api.get_embedding_provider_mode = lambda: "lmstudio"

    def fake_list_local_lmstudio_models(url: str | None = None, *, embeddings_only: bool = False):
        captured["url"] = url
        captured["embeddings_only"] = embeddings_only
        return [], "Aucun modele d'embedding detecte sur LM Studio."

    app_api.list_local_lmstudio_models = fake_list_local_lmstudio_models

    client = TestClient(app_api.app)
    response = client.get("/api/ollama-local-models", params={"url": "http://127.0.0.1:1234"})

    assert response.status_code == 200
    assert captured["url"] == "http://127.0.0.1:1234"
    assert captured["embeddings_only"] is True
    assert response.json()["provider"] == "lmstudio"


def test_app_api_lists_remote_embedding_models_only_when_remote_is_active(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.is_remote_provider = lambda: True
    app_api.get_embedding_provider_mode = lambda: "ollama"
    app_api.list_remote_embedding_models = lambda: (
        ["text-embedding-3-small"],
        "1 modele(s) detecte(s) sur API distante.",
    )

    response = TestClient(app_api.app).get("/api/remote-embedding-models")

    assert response.status_code == 200
    assert response.json() == {
        "models": ["text-embedding-3-small"],
        "status": "1 modele(s) detecte(s) sur API distante.",
        "provider": "remote",
        "required_embedding_model": "",
    }


def test_app_api_validates_and_persists_remote_embedding_selection(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}
    app_api.is_remote_provider = lambda: True
    app_api.validate_remote_embedding_model = lambda model: (True, model, "")

    def fake_set_runtime_embedding_model(model: str, **kwargs):
        captured["model"] = model
        captured["provider_mode"] = kwargs.get("provider_mode")
        return model

    app_api.set_runtime_embedding_model = fake_set_runtime_embedding_model

    def fake_select_profile(**kwargs):
        captured["profile"] = kwargs
        return {"profile_id": "remote-profile", "ready": False, "reused": False}

    app_api.set_active_vector_index_profile = fake_select_profile
    app_api.get_persisted_chunk_size = lambda: 1000
    app_api.current_embedding_profile_label = lambda model: f"remote:{model}"
    app_api.clear_runtime_state = lambda: captured.update(cleared=True)
    app_api.public_provider_info = lambda: {
        "embedding_provider": "remote",
        "remote_embedding_model": "text-embedding-3-small",
    }

    response = TestClient(app_api.app).post(
        "/api/set-embedding-model",
        json={"model": "text-embedding-3-small", "provider": "remote"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["model"] == "text-embedding-3-small"
    assert captured["provider_mode"] == "remote"
    assert captured["profile"]["embedding_model"] == "remote:text-embedding-3-small"
    assert captured["cleared"] is True
    assert response.json()["index_reused"] is False
    assert response.json()["index_action"] == "build"


def test_app_api_can_restore_local_embeddings_after_remote_selection(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}
    app_api.get_embedding_provider_mode = lambda: "remote"
    app_api.disable_runtime_remote_embeddings = lambda: captured.update(disabled=True)
    app_api.required_embedding_model_name = lambda: "nomic-embed-text-v2-moe"
    app_api.current_embedding_profile_label = lambda model: model
    app_api.get_persisted_chunk_size = lambda: 1000

    def fake_select_profile(**kwargs):
        captured["profile"] = kwargs
        return {"profile_id": "local-profile", "ready": True, "reused": True}

    app_api.set_active_vector_index_profile = fake_select_profile
    app_api.clear_runtime_state = lambda: captured.update(cleared=True)
    app_api.public_provider_info = lambda: {
        "embedding_provider": "ollama",
        "remote_embedding_model": "",
    }

    response = TestClient(app_api.app).post(
        "/api/set-embedding-model",
        json={"model": "", "provider": "remote", "use_local": True},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["model"] == "nomic-embed-text-v2-moe"
    assert captured["disabled"] is True
    assert captured["profile"]["embedding_model"] == "nomic-embed-text-v2-moe"
    assert captured["cleared"] is True
    assert response.json()["index_reused"] is True
    assert response.json()["index_action"] == "reuse"


def test_app_api_set_provider_accepts_lmstudio_selection(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _load_llm_provider_module(storage_dir)
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.ensure_internal_embedding_model = lambda: ("", "")
    app_api.clear_runtime_state = lambda: None

    client = TestClient(app_api.app)
    response = client.post("/api/set-provider", json={"mode": "lmstudio"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["mode"] == "lmstudio"
    assert payload["info"]["effective_mode"] == "lmstudio"


def test_app_api_set_provider_offloads_embedding_probe(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    monkeypatch.setattr(app_api, "set_runtime_mode", lambda _mode: None)
    monkeypatch.setattr(app_api, "clear_runtime_state", lambda: None)
    monkeypatch.setattr(
        app_api,
        "public_provider_info",
        lambda: {"mode": "local", "effective_mode": "ollama"},
    )

    def slow_embedding_probe() -> tuple[str, str]:
        time.sleep(0.15)
        return "nomic-embed-text", "ok"

    monkeypatch.setattr(
        app_api,
        "ensure_internal_embedding_model",
        slow_embedding_probe,
    )

    async def run_probe() -> dict[str, Any]:
        request = app_api.SetProviderRequest(mode="ollama")
        started_at = time.perf_counter()
        task = asyncio.create_task(app_api.api_set_provider(request))
        await asyncio.sleep(0.02)
        assert time.perf_counter() - started_at < 0.10
        return await task

    payload = asyncio.run(run_probe())

    assert payload["ok"] is True
    assert payload["applied"] is True


def test_app_api_switching_back_to_ollama_can_restore_default_url(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _load_llm_provider_module(storage_dir)
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.ensure_internal_embedding_model = lambda: ("", "")
    app_api.clear_runtime_state = lambda: None
    client = TestClient(app_api.app)

    custom_url = "http://192.168.1.50:11434"
    custom = client.post(
        "/api/set-ollama-url",
        json={"url": custom_url, "provider": "ollama"},
    )
    assert custom.status_code == 200
    assert custom.json()["info"]["ollama_base_url"] == custom_url

    client.post("/api/set-provider", json={"mode": "lmstudio"})
    response = client.post(
        "/api/set-provider",
        json={"mode": "ollama", "use_default_ollama_url": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["applied"] is True
    assert payload["effective_mode"] == "ollama"
    assert payload["info"]["ollama_base_url"] == "http://localhost:11434"


def test_app_api_set_provider_applies_the_address_with_the_software(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _load_llm_provider_module(storage_dir)
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.ensure_internal_embedding_model = lambda: ("", "")
    app_api.clear_runtime_state = lambda: None
    client = TestClient(app_api.app)

    # Ollama on another computer, then LM Studio on this one: neither moves the other.
    remote_ollama = client.post(
        "/api/set-provider",
        json={"mode": "ollama", "url": "http://192.168.1.50:11434"},
    )
    assert remote_ollama.status_code == 200
    assert remote_ollama.json()["info"]["ollama_base_url"] == "http://192.168.1.50:11434"

    local_lmstudio = client.post(
        "/api/set-provider",
        json={"mode": "lmstudio", "url": "http://127.0.0.1:1234"},
    )
    assert local_lmstudio.status_code == 200
    info = local_lmstudio.json()["info"]
    assert local_lmstudio.json()["effective_mode"] == "lmstudio"
    assert info["lmstudio_base_url"] == "http://127.0.0.1:1234"
    assert info["ollama_base_url"] == "http://192.168.1.50:11434"

    back_to_ollama = client.post("/api/set-provider", json={"mode": "ollama"})
    assert back_to_ollama.json()["info"]["ollama_base_url"] == "http://192.168.1.50:11434"

    rejected = client.post(
        "/api/set-provider",
        json={"mode": "lmstudio", "url": "http://192.168.1.50:1234/v1"},
    )
    assert rejected.status_code == 422
    assert client.get("/api/provider-info").json()["effective_mode"] == "ollama"


def test_app_api_index_stays_local_until_it_is_told_to_follow_the_chat(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _load_llm_provider_module(storage_dir)
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.ensure_internal_embedding_model = lambda: ("embeddinggemma", "")
    app_api.clear_runtime_state = lambda: None
    client = TestClient(app_api.app)

    switched = client.post(
        "/api/set-provider",
        json={"mode": "lmstudio", "url": "http://192.168.1.50:1234"},
    )
    info = switched.json()["info"]
    assert info["effective_mode"] == "lmstudio"
    assert info["embedding_source"] == "local"
    assert info["embedding_provider"] == "ollama"
    assert info["embedding_base_url"] == "http://localhost:11434"

    followed = client.post("/api/set-embedding-source", json={"source": "chat"})
    assert followed.status_code == 200
    info = followed.json()["info"]
    assert info["embedding_source"] == "chat"
    assert info["embedding_provider"] == "lmstudio"
    assert info["embedding_base_url"] == "http://192.168.1.50:1234"

    assert client.post("/api/set-embedding-source", json={"source": "cloud"}).status_code == 422
    assert client.get("/api/provider-info").json()["embedding_source"] == "chat"


def test_app_api_document_tools_source_is_an_explicit_choice(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _load_llm_provider_module(storage_dir)
    app_api = _load_app_api_module(storage_dir, data_dir)
    app_api.ensure_internal_embedding_model = lambda: ("embeddinggemma", "")
    app_api.clear_runtime_state = lambda: None
    from src.rag import ocr_ingestion

    ocr_state = {"active": False}
    monkeypatch.setattr(ocr_ingestion, "status", lambda: dict(ocr_state))
    client = TestClient(app_api.app)

    moved = client.post(
        "/api/set-provider",
        json={"mode": "ollama", "url": "http://192.168.1.50:11434"},
    )
    info = moved.json()["info"]
    assert info["document_tools_source"] == "local"
    assert info["document_tools_base_url"] == "http://localhost:11434"

    ocr_state["active"] = True
    busy = client.post("/api/set-document-tools-source", json={"source": "chat"})
    assert busy.status_code == 409
    ocr_state["active"] = False

    followed = client.post("/api/set-document-tools-source", json={"source": "chat"})
    assert followed.status_code == 200
    assert followed.json()["info"]["document_tools_base_url"] == "http://192.168.1.50:11434"
    assert client.post("/api/set-document-tools-source", json={"source": "x"}).status_code == 422


def test_app_api_set_local_url_accepts_lmstudio_provider(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _load_llm_provider_module(storage_dir)
    app_api = _load_app_api_module(storage_dir, data_dir)

    client = TestClient(app_api.app)
    response = client.post(
        "/api/set-ollama-url",
        json={"url": "http://127.0.0.1:1234", "provider": "lmstudio"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["url"] == "http://127.0.0.1:1234"
    assert payload["info"]["lmstudio_base_url"] == "http://127.0.0.1:1234"


def test_app_api_setting_same_local_url_keeps_rag_runtime(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _load_llm_provider_module(storage_dir)
    app_api = _load_app_api_module(storage_dir, data_dir)
    cleared = {"count": 0}
    app_api.clear_runtime_state = lambda: cleared.update(count=cleared["count"] + 1)

    response = TestClient(app_api.app).post(
        "/api/set-ollama-url",
        json={"url": "http://localhost:11434/", "provider": "ollama"},
    )

    assert response.status_code == 200
    assert response.json()["changed"] is False
    assert cleared["count"] == 0


def test_app_api_setting_changed_local_url_clears_rag_runtime_once(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    _load_llm_provider_module(storage_dir)
    app_api = _load_app_api_module(storage_dir, data_dir)
    cleared = {"count": 0}
    app_api.clear_runtime_state = lambda: cleared.update(count=cleared["count"] + 1)

    response = TestClient(app_api.app).post(
        "/api/set-ollama-url",
        json={"url": "http://192.168.1.50:11434", "provider": "ollama"},
    )

    assert response.status_code == 200
    assert response.json()["changed"] is True
    assert cleared["count"] == 1


def test_provider_info_restores_persisted_lmstudio_selection(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "provider_state.json").write_text(
        '{"mode": "lmstudio", "lmstudio_base_url": "http://127.0.0.1:1234"}',
        encoding="utf-8",
    )
    _load_llm_provider_module(storage_dir)
    app_api = _load_app_api_module(storage_dir, data_dir)

    client = TestClient(app_api.app)
    response = client.get("/api/provider-info")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "lmstudio"
    assert payload["effective_mode"] == "lmstudio"
    assert payload["lmstudio_public_selection_enabled"] is True


def test_app_api_drafting_suggestions_passes_exclusion_keys(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_suggest_citations_for_draft(
        *,
        paragraph: str,
        model_name: str | None = None,
        max_suggestions: int = 5,
        exclude_citation_keys: list[str] | None = None,
        user_hint: str | None = None,
        context_before: str | None = None,
        context_after: str | None = None,
    ) -> dict[str, Any]:
        captured["paragraph"] = paragraph
        captured["model_name"] = model_name
        captured["max_suggestions"] = max_suggestions
        captured["exclude_citation_keys"] = list(exclude_citation_keys or [])
        captured["user_hint"] = user_hint
        captured["context_before"] = context_before
        return {"assistant_text": "ok", "suggestions": []}

    app_api.suggest_citations_for_draft = fake_suggest_citations_for_draft
    client = TestClient(app_api.app)
    response = client.post(
        "/api/drafting-suggestions",
        json={
            "paragraph": "A paragraph with citations.",
            "model": "demo-model",
            "max_suggestions": 3,
            "exclude_citation_keys": ["Doe2024", "Smith2023"],
            "user_hint": "Smith 2020, screening currents",
        },
    )

    assert response.status_code == 200
    assert captured["paragraph"] == "A paragraph with citations."
    assert captured["model_name"] == "demo-model"
    assert captured["max_suggestions"] == 3
    assert captured["exclude_citation_keys"] == ["Doe2024", "Smith2023"]
    assert captured["user_hint"] == "Smith 2020, screening currents"
    assert captured["context_before"] == ""


def test_app_api_drafting_suggestions_streams_events(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_stream_citations_for_draft(
        *,
        paragraph: str,
        model_name: str | None = None,
        max_suggestions: int = 40,
        exclude_citation_keys: list[str] | None = None,
        user_hint: str | None = None,
        context_before: str | None = None,
        context_after: str | None = None,
    ):
        captured["paragraph"] = paragraph
        captured["model_name"] = model_name
        captured["max_suggestions"] = max_suggestions
        captured["exclude_citation_keys"] = list(exclude_citation_keys or [])
        captured["user_hint"] = user_hint
        captured["context"] = (context_before, context_after)
        yield {"type": "start", "total_sentences": 1}
        yield {
            "type": "sentence_result",
            "sentence_index": 0,
            "suggestions": [{"citation_id": "Doe2024"}],
        }
        yield {"type": "done", "accepted_suggestions": 1}

    app_api.stream_citations_for_draft = fake_stream_citations_for_draft
    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/drafting-suggestions/stream",
        json={
            "paragraph": "A paragraph with citations.",
            "model": "demo-model",
            "max_suggestions": 7,
            "exclude_citation_keys": ["Smith2023"],
            "user_hint": "Doe 2024, ripple losses",
            "context_before": "Ripple fields heat the coil.",
            "context_after": "They also drift.",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["paragraph"] == "A paragraph with citations."
    assert captured["user_hint"] == "Doe 2024, ripple losses"
    assert captured["model_name"] == "demo-model"
    assert captured["max_suggestions"] == 7
    assert captured["exclude_citation_keys"] == ["Smith2023"]
    assert captured["context"] == ("Ripple fields heat the coil.", "They also drift.")
    assert '"type": "start"' in body
    assert '"type": "sentence_result"' in body
    assert '"citation_id": "Doe2024"' in body
    assert '"type": "done"' in body


def test_build_chat_debug_metrics_includes_answer_origin(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    metrics = app_api._build_chat_debug_metrics(
        model="demo-model",
        started_at=1.0,
        first_token_at=1.2,
        output_text="Answer: demo\n\nExplanation: demo\n\nResponse note:",
        answer_origin="wiki+rag",
    )

    assert metrics["model"] == "demo-model"
    assert metrics["answer_origin"] == "wiki+rag"


def test_chat_endpoint_stores_the_answer_length(workspace_tmp_dir: Path, monkeypatch: Any) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    length = {"setting": "auto", "resolved": "concise", "list": False, "num_predict": 450}

    def fake_stream_chat_api(message: str, model_name: str, threshold: float, **kwargs: Any):
        yield {"type": "answer_length", "data": dict(length)}
        yield {"type": "token", "text": "YBa2Cu3O7 [S1]."}
        yield {"type": "sources", "data": [], "answer_origin": "rag"}

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "Quelle est la formule des REBCO ?", "model": "demo-model", "threshold": 0.2},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"type": "answer_length"' not in body
    chat_id = app_api.list_conversations()[0]["id"]
    saved = [m for m in app_api.get_conversation_messages(chat_id) if m["role"] == "assistant"]
    assert saved[-1]["meta"]["answer_length"] == length
    assert saved[-1]["meta"]["debug_metrics"]["answer_length"] == length


def test_chat_endpoint_scopes_quick_library_question_to_selected_article(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_stream_chat_api(message: str, model_name: str, threshold: float, **kwargs: Any):
        captured["message"] = message
        captured["model_name"] = model_name
        captured["threshold"] = threshold
        captured["kwargs"] = kwargs
        yield {"type": "token", "text": "article scoped ok"}
        yield {"type": "sources", "data": [], "answer_origin": "rag"}

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "liste tous les articles qui parlent de cette méthode",
            "model": "demo-model",
            "threshold": 0.25,
            "article_path": "ABCD1234/Paper.pdf",
            "article_title": "A focused paper",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["message"] == "liste tous les articles qui parlent de cette méthode"
    assert captured["kwargs"]["source_paths"] == ["ABCD1234/Paper.pdf"]
    assert "article scoped ok" in body

    chat_id = app_api.list_conversations()[0]["id"]
    messages = app_api.get_conversation_messages(chat_id)
    assert messages[0]["meta"]["article_path"] == "ABCD1234/Paper.pdf"
    assert messages[0]["meta"]["article_title"] == "A focused paper"
    assert messages[0]["meta"]["article_scope_once"] is False


def test_chat_endpoint_scopes_question_to_several_selected_articles(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_stream_chat_api(message: str, model_name: str, threshold: float, **kwargs: Any):
        captured["kwargs"] = kwargs
        yield {"type": "token", "text": "multi scoped ok"}
        yield {"type": "sources", "data": [], "answer_origin": "rag"}

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "compare ces articles",
            "model": "demo-model",
            "article_paths": ["A/One.pdf", "B/Two.pdf", "A/One.pdf", " "],
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["kwargs"]["source_paths"] == ["A/One.pdf", "B/Two.pdf"]
    assert "multi scoped ok" in body

    chat_id = app_api.list_conversations()[0]["id"]
    meta = app_api.get_conversation_messages(chat_id)[0]["meta"]
    assert meta["article_paths"] == ["A/One.pdf", "B/Two.pdf"]
    assert meta["article_path"] == ""


def test_discovery_origin_routes(workspace_tmp_dir: Path, monkeypatch: Any) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    import src.chat_origin as chat_origin
    import src.origin_roots as origin_roots

    topics = []

    async def fake_roots(topic: str):
        topics.append(topic)
        return {"ok": True, "topic": topic, "roots": [], "spectrum": [], "meta": {}}

    monkeypatch.setattr(origin_roots, "find_topic_roots", fake_roots)
    monkeypatch.setattr(chat_origin, "trace_library_chain", lambda idea, model, threshold: (
        {"ok": True, "claim": idea, "steps": [], "earliest": None, "stopped_early": False}
        if model else {"ok": False, "error": "index_unavailable", "msg": "Index RAG indisponible."}
    ))
    client = TestClient(app_api.app)
    try:
        app_api.app.dependency_overrides[app_api.external_api_requests_allowed] = lambda: True
        no_ack = client.post("/api/discovery/origins/roots", json={"topic": "critical state"})
        roots = client.post("/api/discovery/origins/roots", json={"topic": "critical state", "privacy_ack": True})
        app_api.app.dependency_overrides[app_api.external_api_requests_allowed] = lambda: False
        offline = client.post("/api/discovery/origins/roots", json={"topic": "critical state", "privacy_ack": True})
    finally:
        app_api.app.dependency_overrides.clear()
    assert no_ack.status_code == 403 and offline.status_code == 403
    assert roots.status_code == 200 and topics == ["critical state"]

    chain = client.post("/api/discovery/origins/library", json={"idea": "critical state", "model": "demo"})
    assert chain.status_code == 200 and chain.json()["claim"] == "critical state"
    no_index = client.post("/api/discovery/origins/library", json={"idea": "critical state"})
    assert no_index.status_code == 503 and no_index.json()["msg"] == "Index RAG indisponible."


def test_chat_origin_command_starts_from_the_selected_article(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    import src.chat_origin as chat_origin

    captured: dict[str, Any] = {}

    def fake_origin(claim: str, model_name: str, threshold: float, **kwargs: Any):
        captured.update(claim=claim, model=model_name, kwargs=kwargs)
        yield {"type": "token", "text": "origin ok"}
        yield {"type": "sources", "data": [], "answer_origin": "origin_trace"}

    monkeypatch.setattr(chat_origin, "stream_chat_origin", fake_origin)
    monkeypatch.setattr(app_api, "stream_chat_api", lambda *args, **kwargs: pytest.fail("not plain RAG"))
    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "/origine le modèle de l'état critique",
            "model": "demo-model",
            "article_path": "ABCD1234/Paper.pdf",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "origin ok" in body
    assert captured["claim"] == "le modèle de l'état critique"
    assert captured["kwargs"]["article_paths"] == ["ABCD1234/Paper.pdf"]
    meta = app_api.get_conversation_messages(app_api.list_conversations()[0]["id"])[0]["meta"]
    assert meta["mode"] == "origin_trace"


def test_chat_endpoint_forced_visual_request_uses_rag_and_forwards_flag(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_stream_chat_api(message: str, model_name: str, threshold: float, **kwargs: Any):
        captured.update(message=message, model=model_name, threshold=threshold, kwargs=kwargs)
        yield {"type": "token", "text": "visual rag ok"}
        yield {"type": "sources", "data": [], "answer_origin": "rag"}

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)
    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "Create a bibliography while reading Figure 8",
            "model": "demo-model",
            "force_visual_analysis": True,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["kwargs"]["force_visual_analysis"] is True
    assert "visual rag ok" in body
    chat_id = app_api.list_conversations()[0]["id"]
    messages = app_api.get_conversation_messages(chat_id)
    assert messages[0]["meta"]["force_visual_analysis"] is True
    assert messages[1]["meta"]["force_visual_analysis"] is True


def test_chat_endpoint_routes_literature_review_intent(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    import src.rag.literature_review as literature_review

    def fake_stream_literature_review(
        user_query: str,
        model_name: str,
        threshold: float,
        **kwargs: Any,
    ):
        captured["query"] = user_query
        captured["model_name"] = model_name
        captured["threshold"] = threshold
        captured["kwargs"] = kwargs
        yield {"type": "token", "text": "review ok"}
        yield {"type": "sources", "data": [], "answer_origin": "literature_review"}

    def unexpected_rag(*args: Any, **kwargs: Any):
        raise AssertionError("normal RAG should not be used for literature-review intent")

    monkeypatch.setattr(
        literature_review, "stream_literature_review", fake_stream_literature_review
    )
    monkeypatch.setattr(app_api, "stream_chat_api", unexpected_rag)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "fais une revue de litterature sur les pertes AC HTS",
            "model": "demo-model",
            "threshold": 0.25,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["query"] == "fais une revue de litterature sur les pertes AC HTS"
    assert captured["model_name"] == "demo-model"
    assert captured["threshold"] == 0.25
    assert captured["kwargs"]["target_chunks"] == 24
    assert '"answer_origin": "literature_review"' in body
    assert "review ok" in body


@pytest.mark.parametrize("endpoint", ["/api/chat", "/api/literature-review"])
def test_review_stopped_during_the_synthesis_is_saved_as_interrupted(
    workspace_tmp_dir: Path, monkeypatch: Any, endpoint: str
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    chat = app_api.create_conversation(title="Review stop")

    import src.rag.literature_review as literature_review

    def stopped_review(*args: Any, abort_signal: Any = None, **kwargs: Any):
        yield {"type": "status", "msg": "12 passages, synthèse en cours"}
        abort_signal.abort("user_stop")
        # The review generator returns on Stop: no token, no error.

    monkeypatch.setattr(literature_review, "stream_literature_review", stopped_review)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        endpoint,
        json={
            "chat_id": chat["id"],
            "message": "fais une revue de litterature sur les pertes AC HTS",
            "model": "demo-model",
            "threshold": 0.25,
            "hint_action": "literature_review",
        },
    ) as response:
        "".join(response.iter_text())

    saved = _last_assistant_message(app_api, chat["id"])
    assert saved["content"] == "[Réponse interrompue par l'utilisateur]"
    assert saved["meta"]["interrupted"] is True
    assert saved["meta"]["interruption_reason"] == "user_stop"


def test_chat_endpoint_routes_paper_list_intent(workspace_tmp_dir: Path, monkeypatch: Any) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(app_api, "get_app_settings", lambda: {"ui_language": "en"})

    import src.rag.paper_list as paper_list

    def fake_stream_paper_list(
        user_query: str,
        model_name: str,
        threshold: float,
        **kwargs: Any,
    ):
        captured["query"] = user_query
        captured["model_name"] = model_name
        captured["threshold"] = threshold
        captured["kwargs"] = kwargs
        yield {"type": "token", "text": "paper list ok"}
        yield {"type": "sources", "data": [], "answer_origin": "paper_list"}

    def unexpected_rag(*args: Any, **kwargs: Any):
        raise AssertionError("normal RAG should not be used for paper-list intent")

    monkeypatch.setattr(paper_list, "stream_paper_list", fake_stream_paper_list)
    monkeypatch.setattr(app_api, "stream_chat_api", unexpected_rag)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "liste tous les papers qui parlent des pertes AC dans les HTS",
            "model": "demo-model",
            "threshold": 0.2,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["query"] == "liste tous les papers qui parlent des pertes AC dans les HTS"
    assert captured["model_name"] == "demo-model"
    assert captured["threshold"] == 0.2
    assert captured["kwargs"]["target_chunks"] == 32
    assert captured["kwargs"]["abort_signal"] is not None
    # The answer follows the question (French here), not the English UI.
    assert captured["kwargs"]["language"] == "fr"
    assert '"type": "tool_started"' in body
    assert '"name": "search.local_articles"' in body
    assert '"type": "tool_result"' in body
    assert '"answer_origin": "paper_list"' in body
    assert "paper list ok" in body


def test_chat_followup_after_paper_list_lists_more_papers(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    calls: list[dict[str, Any]] = []

    import src.rag.paper_list as paper_list

    def fake_stream_paper_list(user_query: str, model_name: str, threshold: float, **kwargs: Any):
        calls.append({"query": user_query, **kwargs})
        yield {"type": "token", "text": f"list {len(calls)}"}
        source = {"article_id": f"a{len(calls)}", "title": f"Paper {len(calls)}"}
        yield {"type": "sources", "data": [source], "answer_origin": "paper_list"}

    def unexpected_rag(*args: Any, **kwargs: Any):
        raise AssertionError("a paper-list follow-up must not reach plain RAG")

    monkeypatch.setattr(paper_list, "stream_paper_list", fake_stream_paper_list)
    monkeypatch.setattr(app_api, "stream_chat_api", unexpected_rag)

    client = TestClient(app_api.app)
    chat_id = ""
    for message in (
        "quels articles parlent de HTS magnets ?",
        "est-ce qu'il y en a d'autres ?",
        "non je voulais dire de low temperature superconductors",
    ):
        payload: dict[str, Any] = {"message": message, "model": "demo-model", "threshold": 0.2}
        if chat_id:
            payload["chat_id"] = chat_id
        with client.stream("POST", "/api/chat", json=payload) as response:
            assert response.status_code == 200
            "".join(response.iter_text())
        chat_id = chat_id or app_api.list_conversations()[0]["id"]

    assert [call["query"] for call in calls] == [
        "quels articles parlent de HTS magnets ?",
        "quels articles parlent de HTS magnets ?",
        "low temperature superconductors",
    ]
    assert calls[0]["exclude_sources"] == []
    assert calls[1]["exclude_sources"] == [{"article_id": "a1", "title": "Paper 1"}]
    assert calls[1]["target_chunks"] == paper_list.FOLLOWUP_PAPER_LIST_CHUNKS
    assert calls[2]["exclude_sources"] == []



def test_chat_count_doubt_after_suggested_author_list_stays_in_paper_list(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    """2026-10-21 report: "seulement 3 ?" reached plain RAG, which denied a listed paper."""
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    calls: list[dict[str, Any]] = []

    import src.rag.paper_list as paper_list

    def fake_stream_paper_list(user_query: str, model_name: str, threshold: float, **kwargs: Any):
        calls.append({"query": user_query, **kwargs})
        yield {"type": "token", "text": "list"}
        yield {
            "type": "sources",
            "data": [{"article_id": "a1", "title": "Paper 1"}],
            "answer_origin": "paper_list",
        }

    def unexpected_rag(*args: Any, **kwargs: Any):
        raise AssertionError("a doubt about the list count must not reach plain RAG")

    monkeypatch.setattr(paper_list, "stream_paper_list", fake_stream_paper_list)
    monkeypatch.setattr(app_api, "stream_chat_api", unexpected_rag)

    client = TestClient(app_api.app)
    chat_id = ""
    for message, hint in (("Find papers written by claire", "local_search"), ("seulement 3 ?", None)):
        payload: dict[str, Any] = {"message": message, "model": "demo-model", "threshold": 0.2}
        if hint:
            payload["hint_action"] = hint
        if chat_id:
            payload["chat_id"] = chat_id
        with client.stream("POST", "/api/chat", json=payload) as response:
            assert response.status_code == 200
            "".join(response.iter_text())
        chat_id = chat_id or app_api.list_conversations()[0]["id"]

    assert len(calls) == 2
    assert calls[1]["query"] == calls[0]["query"]
    assert calls[1]["exclude_sources"] == [{"article_id": "a1", "title": "Paper 1"}]

@pytest.mark.parametrize("depth", ["auto", "quick", "deep"])
def test_chat_ordinary_question_reaches_retrieval_without_llm_planner(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
    depth: str,
) -> None:
    from src import app_help

    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    planner_calls: list[dict] = []
    retrieved_depths: list[str] = []

    def unexpected_planner(**kwargs: Any):
        planner_calls.append(kwargs)
        return None, ""

    def unexpected_documentation():
        raise AssertionError("Scientific questions must not read or search application docs")

    monkeypatch.setattr(app_help, "_load_sections", unexpected_documentation)

    def fake_stream(*args: Any, **kwargs: Any):
        retrieved_depths.append(kwargs["depth"])
        yield {"type": "token", "text": "Direct answer [S1]"}
        yield {"type": "sources", "data": [{"title": "Source"}], "answer_origin": "rag"}

    monkeypatch.setattr(app_api, "_classify_chat_tool_intent_with_llm", unexpected_planner)
    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream)
    response = TestClient(app_api.app).post(
        "/api/chat",
        json={
            "message": "comment modéliser les pertes AC dans les aimants supra",
            "model": "demo-model",
            "depth": depth,
        },
    )
    assert response.status_code == 200
    assert planner_calls == []
    assert retrieved_depths == ["quick" if depth == "auto" else depth]
    assert "Direct answer [S1]" in response.text
    assert '"title": "Source"' in response.text
    assert '"stage": "tool_planner"' in response.text
    assert '"source": "deterministic"' in response.text


@pytest.mark.parametrize("article_path", ["", "selected-paper.pdf"])
def test_chat_help_and_followup_use_docs_without_scientific_retrieval(
    workspace_tmp_dir: Path, monkeypatch: Any, article_path: str,
) -> None:
    from src import app_help

    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    prompts: list[Any] = []

    def unexpected(*args: Any, **kwargs: Any):
        raise AssertionError("Help must not enter RAG or the model tool planner")

    def fake_llm(model, messages, **kwargs):
        prompts.append(messages)
        yield {"content": "Ouvrez les Paramètres, puis Mes documents."}

    monkeypatch.setattr(app_api, "stream_chat_api", unexpected)
    monkeypatch.setattr(app_api, "_classify_chat_tool_intent_with_llm", unexpected)
    monkeypatch.setattr(app_help, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(app_help, "stream_ollama_chat", fake_llm)
    client = TestClient(app_api.app)
    response = client.post("/api/chat", json={
        "message": "Comment connecter Zotero ?", "model": "demo-model",
        "article_path": article_path,
    })
    assert response.status_code == 200
    assert '"name": "app.help"' in response.text
    assert '"answer_origin": "app_help"' in response.text
    assert "Documentation :" in response.text
    chat_id = app_api.list_conversations()[0]["id"]
    response = client.post("/api/chat", json={
        "message": "Et ensuite ?", "model": "demo-model", "chat_id": chat_id,
        "article_path": article_path,
    })
    assert '"source": "previous_help"' in response.text
    assert len(prompts) == 2
    assert "Zotero" in prompts[1][-1]["content"]
    messages = app_api.get_conversation_messages(chat_id)
    assert messages[-1]["meta"]["mode"] == "app_help"
    assert messages[-1]["meta"]["sources"] == []


def test_chat_drafts_from_pasted_material_without_rag_or_planner(
    workspace_tmp_dir: Path, monkeypatch: Any,
) -> None:
    from src import chat_drafting

    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    captured: dict[str, Any] = {}
    message = (
        "Avec ce contexte, tu peux me rédiger une introduction pour l'article ?\n"
        "Titre proposé\nAimant HTS\nObjectif : Valider la conception.\n"
        "Plan : discussion et références.\n" + "Mesures. " * 600 + "59 quenches."
    )

    def unexpected(*args: Any, **kwargs: Any):
        raise AssertionError("Drafting must not enter retrieval or the tool classifier")

    def fake_draft(query, model, **kwargs):
        captured.update(query=query, model=model, **kwargs)
        yield {"type": "token", "text": "Introduction expérimentale."}
        yield {"type": "sources", "data": [], "answer_origin": "drafting"}

    monkeypatch.setattr(app_api, "stream_chat_api", unexpected)
    monkeypatch.setattr(app_api, "_classify_chat_tool_intent_with_llm", unexpected)
    monkeypatch.setattr(chat_drafting, "stream_chat_drafting", fake_draft)
    response = TestClient(app_api.app).post(
        "/api/chat", json={"message": message, "model": "demo-model"}
    )
    assert response.status_code == 200
    assert captured["query"] == message
    assert captured["previous_messages"] == []
    assert captured["abort_signal"] is not None
    assert '"name": "chat.draft"' in response.text
    assert '"answer_origin": "drafting"' in response.text
    assert '"source": "deterministic"' in response.text
    chat = app_api.list_conversations()[0]
    assistant = app_api.get_conversation_messages(chat["id"])[-1]
    assert assistant["meta"]["mode"] == "drafting"
    assert assistant["content"] == "Introduction expérimentale."


def test_contextual_discovery_reuses_previous_scientific_topic(
    workspace_tmp_dir: Path,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    message = "ok est-ce que tu peux chercher des nouveaux articles ?"
    history = [
        {"role": "user", "content": "Bobines HTS sous 20 T ?"},
        {"role": "assistant", "content": "Des essais existent."},
        {"role": "user", "content": "Et pour des bobines soudées ?"},
    ]
    assert not app_api._discovery_request_needs_topic(message, history)
    assert app_api._extract_explicit_discovery_query(message) == ""
    query = app_api._build_local_article_search_query(message, history)
    assert "20 T" in query and "soudées" in query


def test_bug_report_preserves_routing_and_reranker_diagnostics(workspace_tmp_dir: Path) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    metrics = app_api._safe_bug_report_debug_metrics({"pipeline_metrics": [
        {"stage": "tool_planner", "data": {
            "source": "deterministic", "mode": "drafting", "explicit": False,
            "prompt": "private text",
        }},
        {"stage": "reranker", "data": {
            "device": "cuda:0", "dtype": "torch.bfloat16", "profile": "auto",
            "predict_duration_ms": 300.0, "queue_duration_ms": 5.0,
            "max_candidate_chars": 1000, "query": "private query",
        }},
    ]})
    planner, reranker = metrics["pipeline_metrics"]
    assert planner["data"] == {"source": "deterministic", "mode": "drafting", "explicit": False}
    assert reranker["data"]["device"] == "cuda:0"
    assert reranker["data"]["queue_duration_ms"] == 5.0
    assert "private" not in str(metrics)


def test_chat_slow_tool_planner_falls_back_before_planner_finishes(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    planner_started = threading.Event()
    planner_finished = threading.Event()
    release_planner = threading.Event()
    retrieval_before_planner: list[bool] = []

    def slow_planner(**kwargs: Any):
        planner_started.set()
        try:
            release_planner.wait(timeout=2)
            return None, ""
        finally:
            planner_finished.set()

    def fake_stream(*args: Any, **kwargs: Any):
        retrieval_before_planner.append(not planner_finished.is_set())
        release_planner.set()
        yield {"type": "token", "text": "Fallback answer"}
        yield {"type": "sources", "data": [], "answer_origin": "rag"}

    monkeypatch.setattr(app_api, "_CHAT_TOOL_PLANNER_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(app_api, "_classify_chat_tool_intent_with_llm", slow_planner)
    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream)
    try:
        response = TestClient(app_api.app).post(
            "/api/chat",
            json={
                "message": "prepare un panorama des publications consacrees aux pertes AC",
                "model": "demo-model",
                "depth": "auto",
            },
        )
        assert response.status_code == 200
        assert planner_started.is_set()
        assert retrieval_before_planner == [True]
        assert "Fallback answer" in response.text
        assert '"source": "llm_fallback"' in response.text
    finally:
        release_planner.set()
        planner_finished.wait(timeout=2)


def test_chat_endpoint_uses_llm_planner_for_unrecognized_tool_request(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    import src.rag.paper_list as paper_list

    def fake_invoke_ollama_generate(
        model_name: str,
        prompt: str,
        **kwargs: Any,
    ) -> tuple[str, str]:
        captured["planner_model"] = model_name
        captured["planner_kwargs"] = kwargs
        captured["planner_prompt"] = prompt
        return "", "LOCAL_SEARCH"

    def fake_stream_paper_list(
        user_query: str,
        model_name: str,
        threshold: float,
        **kwargs: Any,
    ):
        captured["query"] = user_query
        yield {"type": "token", "text": "planned paper list ok"}
        yield {"type": "sources", "data": [], "answer_origin": "paper_list"}

    monkeypatch.setattr(app_api, "is_remote_provider", lambda: False)
    monkeypatch.setattr(app_api, "is_lmstudio_provider", lambda: False)
    monkeypatch.setattr(app_api, "invoke_ollama_generate", fake_invoke_ollama_generate)
    monkeypatch.setattr(paper_list, "stream_paper_list", fake_stream_paper_list)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "prepare un panorama des publications consacrees aux pertes AC",
            "model": "qwen2.5:0.5b",
            "threshold": 0.2,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["planner_model"] == "qwen2.5:0.5b"
    assert captured["planner_kwargs"]["think"] is False
    assert captured["planner_kwargs"]["options_override"]["num_predict"] == 16
    assert captured["planner_kwargs"]["request_timeout_seconds"] == 5.0
    assert "Return exactly ONE code" in captured["planner_prompt"]
    assert captured["query"] == "prepare un panorama des publications consacrees aux pertes AC"
    assert '"name": "search.local_articles"' in body
    assert "planned paper list ok" in body


def test_chat_endpoint_ui_answer_hint_bypasses_model_tool_planner(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def unexpected_planner(**kwargs: Any):
        raise AssertionError("an explicit UI answer hint must bypass the LLM planner")

    def fake_stream_chat_api(message: str, model_name: str, threshold: float, **kwargs: Any):
        captured["message"] = message
        yield {"type": "token", "text": "rag answer ok"}
        yield {"type": "sources", "data": [], "answer_origin": "rag"}

    monkeypatch.setattr(app_api, "_classify_chat_tool_intent_with_llm", unexpected_planner)
    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "Quels articles comparent des approches pour les pertes AC ?",
            "model": "demo-model",
            "hint_action": "answer",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["message"].startswith("Quels articles comparent")
    assert '"name": "chat.answer"' in body
    assert "rag answer ok" in body


def test_chat_endpoint_replaces_streamed_draft_before_persisting(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    chat = app_api.create_conversation(title="Streaming replacement")

    def fake_stream_chat_api(*args: Any, **kwargs: Any):
        yield {"type": "token", "text": "Draft answer"}
        yield {"type": "answer_replace", "text": "Final guarded answer"}
        yield {"type": "sources", "data": [], "answer_origin": "rag"}

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "chat_id": chat["id"],
            "message": "Question",
            "model": "demo-model",
            "hint_action": "answer",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"type": "token", "text": "Draft answer"' in body
    assert '"type": "answer_replace", "text": "Final guarded answer"' in body
    saved = app_api.get_conversation_messages(chat["id"])
    assistant_messages = [message for message in saved if message["role"] == "assistant"]
    assert assistant_messages[-1]["content"] == "Final guarded answer"


def _stream_chat(app_api: Any, payload: dict[str, Any]) -> str:
    with TestClient(app_api.app).stream("POST", "/api/chat", json=payload) as response:
        assert response.status_code == 200
        return "".join(response.iter_text())


def _last_assistant_message(app_api: Any, chat_id: str) -> dict[str, Any]:
    saved = app_api.get_conversation_messages(chat_id)
    return [message for message in saved if message["role"] == "assistant"][-1]


def test_chat_endpoint_rejects_empty_message(workspace_tmp_dir: Path) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")

    body = _stream_chat(app_api, {"message": "   ", "model": "demo-model"})

    assert '"type": "error", "msg": "Message vide."' in body


# Closing the stream ends the answer, so the model stays loaded for the next
# question, qwen3.5 included now that its answers are streamed too.
@pytest.mark.parametrize("model", ["demo-model", "qwen3.5:4b"])
def test_chat_endpoint_keeps_partial_answer_when_generation_is_interrupted(
    workspace_tmp_dir: Path, monkeypatch: Any, model: str
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    chat = app_api.create_conversation(title="Interrupted")
    unloaded: list[str] = []

    def fake_stream_chat_api(*args: Any, **kwargs: Any):
        yield {"type": "token", "text": "Partial answer"}
        kwargs["abort_signal"].abort("user_stop")
        raise InterruptedError

    def fake_stop_local_ollama_model(model: str) -> bool:
        unloaded.append(model)
        return True

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)
    monkeypatch.setattr(app_api, "stop_local_ollama_model", fake_stop_local_ollama_model)

    body = _stream_chat(
        app_api,
        {"chat_id": chat["id"], "message": "Question", "model": model, "hint_action": "answer"},
    )

    assert '"text": "Partial answer"' in body
    assert '"type": "error"' not in body
    saved = _last_assistant_message(app_api, chat["id"])
    assert saved["content"] == "Partial answer\n\n[Réponse interrompue par l'utilisateur]"
    assert saved["meta"]["interrupted"] is True
    assert saved["meta"]["interruption_reason"] == "user_stop"
    assert saved["meta"]["mode"] == "rag"
    assert "debug_metrics" in saved["meta"]
    assert unloaded == []


# The RAG pipeline turns Stop into an "error" event and returns: the saved
# answer must still say it was interrupted, before or after the first token.
@pytest.mark.parametrize("reason", ["user_stop", "context_switch", "page_exit"])
@pytest.mark.parametrize("tokens", [["Partial ", "answer"], []])
def test_chat_endpoint_marks_answer_stopped_through_an_error_event(
    workspace_tmp_dir: Path, monkeypatch: Any, reason: str, tokens: list[str]
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    chat = app_api.create_conversation(title="Stopped")

    def fake_stream_chat_api(*args: Any, **kwargs: Any):
        for token in tokens:
            yield {"type": "token", "text": token}
        kwargs["abort_signal"].abort(reason)
        yield {"type": "error", "msg": "Generation aborted.", "remote_fallback": False}

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    _stream_chat(
        app_api,
        {"chat_id": chat["id"], "message": "Question", "model": "demo-model", "hint_action": "answer"},
    )

    saved = _last_assistant_message(app_api, chat["id"])
    from src.api.streams import stream_interruption_text

    notice = stream_interruption_text(reason)
    assert saved["meta"]["interrupted"] is True
    assert saved["meta"]["interruption_reason"] == reason
    assert saved["meta"].get("error") is None
    assert "Erreur" not in saved["content"]
    if tokens:
        assert saved["content"].startswith("Partial answer\n\n[Réponse interrompue")
    else:
        assert saved["content"].startswith("[Réponse interrompue")
    assert saved["content"].endswith(notice)


def test_chat_endpoint_marks_answer_cut_by_a_provider_error_as_incomplete(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    chat = app_api.create_conversation(title="Cut")

    def fake_stream_chat_api(*args: Any, **kwargs: Any):
        yield {"type": "token", "text": "Le champ vaut 1"}
        yield {"type": "error", "msg": "Réponse incomplète : flux arrêté.", "remote_fallback": False}

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    _stream_chat(
        app_api,
        {"chat_id": chat["id"], "message": "Question", "model": "demo-model", "hint_action": "answer"},
    )

    saved = _last_assistant_message(app_api, chat["id"])
    assert saved["content"] == (
        "Le champ vaut 1\n\n[Réponse incomplète : la génération s'est arrêtée sur une erreur]"
    )
    assert saved["meta"]["incomplete"] is True
    assert saved["meta"]["incomplete_reason"] == "Réponse incomplète : flux arrêté."
    assert saved["meta"].get("interrupted") is None


@pytest.mark.parametrize(
    ("tokens", "expected_content", "expected_error_flag"),
    [
        (
            ["Half answer"],
            "Half answer\n\n[Réponse incomplète : la génération s'est arrêtée sur une erreur]",
            None,
        ),
        ([], "Erreur: chat_generation_failed", True),
    ],
)
def test_chat_endpoint_reports_generation_failure(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
    tokens: list[str],
    expected_content: str,
    expected_error_flag: bool | None,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    chat = app_api.create_conversation(title="Failure")

    def fake_stream_chat_api(*args: Any, **kwargs: Any):
        for token in tokens:
            yield {"type": "token", "text": token}
        raise RuntimeError("model crashed")

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    body = _stream_chat(
        app_api,
        {"chat_id": chat["id"], "message": "Question", "model": "demo-model", "hint_action": "answer"},
    )

    assert '"type": "error", "msg": "chat_generation_failed"' in body
    assert "model crashed" not in body
    saved = _last_assistant_message(app_api, chat["id"])
    assert saved["content"] == expected_content
    assert saved["meta"].get("error") is expected_error_flag
    assert saved["meta"]["mode"] == "rag"


def test_chat_endpoint_persists_tool_error_without_answer(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    chat = app_api.create_conversation(title="Tool error")

    def fake_stream_chat_api(*args: Any, **kwargs: Any):
        yield {"type": "error", "msg": "Index indisponible"}

    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    body = _stream_chat(
        app_api,
        {"chat_id": chat["id"], "message": "Question", "model": "demo-model", "hint_action": "answer"},
    )

    assert '"msg": "Index indisponible"' in body
    saved = _last_assistant_message(app_api, chat["id"])
    assert saved["content"] == "Erreur: Index indisponible"
    assert saved["meta"] == {"error": True, "mode": "rag"}


def test_chat_endpoint_refuses_external_article_search_without_permission(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    chat = app_api.create_conversation(title="Discovery without permission")

    async def unexpected_run_chat_discovery(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("external discovery must not run without permission")

    monkeypatch.setitem(
        app_api.app.dependency_overrides, app_api.external_api_requests_allowed, lambda: False
    )
    monkeypatch.setattr(app_api, "_run_chat_discovery", unexpected_run_chat_discovery)

    body = _stream_chat(
        app_api,
        {
            "chat_id": chat["id"],
            "message": "cherche des articles sur les pertes AC dans les aimants HTS",
            "model": "demo-model",
            "privacy_ack": True,
            "external_search_confirmed": True,
            "article_search_scope": "external",
        },
    )

    assert '"answer_origin": "article_discovery"' in body
    saved = _last_assistant_message(app_api, chat["id"])
    assert saved["content"] == app_api.EXTERNAL_API_PERMISSION_MESSAGE
    assert saved["meta"]["mode"] == "article_discovery"


def test_chat_endpoint_ui_compare_hint_routes_without_model_planner(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    import src.rag.native_chat as native_chat

    def unexpected_planner(**kwargs: Any):
        raise AssertionError("an explicit UI compare hint must bypass the LLM planner")

    def fake_native_action(mode: str, user_query: str, *args: Any, **kwargs: Any):
        captured["mode"] = mode
        captured["query"] = user_query
        yield {"type": "token", "text": "compare ok"}
        yield {"type": "sources", "data": [], "answer_origin": mode}

    monkeypatch.setattr(app_api, "_classify_chat_tool_intent_with_llm", unexpected_planner)
    monkeypatch.setattr(native_chat, "stream_native_retrieval_action", fake_native_action)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "Compare ces approches",
            "model": "demo-model",
            "hint_action": "compare",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured == {"mode": "paper_compare", "query": "Compare ces approches"}
    assert '"name": "research.compare_papers"' in body
    assert "compare ok" in body


def test_chat_endpoint_rejects_llm_planned_write_tool(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    def fake_invoke_ollama_generate(*args: Any, **kwargs: Any) -> tuple[str, str]:
        return "", "CREATE_NOTEBOOK"

    def fake_stream_chat_api(*args: Any, **kwargs: Any):
        yield {"type": "token", "text": "safe rag fallback"}
        yield {"type": "sources", "data": [], "answer_origin": "rag"}

    monkeypatch.setattr(app_api, "is_remote_provider", lambda: False)
    monkeypatch.setattr(app_api, "is_lmstudio_provider", lambda: False)
    monkeypatch.setattr(app_api, "invoke_ollama_generate", fake_invoke_ollama_generate)
    monkeypatch.setattr(app_api, "stream_chat_api", fake_stream_chat_api)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={"message": "Quels sont les themes principaux ?", "model": "demo-model"},
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"name": "chat.answer"' in body
    assert '"name": "notebook.create"' not in body
    assert "safe rag fallback" in body


def test_chat_endpoint_routes_document_export_intent(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    chat = app_api.create_conversation(title="Export test")
    app_api.add_chat_message(chat["id"], "user", "Question source", model="demo-model")
    app_api.add_chat_message(chat["id"], "assistant", "Reponse source", model="demo-model")

    import src.chat_export as chat_export

    def fake_stream_chat_export(
        *,
        user_query: str,
        model_name: str,
        previous_messages: list[dict[str, Any]],
        export_format: str,
        scope: str,
    ):
        captured["user_query"] = user_query
        captured["model_name"] = model_name
        captured["previous_messages"] = previous_messages
        captured["export_format"] = export_format
        captured["scope"] = scope
        saved = {
            "filename": "demo.md",
            "url": "/api/chat-exports/demo.md",
            "format": "markdown",
        }
        yield {"type": "token", "text": "Fichier cree : [demo.md](/api/chat-exports/demo.md)"}
        yield {"type": "export_file", "data": saved}
        yield {"type": "sources", "data": [], "answer_origin": "document_export"}

    def unexpected_rag(*args: Any, **kwargs: Any):
        raise AssertionError("normal RAG should not be used for document-export intent")

    monkeypatch.setattr(chat_export, "stream_chat_export", fake_stream_chat_export)
    monkeypatch.setattr(app_api, "stream_chat_api", unexpected_rag)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "chat_id": chat["id"],
            "message": "/md message precedent",
            "model": "demo-model",
            "threshold": 0.2,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["user_query"] == "message precedent"
    assert captured["export_format"] == "markdown"
    assert captured["scope"] == "previous"
    assert any(msg["content"] == "Reponse source" for msg in captured["previous_messages"])
    assert '"answer_origin": "document_export"' in body
    assert "/api/chat-exports/demo.md" in body

    saved = app_api.get_conversation_messages(chat["id"])
    assistant_messages = [msg for msg in saved if msg["role"] == "assistant"]
    assert assistant_messages[-1]["meta"]["mode"] == "document_export"
    assert assistant_messages[-1]["meta"]["export_file"]["filename"] == "demo.md"


def test_chat_endpoint_routes_article_discovery_intent(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    chat = app_api.create_conversation(title="Discovery test")
    app_api.add_chat_message(
        chat["id"],
        "user",
        "Je travaille sur les pertes AC dans les aimants HTS.",
        model="demo-model",
    )
    app_api.add_chat_message(
        chat["id"],
        "assistant",
        "On peut maintenant chercher des articles relies.",
        model="demo-model",
    )

    async def fake_run_chat_discovery(
        *,
        chat_id: str = "",
        explicit_query: str = "",
        message_context: list[dict[str, Any]] | None = None,
        model_name: str = "",
        max_results: int = 8,
        include_unpaywall: bool = True,
        debug: bool = False,
        fallback_to_latest_chat: bool = False,
    ) -> dict[str, Any]:
        captured["chat_id"] = chat_id
        captured["explicit_query"] = explicit_query
        captured["message_context"] = list(message_context or [])
        captured["model_name"] = model_name
        captured["max_results"] = max_results
        captured["include_unpaywall"] = include_unpaywall
        captured["debug"] = debug
        captured["fallback_to_latest_chat"] = fallback_to_latest_chat
        return {
            "ok": True,
            "results": [
                {
                    "title": "AC losses in HTS magnets",
                    "year": "2024",
                    "authors": ["Doe"],
                    "authors_line": "Doe",
                    "llm_rationale": "Traite directement des pertes AC dans les aimants HTS.",
                    "short_summary": "Article tres proche du besoin.",
                    "doi": "10.1234/demo",
                    "venue": "Superconductivity Journal",
                }
            ],
            "warnings": [],
            "history_id": "search-1",
            "input_text": "Sujet prioritaire de recherche:\npertes AC dans les aimants HTS",
            "meta": {
                "result_count": 1,
                "provider_used": "Semantic Scholar",
                "elapsed_ms": 42.0,
            },
        }

    def fake_format_chat_discovery_answer(
        payload: dict[str, Any], *, model_name: str, user_message: str
    ) -> str:
        captured["format_payload"] = payload
        captured["format_model_name"] = model_name
        captured["format_user_message"] = user_message
        return (
            "J'ai trouve 1 article pertinent.\n"
            "- **AC losses in HTS magnets** (2024) — Traite directement du sujet.\n\n"
            "Ouvrez l'onglet Decouverte pour voir tous les resultats."
        )

    def unexpected_rag(*args: Any, **kwargs: Any):
        raise AssertionError("normal RAG should not be used for article-discovery intent")

    monkeypatch.setitem(
        app_api.app.dependency_overrides, app_api.external_api_requests_allowed, lambda: True
    )
    monkeypatch.setattr(app_api, "_run_chat_discovery", fake_run_chat_discovery)
    monkeypatch.setattr(app_api, "_format_chat_discovery_answer", fake_format_chat_discovery_answer)
    monkeypatch.setattr(app_api, "stream_chat_api", unexpected_rag)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "chat_id": chat["id"],
            "message": "cherche des articles sur les pertes AC dans les aimants HTS",
            "model": "demo-model",
            "threshold": 0.2,
            "privacy_ack": True,
            "external_search_confirmed": True,
            "article_search_scope": "external",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["chat_id"] == chat["id"]
    assert captured["explicit_query"] == "les pertes AC dans les aimants HTS"
    assert any(
        msg["content"] == "On peut maintenant chercher des articles relies."
        for msg in captured["message_context"]
    )
    assert captured["model_name"] == "demo-model"
    assert captured["max_results"] == 8
    assert captured["format_model_name"] == "demo-model"
    assert (
        captured["format_user_message"]
        == "cherche des articles sur les pertes AC dans les aimants HTS"
    )
    assert '"type": "discovery_session"' in body
    assert '"answer_origin": "article_discovery"' in body
    assert '"history_id": "search-1"' in body

    saved = app_api.get_conversation_messages(chat["id"])
    assistant_messages = [msg for msg in saved if msg["role"] == "assistant"]
    assert assistant_messages[-1]["meta"]["mode"] == "article_discovery"
    assert assistant_messages[-1]["meta"]["discovery_session"]["history_id"] == "search-1"


@pytest.mark.parametrize("message", [
    "cherche des articles sur les pertes AC dans les aimants HTS",
    "ok est-ce que tu peux chercher des nouveaux articles ?",
])
def test_chat_endpoint_asks_confirmation_before_external_article_search(
    workspace_tmp_dir: Path, monkeypatch: Any, message: str
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    chat = app_api.create_conversation(title="Discovery confirmation")
    app_api.add_chat_message(chat["id"], "user", "Tests de bobines HTS soudées sous 20 T ?")

    async def unexpected_run_chat_discovery(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("external article discovery should wait for user confirmation")

    monkeypatch.setitem(
        app_api.app.dependency_overrides, app_api.external_api_requests_allowed, lambda: True
    )
    monkeypatch.setattr(app_api, "_run_chat_discovery", unexpected_run_chat_discovery)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "chat_id": chat["id"],
            "message": message,
            "model": "demo-model",
            "threshold": 0.2,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert '"type": "tool_confirmation"' in body
    assert '"type": "external_search_confirmation"' in body
    assert "Ou voulez-vous chercher ces articles" in body
    assert '"external_allowed": true' in body
    assert '"type": "discovery_session"' not in body

    saved = app_api.get_conversation_messages(chat["id"])
    assistant_messages = [msg for msg in saved if msg["role"] == "assistant"]
    assert assistant_messages[-1]["meta"]["mode"] == "article_discovery"
    assert "external_search_confirmation" in assistant_messages[-1]["meta"]


@pytest.mark.parametrize("message", [
    "Cherche des articles en rapport avec ce sujet",
    "ok est-ce que tu peux chercher des nouveaux articles ?",
    "Cherche de nouveaux articles ?",
])
def test_chat_endpoint_asks_for_missing_referential_topic_before_any_search(
    workspace_tmp_dir: Path, monkeypatch: Any, message: str
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    import src.rag.paper_list as paper_list

    async def unexpected_run_chat_discovery(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a missing topic must not launch external discovery")

    def unexpected_stream_paper_list(*args: Any, **kwargs: Any):
        raise AssertionError("a missing topic must not launch local retrieval")

    monkeypatch.setattr(app_api, "_run_chat_discovery", unexpected_run_chat_discovery)
    monkeypatch.setattr(paper_list, "stream_paper_list", unexpected_stream_paper_list)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": message,
            "model": "demo-model",
            "threshold": 0.2,
            "article_search_scope": "local",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert "Quel sujet souhaitez-vous utiliser" in body
    assert '"type": "tool_started"' not in body
    assert '"type": "tool_confirmation"' not in body
    assert '"type": "external_search_confirmation"' not in body
    assert '"type": "discovery_session"' not in body
    assert "Aucun article pertinent" not in body

    chat_id = app_api.list_conversations()[0]["id"]
    saved = app_api.get_conversation_messages(chat_id)
    assistant_messages = [msg for msg in saved if msg["role"] == "assistant"]
    assert assistant_messages[-1]["meta"]["mode"] == "article_discovery"
    assert assistant_messages[-1]["meta"]["tool_plan"]["action"] == "clarify"
    assert assistant_messages[-1]["meta"]["tool_plan"]["calls"] == []


def test_chat_endpoint_routes_article_discovery_local_choice_to_paper_list(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    import src.rag.paper_list as paper_list

    def fake_stream_paper_list(
        user_query: str,
        model_name: str,
        threshold: float,
        **kwargs: Any,
    ):
        captured["query"] = user_query
        captured["model_name"] = model_name
        captured["threshold"] = threshold
        captured["kwargs"] = kwargs
        yield {"type": "token", "text": "local paper search ok"}
        yield {"type": "sources", "data": [], "answer_origin": "paper_list"}

    async def unexpected_run_chat_discovery(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("external discovery should not be used for local article search")

    monkeypatch.setattr(paper_list, "stream_paper_list", fake_stream_paper_list)
    monkeypatch.setattr(app_api, "_run_chat_discovery", unexpected_run_chat_discovery)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/chat",
        json={
            "message": "cherche des articles sur les pertes AC dans les aimants HTS",
            "model": "demo-model",
            "threshold": 0.2,
            "article_search_scope": "local",
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["query"] == "les pertes AC dans les aimants HTS"
    assert captured["model_name"] == "demo-model"
    assert captured["threshold"] == 0.2
    assert captured["kwargs"]["target_chunks"] == 32
    assert '"answer_origin": "paper_list"' in body
    assert "local paper search ok" in body


def test_notebook_chat_endpoint_plans_note_write_from_natural_language(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_stream_notebook_chat(
        nb_id: str,
        message: str,
        model_name: str,
        append_to_notes: bool = False,
        **kwargs: Any,
    ):
        captured["nb_id"] = nb_id
        captured["message"] = message
        captured["model_name"] = model_name
        captured["append_to_notes"] = append_to_notes
        yield {"type": "token", "text": "analyse ok"}
        if append_to_notes:
            yield {"type": "notes_updated", "notes": "analyse ok"}

    monkeypatch.setattr(app_api, "stream_notebook_chat", fake_stream_notebook_chat)

    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/notebooks/nb-demo/chat",
        json={
            "message": "ok est-ce que tu rajouter tout ça dans les Notes ?",
            "model": "qwen2.5:0.5b",
            "append_to_notes": False,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["append_to_notes"] is True
    assert '"name": "notebook.analyze"' in body
    assert '"name": "notebook.append_notes"' in body
    assert '"type": "notes_updated"' in body


def test_notebook_chat_read_only_mode_skips_note_write_and_history(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    captured: dict[str, Any] = {}

    def fake_stream_notebook_chat(
        nb_id: str,
        message: str,
        model_name: str,
        append_to_notes: bool = False,
        **kwargs: Any,
    ):
        captured["append_to_notes"] = append_to_notes
        captured["persist_history"] = kwargs.get("persist_history")
        captured["read_only"] = kwargs.get("read_only")
        yield {"type": "token", "text": "recherche ok"}

    monkeypatch.setattr(app_api, "stream_notebook_chat", fake_stream_notebook_chat)

    with TestClient(app_api.app).stream(
        "POST",
        "/api/notebooks/nb-demo/chat",
        json={
            "message": "analyse ce passage et ajoute la réponse dans les notes",
            "model": "demo-model",
            "append_to_notes": True,
            "persist_history": False,
            "read_only": True,
        },
    ) as response:
        body = "".join(response.iter_text())

    assert response.status_code == 200
    assert captured["append_to_notes"] is False
    assert captured["persist_history"] is False
    assert captured["read_only"] is True
    assert '"name": "notebook.analyze"' in body
    assert '"name": "notebook.append_notes"' not in body
    assert '"type": "notes_updated"' not in body


def test_chat_export_download_endpoint_serves_only_export_dir(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    import src.chat_export as chat_export

    out_dir = chat_export.export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "demo.md").write_text("# Demo\n", encoding="utf-8")
    (storage_dir / "secret.md").write_text("# Secret\n", encoding="utf-8")

    client = TestClient(app_api.app)
    ok = client.get("/api/chat-exports/demo.md")
    assert ok.status_code == 200
    assert "# Demo" in ok.text

    missing = client.get("/api/chat-exports/missing.md")
    assert missing.status_code == 404

    traversal = client.get("/api/chat-exports/..%2Fsecret.md")
    assert traversal.status_code == 404


def test_chat_export_clear_endpoint_deletes_generated_files(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    import src.chat_export as chat_export

    out_dir = chat_export.export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "demo.md").write_text("# Demo\n", encoding="utf-8")
    (out_dir / "demo.tex").write_text("\\documentclass{article}\n", encoding="utf-8")
    (out_dir / "keep.tmp").write_text("tmp", encoding="utf-8")

    client = TestClient(app_api.app)
    response = client.post("/api/chat-exports/clear", json={"confirm": True})

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["chat_exports"]["files_deleted"] == 2
    assert not (out_dir / "demo.md").exists()
    assert not (out_dir / "demo.tex").exists()
    assert (out_dir / "keep.tmp").exists()


def test_drafting_export_endpoint_generates_markdown_file(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    client = TestClient(app_api.app)
    response = client.post(
        "/api/drafting-export",
        json={
            "content": "Draft section with evidence [1].",
            "export_format": "markdown",
            "bibliography": [
                {
                    "authors": "Dupont",
                    "year": "2026",
                    "title": "Draft source",
                    "doi": "10.123/demo",
                }
            ],
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    export_file = payload["export_file"]
    assert export_file["filename"].endswith(".md")
    assert export_file["bibliography_count"] == 1
    exported_path = Path(export_file["path"])
    assert exported_path.exists()
    content = exported_path.read_text(encoding="utf-8")
    assert content.startswith("# Draft section with evidence")
    assert "## Bibliographie" in content
    assert "1. Dupont (2026). Draft source. DOI: 10.123/demo." in content


def test_drafting_export_endpoint_writes_latex_citations_and_bibliography(
    workspace_tmp_dir: Path,
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")

    client = TestClient(app_api.app)
    response = client.post(
        "/api/drafting-export",
        json={
            "content": "## Methods\n\nField *limits* matter \uE020dupont_2026\uE021\uE020lee2025\uE021.",
            "export_format": "latex",
            "title": "Methods",
            "bibliography": [
                {"latex_key": "dupont_2026", "authors_display": "Dupont", "year": "2026", "title": "Draft source"},
                {"latex_key": "lee2025", "authors_display": "Lee", "year": "2025", "title": "Other"},
            ],
        },
    )

    assert response.status_code == 200
    content = Path(response.json()["export_file"]["path"]).read_text(encoding="utf-8")
    assert r"\subsection*{Methods}" in content
    assert r"Field \emph{limits} matter \cite{dupont_2026,lee2025}." in content
    assert r"\bibitem{dupont_2026} Dupont, ``Draft source'', 2026." in content
    assert content.index(r"\end{thebibliography}") < content.index(r"\end{document}")


def test_drafting_resolve_citations_endpoint_returns_known_keys(
    workspace_tmp_dir: Path, monkeypatch
) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    from src import library
    from src.zotero_db import ZoteroItem

    monkeypatch.setattr(
        library,
        "_zotero_library",
        {"a.pdf": ZoteroItem(item_key="K1", title="Known", authors=["Doe"], year="2020", citation_key="doe2020")},
    )
    client = TestClient(app_api.app)
    response = client.post("/api/drafting-resolve-citations", json={"keys": ["doe2020", "nope"]})

    assert response.status_code == 200
    payload = response.json()
    assert list(payload["records"]) == ["doe2020"]
    assert payload["records"]["doe2020"]["title"] == "Known"


def test_drafting_origin_route(workspace_tmp_dir: Path, monkeypatch) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    import src.api.drafting as drafting_api
    from src.origin_tracing import OriginTraceError

    calls = []

    def fake_trace(sentence, rel_path, *, evidence_quote, model_name):
        calls.append((sentence, rel_path, evidence_quote, model_name))
        if rel_path == "missing.pdf":
            raise OriginTraceError("document_not_found", "Article introuvable dans la bibliothèque.")
        return {"status": "ok", "verdict": "no_attribution", "citations": []}

    monkeypatch.setattr(drafting_api, "trace_origin", fake_trace)
    client = TestClient(app_api.app)
    body = {"sentence": "Bulks trap 17 T.", "rel_path": "a.pdf", "evidence_quote": "trap 17 T", "model": "qwen3.5:4b"}
    response = client.post("/api/drafting-origin", json=body)
    assert response.status_code == 200
    assert response.json()["verdict"] == "no_attribution"
    assert calls[0] == ("Bulks trap 17 T.", "a.pdf", "trap 17 T", "qwen3.5:4b")

    missing = client.post("/api/drafting-origin", json={**body, "rel_path": "missing.pdf"})
    assert missing.status_code == 404
    assert missing.json() == {"ok": False, "error": "document_not_found", "msg": "Article introuvable dans la bibliothèque."}


def test_drafting_origin_identify_needs_web_access(workspace_tmp_dir: Path, monkeypatch) -> None:
    app_api = _load_app_api_module(workspace_tmp_dir / "storage", workspace_tmp_dir / "data")
    import src.api.drafting as drafting_api

    sent = []

    def identify_reference(text):
        sent.append(text)
        return {"year": "1964"}

    monkeypatch.setattr(drafting_api, "identify_reference", identify_reference)
    client = TestClient(app_api.app)
    body = {"reference": "[1] C. P. Bean, Rev. Mod. Phys. 36, 31 (1964)."}

    app_api.app.dependency_overrides[app_api.external_api_requests_allowed] = lambda: False
    try:
        refused = client.post("/api/drafting-origin-identify", json=body)
        app_api.app.dependency_overrides[app_api.external_api_requests_allowed] = lambda: True
        allowed = client.post("/api/drafting-origin-identify", json=body)
    finally:
        app_api.app.dependency_overrides.clear()

    assert refused.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json() == {"ok": True, "work": {"year": "1964"}}
    assert sent == [body["reference"]]  # only the reference, after the permission check


def test_delete_chat_also_deletes_linked_export_files(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    import src.chat_export as chat_export

    out_dir = chat_export.export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    export_file = out_dir / "linked.md"
    export_file.write_text("# Linked\n", encoding="utf-8")

    chat = app_api.create_conversation(title="Export linked")
    app_api.add_chat_message(chat["id"], "user", "Question")
    app_api.add_chat_message(
        chat["id"],
        "assistant",
        "Fichier cree : [linked.md](/api/chat-exports/linked.md)",
        meta={"mode": "document_export", "export_file": {"filename": "linked.md"}},
    )

    client = TestClient(app_api.app)
    response = client.delete(f"/api/chats/{chat['id']}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["export_files_deleted"] == 1
    assert not export_file.exists()


def test_app_api_removes_notebook_message_by_index(workspace_tmp_dir: Path) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    client = TestClient(app_api.app)
    notebooks = cast(Any, importlib.import_module("src.notebooks"))

    created = client.post("/api/notebooks", json={"title": "Carnet test"}).json()
    nb_id = created["id"]
    notebooks.add_message(nb_id, "user", "Question")
    notebooks.add_message(nb_id, "assistant", "Reponse")

    response = client.delete(f"/api/notebooks/{nb_id}/messages/0")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    saved = client.get(f"/api/notebooks/{nb_id}").json()
    assert [msg["content"] for msg in saved["messages"]] == ["Reponse"]


def test_app_api_open_pdf_serves_only_files_under_data_path(workspace_tmp_dir: Path) -> None:
    """L'endpoint PDF doit servir un fichier autorise et refuser une tentative de remontee de repertoire."""
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    client = TestClient(app_api.app)

    pdf_path = data_dir / "papers" / "safe.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")
    (workspace_tmp_dir / "secret.pdf").write_bytes(b"%PDF-1.7\n")

    ok_response = client.get("/api/open-pdf", params={"path": "papers/safe.pdf"})
    assert ok_response.status_code == 200
    assert ok_response.headers["content-type"].startswith("application/pdf")

    blocked_response = client.get("/api/open-pdf", params={"path": "../secret.pdf"})
    assert blocked_response.status_code == 404


def test_app_api_open_pdf_supports_unicode_filename_in_same_origin_reader(
    workspace_tmp_dir: Path,
) -> None:
    """Le lecteur intégré doit accepter les noms Unicode sans relâcher les autres réponses."""
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    client = TestClient(app_api.app)

    filename = "Wallis et al. – 2021 – Transient voltages.pdf"
    pdf_path = data_dir / "papers" / filename
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")

    response = client.get("/api/open-pdf", params={"path": f"papers/{filename}"})

    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "frame-ancestors 'self'" in response.headers["content-security-policy"]
    assert "filename*=utf-8''" in response.headers["content-disposition"].lower()


def test_app_api_extract_pdf_text_uses_loader_and_returns_preview(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    """L'extraction texte doit renvoyer un apercu de pages quand le chargeur PDF reussit."""
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    client = TestClient(app_api.app)

    pdf_path = data_dir / "papers" / "preview.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")

    class FakeLoader:
        def __init__(self, path: str) -> None:
            self.path = path

        def load(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(page_content="Page 1"),
                SimpleNamespace(page_content="Page 2"),
            ]

    import langchain_community.document_loaders as document_loaders

    monkeypatch.setattr(document_loaders, "PyPDFLoader", FakeLoader)

    response = client.get("/api/extract-pdf-text", params={"path": "papers/preview.pdf"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["pages"] == 2
    assert "Page 1" in payload["text"]
    assert "Page 2" in payload["text"]


def test_app_api_imports_zotero_collection_into_notebook(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    client = TestClient(app_api.app)

    pdf_path = data_dir / "papers" / "article.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")

    fake_item = SimpleNamespace(
        as_markdown=lambda: "## Zotero metadata\n\n**Collections:** Projet HTS"
    )
    collection_records = [
        {
            "path": "papers/article.pdf",
            "full_path": str(pdf_path),
            "title": "Article HTS",
            "item": fake_item,
        }
    ]
    monkeypatch.setattr(
        app_api,
        "list_zotero_collection_pdfs",
        lambda collection, include_descendants=True: list(collection_records),
    )

    class FakeLoader:
        def __init__(self, path: str) -> None:
            self.path = path

        def load(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(page_content="Texte PDF de la collection")]

    import langchain_community.document_loaders as document_loaders

    monkeypatch.setattr(document_loaders, "PyPDFLoader", FakeLoader)
    monkeypatch.setattr(
        app_api,
        "visual_analysis_model_settings",
        lambda: ("index", "vision:test", ""),
    )
    monkeypatch.setattr(
        app_api,
        "analyze_pdf_visuals",
        lambda *_args, **_kwargs: {
            "model": "vision:test",
            "results": [{"page": 1, "text": "Le diagramme compare deux conducteurs HTS."}],
        },
    )

    response = client.post(
        "/api/notebooks/import-zotero-collection",
        json={"collection": "Projet HTS"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["created"] is True
    assert payload["added"] == 1
    notebook = client.get(f"/api/notebooks/{payload['notebook_id']}").json()
    assert notebook["title"] == "Zotero - Projet HTS"
    assert notebook["source_collection"]["provider"] == "zotero"
    assert notebook["source_collection"]["name"] == "Projet HTS"
    assert notebook["source_collection"]["include_descendants"] is True
    assert notebook["source_collection"]["last_synced"]
    assert notebook["sources"][0]["title"] == "Article HTS"
    assert notebook["sources"][0]["source_path"] == "papers/article.pdf"
    assert "Zotero metadata" in notebook["sources"][0]["content"]
    assert "Texte PDF de la collection" in notebook["sources"][0]["content"]
    assert notebook["sources"][0]["visual_model"] == "vision:test"
    assert "deux conducteurs HTS" in notebook["sources"][0]["visual_content"]

    second_pdf_path = data_dir / "papers" / "article-2.pdf"
    second_pdf_path.write_bytes(b"%PDF-1.7\n")
    collection_records.append(
        {
            "path": "papers/article-2.pdf",
            "full_path": str(second_pdf_path),
            "title": "Article HTS 2",
            "item": fake_item,
        }
    )
    refresh = client.post(
        "/api/notebooks/import-zotero-collection",
        json={
            "notebook_id": payload["notebook_id"],
            "collection": "Projet HTS",
        },
    )
    assert refresh.status_code == 200
    assert refresh.json()["added"] == 1
    assert refresh.json()["skipped"] == 1
    refreshed_notebook = client.get(f"/api/notebooks/{payload['notebook_id']}").json()
    assert [source["title"] for source in refreshed_notebook["sources"]] == [
        "Article HTS",
        "Article HTS 2",
    ]


def test_app_api_streams_zotero_import_progress_and_limits_lazy_pdf_pages(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    client = TestClient(app_api.app)

    pdf_path = data_dir / "papers" / "long-article.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7\n")
    monkeypatch.setattr(
        app_api,
        "list_zotero_collection_pdfs",
        lambda collection, include_descendants=True: [
            {
                "path": "papers/long-article.pdf",
                "full_path": str(pdf_path),
                "title": "Article long",
                "item": SimpleNamespace(as_markdown=lambda: "Metadonnees Zotero"),
            }
        ],
    )

    class LazyLoader:
        pages_read = 0

        def __init__(self, path: str) -> None:
            self.path = path

        def lazy_load(self):
            for number in range(1, 6):
                type(self).pages_read += 1
                yield SimpleNamespace(page_content=f"Page limitee {number}")

    import langchain_community.document_loaders as document_loaders

    monkeypatch.setattr(document_loaders, "PyPDFLoader", LazyLoader)

    response = client.post(
        "/api/notebooks/import-zotero-collection",
        headers={"Accept": "text/event-stream"},
        json={"collection": "Projet HTS", "max_pages": 2},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "start"' in response.text
    assert '"type": "progress"' in response.text
    assert '"type": "done"' in response.text
    assert LazyLoader.pages_read == 2

    notebook_id = next(
        notebook["id"]
        for notebook in client.get("/api/notebooks").json()["notebooks"]
        if notebook["title"] == "Zotero - Projet HTS"
    )
    source_content = client.get(f"/api/notebooks/{notebook_id}").json()["sources"][0]["content"]
    assert "Page limitee 1" in source_content
    assert "Page limitee 2" in source_content
    assert "Page limitee 3" not in source_content


def test_app_api_knowledge_graph_uses_article_taxonomy(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    expected = {
        "ok": True,
        "engine": "rag_article_taxonomy",
        "graph_type": "article_taxonomy",
        "root_ids": ["article-tax-root"],
        "nodes": [{"id": "article-tax-root", "label": "Applied superconductivity"}],
        "edges": [],
        "children_by_parent": {},
        "articles_by_concept": {"article-tax-root": [{"article_id": "p1"}]},
        "stats": {"categories": 1, "roots": 1, "articles": 1, "build_ms": 0},
        "hidden_node_count": 0,
    }
    monkeypatch.setattr(app_api, "build_article_taxonomy_payload", lambda **_kwargs: expected)

    client = TestClient(app_api.app)
    response = client.get("/api/knowledge-graph")

    assert response.status_code == 200
    assert response.json() == expected


def test_progressive_article_discovery_streams_progress_preliminary_then_done(
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)
    from src.api import application
    from src.api import discovery as discovery_routes

    budgets: list[Any] = []

    async def fake_discover_articles(**kwargs: Any) -> dict[str, Any]:
        budgets.append(kwargs.get("budget_seconds"))
        on_event = kwargs["on_event"]
        on_event({"type": "progress", "stage": "search", "msg": "Interrogation des sources…"})
        on_event({"type": "preliminary", "results": [{"title": "Draft ranking"}], "meta": {}})
        return {"ok": True, "results": [{"title": "Validated ranking"}], "warnings": [], "meta": {}}

    original_discover = application.discover_articles
    app_api.app.dependency_overrides[app_api.external_api_requests_allowed] = lambda: True
    application.discover_articles = fake_discover_articles
    try:
        response = TestClient(app_api.app).post(
            "/api/article-discovery/stream",
            json={"mode": "search", "input_text": "HTS AC losses", "model": "demo", "privacy_ack": True},
        )
    finally:
        app_api.app.dependency_overrides.clear()
        application.discover_articles = original_discover

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    body = response.text
    assert body.index('"type": "progress"') < body.index('"type": "preliminary"') < body.index('"type": "done"')
    assert "Draft ranking" in body and "Validated ranking" in body
    assert '"history_id"' in body
    assert budgets == [discovery_routes._PROGRESSIVE_DISCOVERY_BUDGET_SECONDS]


def test_a_stopped_literature_review_keeps_its_partial_answer(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    app_api = _load_app_api_module(storage_dir, data_dir)

    import src.api.chat as chat_api
    import src.rag.literature_review as literature_review

    def stopped_review(*_args: Any, **_kwargs: Any):
        yield {"type": "token", "text": "Première partie de la revue"}
        raise InterruptedError("user_stop")

    saved: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(literature_review, "stream_literature_review", stopped_review)
    monkeypatch.setattr(
        chat_api,
        "add_chat_message",
        lambda _chat_id, role, text, **kwargs: saved.append((role, text, kwargs.get("meta") or {})),
    )
    client = TestClient(app_api.app)
    with client.stream(
        "POST",
        "/api/literature-review",
        json={"message": "revue sur les pertes AC", "model": "demo-model", "threshold": 0.25},
    ) as response:
        "".join(response.iter_text())

    assert response.status_code == 200
    assistant = [entry for entry in saved if entry[0] == "assistant"]
    assert len(assistant) == 1
    assert assistant[0][1].startswith("Première partie de la revue")
    assert assistant[0][2]["interrupted"] is True
    assert assistant[0][2]["mode"] == "literature_review"
