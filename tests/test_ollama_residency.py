from __future__ import annotations

import json
from typing import Any

import pytest

from src import llm_provider, ollama_residency
from src.api import streams

_GIB = 1024**3


@pytest.fixture
def gpu(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A 12 GiB NVIDIA GPU, Auto context, no measured model yet."""
    machine: dict[str, Any] = {"vram": 12 * _GIB, "saved": {}}
    monkeypatch.setattr(ollama_residency, "_calibrations", {})
    monkeypatch.setattr(llm_provider, "get_ollama_context_length", lambda: 0)
    monkeypatch.setattr(llm_provider, "_vram_context_cap", lambda: 16384)
    monkeypatch.setattr(llm_provider, "_detected_gpu_vram_bytes", lambda: machine["vram"])
    monkeypatch.setattr(llm_provider, "_model_advertised_context_length", lambda _m=None: 262144)
    monkeypatch.setattr(llm_provider, "_ollama_base_url", lambda: "http://localhost:11434")
    monkeypatch.setattr(
        llm_provider, "_save_provider_state", lambda payload: machine["saved"].update(payload)
    )
    monkeypatch.setattr(
        ollama_residency, "_reserved_bytes", lambda: ollama_residency._RESERVE_BYTES
    )
    return machine


def _calibrations() -> dict[str, dict[str, Any]]:
    calibrations = ollama_residency._calibrations
    assert calibrations is not None
    return calibrations


def _payload(body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return json.loads(llm_provider._ollama_request_payload(body, **kwargs))


def test_a_planner_call_without_window_gets_the_chat_window(gpu: dict[str, Any]) -> None:
    _calibrations()["qwen3.5:4b"] = {"num_ctx": 98304}

    planner = _payload(
        {"model": "qwen3.5:4b", "prompt": "Which tool?", "options": {"num_predict": 16}}
    )
    answer = _payload(
        {"model": "qwen3.5:4b", "messages": [{"role": "user", "content": "q"}]},
        options_override={"num_ctx": 8192},
    )

    assert planner["options"] == {"num_predict": 16, "num_ctx": 98304}
    assert answer["options"]["num_ctx"] == 98304
    assert planner["keep_alive"] == answer["keep_alive"] == 1800


def test_a_prompt_longer_than_the_window_still_gets_room(gpu: dict[str, Any]) -> None:
    _calibrations()["qwen3.5:4b"] = {"num_ctx": 8192}

    body = _payload({"model": "qwen3.5:4b", "prompt": "long"}, options_override={"num_ctx": 16384})

    assert body["options"]["num_ctx"] == 16384


def test_an_unmeasured_chat_model_uses_the_vram_tier(gpu: dict[str, Any]) -> None:
    body = _payload({"model": "gemma4:e2b", "prompt": "Titre ?"})

    assert body["options"]["num_ctx"] == 16384


def test_unloads_lookups_and_embedding_models_are_left_alone(gpu: dict[str, Any]) -> None:
    unload = _payload({"model": "qwen3.5:4b", "prompt": "", "keep_alive": 0})
    show = _payload({"model": "qwen3.5:4b"})
    embedding = _payload({"model": "embeddinggemma", "prompt": "x"})

    assert unload == {"model": "qwen3.5:4b", "prompt": "", "keep_alive": 0}
    assert show == {"model": "qwen3.5:4b"}
    assert "options" not in embedding and "keep_alive" not in embedding


def test_an_explicit_window_in_the_settings_wins(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _calibrations()["qwen3.5:4b"] = {"num_ctx": 98304}
    monkeypatch.setattr(llm_provider, "get_ollama_context_length", lambda: 32768)

    assert ollama_residency.pinned_context_length("qwen3.5:4b") == 0
    assert llm_provider.resolve_ollama_context_length("qwen3.5:4b", required_tokens=100) == 32768


def test_resolved_window_is_the_pinned_one_unless_the_prompt_needs_more(
    gpu: dict[str, Any],
) -> None:
    _calibrations()["qwen3.5:4b"] = {"num_ctx": 24576}

    assert llm_provider.resolve_ollama_context_length("qwen3.5:4b") == 24576
    assert llm_provider.resolve_ollama_context_length("qwen3.5:4b", required_tokens=3000) == 24576


def test_keep_alive_setting_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE_SECONDS", "5")
    assert ollama_residency.keep_alive_seconds() == 60
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE_SECONDS", "not a number")
    assert ollama_residency.keep_alive_seconds() == 1800


def _footprint(context_length: int, *, per_token: int = 35_000, on_gpu: bool = True):
    size = 3_000_000_000 + per_token * context_length
    return ollama_residency._Footprint(
        context_length=context_length,
        size=size,
        size_vram=size if on_gpu else size // 2,
        digest="abc",
    )


def test_the_window_is_the_largest_that_fits_the_budget() -> None:
    chosen = ollama_residency._fitting_context_length(
        _footprint(8192),
        _footprint(16384),
        budget=3_000_000_000 + 35_000 * 100_000,
        ceiling=262144,
    )

    assert chosen == 98304  # 100 000 rounded down to the 4096 step
    assert (
        ollama_residency._fitting_context_length(
            _footprint(8192), _footprint(16384), budget=10**15, ceiling=131072
        )
        == 131072
    )


def test_calibration_measures_twice_checks_the_fit_and_remembers(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    loads: list[int] = []

    def load_at(_model: str, context_length: int):
        loads.append(context_length)
        return _footprint(context_length)

    monkeypatch.setattr(ollama_residency, "_load_at", load_at)

    chosen = ollama_residency.calibrate("qwen3.5:4b")

    budget = 12 * _GIB - ollama_residency._RESERVE_BYTES
    assert chosen == ollama_residency._round_down((budget - 3_000_000_000) / 35_000)
    assert loads == [8192, 16384, chosen]
    assert ollama_residency.pinned_context_length("qwen3.5:4b") == chosen
    saved = gpu["saved"]["chat_context_calibration"]["qwen3.5:4b"]
    assert saved["num_ctx"] == chosen and saved["vram_total"] == 12 * _GIB


def test_calibration_steps_down_when_the_model_spills_to_cpu(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    loads: list[int] = []

    def load_at(_model: str, context_length: int):
        loads.append(context_length)
        return _footprint(context_length, on_gpu=context_length <= 40960)

    monkeypatch.setattr(ollama_residency, "_load_at", load_at)

    chosen = ollama_residency.calibrate("qwen3.5:4b")

    # 233 472 estimated, then bisected: 122 880 and 69 632 spill, 40 960 fits.
    assert chosen == 40960
    assert loads[-1] == chosen and len(loads) == 6


def test_a_model_too_big_for_the_gpu_keeps_the_floor(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ollama_residency,
        "_load_at",
        lambda _model, context_length: _footprint(context_length, on_gpu=False),
    )

    assert ollama_residency.calibrate("gemma4:26b") == ollama_residency.CONTEXT_FLOOR


def test_without_a_vram_figure_the_provisional_window_stays_unsaved(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gpu["vram"] = 0
    loads: list[int] = []

    def load_at(_model: str, context_length: int) -> Any:
        loads.append(context_length)
        return _footprint(context_length)

    monkeypatch.setattr(ollama_residency, "_load_at", load_at)

    assert ollama_residency.calibrate("qwen3.5:4b") == 16384
    assert loads == [16384]
    assert gpu["saved"] == {}


def test_a_measure_disturbed_by_a_chat_request_is_not_kept(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A question sent while measuring reloads the model at its own window,
    # so /api/ps shows another window than the one just requested.
    loads: list[int] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def read(self) -> bytes:
            return b"{}"

    def open_url(_url: str, *, json_payload: dict[str, Any], **_kwargs: Any) -> _Response:
        loads.append(json_payload["options"]["num_ctx"])
        return _Response()

    shown = {"models": [{"name": "qwen3.5:4b", "context_length": 16384, "size": 1, "size_vram": 1}]}
    monkeypatch.setattr(llm_provider, "_open_ollama_url", open_url)
    monkeypatch.setattr(llm_provider, "_read_ollama_json", lambda *_a, **_k: shown)

    assert ollama_residency._load_at("qwen3.5:4b", 8192) is None
    assert loads == [8192, 8192]  # tried twice
    assert ollama_residency.calibrate("qwen3.5:4b") == 16384  # provisional
    assert gpu["saved"] == {}


def test_a_question_asked_while_measuring_waits_for_the_window(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    measuring = threading.Event()
    release = threading.Event()

    def slow_load(_model: str, context_length: int):
        measuring.set()
        release.wait(timeout=5)
        return _footprint(context_length)

    monkeypatch.setattr(ollama_residency, "_load_at", slow_load)
    calibration = threading.Thread(target=ollama_residency.calibrate, args=("qwen3.5:4b",))
    calibration.start()
    assert measuring.wait(timeout=5)

    seen: list[int] = []
    question = threading.Thread(
        target=lambda: seen.append(ollama_residency.pinned_context_length("qwen3.5:4b"))
    )
    question.start()
    question.join(timeout=0.2)
    assert seen == []  # still waiting for the measure

    release.set()
    calibration.join(timeout=5)
    question.join(timeout=5)
    assert seen == [ollama_residency.calibrated_context_length("qwen3.5:4b")]
    assert seen[0] > 16384


def test_warm_up_reuses_a_valid_measure(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _calibrations()["qwen3.5:4b"] = {
        "num_ctx": 65536,
        "digest": "abc",
        "vram_total": 12 * _GIB,
    }
    loads: list[int] = []
    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(ollama_residency, "_warm_embedding_model", lambda: None)

    def load_at(_model: str, context_length: int) -> Any:
        loads.append(context_length)
        return _footprint(context_length)

    monkeypatch.setattr(ollama_residency, "_load_at", load_at)

    result = ollama_residency.warm_up("qwen3.5:4b")

    assert result["num_ctx"] == 65536 and result["calibrated"] is False
    assert loads == [65536]


def test_warm_up_measures_again_after_a_new_pull(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _calibrations()["qwen3.5:4b"] = {
        "num_ctx": 65536,
        "digest": "old",
        "vram_total": 12 * _GIB,
    }
    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(ollama_residency, "_warm_embedding_model", lambda: None)
    monkeypatch.setattr(ollama_residency, "_load_at", lambda _m, n: _footprint(n))

    assert ollama_residency.warm_up("qwen3.5:4b")["calibrated"] is True


def test_warm_up_skips_embedding_models_and_other_providers(
    gpu: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: True)
    assert ollama_residency.warm_up("embeddinggemma")["ok"] is False
    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: False)
    assert ollama_residency.warm_up("qwen3.5:4b")["ok"] is False


def test_a_chat_stop_keeps_the_model_loaded() -> None:
    assert streams.stop_needs_unload("user_stop") is False
    assert streams.stop_needs_unload("client_disconnect") is False
    # Leaving the page, and drafting or notebook stops, still free the model.
    assert streams.stop_needs_unload("page_exit") is True
    assert streams.stop_needs_unload("drafting_stop") is True
    assert streams.stop_needs_unload("notebook_stop") is True


def test_ollama_timings_reads_the_last_message() -> None:
    timings = llm_provider.ollama_timings(
        {
            "done": True,
            "load_duration": 2_980_000_000,
            "prompt_eval_count": 2305,
            "prompt_eval_duration": 389_000_000,
            "eval_count": 300,
            "eval_duration": 2_700_000_000,
        }
    )

    assert timings == {
        "load_ms": 2980.0,
        "reloaded": True,
        "prompt_tokens": 2305,
        "prefill_ms": 389.0,
        "output_tokens": 300,
        "decode_ms": 2700.0,
        "tokens_per_second": 111.1,
    }
    assert llm_provider.ollama_timings({"message": {"content": "partial"}}) == {}
