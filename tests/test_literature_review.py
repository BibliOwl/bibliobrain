"""Tests for the literature review pipeline.

Stubs the LLM call and retrieval so the tests are hermetic.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.documents import Document


def _doc(content: str, **meta: Any) -> Document:
    return Document(page_content=content, metadata=meta)


# Requests whose language is unambiguous, so the tests never depend on the
# machine's stored UI language.
_FRENCH_REQUEST = (
    "Fais une revue de littérature concise sur la magnétisation par champ pulsé "
    "des bulks HTS, avec les principaux résultats et limites."
)
_ENGLISH_REQUEST = (
    "Write a concise literature review on pulsed field magnetization of HTS bulks, "
    "with the main results and limitations."
)
_AMBIGUOUS_REQUEST = "HTS bulk magnetization"


def test_to_markdown_renders_sections() -> None:
    from src.rag.literature_review import (
        LiteratureReview,
        ReviewDisagreement,
        ReviewTheme,
    )

    review = LiteratureReview(
        overview="Vue d'ensemble [S1, S2].",
        chronology=["2017 — Transformers [S1]", "2020 — GPT-3 [S2]"],
        themes=[
            ReviewTheme(
                title="Architectures attention-based",
                summary="Les modèles attention-based dominent depuis 2017 [S1].",
                sources=["S1"],
            ),
        ],
        disagreements=[
            ReviewDisagreement(
                point="Capacité de raisonnement",
                positions="Certains pensent que oui [S1], d'autres non [S2].",
            ),
        ],
        synthesis="Synthèse finale [S1, S2].",
    )
    md = review.to_markdown()
    assert "## Vue d'ensemble" in md
    assert "## Chronologie" in md
    assert "## Écoles de pensée" in md
    assert "## Désaccords identifiés" in md
    assert "## Synthèse" in md
    assert "[S1]" in md


def test_to_markdown_skips_empty_sections() -> None:
    from src.rag.literature_review import LiteratureReview

    review = LiteratureReview(overview="", synthesis="Une synthèse.")
    md = review.to_markdown()
    assert "## Synthèse" in md
    assert "## Vue d'ensemble" not in md
    assert "## Chronologie" not in md


def test_build_review_prompt_groups_by_paper() -> None:
    from src.rag.literature_review import build_review_prompt

    docs = [
        _doc("Chunk A1 contenu.", article_id="paper-1", zotero_title="Paper One",
             zotero_year="2020", zotero_authors="A. One"),
        _doc("Chunk A2 contenu.", article_id="paper-1", zotero_title="Paper One",
             zotero_year="2020", zotero_authors="A. One"),
        _doc("Chunk B contenu.", article_id="paper-2", zotero_title="Paper Two",
             zotero_year="2022", zotero_authors="B. Two"),
    ]
    prompt, capped_docs, labels = build_review_prompt("Question", docs)

    assert labels == ["S1", "S2"]
    assert "[S1]" in prompt
    assert "[S2]" in prompt
    assert "Paper One" in prompt
    assert "Paper Two" in prompt
    assert len(capped_docs) == 3, "all docs of capped papers should be retained"


def test_build_review_prompt_caps_paper_count() -> None:
    from src.rag.literature_review import MAX_PAPERS_IN_PROMPT, build_review_prompt

    docs = [
        _doc(f"chunk {i}", article_id=f"paper-{i}", zotero_title=f"Paper {i}",
             zotero_year="2020", zotero_authors="X")
        for i in range(MAX_PAPERS_IN_PROMPT + 5)
    ]
    _prompt, _docs, labels = build_review_prompt("Q", docs)
    assert len(labels) == MAX_PAPERS_IN_PROMPT


def test_generate_literature_review_uses_structured_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import literature_review as lr
    from src.rag.literature_review import LiteratureReview

    captured: dict[str, Any] = {}

    def _fake_invoke(model_name, messages, response_model=None, **kwargs):
        captured["model_name"] = model_name
        captured["response_model"] = response_model
        return LiteratureReview(
            overview="Aperçu",
            synthesis="Synthèse [S1].",
        )

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _fake_invoke)

    docs = [_doc("contenu", article_id="p1", zotero_title="Paper", zotero_year="2020")]
    review, used_docs, labels = lr.generate_literature_review(
        _FRENCH_REQUEST,
        docs=docs,
        model_name="dummy",
    )

    assert isinstance(review, LiteratureReview)
    assert captured["model_name"] == "dummy"
    assert issubclass(captured["response_model"], LiteratureReview)
    assert len(used_docs) == 1
    assert labels == ["S1"]


def test_generate_literature_review_rejects_empty_docs() -> None:
    from src.rag import literature_review as lr

    with pytest.raises(ValueError, match="at least one"):
        lr.generate_literature_review("q", docs=[], model_name="m")


def test_stream_literature_review_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end happy path: status → token → sources events."""
    from src.rag import literature_review as lr
    from src.rag import query_engine as qe
    from src.rag import state
    from src.rag.literature_review import LiteratureReview

    # Pretend RAG is ready.
    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    fake_docs = [
        _doc("Paper one content", article_id="p1", zotero_title="Paper One",
             zotero_year="2020", source="p1.pdf", page="1"),
    ]
    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: fake_docs)
    monkeypatch.setattr(
        "src.llm_provider.invoke_structured_chat",
        lambda *a, **kw: LiteratureReview(
            overview="Overview text",
            synthesis="Synthesis text [S1].",
        ),
    )

    events = list(
        lr.stream_literature_review(
            "tell me about transformers",
            model_name="dummy",
            threshold=0.0,
            target_chunks=10,
        )
    )

    types = [e.get("type") for e in events]
    assert "status" in types
    assert "token" in types
    assert "sources" in types
    # Last event must be one of the post-sources signals (sources / citation_anchors / pipeline_metrics).
    assert types[-1] in {"sources", "citation_anchors", "pipeline_metrics"}

    sources_event = next(e for e in events if e.get("type") == "sources")
    assert sources_event["answer_origin"] == "literature_review"
    assert isinstance(sources_event["data"], list)


def test_stream_literature_review_reports_init_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import literature_review as lr
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = None
        state.vectorstore = None

    monkeypatch.setattr(qe, "init_rag", lambda *a, **kw: (False, "rag boom"))

    events = list(
        lr.stream_literature_review(
            "anything",
            model_name="m",
            threshold=0.0,
        )
    )
    assert events == [{"type": "error", "msg": "rag boom"}]


def test_stream_literature_review_no_docs_returns_friendly_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: [])

    events = list(
        lr.stream_literature_review(
            "obscure topic",
            model_name="m",
            threshold=0.0,
        )
    )
    types = [e.get("type") for e in events]
    assert "token" in types
    assert "sources" in types
    sources_event = next(e for e in events if e.get("type") == "sources")
    assert sources_event["data"] == []


def _long_doc(index: int, *, chars: int = 6000) -> Document:
    return _doc(
        f"Contenu {index}. " + "magnetisation des supraconducteurs massifs. " * (chars // 44),
        article_id=f"p{index}",
        zotero_title=f"Article {index}",
        zotero_year=str(2000 + index),
    )


def _stub_review(label: str):
    from src.rag.literature_review import LiteratureReview

    return LiteratureReview(
        overview=f"Apercu {label}",
        chronology=[f"2005 — jalon {label} [{label}]"],
        synthesis=f"Synthese {label}.",
    )


def test_generate_literature_review_batches_when_context_is_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr
    from src.rag.literature_review import LiteratureReview

    prompts: list[str] = []
    hints: list[int] = []

    def _fake_invoke(model_name, messages, response_model=None, **kwargs):
        prompts.append(messages[0]["content"])
        hints.append(int(kwargs.get("context_tokens_hint", 0)))
        return LiteratureReview(overview="Apercu", synthesis="Synthese [S1].")

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _fake_invoke)
    monkeypatch.setattr(lr, "_resolve_review_context", lambda *_a, **_kw: 4096)

    docs = [_long_doc(i) for i in range(1, 7)]
    review, used_docs, labels = lr.generate_literature_review(
        _FRENCH_REQUEST,
        docs=docs,
        model_name="dummy",
    )

    assert isinstance(review, LiteratureReview)
    # More than one synthesis pass, plus a final merge pass.
    assert len(prompts) > 2
    assert prompts[-1].startswith("Tu fusionnes plusieurs synthèses partielles")
    assert all(hint > 0 for hint in hints)
    # Every paper that survived keeps its original label and its chunks.
    assert labels == [f"S{i}" for i in range(1, len(labels) + 1)]
    assert len(used_docs) == len(labels)


def test_generate_literature_review_respects_the_batch_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr
    from src.rag.literature_review import MAX_REVIEW_BATCHES, LiteratureReview

    calls: list[str] = []

    def _fake_invoke(model_name, messages, response_model=None, **kwargs):
        calls.append(messages[0]["content"])
        return LiteratureReview(overview="Apercu", synthesis="Synthese [S1].")

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _fake_invoke)
    monkeypatch.setattr(lr, "_resolve_review_context", lambda *_a, **_kw: 4096)

    docs = [_long_doc(i) for i in range(1, 16)]
    _review, used_docs, labels = lr.generate_literature_review(
        _FRENCH_REQUEST,
        docs=docs,
        model_name="dummy",
    )

    merge_calls = [c for c in calls if c.startswith("Tu fusionnes")]
    assert len(calls) - len(merge_calls) <= MAX_REVIEW_BATCHES
    assert len(labels) < 15  # the tail was dropped rather than overflowing
    assert len(used_docs) == len(labels)


def test_generate_literature_review_merges_locally_when_the_reduce_pass_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr
    from src.rag.literature_review import LiteratureReview

    def _fake_invoke(model_name, messages, response_model=None, **kwargs):
        if messages[0]["content"].startswith("Tu fusionnes"):
            raise RuntimeError("merge boom")
        return _stub_review("S1")

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _fake_invoke)
    monkeypatch.setattr(lr, "_resolve_review_context", lambda *_a, **_kw: 4096)

    review, _used_docs, _labels = lr.generate_literature_review(
        _FRENCH_REQUEST,
        docs=[_long_doc(i) for i in range(1, 7)],
        model_name="dummy",
    )

    assert isinstance(review, LiteratureReview)
    assert review.synthesis.strip()


def test_generate_literature_review_retries_an_empty_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr
    from src.rag.literature_review import LiteratureReview

    calls: list[str] = []

    def _fake_invoke(model_name, messages, response_model=None, **kwargs):
        calls.append(messages[0]["content"])
        # Small local models sometimes return a valid but empty review.
        return LiteratureReview(overview="", synthesis="") if len(calls) == 1 else _stub_review("S1")

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _fake_invoke)
    monkeypatch.setattr(lr, "_resolve_review_context", lambda *_a, **_kw: 32768)

    review, _used_docs, _labels = lr.generate_literature_review(
        _FRENCH_REQUEST,
        docs=[_long_doc(1)],
        model_name="dummy",
    )

    assert len(calls) == 2
    assert review.to_markdown("fr").strip()


def test_generate_literature_review_retries_the_empty_structured_output_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr

    calls: list[str] = []

    def _fake_invoke(model_name, messages, response_model=None, **kwargs):
        calls.append(messages[0]["content"])
        if len(calls) == 1:
            raise RuntimeError("Echec structured output via Ollama natif: all structured fields were empty")
        return _stub_review("S1")

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _fake_invoke)
    monkeypatch.setattr(lr, "_resolve_review_context", lambda *_a, **_kw: 32768)

    review, _used_docs, _labels = lr.generate_literature_review(
        _FRENCH_REQUEST, docs=[_long_doc(1)], model_name="dummy"
    )

    assert len(calls) == 2
    assert review.to_markdown("fr").strip()


def test_generate_literature_review_keeps_other_llm_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr

    def _fake_invoke(model_name, messages, response_model=None, **kwargs):
        raise RuntimeError("Ollama unreachable")

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _fake_invoke)
    monkeypatch.setattr(lr, "_resolve_review_context", lambda *_a, **_kw: 32768)

    with pytest.raises(RuntimeError, match="Ollama unreachable"):
        lr.generate_literature_review(_FRENCH_REQUEST, docs=[_long_doc(1)], model_name="dummy")


def test_merge_reviews_locally_dedupes_and_sorts_chronology() -> None:
    from src.rag.literature_review import LiteratureReview, _merge_reviews_locally

    merged = _merge_reviews_locally(
        [
            LiteratureReview(
                overview="Premier apercu",
                chronology=["2015 — b [S2]", "1998 — a [S1]"],
                synthesis="Un.",
            ),
            LiteratureReview(
                overview="Second apercu",
                chronology=["1998 — a [S1]", "2021 — c [S3]"],
                synthesis="Deux.",
            ),
        ]
    )

    assert merged.overview == "Premier apercu"
    assert merged.chronology == ["1998 — a [S1]", "2015 — b [S2]", "2021 — c [S3]"]
    assert merged.synthesis == "Un. Deux."


def test_generate_literature_review_sends_a_context_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr
    from src.rag.literature_review import LiteratureReview

    captured: dict[str, Any] = {}

    def _fake_invoke(model_name, messages, response_model=None, **kwargs):
        captured.update(kwargs)
        return LiteratureReview(overview="Apercu", synthesis="Synthese [S1].")

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _fake_invoke)

    lr.generate_literature_review(
        _FRENCH_REQUEST,
        docs=[_doc("contenu", article_id="p1", zotero_title="Paper")],
        model_name="dummy",
    )

    assert captured["context_tokens_hint"] > 0


def test_stream_literature_review_reports_batch_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr
    from src.rag import query_engine as qe
    from src.rag import state
    from src.rag.literature_review import LiteratureReview

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    docs = [_long_doc(i) for i in range(1, 7)]
    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: docs)
    monkeypatch.setattr(lr, "_resolve_review_context", lambda *_a, **_kw: 4096)
    monkeypatch.setattr(
        "src.llm_provider.invoke_structured_chat",
        lambda *a, **kw: LiteratureReview(overview="Apercu", synthesis="Synthese [S1]."),
    )

    events = list(
        lr.stream_literature_review(_FRENCH_REQUEST, model_name="m", threshold=0.0)
    )
    statuses = [e["msg"] for e in events if e.get("type") == "status"]

    assert any("lot 1/" in msg for msg in statuses)
    assert any("Fusion" in msg for msg in statuses)
    assert any(e.get("type") == "token" for e in events)


# --- Output language --------------------------------------------------------


def test_resolve_review_language_prefers_an_explicit_language_directive() -> None:
    from src.rag.literature_review import resolve_review_language

    assert (
        resolve_review_language(
            _FRENCH_REQUEST + " Réponds en anglais s'il te plaît.", "fr"
        )
        == "en"
    )
    assert resolve_review_language(_ENGLISH_REQUEST + " In French, please.", "en") == "fr"


def test_resolve_review_language_detects_the_language_of_the_request() -> None:
    from src.rag.literature_review import resolve_review_language

    assert resolve_review_language(_FRENCH_REQUEST) == "fr"
    assert resolve_review_language(_ENGLISH_REQUEST) == "en"
    # The request itself outranks a conflicting preference.
    assert resolve_review_language(_ENGLISH_REQUEST, "fr") == "en"
    assert resolve_review_language(_FRENCH_REQUEST, "en") == "fr"


def test_resolve_review_language_falls_back_to_preference_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr

    monkeypatch.setattr(lr, "_settings_review_language", lambda: "")
    assert lr.resolve_review_language(_AMBIGUOUS_REQUEST) == "fr"
    assert lr.resolve_review_language(_AMBIGUOUS_REQUEST, "en") == "en"
    assert lr.resolve_review_language(_AMBIGUOUS_REQUEST, "fr-FR") == "fr"

    monkeypatch.setattr(lr, "_settings_review_language", lambda: "en")
    assert lr.resolve_review_language(_AMBIGUOUS_REQUEST) == "en"


def _stream_with_stub(
    monkeypatch: pytest.MonkeyPatch,
    request: str,
    review: Any,
    *,
    docs: list[Document] | None = None,
    captures: list[dict[str, Any]] | None = None,
    **stream_kwargs: Any,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    """Run ``stream_literature_review`` against a stubbed LLM call."""
    from src.rag import literature_review as lr
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    payload_docs = docs if docs is not None else [
        _doc("Paper content", article_id="p1", zotero_title="Paper One",
             zotero_year="2020", source="p1.pdf", page="1"),
    ]
    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: payload_docs)

    def _fake_invoke(model_name, messages, response_model=None, **kwargs):
        if captures is not None:
            captures.append(
                {
                    "prompt": messages[0]["content"],
                    "schema": json.dumps(
                        response_model.model_json_schema(), ensure_ascii=False
                    ),
                }
            )
        return review

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _fake_invoke)

    events = list(
        lr.stream_literature_review(request, model_name="dummy", threshold=0.0, **stream_kwargs)
    )
    markdown = "".join(str(e.get("text") or "") for e in events if e.get("type") == "token")
    statuses = [str(e.get("msg") or "") for e in events if e.get("type") == "status"]
    errors = [str(e.get("msg") or "") for e in events if e.get("type") == "error"]
    assert not errors, errors
    return markdown, statuses, captures or []


def _stub_review_for(language: str):
    from src.rag.literature_review import LiteratureReview, ReviewTheme

    if language == "en":
        return LiteratureReview(
            overview="Pulsed-field magnetization dominates the field [S1].",
            chronology=["2015 — first milestone [S1]"],
            themes=[
                ReviewTheme(
                    title="Waveform control",
                    summary="Shaping the pulse limits heat [S1].",
                    sources=["S1"],
                )
            ],
            synthesis="Waveform control is the agreed direction [S1].",
        )
    return LiteratureReview(
        overview="La magnétisation par champ pulsé domine le domaine [S1].",
        chronology=["2015 — premier jalon [S1]"],
        themes=[
            ReviewTheme(
                title="Contrôle de forme d'onde",
                summary="Maîtriser l'impulsion limite l'échauffement [S1].",
                sources=["S1"],
            )
        ],
        synthesis="Le contrôle de forme d'onde fait consensus [S1].",
    )


def test_french_request_produces_one_consistent_french_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[dict[str, Any]] = []
    markdown, _statuses, _ = _stream_with_stub(
        monkeypatch, _FRENCH_REQUEST, _stub_review_for("fr"), captures=captures
    )

    assert "## Vue d'ensemble" in markdown
    assert "## Synthèse" in markdown
    assert "## Overview" not in markdown
    assert "## Synthesis" not in markdown
    assert "La magnétisation par champ pulsé" in markdown

    prompt = captures[0]["prompt"]
    assert "Langue de sortie" in prompt
    assert "Output language" not in prompt
    assert "Aperçu en 2 ou 3 phrases" in captures[0]["schema"]
    assert "Ce que ce groupe d'articles défend" in captures[0]["schema"]
    assert "2-3 sentence overview" not in captures[0]["schema"]
    assert "What this group of papers argues" not in captures[0]["schema"]


def test_english_request_produces_one_consistent_english_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[dict[str, Any]] = []
    markdown, statuses, _ = _stream_with_stub(
        monkeypatch, _ENGLISH_REQUEST, _stub_review_for("en"), captures=captures
    )

    assert "## Overview" in markdown
    assert "## Synthesis" in markdown
    assert "## Vue d'ensemble" not in markdown
    assert "## Synthèse" not in markdown
    assert "Pulsed-field magnetization dominates" in markdown
    assert any("passages retrieved" in msg for msg in statuses)

    prompt = captures[0]["prompt"]
    assert "Output language" in prompt
    assert "Langue de sortie" not in prompt
    assert "2-3 sentence overview" in captures[0]["schema"]
    assert "What this group of papers argues" in captures[0]["schema"]
    assert "Aperçu en 2 ou 3 phrases" not in captures[0]["schema"]
    assert "Ce que ce groupe d'articles défend" not in captures[0]["schema"]


def test_batched_review_keeps_prompts_progress_and_render_in_one_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import literature_review as lr

    monkeypatch.setattr(lr, "_resolve_review_context", lambda *_a, **_kw: 4096)
    captures: list[dict[str, Any]] = []
    markdown, statuses, _ = _stream_with_stub(
        monkeypatch,
        _ENGLISH_REQUEST,
        _stub_review_for("en"),
        docs=[_long_doc(i) for i in range(1, 7)],
        captures=captures,
    )

    assert len(captures) > 2, "the small context must force the batched path"
    assert all("Output language" in item["prompt"] for item in captures)
    assert all("Langue de sortie" not in item["prompt"] for item in captures)
    assert all("2-3 sentence overview" in item["schema"] for item in captures)
    assert all("Aperçu en 2 ou 3 phrases" not in item["schema"] for item in captures)
    assert any("batch 1/" in msg for msg in statuses)
    assert any("Merging the" in msg for msg in statuses)
    assert not any("Contexte limité" in msg for msg in statuses)
    assert "## Overview" in markdown
    assert "## Vue d'ensemble" not in markdown


def test_explicit_english_directive_wins_over_a_french_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _FRENCH_REQUEST + " Réponds en anglais."
    captures: list[dict[str, Any]] = []
    markdown, statuses, _ = _stream_with_stub(
        monkeypatch, request, _stub_review_for("en"), captures=captures
    )

    assert "Output language" in captures[0]["prompt"]
    assert any("passages retrieved" in msg for msg in statuses)
    assert "## Overview" in markdown
    assert "## Vue d'ensemble" not in markdown


def test_review_prompt_carries_no_english_model_profile_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audited model profile must not inject English text into the prompt."""
    from src.model_profiles import (
        get_chat_response_note,
        needs_two_step_json,
        resolve_model_family,
    )

    # ``ornith-1.5:9b`` is a 9B model: it stays on the single-call native
    # structured path, so the review prompt is exactly what this module builds.
    assert resolve_model_family("ornith-1.5:9b") == "generic"
    assert needs_two_step_json("ornith-1.5:9b") is False

    captures: list[dict[str, Any]] = []
    _stream_with_stub(
        monkeypatch, _FRENCH_REQUEST, _stub_review_for("fr"), captures=captures
    )
    note = get_chat_response_note("ornith-1.5:9b")
    assert note.strip(), "the generic profile note should exist"
    assert note not in captures[0]["prompt"]


def test_stop_during_the_synthesis_reaches_the_llm_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop while the structured call runs: the call gets the signal and the
    stream ends without an error event (the endpoint saves the interruption)."""
    from src.llm_provider import StreamAbortSignal
    from src.rag import literature_review as lr
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()
    fake_docs = [_doc("Paper one content", article_id="p1", zotero_title="Paper One",
                      zotero_year="2020", source="p1.pdf", page="1")]
    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: fake_docs)
    signal = StreamAbortSignal()
    seen: dict[str, Any] = {}

    def _stopped_call(*args: Any, abort_signal: Any = None, **kwargs: Any):
        seen["abort_signal"] = abort_signal
        signal.abort("user_stop")
        abort_signal.throw_if_aborted()

    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", _stopped_call)

    events = list(
        lr.stream_literature_review(
            "revue sur les pertes AC", model_name="dummy", threshold=0.0, abort_signal=signal
        )
    )

    assert seen["abort_signal"] is signal
    assert [e["type"] for e in events if e["type"] in {"token", "error", "sources"}] == []
