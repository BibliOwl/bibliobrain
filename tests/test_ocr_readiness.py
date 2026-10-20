from collections import deque

import pytest

from src import document_ocr
from src import ocr_readiness as ready
from src.rag.ocr_ingestion import update_timing


@pytest.mark.parametrize(
    "ram,gpu,size,installed,expected",
    [
        (32, 12, 2, True, "recommended"),
        (4, 12, 2, True, "slow"),
        (32, 2, 2, True, "slow"),
        (32, 12, 8, True, "slow"),
        (32, None, 2, True, "unknown"),
        (None, 12, 2, True, "unknown"),
        (32, 12, None, True, "unknown"),
        (32, 12, 2, False, "unknown"),
    ],
)
def test_readiness_is_conservative_about_missing_or_insufficient_hardware(
    ram, gpu, size, installed, expected
):
    result = ready.assess(
        {
            "ram_bytes": ram * ready.GIB if ram else None,
            "gpu_bytes": gpu * ready.GIB if gpu else None,
        },
        {"installed": installed, "model_bytes": size * ready.GIB if size else None},
    )
    assert result["level"] == expected


def test_passive_check_only_reads_tags_and_caches_without_enabling_ocr(monkeypatch, tmp_path):
    from src import llm_provider

    monkeypatch.setattr(document_ocr, "root", lambda: tmp_path)
    monkeypatch.setattr(ready, "_cache", {})
    monkeypatch.setattr(llm_provider, "_ollama_base_url", lambda: "http://localhost:11434")
    calls = []

    def read_tags(base, path, **kwargs):
        calls.append(path)
        assert path == "/api/tags"
        return {"models": [{"name": "ovis", "size": 2 * ready.GIB}]}

    monkeypatch.setattr(llm_provider, "_read_ollama_json", read_tags)
    monkeypatch.setattr(
        ready,
        "hardware_snapshot",
        lambda: {"ram_bytes": 32 * ready.GIB, "gpu_bytes": 12 * ready.GIB},
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("No active OCR or model request in readiness")

    monkeypatch.setattr(document_ocr, "request_transcription", forbidden)
    monkeypatch.setattr(llm_provider, "_open_ollama_request", forbidden)
    assert ready.readiness("ovis")["level"] == "recommended"
    assert ready.readiness("ovis")["passive"]
    assert calls == ["/api/tags"]
    assert document_ocr.settings()["enabled"] is False
    assert not list(tmp_path.iterdir())
    assert ready.readiness("other")["installed"] is False
    assert len(calls) == 2


def test_offline_ollama_remains_unknown(monkeypatch):
    from src import llm_provider

    monkeypatch.setattr(llm_provider, "_ollama_base_url", lambda: "http://localhost:11434")

    def offline(*a, **kw):
        raise OSError("offline")

    monkeypatch.setattr(llm_provider, "_read_ollama_json", offline)
    assert ready.installed_model("ovis")["installed"] is None
    assert ready.installed_model("ovis")["reason"] == "ollama_unavailable"


def test_scans_are_read_on_this_computer_unless_expert_sends_them_elsewhere(monkeypatch):
    from src import llm_provider

    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(ready, "_cache", {})
    # The chat uses Ollama on another computer.
    monkeypatch.setattr(llm_provider, "_ollama_base_url", lambda: "http://192.168.1.20:11434")
    asked = []

    def read_tags(base, path, **kwargs):
        asked.append(base)
        return {"models": [{"name": "ovis", "size": 2 * ready.GIB}]}

    monkeypatch.setattr(llm_provider, "_read_ollama_json", read_tags)
    monkeypatch.setattr(
        ready,
        "hardware_snapshot",
        lambda: {"ram_bytes": 32 * ready.GIB, "gpu_bytes": 12 * ready.GIB},
    )

    monkeypatch.setattr(llm_provider, "_runtime_document_tools_source", "local")
    local = ready.readiness("ovis")
    assert asked == ["http://localhost:11434"]
    assert local["level"] == "recommended"
    assert "remote" not in local

    monkeypatch.setattr(llm_provider, "_runtime_document_tools_source", "chat")
    remote = ready.readiness("ovis")
    assert asked[-1] == "http://192.168.1.20:11434"
    assert remote["installed"] is True
    # This computer's memory says nothing about the other one.
    assert remote["remote"] is True
    assert remote["level"] == "unknown"
    assert remote.get("ram_bytes") is None


def test_eta_ignores_warmup_cache_and_blank_pages_but_includes_real_processing():
    report = {"timed_pages": 0, "candidates": 100, "completed": 0}
    samples: deque[float] = deque(maxlen=20)
    fresh = {"cache_hit": False, "attempts": [{"issue": ""}]}
    for duration in (60, 6, 8):
        report["completed"] += 1
        update_timing(report, samples, fresh, duration)
    assert report["estimated_remaining_seconds"] is None
    for result in ({"cache_hit": True, "attempts": [{}]}, {"cache_hit": False, "attempts": []}):
        report["completed"] += 1
        update_timing(report, samples, result, 0.01)
    report["completed"] += 1
    update_timing(report, samples, fresh, 10)
    assert report["seconds_per_page"] == 8
    assert report["eta_sample_pages"] == 3
    assert report["estimated_remaining_seconds"] == 94 * 8
    # A repeated/failed inference consumed real time and must affect the estimate.
    report["completed"] += 1
    update_timing(report, samples, {"cache_hit": False, "attempts": [{}, {}]}, 40)
    assert report["seconds_per_page"] == 16
    assert report["estimated_remaining_seconds"] == 93 * 16


def test_eta_adapts_to_recent_pages_and_starts_fresh_on_resume():
    report = {"timed_pages": 0, "candidates": 100, "completed": 0}
    samples: deque[float] = deque(maxlen=20)
    for duration in [100, 90, 90, 90] + [5] * 20:
        report["completed"] += 1
        update_timing(report, samples, {"cache_hit": False, "attempts": [{}]}, duration)
    assert report["seconds_per_page"] == 5
    assert report["eta_sample_pages"] == 20
    resumed = {"timed_pages": 0, "candidates": 100, "completed": 24}
    update_timing(resumed, deque(maxlen=20), {"cache_hit": True, "attempts": [{}]}, 0.001)
    assert resumed["estimated_remaining_seconds"] is None
