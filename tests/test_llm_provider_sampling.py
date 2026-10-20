from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from src import llm_provider


class _Payload(BaseModel):
    value: str


@pytest.mark.parametrize("provider", ["remote", "lmstudio"])
def test_chat_planner_timeout_does_not_change_normal_generation_defaults(monkeypatch, provider):
    captured: list[dict] = []

    def fake_chat_model(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "langchain_openai", SimpleNamespace(ChatOpenAI=fake_chat_model))
    monkeypatch.setattr(llm_provider, "is_remote_provider", lambda: provider == "remote")
    monkeypatch.setattr(llm_provider, "is_lmstudio_provider", lambda: provider == "lmstudio")
    monkeypatch.setattr(llm_provider, "_validate_remote_config", lambda: (True, ""))
    monkeypatch.setattr(llm_provider, "_validate_lmstudio_config", lambda: (True, ""))
    monkeypatch.setattr(llm_provider, "_remote_timeout", lambda: 120)
    monkeypatch.setattr(llm_provider, "_remote_api_key", lambda: "test-key")
    monkeypatch.setattr(llm_provider, "_remote_base_url", lambda: "http://localhost:1234/v1")
    monkeypatch.setattr(llm_provider, "_lmstudio_api_base_url", lambda: "http://localhost:1234/v1")
    monkeypatch.setattr(llm_provider, "get_secure_http_client", lambda: None)

    llm_provider.create_chat_llm(
        "demo-model", max_output_tokens=16, request_timeout_seconds=5.0, max_retries=0,
    )
    llm_provider.create_chat_llm("demo-model")

    assert captured[0]["timeout"] == 5.0
    assert captured[0]["max_retries"] == 0
    assert captured[0]["max_tokens"] == 16
    assert captured[1]["timeout"] == 120
    assert "max_retries" not in captured[1]
    assert "max_tokens" not in captured[1]


@pytest.mark.parametrize(
    ("model", "thinking", "expected"),
    [
        # Ollama thinks whenever the field is absent; granite4.2 then writes
        # its reasoning into the answer, so "off" must be sent explicitly.
        ("granite4.2:3b", False, False),
        ("granite4.2:8b", True, None),
        ("qwen3.5:9b", True, False),
    ],
)
def test_ollama_text_generation_sets_reasoning_explicitly(monkeypatch, model, thinking, expected):
    captured: list[dict] = []

    def fake_ollama_llm(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setitem(sys.modules, "langchain_ollama", SimpleNamespace(OllamaLLM=fake_ollama_llm))
    monkeypatch.setattr(llm_provider, "is_remote_provider", lambda: False)
    monkeypatch.setattr(llm_provider, "is_lmstudio_provider", lambda: False)
    monkeypatch.setattr(llm_provider, "resolve_ollama_context_length", lambda *_a, **_kw: 0)

    llm_provider.create_chat_llm(model, thinking_enabled=thinking)
    llm_provider.create_chat_llm(model, json_mode=True)

    assert captured[0].get("reasoning") is expected
    assert captured[1]["reasoning"] is False
    assert captured[1]["format"] == "json"


def test_openai_compatible_sampling_uses_top_p_one_for_greedy_temperature() -> None:
    settings = {
        "temperature": 0.0,
        "repeat_penalty": 1.2,
        "top_p": 0.2,
    }

    sanitized = llm_provider._openai_compatible_sampling_settings(settings)

    assert sanitized["temperature"] == 0.0
    assert sanitized["top_p"] == 1.0
    assert settings["top_p"] == 0.2


def test_openai_compatible_sampling_preserves_top_p_for_non_greedy_temperature() -> None:
    settings = {
        "temperature": 0.1,
        "repeat_penalty": 1.2,
        "top_p": 0.2,
    }

    sanitized = llm_provider._openai_compatible_sampling_settings(settings)

    assert sanitized["temperature"] == 0.1
    assert sanitized["top_p"] == 0.2


def test_ollama_generation_options_include_configured_context() -> None:
    previous = llm_provider.get_ollama_context_length()
    try:
        llm_provider.set_ollama_context_length(16384, persist=False)
        options = llm_provider._build_ollama_generation_options()
        assert options["num_ctx"] == 16384

        llm_provider.set_ollama_context_length(0, persist=False)
        assert "num_ctx" not in llm_provider._build_ollama_generation_options()
    finally:
        llm_provider.set_ollama_context_length(previous, persist=False)


def test_structured_remote_kwargs_sanitize_greedy_top_p(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: False)

    kwargs = llm_provider._structured_request_kwargs(
        model_name="mistralai-medium",
        messages=[{"role": "user", "content": "Return JSON."}],
        response_model=_Payload,
        settings={
            "temperature": 0.0,
            "repeat_penalty": 1.2,
            "top_p": 0.2,
        },
    )

    assert kwargs["temperature"] == 0.0
    assert kwargs["top_p"] == 1.0


def test_structured_ollama_kwargs_keep_requested_top_p(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: True)

    kwargs = llm_provider._structured_request_kwargs(
        model_name="local-model",
        messages=[{"role": "user", "content": "Return JSON."}],
        response_model=_Payload,
        settings={
            "temperature": 0.0,
            "repeat_penalty": 1.2,
            "top_p": 0.2,
        },
    )

    assert kwargs["temperature"] == 0.0
    assert kwargs["top_p"] == 0.2
    assert kwargs["extra_body"]["think"] is False
    assert kwargs["extra_body"]["options"]["repeat_penalty"] == 1.2


def test_structured_qwen35_ollama_kwargs_disable_thinking(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: True)

    kwargs = llm_provider._structured_request_kwargs(
        model_name="qwen3.5:9b",
        messages=[{"role": "user", "content": "Return JSON."}],
        response_model=_Payload,
        settings={
            "temperature": 0.0,
            "repeat_penalty": 1.2,
            "top_p": 0.2,
        },
    )

    assert kwargs["messages"][-1]["content"].endswith("/no_think")
    assert kwargs["extra_body"]["think"] is False


def test_resolve_ollama_context_length_prefers_the_configured_value(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "get_ollama_context_length", lambda: 16384)
    monkeypatch.setattr(llm_provider, "_vram_context_cap", lambda: 8192)

    # An explicit Settings value is the user's call and is never trimmed.
    assert llm_provider.resolve_ollama_context_length("m", required_tokens=100) == 16384


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        # The interface saved its 1.2 default on every settings sync.
        ({"repeat_penalty": 1.2}, 1.0),
        ({"repeat_penalty": 1.3}, 1.3),
        # Saved after the new default: the user's own choice.
        ({"repeat_penalty": 1.2, "generation_defaults_version": 2}, 1.2),
        ({}, 1.0),
    ],
)
def test_the_old_repeat_penalty_default_reads_as_the_new_one(monkeypatch, stored, expected) -> None:
    monkeypatch.setattr(llm_provider, "_runtime_generation_settings", None)
    monkeypatch.setattr(llm_provider, "_load_provider_state", lambda: dict(stored))

    assert llm_provider.get_generation_settings()["repeat_penalty"] == expected


def test_saving_generation_settings_marks_them_as_chosen(monkeypatch) -> None:
    saved: dict = {}
    monkeypatch.setattr(llm_provider, "_runtime_generation_settings", None)
    monkeypatch.setattr(llm_provider, "_load_provider_state", lambda: {})
    monkeypatch.setattr(llm_provider, "_save_provider_state", saved.update)

    llm_provider.set_generation_settings(repeat_penalty=1.2)

    assert saved["repeat_penalty"] == 1.2
    assert saved["generation_defaults_version"] == 2


# A chat model's pinned window is tested in test_ollama_residency.py; these
# cover a model without one.
def test_resolve_ollama_context_length_keeps_auto_when_nothing_is_needed(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "get_ollama_context_length", lambda: 0)
    monkeypatch.setattr(llm_provider, "pinned_context_length", lambda _m=None: 0)

    assert llm_provider.resolve_ollama_context_length("m") == 0


def test_resolve_ollama_context_length_grows_to_fit_the_prompt(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "get_ollama_context_length", lambda: 0)
    monkeypatch.setattr(llm_provider, "pinned_context_length", lambda _m=None: 0)
    monkeypatch.setattr(llm_provider, "_vram_context_cap", lambda: 16384)
    monkeypatch.setattr(llm_provider, "_model_advertised_context_length", lambda _m: 131072)

    assert llm_provider.resolve_ollama_context_length("m", required_tokens=1000) == 4096
    assert llm_provider.resolve_ollama_context_length("m", required_tokens=5289) == 8192


def test_resolve_ollama_context_length_clamps_to_vram_and_model(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "get_ollama_context_length", lambda: 0)
    monkeypatch.setattr(llm_provider, "pinned_context_length", lambda _m=None: 0)
    monkeypatch.setattr(llm_provider, "_vram_context_cap", lambda: 8192)
    monkeypatch.setattr(llm_provider, "_model_advertised_context_length", lambda _m: 131072)
    assert llm_provider.resolve_ollama_context_length("m", required_tokens=40000) == 8192

    monkeypatch.setattr(llm_provider, "_vram_context_cap", lambda: 16384)
    monkeypatch.setattr(llm_provider, "_model_advertised_context_length", lambda _m: 8192)
    assert llm_provider.resolve_ollama_context_length("m", required_tokens=40000) == 8192


def test_vram_context_cap_scales_down_on_small_gpus(monkeypatch) -> None:
    for vram_gib, expected in ((0, 16384), (4, 4096), (7, 8192), (10, 12288), (24, 16384)):
        monkeypatch.setattr(
            llm_provider,
            "_detected_gpu_vram_bytes",
            lambda gib=vram_gib: int(gib * 1024**3),
        )
        assert llm_provider._vram_context_cap() == expected


def test_humanize_llm_error_explains_a_context_overflow() -> None:
    raw = (
        "Error code: 400 - {'error': {'message': '{\"error\":{\"code\":400,"
        "\"message\":\"request (5289 tokens) exceeds the available context size "
        "(4096 tokens), try increasing it\",\"type\":\"exceed_context_size_error\"}}'}}"
    )

    humanized = llm_provider._humanize_llm_error(raw)

    assert humanized.startswith("Contexte trop court")
    assert "5289" in humanized
    assert "4096" in humanized
    assert "Fenetre de contexte Ollama" in humanized


def test_humanize_llm_error_leaves_other_messages_alone() -> None:
    assert llm_provider._humanize_llm_error("connection refused") == "connection refused"


def test_invoke_structured_chat_uses_the_native_endpoint_for_ollama(monkeypatch) -> None:
    captured: dict = {}

    def _fake_native(**kwargs):
        captured.update(kwargs)
        return _Payload(value="ok")

    def _explode():
        raise AssertionError("Ollama must not go through the /v1 instructor client")

    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(llm_provider, "_invoke_native_structured", _fake_native)
    monkeypatch.setattr(llm_provider, "_get_cached_instructor_client", _explode)
    monkeypatch.setattr(
        llm_provider,
        "resolve_ollama_context_length",
        lambda *_a, **_kw: 8192,
    )

    result = llm_provider.invoke_structured_chat(
        "local-model",
        [{"role": "user", "content": "Return JSON."}],
        response_model=_Payload,
        context_tokens_hint=5289,
    )

    assert isinstance(result, _Payload)
    assert result.value == "ok"
    assert captured["context_length"] == 8192
    assert captured["model_name"] == "local-model"
