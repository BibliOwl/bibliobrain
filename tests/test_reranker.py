"""Tests for the cross-encoder reranker module.

The reranker has a soft dependency on ``sentence-transformers``. We test:
  - graceful fallback when the model is missing
  - reranking actually changes order when the model is stubbed
  - integration with _retrieve_documents via state stubbing
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from langchain_core.documents import Document


@pytest.fixture(autouse=True)
def _reset_reranker_state():
    """Reset the module-level singleton so each test loads fresh."""
    from src.rag import reranker as rr

    rr._model_state["model"] = None
    rr._model_state["name"] = ""
    rr._model_state["device"] = ""
    rr._model_state["profile"] = ""
    rr._model_state["load_error"] = ""
    rr._model_state["predict_error"] = ""
    rr._model_state["tried"] = False
    rr._alt_models.clear()
    rr._translation_state.update({"model": None, "tokenizer": None, "load_error": "", "tried": False})
    rr._slow_gpu_state["reason"] = ""
    rr._gpu_probe.clear()
    yield
    rr._slow_gpu_state["reason"] = ""
    rr._gpu_probe.clear()
    rr._model_state["model"] = None
    rr._model_state["name"] = ""
    rr._model_state["device"] = ""
    rr._model_state["profile"] = ""
    rr._model_state["load_error"] = ""
    rr._model_state["predict_error"] = ""
    rr._model_state["tried"] = False
    rr._alt_models.clear()
    rr._translation_state.update({"model": None, "tokenizer": None, "load_error": "", "tried": False})


def _doc(text: str, **meta: Any) -> Document:
    return Document(page_content=text, metadata=meta)


@pytest.mark.parametrize("kind", ["documents", "texts", "scored"])
def test_busy_reranker_falls_back_without_poisoning_model(
    monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    from src.rag import reranker as rr

    class Model:
        device = "cuda:0"
        dtype = "torch.bfloat16"

        def predict(self, pairs):
            return [float(len(text)) for _, text in pairs]

    model = Model()
    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", lambda: model)
    monkeypatch.setattr(rr, "_PREDICT_QUEUE_TIMEOUT_SECONDS", 0.01)
    docs = [_doc("a"), _doc("longer")]

    def call():
        if kind == "documents":
            return rr.rerank_documents("query", docs, top_k=1)
        if kind == "texts":
            return rr.rerank_texts("query", ["a", "longer"], top_k=1)
        return rr.rerank_scored_docs("query", [(docs[0], 0.1), (docs[1], 0.2)], top_k=1)

    fallback: list[Any]
    if kind == "documents":
        fallback = docs[:1]
    elif kind == "texts":
        fallback = [(0, 0.0)]
    else:
        fallback = [(docs[0], 0.1)]
    with rr._predict_lock:
        assert call() == fallback
    metrics = rr.consume_last_metrics()
    assert metrics is not None
    assert metrics["skipped_reason"] == "reranker_busy"
    assert metrics["device"] == "cuda:0"
    assert metrics["dtype"] == "torch.bfloat16"
    assert metrics["queue_duration_ms"] >= 0
    assert not rr.has_reranker_predict_error()
    assert call() != fallback
    metrics = rr.consume_last_metrics()
    assert metrics is not None
    assert metrics["applied"]
    assert metrics["predict_duration_ms"] >= 0
    assert metrics["queue_duration_ms"] >= 0
    assert metrics["max_candidate_chars"] == len("longer")


def test_rerank_disabled_returns_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: False)

    docs = [_doc("a"), _doc("b"), _doc("c")]
    result = rr.rerank_documents("query", docs, top_k=2)
    assert result == docs[:2]


def test_rerank_enabled_with_missing_lib_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """When sentence-transformers is unavailable, return docs unchanged."""
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    # Simulate failed load (no real lib needed in test env).
    monkeypatch.setattr(rr, "_load_model", lambda: None)

    docs = [_doc("first"), _doc("second"), _doc("third")]
    result = rr.rerank_documents("query", docs, top_k=2)
    assert result == docs[:2]


def test_rerank_reorders_by_stubbed_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a stub model, doc with highest predicted score must come first."""
    from src.rag import reranker as rr

    class _StubModel:
        def predict(self, pairs):
            # Score = length of the candidate text. Longest wins.
            return [float(len(text)) for _, text in pairs]

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", lambda: _StubModel())

    docs = [_doc("xx"), _doc("xxxxxxxxxx"), _doc("xxxxx")]
    result = rr.rerank_documents("query", docs, top_k=3)
    assert [d.page_content for d in result] == ["xxxxxxxxxx", "xxxxx", "xx"]


def test_reranker_serializes_concurrent_model_predictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import reranker as rr

    class _ConcurrentProbeModel:
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0
            self.lock = threading.Lock()

        def predict(self, pairs):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            time.sleep(0.02)
            with self.lock:
                self.active -= 1
            return [float(len(text)) for _, text in pairs]

    model = _ConcurrentProbeModel()
    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", lambda: model)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(
            executor.map(
                lambda _: rr.rerank_documents("query", [_doc("a"), _doc("bb")]),
                range(8),
            )
        )

    assert model.max_active == 1


def test_rerank_texts_reorders_by_stubbed_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    class _StubModel:
        def predict(self, pairs):
            return [float(len(text)) for _, text in pairs]

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", lambda: _StubModel())

    result = rr.rerank_texts("query", ["short", "much longer text", "mid"], top_k=2)

    assert result == [(1, float(len("much longer text"))), (0, float(len("short")))]
    metrics = rr.consume_last_metrics()
    assert metrics is not None
    assert metrics["applied"] is True
    assert metrics["input_type"] == "text"


def test_rerank_texts_disabled_returns_index_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: False)

    assert rr.rerank_texts("query", ["a", "b", "c"], top_k=2) == [(0, 0.0), (1, 0.0)]


def test_rerank_handles_predict_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A misbehaving model must not crash retrieval."""
    from src.rag import reranker as rr

    class _BoomModel:
        def predict(self, pairs):
            raise RuntimeError("boom")

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", lambda: _BoomModel())

    docs = [_doc("a"), _doc("b")]
    result = rr.rerank_documents("query", docs, top_k=2)
    assert result == docs
    metrics = rr.consume_last_metrics()
    assert metrics is not None
    assert metrics["predict_error"] == "boom"
    assert rr.has_reranker_predict_error() is True
    assert rr._model_state["model"] is None


def test_rerank_skips_predict_after_previous_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    class _ShouldNotPredict:
        def predict(self, pairs):
            raise AssertionError("predict should be skipped after a previous predict error")

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", lambda: _ShouldNotPredict())
    rr._model_state["predict_error"] = "previous boom"

    docs = [_doc("a"), _doc("b")]
    result = rr.rerank_documents("query", docs, top_k=1)
    assert result == docs[:1]
    metrics = rr.consume_last_metrics()
    assert metrics is not None
    assert metrics["skipped_reason"] == "previous_predict_error"


def test_rerank_empty_input() -> None:
    from src.rag import reranker as rr

    assert rr.rerank_documents("q", [], top_k=5) == []
    assert rr.rerank_scored_docs("q", [], top_k=5) == []


def test_rerank_top_k_zero() -> None:
    from src.rag import reranker as rr

    assert rr.rerank_documents("q", [_doc("a")], top_k=0) == []


def test_get_reranker_status_shape() -> None:
    from src.rag import reranker as rr

    status = rr.get_reranker_status()
    for key in ("enabled", "model_name", "loaded", "loading", "load_error", "candidate_multiplier"):
        assert key in status
    assert isinstance(status["enabled"], bool)
    assert isinstance(status["loading"], bool)
    assert isinstance(status["candidate_multiplier"], int)


def test_auto_profile_selects_qwen_for_suitable_cuda(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_reranker_settings", lambda: {"reranker_profile": "auto"})
    monkeypatch.setattr(rr, "_cuda_can_run_qwen", lambda: True)
    monkeypatch.delenv("RERANKER_DEVICE", raising=False)

    assert rr._resolve_runtime_target() == (rr._GPU_MODEL, "cuda")


def test_auto_profile_keeps_loaded_qwen_when_free_memory_drops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_reranker_settings", lambda: {"reranker_profile": "auto"})
    monkeypatch.setattr(rr, "_cuda_can_run_qwen", lambda: False)
    rr._model_state.update(
        {"model": object(), "name": rr._GPU_MODEL, "device": "cuda", "profile": "auto"}
    )

    assert rr._resolve_runtime_target() == (rr._GPU_MODEL, "cuda")


def _fake_cuda(monkeypatch: pytest.MonkeyPatch, *, major: int, total_gib: float, free_gib: float) -> None:
    import sys
    import types

    class _Props:
        name = "Fake GPU"
        minor = 0
        total_memory = int(total_gib * 1024**3)

    _Props.major = major
    cuda = types.SimpleNamespace(
        is_available=lambda: True,
        get_device_properties=lambda _index: _Props(),
        mem_get_info=lambda _index: (int(free_gib * 1024**3), _Props.total_memory),
    )
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda))


@pytest.mark.parametrize(
    ("major", "total_gib", "free_gib", "expected"),
    [
        (8, 12.0, 9.0, True),
        # 4 GB laptop GPU: passed the old 3 GiB gate, then spilled to shared RAM.
        (8, 4.0, 3.2, False),
        # Pre-Ampere cards emulate the bf16 weights.
        (7, 16.0, 14.0, False),
    ],
)
def test_cuda_gate_requires_room_for_a_deep_pool_and_native_bf16(
    monkeypatch: pytest.MonkeyPatch, major: int, total_gib: float, free_gib: float, expected: bool,
) -> None:
    from src.rag import reranker as rr

    _fake_cuda(monkeypatch, major=major, total_gib=total_gib, free_gib=free_gib)

    assert rr._cuda_can_run_qwen() is expected
    assert rr._gpu_probe["name"] == "Fake GPU"
    assert rr._gpu_probe["capability"] == f"{major}.0"


def test_slow_gpu_prediction_moves_auto_profile_to_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    class _SlowGpuModel:
        device = "cuda:0"
        dtype = "torch.bfloat16"

        def predict(self, pairs):
            return [0.0 for _ in pairs]

    clock = iter([0.0, 0.0, 95.0])
    monkeypatch.setattr(rr.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(rr, "_reranker_settings", lambda: {"reranker_profile": "auto"})
    monkeypatch.setattr(rr, "_cuda_can_run_qwen", lambda: True)
    monkeypatch.delenv("RERANKER_DEVICE", raising=False)
    rr._model_state.update(
        {"model": _SlowGpuModel(), "name": rr._GPU_MODEL, "device": "cuda", "profile": "auto"}
    )
    assert rr._resolve_runtime_target() == (rr._GPU_MODEL, "cuda")

    rr._predict_scores(_SlowGpuModel(), [("q", "doc")] * 45, rr._model_state)

    assert rr._resolve_runtime_target() == (rr._CPU_MODEL, "cpu")
    assert "45 candidates took 95s" in rr.get_reranker_status()["slow_gpu_fallback"]


def test_slow_gpu_prediction_keeps_an_explicit_gpu_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    class _SlowGpuModel:
        device = "cuda:0"

        def predict(self, pairs):
            return [0.0 for _ in pairs]

    clock = iter([0.0, 0.0, 95.0])
    monkeypatch.setattr(rr.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(rr, "_reranker_settings", lambda: {"reranker_profile": "qwen_gpu"})
    rr._model_state["profile"] = "qwen_gpu"

    rr._predict_scores(_SlowGpuModel(), [("q", "doc")], rr._model_state)

    assert rr._resolve_runtime_target() == (rr._GPU_MODEL, "cuda")


def test_auto_profile_falls_back_to_ettin_on_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_reranker_settings", lambda: {"reranker_profile": "auto"})
    monkeypatch.setattr(rr, "_cuda_can_run_qwen", lambda: False)
    monkeypatch.delenv("RERANKER_DEVICE", raising=False)

    assert rr._resolve_runtime_target() == (rr._CPU_MODEL, "cpu")


@pytest.mark.parametrize(
    ("profile", "model_name", "device"),
    [
        ("ettin_17m_cpu", "cross-encoder/ettin-reranker-17m-v1", "cpu"),
        ("ettin_68m_cpu", "cross-encoder/ettin-reranker-68m-v1", "cpu"),
        ("ettin_150m_cpu", "cross-encoder/ettin-reranker-150m-v1", "cpu"),
        ("ettin_400m_gpu", "cross-encoder/ettin-reranker-400m-v1", "cuda"),
        ("ettin_1b_gpu", "cross-encoder/ettin-reranker-1b-v1", "cuda"),
        ("gte_modernbert_cpu", "Alibaba-NLP/gte-reranker-modernbert-base", "cpu"),
        ("bge_base_gpu", "BAAI/bge-reranker-base", "cuda"),
        ("bge_v2_m3_gpu", "BAAI/bge-reranker-v2-m3", "cuda"),
    ],
)
def test_reviewed_optional_profiles_resolve_to_pinned_targets(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    model_name: str,
    device: str,
) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_reranker_settings", lambda: {"reranker_profile": profile})

    assert rr._resolve_runtime_target() == (model_name, device)
    assert rr._model_revision(model_name)


def test_changing_profile_retries_after_previous_model_load_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import reranker as rr

    profile = {"value": "qwen_gpu"}
    monkeypatch.setattr(
        rr,
        "_reranker_settings",
        lambda: {"reranker_profile": profile["value"]},
    )
    rr._model_state.update(
        {
            "name": rr._GPU_MODEL,
            "device": "cuda",
            "profile": "qwen_gpu",
            "tried": True,
            "load_error": "CUDA out of memory",
        }
    )

    profile["value"] = "ettin_cpu"
    name, device = rr._resolve_runtime_target()

    assert (name, device) == (rr._CPU_MODEL, "cpu")
    assert rr._model_state["name"] != name  # _load_model will detect and reset the error


def test_cpu_ettin_translates_french_query_for_english_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_translation_enabled", lambda: True)
    monkeypatch.setattr(
        rr,
        "_translate_french_query",
        lambda query: "What formulations model AC losses in HTS magnets?",
    )
    candidates = [
        "This paper presents a formulation for the numerical model.",
        "The losses are computed with a finite-element method.",
        "A comparison of models for superconducting magnets is provided.",
    ]

    translated, applied, _ = rr._prepare_query_for_reranking(
        "Je cherche les formulations pour modéliser les pertes dans les aimants HTS.",
        candidates,
        {"name": rr._CPU_MODEL, "device": "cpu"},
    )

    assert applied is True
    assert translated.startswith("What formulations")


def test_qwen_gpu_never_translates_query(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_translation_enabled", lambda: True)

    def _unexpected_translation(query: str) -> str:
        raise AssertionError("Qwen is multilingual; translation must not run")

    monkeypatch.setattr(rr, "_translate_french_query", _unexpected_translation)
    query, applied, elapsed = rr._prepare_query_for_reranking(
        "Je cherche les formulations pour les pertes.",
        ["This is an English scientific passage with the requested formulation."] * 3,
        {"name": rr._GPU_MODEL, "device": "cuda"},
    )

    assert query.startswith("Je cherche")
    assert applied is False
    assert elapsed == 0.0


def test_multilingual_bge_never_translates_query(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_translation_enabled", lambda: True)

    def _unexpected_translation(query: str) -> str:
        raise AssertionError("BGE is multilingual; translation must not run")

    monkeypatch.setattr(rr, "_translate_french_query", _unexpected_translation)
    query, applied, _ = rr._prepare_query_for_reranking(
        "Je cherche les formulations pour les pertes.",
        ["This is an English scientific passage with the requested formulation."] * 3,
        {"name": rr._BGE_BASE_MODEL, "device": "cuda"},
    )

    assert query.startswith("Je cherche")
    assert applied is False


def test_warmup_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: False)
    result = rr.warmup_reranker()
    assert result.get("warmup_skipped")
    assert result["enabled"] is False


def test_warmup_skips_when_unload_after_use(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setenv("RERANKER_UNLOAD_AFTER_USE", "true")

    def _should_not_load():
        raise AssertionError("warmup is pointless when unload-after-use is enabled")

    monkeypatch.setattr(rr, "_load_model", _should_not_load)
    result = rr.warmup_reranker()
    assert result.get("warmup_skipped") == "unload-after-use is enabled"


def test_warmup_calls_load_and_reports_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """warmup_reranker should invoke _load_model and surface its result."""
    from src.rag import reranker as rr

    calls = {"n": 0}

    class _StubModel:
        def predict(self, pairs):
            return [0.0 for _ in pairs]

    def _fake_load():
        calls["n"] += 1
        rr._model_state["model"] = _StubModel()
        rr._model_state["name"], rr._model_state["device"] = rr._resolve_runtime_target()
        rr._model_state["load_error"] = ""
        return rr._model_state["model"]

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", _fake_load)
    monkeypatch.setenv("RERANKER_UNLOAD_AFTER_USE", "false")

    status = rr.warmup_reranker()
    assert calls["n"] == 1
    assert status["loaded"] is True
    assert status["loading"] is False


def test_rerank_unloads_after_success_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    class _StubModel:
        def predict(self, pairs):
            return [float(i) for i in range(len(pairs))]

    model = _StubModel()
    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", lambda: model)
    monkeypatch.setenv("RERANKER_UNLOAD_AFTER_USE", "true")
    rr._model_state["model"] = model
    rr._model_state["name"] = "stub-model"

    result = rr.rerank_documents("q", [_doc("a"), _doc("b")], top_k=1)
    assert [doc.page_content for doc in result] == ["b"]
    assert rr._model_state["model"] is None
    metrics = rr.consume_last_metrics()
    assert metrics is not None
    assert metrics["unloaded_after_use"] is True


def test_rerank_records_metrics_when_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    class _StubModel:
        def predict(self, pairs):
            return [float(i) for i in range(len(pairs))]

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", lambda: _StubModel())
    rr._model_state["model"] = _StubModel()
    rr._model_state["name"] = "stub-model"

    docs = [_doc(f"d{i}") for i in range(8)]
    rr.rerank_documents("q", docs, top_k=3)

    metrics = rr.consume_last_metrics()
    assert metrics is not None
    assert metrics["enabled"] is True
    assert metrics["applied"] is True
    assert metrics["candidates_in"] == 8
    assert metrics["top_k"] == 3
    assert metrics["model_name"] == "stub-model"
    assert metrics["predict_duration_ms"] >= 0
    # consume should empty the slot
    assert rr.consume_last_metrics() is None


def test_rerank_records_metrics_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: False)
    rr.rerank_documents("q", [_doc("a"), _doc("b")], top_k=1)
    metrics = rr.consume_last_metrics()
    assert metrics is not None
    assert metrics["enabled"] is False
    assert metrics["applied"] is False


def test_stream_warmup_status_yields_when_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    """When reranker is enabled but cold, the warmup helper must yield status events."""
    from src.rag import query_engine as qe
    from src.rag import reranker as rr

    monkeypatch.setattr(qe, "_current_ui_language", lambda: "fr")

    rr._model_state["model"] = None
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_is_reranker_loaded", lambda: False)
    monkeypatch.setattr(qe, "_reranker_should_unload_after_use", lambda: False)

    def _fake_trigger():
        rr._model_state["model"] = object()
        rr._model_state["name"], rr._model_state["device"] = rr._resolve_runtime_target()
        return rr.get_reranker_status()

    monkeypatch.setattr(qe, "_reranker_trigger_load", _fake_trigger)

    events = list(qe._stream_reranker_warmup_status())
    assert events
    assert events[0]["type"] == "status"
    assert "Préchauffage" in events[0]["msg"]
    assert events[-1]["type"] == "status"
    assert "Reranker prêt" in events[-1]["msg"]


def test_stream_warmup_status_skips_when_already_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import query_engine as qe

    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_is_reranker_loaded", lambda: True)
    assert list(qe._stream_reranker_warmup_status()) == []


def test_stream_warmup_status_skips_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import query_engine as qe

    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)
    assert list(qe._stream_reranker_warmup_status()) == []


def test_stream_warmup_status_skips_after_predict_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import query_engine as qe

    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_has_reranker_predict_error", lambda: True)
    monkeypatch.setattr(qe, "_is_reranker_loaded", lambda: False)

    def _should_not_load():
        raise AssertionError("reranker warmup must not reload after a predict error")

    monkeypatch.setattr(qe, "_reranker_trigger_load", _should_not_load)
    assert list(qe._stream_reranker_warmup_status()) == []


def test_stream_warmup_status_skips_when_unload_after_use(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import query_engine as qe

    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_has_reranker_predict_error", lambda: False)
    monkeypatch.setattr(qe, "_reranker_should_unload_after_use", lambda: True)
    monkeypatch.setattr(qe, "_is_reranker_loaded", lambda: False)

    def _should_not_load():
        raise AssertionError("auto warmup must not load in unload-after-use mode")

    monkeypatch.setattr(qe, "_reranker_trigger_load", _should_not_load)
    assert list(qe._stream_reranker_warmup_status()) == []


def test_load_model_sets_and_clears_loading_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """During _load_model, ``loading`` must be True; after, must be False."""
    from src.rag import reranker as rr

    seen_loading_state: dict[str, Any] = {}

    class _StubModel:
        def predict(self, pairs):
            return [0.0 for _ in pairs]

    def _fake_cross_encoder(name, **kwargs):
        # At this point we are inside the load critical section.
        seen_loading_state["during"] = bool(rr._model_state["loading"])
        seen_loading_state["kwargs"] = kwargs
        return _StubModel()

    # Patch the import target so _load_model uses our fake CrossEncoder.
    import sys
    import types

    fake_module = types.ModuleType("sentence_transformers")
    fake_module.CrossEncoder = _fake_cross_encoder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_module)

    model = rr._load_model()
    assert model is not None
    assert seen_loading_state.get("during") is True
    assert seen_loading_state["kwargs"]["trust_remote_code"] is False
    assert seen_loading_state["kwargs"]["model_kwargs"]["use_safetensors"] is True
    assert rr._model_state["loading"] is False


def test_retrieve_documents_uses_reranker_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_retrieve_documents must call rerank_documents when the flag is on."""
    from src.rag import query_engine as qe

    # Simulate vectorstore returning many candidates.
    fake_scored = [
        (_doc(f"chunk-{i}", source=f"f{i}.pdf", page=str(i), content_type="article"), 0.9 - i * 0.01)
        for i in range(20)
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: fake_scored[:k])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_reranker_candidate_multiplier", lambda: 4)

    rerank_calls: dict[str, Any] = {"count": 0, "n_docs": 0, "top_k": 0}

    def _fake_rerank(query, docs, top_k, **kwargs):
        rerank_calls["count"] += 1
        rerank_calls["n_docs"] = len(docs)
        rerank_calls["top_k"] = top_k
        return list(reversed(docs))[:top_k]

    monkeypatch.setattr(qe, "_rerank_documents", _fake_rerank)

    result = qe._retrieve_documents(
        "test query",
        threshold=0.0,
        max_chunks=5,
        filters={},
    )

    assert rerank_calls["count"] == 1
    assert rerank_calls["n_docs"] > 5, "reranker must see more than max_chunks candidates"
    assert rerank_calls["top_k"] == 5
    assert len(result) == 5


def test_retrieve_documents_expands_t_phi_notation_for_symbolic_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    captured: dict[str, Any] = {}
    fake_scored = [
        (
            _doc(
                "The T-A formulation has limitations for tangential fields.",
                source="ta.pdf",
                page="1",
                content_type="article",
            ),
            0.88,
        ),
        (
            _doc(
                "The T-φ formulation can be inconvenient for thin HTS tapes.",
                source="tphi.pdf",
                page="2",
                content_type="article",
            ),
            0.62,
        ),
        (
            _doc(
                "General finite-element modeling of HTS coils.",
                source="general.pdf",
                page="3",
                content_type="article",
            ),
            0.75,
        ),
    ]

    def _fake_collect(query: str, k: int):
        captured["query"] = query
        captured["k"] = k
        return fake_scored[:k]

    monkeypatch.setattr(qe, "_collect_scored_docs", _fake_collect)
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    result = qe._retrieve_documents(
        "Quels sont les inconvenients de la methode T-phi pour les rubans HTS ?",
        threshold=0.0,
        max_chunks=2,
        filters={},
    )

    assert "t-φ" in str(captured["query"])
    assert "tphi" in str(captured["query"])
    assert result[0].metadata["source"] == "tphi.pdf"


def test_symbolic_query_expansion_covers_greek_alphabet_not_only_phi() -> None:
    from src.rag import query_engine as qe

    expanded = qe._expand_symbolic_retrieval_query(
        "Comparer H-alpha, beta et epsilon_r dans une formulation HTS"
    )

    assert "h-α" in expanded
    assert "hα" in expanded
    assert "β" in expanded
    assert "ε_r" in expanded
    assert "epsilon_r" in expanded
    assert "β-et" not in expanded


def test_generic_lexical_score_tolerates_metadata_typos() -> None:
    from src.rag import query_engine as qe

    score = qe._generic_lexical_score(
        "bayseian calibraiton",
        metadata={
            "zotero_title": "Bayesian calibration of thermal models",
            "zotero_authors": "Garcia",
        },
        content="",
    )

    assert score > 0


def test_retrieve_documents_boosts_author_metadata_without_explicit_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    fake_scored = [
        (
            _doc(
                "General REBCO conductor note.",
                source="lateb.pdf",
                page="1",
                content_type="article",
                zotero_title="Critical surface of REBCO conductors",
                zotero_authors="Lateb",
            ),
            0.90,
        ),
        (
            _doc(
                "Fujikura conductor parameterization.",
                source="fleiter.pdf",
                page="1",
                content_type="article",
                zotero_title="Critical surface of REBCO conductors",
                zotero_authors="Fleiter, Ballarino",
            ),
            0.84,
        ),
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: fake_scored[:k])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    result = qe._retrieve_documents(
        "Fleiter critical surface",
        threshold=0.0,
        max_chunks=2,
        filters={},
    )

    assert result[0].metadata["source"] == "fleiter.pdf"


def test_retrieve_documents_adds_generic_lexical_metadata_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe
    from src.rag import state

    class FakeVectorstore:
        def get(self, **kwargs):
            return {
                "documents": [
                    "This chunk describes the arbitrary domain experiment.",
                    "Completely unrelated passage.",
                ],
                "metadatas": [
                    {
                        "source": "garcia.pdf",
                        "page": "4",
                        "content_type": "article",
                        "zotero_title": "Bayesian calibration of masonry arches",
                        "zotero_authors": "Garcia",
                    },
                    {
                        "source": "noise.pdf",
                        "page": "1",
                        "content_type": "article",
                        "zotero_title": "Unrelated report",
                    },
                ],
            }

    monkeypatch.setattr(state, "vectorstore", FakeVectorstore())

    monkeypatch.setattr(
        qe,
        "_collect_scored_docs",
        lambda query, k: [
            (
                _doc(
                    "Vector-only candidate.",
                    source="vector.pdf",
                    page="1",
                    content_type="article",
                    zotero_title="Generic vector match",
                ),
                0.2,
            )
        ],
    )
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    result = qe._retrieve_documents(
        "Garcia bayseian calibraiton",
        threshold=0.0,
        max_chunks=2,
        filters={},
    )

    assert result[0].metadata["source"] == "garcia.pdf"


def test_lexical_metadata_scan_paginates_for_author_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import query_engine as qe
    from src.rag import state

    class FakeCollection:
        def count(self):
            return 6500

    class FakeVectorstore:
        _collection = FakeCollection()

        def __init__(self):
            self.calls: list[int] = []

        def get(self, **kwargs):
            offset = int(kwargs.get("offset") or 0)
            self.calls.append(offset)
            if offset < 3000:
                return {
                    "documents": ["noise"],
                    "metadatas": [{"source": f"noise-{offset}.pdf", "zotero_title": "Noise"}],
                }
            return {
                "documents": ["late page"],
                "metadatas": [
                    {
                        "source": "moreau.pdf",
                        "page": "1",
                        "content_type": "article",
                        "zotero_title": "Late indexed paper",
                        "zotero_authors": "Moreau, A.",
                    }
                ],
            }

    fake = FakeVectorstore()
    monkeypatch.setattr(state, "vectorstore", fake)

    result = qe._collect_lexical_metadata_docs(
        "articles écrits par Moreau",
        max_docs=5,
        filters={"authors": ["Moreau"], "tags": [], "collections": [], "years": [], "dois": []},
    )

    assert 3000 in fake.calls
    assert len(result) == 1
    assert result[0][0].metadata["source"] == "moreau.pdf"


def test_lexical_metadata_scan_is_complete_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import query_engine as qe
    from src.rag import state

    class FakeCollection:
        def count(self):
            return 6500

    class FakeVectorstore:
        _collection = FakeCollection()

        def __init__(self):
            self.calls: list[int] = []

        def get(self, **kwargs):
            offset = int(kwargs.get("offset") or 0)
            self.calls.append(offset)
            if offset < 6000:
                return {
                    "documents": ["noise"],
                    "metadatas": [{"source": f"noise-{offset}.pdf", "zotero_title": "Noise"}],
                }
            return {
                "documents": ["A late chunk about Bayesian calibration."],
                "metadatas": [
                    {
                        "source": "late.pdf",
                        "page": "1",
                        "content_type": "article",
                        "zotero_title": "Bayesian calibration of masonry arches",
                    }
                ],
            }

    fake = FakeVectorstore()
    monkeypatch.delenv("RAG_LEXICAL_SCAN_LIMIT", raising=False)
    monkeypatch.setattr(state, "vectorstore", fake)

    result = qe._collect_lexical_metadata_docs(
        "Bayesian calibration",
        max_docs=5,
        filters={},
    )

    assert 6000 in fake.calls
    assert len(result) == 1
    assert result[0][0].metadata["source"] == "late.pdf"


def test_lexical_metadata_scan_dedupes_author_matches_by_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe
    from src.rag import state

    class FakeCollection:
        def count(self):
            return 3

    class FakeVectorstore:
        _collection = FakeCollection()

        def get(self, **kwargs):
            return {
                "documents": ["", "", ""],
                "metadatas": [
                    {
                        "article_id": "p1",
                        "source": "p1.pdf",
                        "page": "1",
                        "zotero_title": "First paper",
                        "zotero_authors": "Moreau, A.",
                    },
                    {
                        "article_id": "p1",
                        "source": "p1.pdf",
                        "page": "2",
                        "zotero_title": "First paper",
                        "zotero_authors": "Moreau, A.",
                    },
                    {
                        "article_id": "p2",
                        "source": "p2.pdf",
                        "page": "1",
                        "zotero_title": "Second paper",
                        "zotero_authors": "Moreau, A.",
                    },
                ],
            }

    monkeypatch.setattr(state, "vectorstore", FakeVectorstore())

    result = qe._collect_lexical_metadata_docs(
        "articles écrits par Moreau",
        max_docs=5,
        filters={"authors": ["Moreau"], "tags": [], "collections": [], "years": [], "dois": []},
    )

    assert [doc.metadata["source"] for doc, _ in result] == ["p1.pdf", "p2.pdf"]


def test_retrieve_documents_sends_wider_pool_to_reranker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    fake_scored = [
        (_doc(f"chunk-{i}", source=f"f{i}.pdf", page=str(i), content_type="article"), 0.9 - i * 0.01)
        for i in range(40)
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: fake_scored[:k])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_reranker_candidate_multiplier", lambda: 4)

    rerank_calls: dict[str, Any] = {"n_docs": 0}

    def _fake_rerank(query, docs, top_k, **kwargs):
        rerank_calls["n_docs"] = len(docs)
        return docs[:top_k]

    monkeypatch.setattr(qe, "_rerank_documents", _fake_rerank)

    qe._retrieve_documents(
        "test query",
        threshold=0.0,
        max_chunks=5,
        filters={},
    )

    assert rerank_calls["n_docs"] == 40


def test_retrieve_documents_skips_reranker_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    fake_scored = [
        (_doc(f"chunk-{i}", source=f"f{i}.pdf", page=str(i), content_type="article"), 0.9 - i * 0.01)
        for i in range(20)
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: fake_scored[:k])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    def _should_not_be_called(*a, **kw):
        raise AssertionError("reranker must not be called when disabled")

    monkeypatch.setattr(qe, "_rerank_documents", _should_not_be_called)

    result = qe._retrieve_documents(
        "test query",
        threshold=0.0,
        max_chunks=5,
        filters={},
    )
    assert len(result) == 5


def test_retrieve_documents_records_retrieval_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    fake_scored = [
        (_doc(f"chunk-{i}", source=f"f{i}.pdf", page=str(i), content_type="article"), 0.9)
        for i in range(8)
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: fake_scored[:k])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    qe._consume_pipeline_metrics()
    result = qe._retrieve_documents(
        "test query",
        threshold=0.0,
        max_chunks=3,
        filters={},
    )
    metrics = qe._consume_pipeline_metrics()

    assert len(result) == 3
    assert len(metrics) == 1
    assert metrics[0]["stage"] == "retrieval"
    data = metrics[0]["data"]
    assert data["scored_candidates"] >= 3
    assert data["unique_candidates"] >= 3
    assert data["returned"] == 3
    assert data["duration_ms"] >= 0


def test_retrieve_documents_does_not_fall_back_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    monkeypatch.setattr(
        qe,
        "_collect_scored_docs",
        lambda query, k: [
            (
                _doc(
                    "Unrelated magnetic field calculation.",
                    source="unrelated.pdf",
                    page="1",
                    content_type="article",
                ),
                0.12,
            )
        ],
    )
    monkeypatch.setattr(qe, "_collect_lexical_metadata_docs", lambda *args, **kwargs: [])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    result = qe._retrieve_documents(
        "Kim-Anderson default parameters",
        threshold=0.43,
        max_chunks=6,
        filters={},
    )

    assert result == []


def test_retrieve_documents_does_not_give_lexical_candidates_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    weak_lexical_doc = _doc(
        "This page only mentions a generic model.",
        source="generic.pdf",
        page="1",
        content_type="article",
    )
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: [])
    monkeypatch.setattr(
        qe,
        "_collect_lexical_metadata_docs",
        lambda *args, **kwargs: [(weak_lexical_doc, 0.12)],
    )
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    result = qe._retrieve_documents(
        "default model parameters",
        threshold=0.43,
        max_chunks=6,
        filters={},
    )

    assert result == []


def test_retrieve_documents_requires_named_hyphenated_model_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    unrelated = _doc(
        "Magnetic vector potential for an axisymmetric magnet sector. Kim K. et al.",
        source="aubert.pdf",
        page="4",
        content_type="article",
        zotero_title="Champ magnetique et potentiel vecteur magnetique",
    )
    relevant = _doc(
        "The Kim model uses Jc0 and B0 to describe field-dependent critical current.",
        source="kim.pdf",
        page="17",
        content_type="article",
        zotero_title="Numerical Modeling of Superconducting Applications",
    )
    monkeypatch.setattr(
        qe,
        "_collect_scored_docs",
        lambda query, k: [(unrelated, 0.92), (relevant, 0.70)],
    )
    monkeypatch.setattr(qe, "_collect_lexical_metadata_docs", lambda *args, **kwargs: [])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    result = qe._retrieve_documents(
        "quelles valeurs pour le modele de Kim-Anderson ?",
        threshold=0.43,
        max_chunks=6,
        filters={},
    )

    assert [doc.metadata["source"] for doc in result] == ["kim.pdf"]


def test_rerank_documents_routes_to_deep_alt_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-primary model_name must load via the alt registry, not _model_state."""
    from src.rag import reranker as rr

    class _AltStub:
        def predict(self, pairs):
            return [float(len(text)) for _, text in pairs]

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)

    loaded: dict[str, str] = {}

    def _fake_load_alt(name):
        loaded["name"] = name
        rr._alt_entry(name)["model"] = _AltStub()
        return rr._alt_entry(name)["model"]

    monkeypatch.setattr(rr, "_load_alt_model", _fake_load_alt)

    deep_name = rr._resolve_deep_model_name()
    docs = [_doc("xx"), _doc("xxxxxxxx"), _doc("xxxx")]
    result = rr.rerank_documents("q", docs, top_k=3, model_name=deep_name)

    assert loaded["name"] == deep_name
    assert rr._model_state["model"] is None  # primary untouched
    assert [d.page_content for d in result] == ["xxxxxxxx", "xxxx", "xx"]
    metrics = rr.consume_last_metrics()
    assert metrics is not None and metrics["model_name"] == deep_name


def test_rerank_documents_truncates_text_to_max_chars(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import reranker as rr

    seen: dict[str, int] = {}

    class _StubModel:
        def predict(self, pairs):
            seen["max_len"] = max(len(text) for _, text in pairs)
            return [0.0 for _ in pairs]

    monkeypatch.setattr(rr, "_enabled_from_settings", lambda: True)
    monkeypatch.setattr(rr, "_load_model", lambda: _StubModel())

    long_docs = [_doc("z" * 5000), _doc("y" * 5000)]
    rr.rerank_documents("q", long_docs, top_k=1, max_text_chars=300)
    assert seen["max_len"] <= 300


def test_retrieve_documents_quick_uses_tight_pool_and_primary_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    fake_scored = [
        (_doc(f"chunk-{i}", source=f"f{i}.pdf", page=str(i), content_type="article"), 0.9 - i * 0.01)
        for i in range(60)
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: fake_scored[:k])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_reranker_candidate_multiplier", lambda: 4)

    captured: dict[str, Any] = {}

    def _fake_rerank(query, docs, top_k, *, model_name=None, max_text_chars=None):
        captured["n_docs"] = len(docs)
        captured["model_name"] = model_name
        captured["max_text_chars"] = max_text_chars
        return docs[:top_k]

    monkeypatch.setattr(qe, "_rerank_documents", _fake_rerank)

    qe._retrieve_documents("test query", threshold=0.0, max_chunks=5, filters={}, depth="quick")

    # Quick keeps the pool tight (no wide *2 fan-out) and uses the primary model.
    assert captured["n_docs"] <= 24
    assert captured["model_name"] is None
    assert captured["max_text_chars"] and captured["max_text_chars"] > 0


def test_retrieve_documents_keeps_every_selected_article(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    def _chunk(article: str, i: int, score: float):
        return (
            _doc(
                f"{article} chunk {i}",
                source=f"{article}.pdf",
                rel_path=f"{article}.pdf",
                article_id=article,
                page=str(i),
                content_type="article",
            ),
            score,
        )

    # Article A dominates every score; B and C must still be represented.
    fake_scored = [_chunk("A", i, 0.95 - i * 0.001) for i in range(20)]
    fake_scored += [_chunk("B", i, 0.5 - i * 0.01) for i in range(3)]
    fake_scored += [_chunk("C", i, 0.4 - i * 0.01) for i in range(3)]
    monkeypatch.setattr(qe, "_collect_source_scored_docs", lambda query, k, source_paths: fake_scored)
    monkeypatch.setattr(qe, "_collect_lexical_metadata_docs", lambda *_a, **_k: [])
    monkeypatch.setattr(qe, "_english_retrieval_query", lambda query: "")
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_reranker_candidate_multiplier", lambda: 4)

    def _fake_rerank(query, docs, top_k, *, model_name=None, max_text_chars=None):
        ordered = sorted(docs, key=lambda doc: doc.metadata["article_id"])
        return ordered[:top_k]

    monkeypatch.setattr(qe, "_rerank_documents", _fake_rerank)

    docs = qe._retrieve_documents(
        "compare these papers",
        threshold=0.3,
        max_chunks=5,
        filters={"sources": ["A.pdf", "B.pdf", "C.pdf"]},
        depth="quick",
    )

    assert len(docs) == 5
    assert {doc.metadata["article_id"] for doc in docs} == {"A", "B", "C"}


def test_retrieve_documents_deep_uses_hardware_selected_primary_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe

    fake_scored = [
        (_doc(f"chunk-{i}", source=f"f{i}.pdf", page=str(i), content_type="article"), 0.9 - i * 0.01)
        for i in range(60)
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: fake_scored[:k])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_reranker_candidate_multiplier", lambda: 4)

    captured: dict[str, Any] = {}
    monkeypatch.delenv("RAG_LEXICAL_SCAN_LIMIT", raising=False)

    def _fake_lexical(*_args, **kwargs):
        captured["lexical_scan_limit"] = kwargs.get("scan_limit")
        return []

    def _fake_rerank(query, docs, top_k, *, model_name=None, max_text_chars=None):
        captured["n_docs"] = len(docs)
        captured["model_name"] = model_name
        return docs[:top_k]

    monkeypatch.setattr(qe, "_collect_lexical_metadata_docs", _fake_lexical)
    monkeypatch.setattr(qe, "_rerank_documents", _fake_rerank)

    qe._retrieve_documents("test query", threshold=0.0, max_chunks=5, filters={}, depth="deep")

    assert captured["model_name"] is None
    assert captured["n_docs"] > 24  # deep keeps the wide pool
    assert captured["lexical_scan_limit"] == 12000


def test_collect_lexical_metadata_docs_respects_scan_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import query_engine as qe
    from src.rag import state

    class FakeCollection:
        def count(self):
            return 50000

    class FakeVectorstore:
        _collection = FakeCollection()

        def __init__(self):
            self.calls: list[int] = []

        def get(self, **kwargs):
            offset = int(kwargs.get("offset") or 0)
            self.calls.append(offset)
            return {
                "documents": ["noise"],
                "metadatas": [{"source": f"n-{offset}.pdf", "zotero_title": "Noise"}],
            }

    fake = FakeVectorstore()
    monkeypatch.delenv("RAG_LEXICAL_SCAN_LIMIT", raising=False)
    monkeypatch.setattr(state, "vectorstore", fake)

    qe._collect_lexical_metadata_docs("query", max_docs=5, filters={}, scan_limit=2500)

    # The scan must stop within the cap and never page out toward the full 50000.
    assert fake.calls and all(offset < 2500 for offset in fake.calls)


def test_dedupe_source_docs_collapses_same_chunk_across_content_types() -> None:
    from src.rag import query_engine as qe

    docs = [
        _doc(
            "same extracted passage",
            source="aubert.pdf",
            page="4",
            content_type="article",
            chunk_uid="AUBERT:10",
        ),
        _doc(
            "same extracted passage",
            source="aubert.pdf",
            page="4",
            content_type="pdf_page",
            chunk_uid="AUBERT:10",
        ),
    ]

    assert qe._dedupe_source_docs(docs) == [docs[0]]
