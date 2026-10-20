"""Remote calls stream so an idle-cutting gateway cannot drop them.

The fake server mimics the remote gateway seen in a user report: a request
that sends nothing back for longer than ``IDLE_LIMIT`` is closed without a
response ("Server disconnected without sending a response"). A streamed
answer keeps bytes flowing and survives the same total generation time.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import BaseModel

from src import llm_provider

IDLE_LIMIT = 0.5
GENERATION_SECONDS = 1.2


class _Verdict(BaseModel):
    status: str
    score: int


class _Gateway:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.requests: list[dict] = []
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                gateway.requests.append(body)
                answer = gateway.answers.pop(0) if len(gateway.answers) > 1 else gateway.answers[0]
                if not body.get("stream"):
                    # Silent while "generating": the gateway gives up.
                    time.sleep(GENERATION_SECONDS)
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                pieces = [answer[i : i + 4] for i in range(0, len(answer), 4)]
                delay = GENERATION_SECONDS / max(1, len(pieces))
                for index, piece in enumerate(pieces):
                    time.sleep(min(delay, IDLE_LIMIT / 2))
                    chunk = {
                        "id": "c1", "object": "chat.completion.chunk", "created": 0,
                        "model": body["model"],
                        "choices": [{
                            "index": 0, "delta": {"content": piece},
                            "finish_reason": "stop" if index == len(pieces) - 1 else None,
                        }],
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def __enter__(self) -> _Gateway:
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch):
    def configure(gateway: _Gateway, *, streaming: bool = True) -> None:
        monkeypatch.setenv("REMOTE_STREAMING", "true" if streaming else "false")
        monkeypatch.setattr(llm_provider, "is_remote_provider", lambda: True)
        monkeypatch.setattr(llm_provider, "is_lmstudio_provider", lambda: False)
        monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: False)
        monkeypatch.setattr(llm_provider, "_validate_remote_config", lambda: (True, ""))
        monkeypatch.setattr(llm_provider, "_remote_base_url", lambda: gateway.url)
        monkeypatch.setattr(llm_provider, "_remote_api_key", lambda: "test-key")
        monkeypatch.setattr(llm_provider, "_remote_timeout", lambda: 10)
        monkeypatch.setattr(llm_provider, "get_secure_http_client", lambda: None)
        monkeypatch.setattr(llm_provider, "needs_two_step_json", lambda _name: False)
        monkeypatch.setattr(llm_provider, "_instructor_client_cache", None)

    return configure


def _structured(**kwargs):
    return llm_provider.invoke_structured_chat(
        "demo-model", [{"role": "user", "content": "judge"}], response_model=_Verdict, **kwargs,
    )


def test_non_streamed_call_is_cut_like_the_reported_gateway(remote) -> None:
    with _Gateway(['{"status": "ok", "score": 3}']) as gateway:
        remote(gateway, streaming=False)
        with pytest.raises(RuntimeError, match="Server disconnected without sending a response"):
            llm_provider.invoke_structured_chat(
                "demo-model", [{"role": "user", "content": "judge"}], response_model=_Verdict,
            )


def test_streamed_structured_call_survives_the_gateway(remote) -> None:
    with _Gateway(['{"status": "ok", "score": 3}']) as gateway:
        remote(gateway)
        result = _structured()

    assert isinstance(result, _Verdict)
    assert result.model_dump() == {"status": "ok", "score": 3}
    request = gateway.requests[-1]
    assert request["stream"] is True
    # Same request Instructor builds in JSON mode.
    assert request["response_format"] == {"type": "json_object"}
    assert "score" in request["messages"][0]["content"]


def test_streamed_structured_call_reasks_after_invalid_json(remote) -> None:
    with _Gateway(['{"status": "ok"}', '{"status": "ok", "score": 5}']) as gateway:
        remote(gateway)
        result = _structured()

    assert isinstance(result, _Verdict)
    assert result.model_dump() == {"status": "ok", "score": 5}
    assert len(gateway.requests) == 2
    reask = gateway.requests[1]["messages"]
    assert reask[-2] == {"role": "assistant", "content": '{"status": "ok"}'}
    assert "score" in reask[-1]["content"]


def test_streamed_text_call_survives_the_gateway(remote) -> None:
    with _Gateway(["A long free-text validation answer."]) as gateway:
        remote(gateway)
        llm = llm_provider.create_chat_llm("demo-model", max_retries=0)
        text = llm_provider.invoke_text(llm, "judge")

    assert text == "A long free-text validation answer."
    assert gateway.requests[-1]["stream"] is True
