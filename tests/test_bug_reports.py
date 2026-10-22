from __future__ import annotations

import asyncio
import base64
import http.server
import json
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from src import bug_reports


def _accepted(captured: dict[str, Any], remote_id: str) -> Callable[[dict[str, Any]], str]:
    """A capture_event that keeps the event and settles it as a delivering transport would."""

    def capture_event(event: dict[str, Any]) -> str:
        captured["event"] = event
        bug_reports._DROPS.settle(event["event_id"])
        return remote_id

    return capture_event


def test_attached_chat_reports_truncation_without_exposing_more_content() -> None:
    result = bug_reports._sanitize_attached_chat({"entries": [
        {"role": "user", "content": "a" * 4100 + "private suffix"},
        {"role": "assistant", "content": "Short answer"},
    ]})
    assert result is not None
    first, second = result["entries"]
    assert first["content_truncated"] is True
    assert first["original_content_length"] == 4114
    assert len(first["content"]) == 4000
    assert "private suffix" not in first["content"]
    assert second["content_truncated"] is False


def test_redact_text_masks_credentials_dsn_query_values_and_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bug_reports.os.path, "expanduser", lambda _value: r"C:\Users\Alice")

    value = bug_reports.redact_text(
        r'''Authorization: Bearer auth-token
token="my token" C:\Users\Alice\papers?api_key=secret
https://public-project-key@bugs.example.test/1'''
    )

    assert "auth-token" not in value
    assert "my token" not in value
    assert "secret" not in value
    assert "public-project-key" not in value
    assert r"C:\Users\Alice" not in value
    assert value.count("[REDACTED]") >= 3
    assert "[LOCAL_PATH]" in value


def test_redact_text_masks_personal_network_document_and_llm_data() -> None:
    value = r'''Contact alice@example.org from 192.168.1.42 or 2001:db8::1.
Open "D:\Research\Secret Project\private-paper.pdf" and /home/alice/library/notes.docx.
See https://example.test/search?q=private-topic&user=alice#private-fragment.
file:///C:/Users/Alice/Zotero/storage/paper.pdf
api_key: "json-secret"
--token command-line-secret
sk-proj-1234567890abcdefghij
eyJabcdefghijk.abcdefghijk.abcdefghijk
prompt: summarize the confidential document contents'''

    cleaned = bug_reports.redact_text(value, max_length=5000)

    for secret in (
        "alice@example.org",
        "192.168.1.42",
        "2001:db8::1",
        "Secret Project",
        "private-paper.pdf",
        "/home/alice",
        "private-topic",
        "private-fragment",
        "json-secret",
        "command-line-secret",
        "sk-proj-1234567890abcdefghij",
        "eyJabcdefghijk.abcdefghijk.abcdefghijk",
        "confidential document contents",
    ):
        assert secret not in cleaned
    assert "[EMAIL]" in cleaned
    assert "[IP_ADDRESS]" in cleaned
    assert "[LOCAL_PATH]" in cleaned
    assert "[QUERY_REDACTED]" in cleaned
    assert "[CONTENT OMITTED]" in cleaned


def test_redact_text_hides_url_query_and_fragment() -> None:
    cleaned = bug_reports.redact_text("see https://api.example.org/v1?key=abc#token-xyz now")

    assert cleaned == "see https://api.example.org/v1?[QUERY_REDACTED]#[FRAGMENT_REDACTED] now"
    assert bug_reports.redact_text("http://example.org/page") == "http://example.org/page"


def test_redact_text_stays_linear_on_nested_urls() -> None:
    """CodeQL py/polynomial-redos: one match per URL, never one per inner "http://"."""
    import time

    started = time.monotonic()
    bug_reports.redact_text("http://a" * 50_000 + "?secret")
    assert time.monotonic() - started < 2.0


def test_bug_report_status_uses_bundled_bugsink_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BIBLIOBRAIN_BUGSINK_DSN", raising=False)
    monkeypatch.setenv("BIBLIOBRAIN_ERROR_REPORTING_ENABLED", "true")

    assert bug_reports.bug_report_status() == {"configured": True, "provider": "bugsink"}


def test_deliver_bug_report_sends_expected_bugsink_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_initialize(dsn: str) -> None:
        captured["dsn"] = dsn

    def fake_flush(*, timeout: float) -> None:
        captured["flush_timeout"] = timeout

    monkeypatch.setenv("BIBLIOBRAIN_BUGSINK_DSN", "https://public@example.test/42")
    monkeypatch.setenv("BIBLIOBRAIN_VERSION", "0.5-test")
    monkeypatch.setattr(bug_reports, "_ensure_sdk_initialized", fake_initialize)
    monkeypatch.setattr(bug_reports.sentry_sdk, "capture_event", _accepted(captured, "a" * 32))
    monkeypatch.setattr(bug_reports.sentry_sdk, "flush", fake_flush)
    logging.getLogger("bibliobrain.test").warning("Failed with token=must-not-leak")

    result = asyncio.run(
        bug_reports.deliver_bug_report(
            description="The save button does nothing.",
            steps="Open settings, then save.",
            contact="user@example.test",
            include_diagnostics=True,
            client_logs=[
                {
                    "timestamp": "2026-07-21T10:00:00Z",
                    "type": "window.error",
                    "message": "api_key=client-secret",
                }
            ],
            client_context={"language": "fr", "view": "tab-chat", "user_agent": "Test/1"},
        )
    )

    assert captured["dsn"] == "https://public@example.test/42"
    event = captured["event"]
    assert event["message"] == "The save button does nothing."
    assert event["tags"]["source"] == "bibliobrain-desktop"
    assert event["tags"]["report_kind"] == "manual"
    assert event["extra"]["steps_to_reproduce"] == "Open settings, then save."
    assert event["extra"]["contact"] == "user@example.test"
    assert event["contexts"]["bibliobrain"]["app_version"] == "0.5-test"
    assert event["contexts"]["bibliobrain"]["active_view"] == "tab-chat"
    breadcrumb_messages = [item["message"] for item in event["breadcrumbs"]["values"]]
    assert all("must-not-leak" not in message for message in breadcrumb_messages)
    assert all("client-secret" not in message for message in breadcrumb_messages)
    assert captured["flush_timeout"] == 5.0
    assert result["remote_id"] == "a" * 32
    assert result["report_id"]
    assert result["delivery"] == "confirmed"


def test_deliver_bug_report_attaches_opt_in_chat_models_debug_and_screenshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    jpeg = b"\xff\xd8\xff" + (b"safe-image" * 20)
    screenshot = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")

    monkeypatch.setattr(bug_reports, "_ensure_sdk_initialized", lambda _dsn: None)
    monkeypatch.setattr(bug_reports.sentry_sdk, "capture_event", _accepted(captured, "c" * 32))
    monkeypatch.setattr(bug_reports.sentry_sdk, "flush", lambda **_kwargs: None)

    asyncio.run(
        bug_reports.deliver_bug_report(
            description="The current chat fails after retrieval.",
            attached_chat={
                "message_count": 2,
                "entries": [
                    {
                        "role": "user",
                        "content": "Question with token=must-not-leak",
                        "model": "qwen3:8b",
                    },
                    {
                        "role": "assistant",
                        "content": "Safe answer",
                        "model": "qwen3:8b",
                        "debug_metrics": {"latency_ms": 120, "prompt": "hidden"},
                        "interrupted": True,
                        "interruption_reason": "page_exit",
                        "interruption_elapsed_ms": 5321.4,
                        "answer_origin": "paper_list",
                        "mode": "paper_list",
                    },
                ],
            },
            model_info={"chat_models": ["qwen3:8b"], "embedding_model": "nomic-embed"},
            debug_info={"runtime": {"ready": True}},
            screenshot_data_url=screenshot,
        )
    )

    event = bug_reports._before_send(captured["event"], {})
    attached = event["extra"]["attached_conversation"]
    assert attached["included_count"] == 2
    assert attached["entries"][0]["role"] == "user"
    assert "must-not-leak" not in attached["entries"][0]["content"]
    assert attached["entries"][1]["debug_metrics"]["prompt"] == "[CONTENT OMITTED]"
    assert attached["entries"][1]["interrupted"] is True
    assert attached["entries"][1]["interruption_reason"] == "page_exit"
    assert attached["entries"][1]["interruption_elapsed_ms"] == 5321.4
    assert attached["entries"][1]["answer_origin"] == "paper_list"
    assert event["extra"]["models"]["chat_models"] == ["qwen3:8b"]
    assert event["extra"]["debug"]["runtime"]["ready"] is True
    assert event["extra"]["screenshot_data_url"] == screenshot
    assert event["extra"]["screenshot_bytes"] == len(jpeg)


@pytest.mark.parametrize(
    "value",
    [
        "data:image/png;base64,iVBORw0KGgo=",
        "data:image/jpeg;base64,not-base64!",
        "data:image/jpeg;base64," + base64.b64encode(b"not-a-jpeg").decode("ascii"),
    ],
)
def test_validate_screenshot_data_url_rejects_unsupported_payloads(value: str) -> None:
    with pytest.raises(ValueError, match="bug_report_invalid_screenshot"):
        bug_reports.validate_screenshot_data_url(value)


def test_maximal_opt_in_report_stays_below_bugsink_event_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    jpeg = b"\xff\xd8\xff" + (b"x" * (285 * 1024 - 3))
    screenshot = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")

    monkeypatch.setattr(bug_reports, "_ensure_sdk_initialized", lambda _dsn: None)
    monkeypatch.setattr(bug_reports.sentry_sdk, "capture_event", _accepted(captured, "d" * 32))
    monkeypatch.setattr(bug_reports.sentry_sdk, "flush", lambda **_kwargs: None)
    monkeypatch.setattr(
        bug_reports._recent_logs,
        "snapshot",
        lambda _limit: [
            {
                "timestamp": "2026-07-21T10:00:00Z",
                "level": "ERROR",
                "logger": "app",
                "message": "x" * 2000,
            }
            for _ in range(100)
        ],
    )

    asyncio.run(
        bug_reports.deliver_bug_report(
            description="x" * 5000,
            steps="x" * 5000,
            include_diagnostics=True,
            client_logs=[{"type": "error", "message": "x" * 1000} for _ in range(30)],
            attached_chat={
                "message_count": 20,
                "entries": [
                    {
                        "role": "user" if index % 2 == 0 else "assistant",
                        "content": "x" * 4000,
                    }
                    for index in range(20)
                ],
            },
            screenshot_data_url=screenshot,
        )
    )

    serialized = json.dumps(
        bug_reports._before_send(captured["event"], {}),
        default=str,
        ensure_ascii=False,
    ).encode("utf-8")
    assert len(serialized) < 1024 * 1024


def test_deliver_bug_report_can_exclude_all_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    monkeypatch.setattr(bug_reports, "_ensure_sdk_initialized", lambda _dsn: None)
    monkeypatch.setattr(bug_reports.sentry_sdk, "capture_event", _accepted(captured, "b" * 32))
    monkeypatch.setattr(bug_reports.sentry_sdk, "flush", lambda **_kwargs: None)

    asyncio.run(
        bug_reports.deliver_bug_report(
            description="A sufficiently detailed report.",
            include_diagnostics=False,
            client_logs=[{"message": "must not be sent"}],
        )
    )

    assert "contexts" not in captured["event"]
    assert "breadcrumbs" not in captured["event"]
    assert "must not be sent" not in str(captured["event"])


def test_bug_reports_never_send_the_computer_name(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(bug_reports.sentry_sdk, "init", lambda **options: captured.update(options))
    monkeypatch.setattr(bug_reports, "_SDK_INITIALIZED_DSN", "")

    bug_reports._ensure_sdk_initialized("http://public@127.0.0.1:9/1")
    cleaned = bug_reports._before_send({"server_name": "PC-de-Camille", "message": "Broke."}, {})

    # None would make the SDK fill in socket.gethostname().
    assert captured["server_name"] == ""
    assert "server_name" not in cleaned
    assert cleaned["message"] == "Broke."


def test_before_send_recursively_redacts_sensitive_fields_and_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bug_reports.os.path, "expanduser", lambda _value: r"C:\Users\Alice")
    event = {
        "message": r"Failure in C:\Users\Alice with token=visible",
        "extra": {
            "authorization": "Bearer visible",
            "nested": {"api_key": "visible", "safe": "kept"},
        },
    }

    cleaned = bug_reports._before_send(event, {})

    assert "visible" not in str(cleaned)
    assert r"C:\Users\Alice" not in str(cleaned)
    assert cleaned["extra"]["nested"]["safe"] == "kept"


def test_before_send_drops_implicit_pii_bodies_and_prompts_but_keeps_contact() -> None:
    event = {
        "user": {
            "email": "implicit@example.test",
            "ip_address": "203.0.113.9",
        },
        "request": {
            "url": "https://app.example.test/api",
            "headers": {"Authorization": "Bearer request-secret"},
            "cookies": "session=secret",
            "query_string": "paper=private",
            "data": {"document": "private"},
            "env": {"REMOTE_ADDR": "203.0.113.9"},
        },
        "extra": {
            "contact": "voluntary@example.test",
            "openai_api_key": "provider-secret",
            "session_id": "session-secret",
            "prompt": "private prompt",
            "chat_history": ["private message"],
        },
    }

    cleaned = bug_reports._before_send(event, {})

    assert "user" not in cleaned
    assert cleaned["request"]["url"] == "https://app.example.test/api"
    for field in ("headers", "cookies", "query_string", "data", "env"):
        assert cleaned["request"][field] == "[REDACTED]"
    assert cleaned["extra"]["contact"] == "voluntary@example.test"
    assert cleaned["extra"]["openai_api_key"] == "[REDACTED]"
    assert cleaned["extra"]["session_id"] == "[REDACTED]"
    assert cleaned["extra"]["prompt"] == "[CONTENT OMITTED]"
    assert cleaned["extra"]["chat_history"] == "[CONTENT OMITTED]"


def test_deliver_bug_report_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_ERROR_REPORTING_ENABLED", "false")

    with pytest.raises(ValueError, match="bug_report_not_configured"):
        asyncio.run(bug_reports.deliver_bug_report(description="A sufficiently detailed report."))


def test_deliver_bug_report_rejects_invalid_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_ERROR_REPORTING_ENABLED", "true")
    monkeypatch.setenv("BIBLIOBRAIN_BUGSINK_DSN", "https://bugs.example.test/no-public-key")

    with pytest.raises(ValueError, match="bug_report_invalid_dsn"):
        asyncio.run(bug_reports.deliver_bug_report(description="A sufficiently detailed report."))


@pytest.fixture
def bugsink(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., list[int]]]:
    """Point the real SDK at a local server; returns the statuses it has answered."""
    servers: list[tuple[http.server.HTTPServer, threading.Thread]] = []
    clients: list[Any] = []
    holds: list[threading.Event] = []

    def serve(*statuses: int, hold: threading.Event | None = None) -> list[int]:
        pending = list(statuses)
        answered: list[int] = []
        if hold is not None:
            holds.append(hold)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                # A slow server: nothing is answered until the test lets go.
                if hold is not None:
                    hold.wait(timeout=10.0)
                # One status per envelope, in order; the last one repeats.
                status = pending.pop(0) if len(pending) > 1 else pending[0]
                answered.append(status)
                self.send_response(status)
                if status == 429:
                    self.send_header("Retry-After", "1")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args: Any) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        servers.append((server, thread))
        clients.append(bug_reports.sentry_sdk.get_client())
        monkeypatch.setenv(
            "BIBLIOBRAIN_BUGSINK_DSN", f"http://public@127.0.0.1:{server.server_port}/1"
        )
        monkeypatch.setattr(bug_reports, "_SDK_INITIALIZED_DSN", "")
        return answered

    yield serve

    # Close the clients while their servers still answer: closing sends a last
    # client report, and one left hanging on a stopped server would keep the SDK
    # worker thread busy into the following tests. The same goes for a report
    # still held back by its server.
    for hold in holds:
        hold.set()
    clients.append(bug_reports.sentry_sdk.get_client())
    for client in clients:
        client.close(timeout=2.0)
    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _deliver() -> dict[str, str]:
    return asyncio.run(bug_reports.deliver_bug_report(description="Something broke."))


@pytest.mark.parametrize(
    ("status", "expected"),
    [(429, "bug_report_rate_limited"), (500, "bug_report_delivery_failed"), (200, None)],
)
def test_deliver_bug_report_reports_server_refusal(
    bugsink: Callable[..., list[int]], status: int, expected: str | None
) -> None:
    bugsink(status)

    if expected is None:
        assert _deliver()["remote_id"]
    else:
        with pytest.raises(RuntimeError, match=expected):
            _deliver()


def test_deliver_bug_report_ignores_a_refused_client_report(
    bugsink: Callable[..., list[int]],
) -> None:
    # After a lost event the SDK sends a client report of its own on each flush.
    # The server refusing that envelope says nothing about the report it accepted.
    answered = bugsink(500, 500, 200, 500)

    with pytest.raises(RuntimeError, match="bug_report_delivery_failed"):
        _deliver()
    assert _deliver()["remote_id"]
    assert answered == [500, 500, 200, 500]


def test_deliver_bug_report_ignores_late_drops_from_earlier_deliveries(
    bugsink: Callable[..., list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    bugsink(200)
    earlier = _deliver()["remote_id"]
    replaced = bug_reports.sentry_sdk.get_client().transport
    assert isinstance(replaced, bug_reports._RecordingTransport)
    bugsink(200)
    flush = bug_reports.sentry_sdk.flush

    def flush_after_late_drops(**kwargs: Any) -> None:
        # What requests still pending on the replaced transport report when they
        # end: a client report first, then the earlier bug report itself.
        replaced.on_dropped_event("network")
        with bug_reports._handling(bug_reports.Envelope(headers={"event_id": earlier})):
            replaced.on_dropped_event("network")
        flush(**kwargs)

    monkeypatch.setattr(bug_reports.sentry_sdk, "flush", flush_after_late_drops)

    assert _deliver()["remote_id"]


def test_deliver_bug_report_waits_past_the_flush_for_a_slow_refusal(
    bugsink: Callable[..., list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The server answers after a flush would have given up. No drop being recorded
    # by then used to be read as a delivery.
    monkeypatch.setattr(bug_reports, "_FLUSH_TIMEOUT_SECONDS", 0.05)
    hold = threading.Event()
    answered = bugsink(500, hold=hold)
    release = threading.Timer(0.3, hold.set)
    release.start()

    try:
        with pytest.raises(RuntimeError, match="bug_report_delivery_failed"):
            _deliver()
    finally:
        release.cancel()
    assert answered[0] == 500


def test_deliver_bug_report_does_not_confirm_a_report_still_in_flight(
    bugsink: Callable[..., list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bug_reports, "_DELIVERY_WAIT_SECONDS", 0.2)
    hold = threading.Event()
    answered = bugsink(200, hold=hold)

    result = _deliver()

    assert result["remote_id"]
    assert result["delivery"] == "unconfirmed"
    assert answered == []
    # Late, not lost: the request goes through once the server answers.
    hold.set()
    bug_reports.sentry_sdk.flush(timeout=2.0)
    assert answered == [200]


def test_deliver_bug_report_reports_an_envelope_the_sdk_could_not_send(
    bugsink: Callable[..., list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The SDK swallows what goes wrong while it prepares an envelope, and reports
    # no drop for it.
    answered = bugsink(200)

    def broken(_self: Any, _envelope: Any) -> None:
        raise ValueError("cannot serialize")

    monkeypatch.setattr(bug_reports.HttpTransport, "_prepare_envelope", broken)

    with pytest.raises(RuntimeError, match="bug_report_delivery_failed"):
        _deliver()
    assert answered == []
