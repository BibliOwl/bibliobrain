"""Integration-style tests for stream_chat_api.

The pipeline is heavy (Chroma, LangChain, Ollama). We stub the I/O-bound
collaborators (init_rag, _retrieve_documents, the LLM, the wiki path) and
assert on the event sequence yielded by the generator.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def query_engine_module(monkeypatch: pytest.MonkeyPatch):
    """Load query_engine with a clean state object and patchable internals."""
    from src.rag import query_engine, state

    with state._state_lock:
        state.document_chain = None
        state.vectorstore = None
        state.last_sources = []
    query_engine._consume_pipeline_metrics()
    query_engine._reranker_consume_metrics()

    yield query_engine

    query_engine._consume_pipeline_metrics()
    with state._state_lock:
        state.document_chain = None
        state.vectorstore = None
        state.last_sources = []


def _drain(gen) -> list[dict[str, Any]]:
    return list(gen)


def _prepare_streaming_rag(
    qe: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    guarded_text: str,
) -> None:
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    docs = [
        qe.Document(
            page_content="Grounded source content.",
            metadata={"source": "paper.pdf", "rel_path": "paper.pdf", "page": 1},
        )
    ]
    monkeypatch.setattr(qe, "_retrieve_documents", lambda *args, **kwargs: list(docs))
    monkeypatch.setattr(qe, "_stream_reranker_warmup_status", lambda *args, **kwargs: iter(()))
    monkeypatch.setattr(qe, "_build_reranker_metrics_event", lambda: None)
    monkeypatch.setattr(qe, "_consume_pipeline_metrics", lambda: [])

    def _no_visual_enrichment(*args: Any, **kwargs: Any):
        if False:
            yield None
        return list(docs)

    monkeypatch.setattr(qe, "_stream_auto_visual_enrichment", _no_visual_enrichment)
    monkeypatch.setattr(
        qe,
        "validate_chat_output",
        lambda *args, **kwargs: SimpleNamespace(
            severity="warning",
            normalized_text=guarded_text,
        ),
    )
    monkeypatch.setattr(qe, "strip_invalid_citations", lambda text, **kwargs: text)
    monkeypatch.setattr(qe, "_filter_citation_docs_by_answer", lambda answer, sources: sources)
    monkeypatch.setattr(qe, "format_sources_api", lambda sources: [{"title": "Paper"}])
    monkeypatch.setattr(qe, "_build_anchor_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        qe,
        "sanitize_assistant_content",
        lambda text, *, thinking_enabled=False: str(text or ""),
    )


@pytest.mark.parametrize("language", ["fr", "en"])
@pytest.mark.parametrize("message", ["", " \n\t", "??? 🥐", "??? 🦄", "...!!!", "\u200b\ufeff"])
def test_stream_chat_api_clarifies_noise_before_any_rag_work(
    query_engine_module, monkeypatch: pytest.MonkeyPatch, message: str, language: str
) -> None:
    qe = query_engine_module
    qe.state.last_sources = [qe.Document(page_content="Previous answer's source")]

    def unexpected(*args, **kwargs):
        raise AssertionError("Noise must not initialise RAG, retrieve, or generate")

    for name in ("init_rag", "_retrieve_documents", "stream_ollama_chat", "create_chat_llm"):
        monkeypatch.setattr(qe, name, unexpected)
    monkeypatch.setattr(qe, "_current_ui_language", lambda: language)
    events = list(qe.stream_chat_api(message, "dummy-model", 0.0))
    expected = (
        "Please provide a question or a topic to search for."
        if language == "en"
        else "Précisez votre question ou le sujet à rechercher."
    )
    assert events == [
        {"type": "token", "text": expected},
        {"type": "sources", "data": [], "answer_origin": "clarification"},
    ]
    assert qe.state.last_sources == []


@pytest.mark.parametrize(
    "message", ["x", "B?", "77 K", "-0.19 T", "∇", "∫", "+", "°", "%", "α", "温度", "E=mc²"]
)
def test_short_scientific_queries_still_reach_rag(
    query_engine_module, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    qe = query_engine_module
    monkeypatch.setattr(qe, "init_rag", lambda *args, **kwargs: (False, "reached RAG"))
    assert list(qe.stream_chat_api(message, "dummy-model", 0.0)) == [
        {"type": "error", "msg": "reached RAG"}
    ]


@pytest.mark.parametrize("language", ["fr", "en"])
def test_chat_prompt_preserves_non_positional_labels(
    query_engine_module, monkeypatch: pytest.MonkeyPatch, language: str
) -> None:
    qe = query_engine_module
    monkeypatch.setattr(qe, "_current_ui_language", lambda: language)
    docs = [qe.Document(page_content="A result.", metadata={"ref_label": "S9"})]
    messages = qe.build_chat_messages("What is the result?", docs, docs)
    prompt = "\n".join(m["content"] for m in messages)
    assert "[S9]" in prompt
    assert "Ne cree jamais de source comme [S9]" not in prompt


def test_stream_chat_api_yields_error_when_init_fails(
    query_engine_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If init_rag fails, the generator must yield a single error event and stop."""
    qe = query_engine_module

    def _fake_init(model_name, threshold, force_reload=False):
        return False, "boom: vectorstore unavailable"

    monkeypatch.setattr(qe, "init_rag", _fake_init)

    events = _drain(
        qe.stream_chat_api(
            message="hello",
            model_name="dummy-model",
            threshold=0.0,
        )
    )

    assert events == [{"type": "error", "msg": "boom: vectorstore unavailable"}]


def test_stream_chat_api_no_results_path(
    query_engine_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When retrieval returns no docs, generator yields status→fallback token→sources."""
    qe = query_engine_module
    from src.rag import state

    # Pretend RAG is already initialized so init_rag is never called.
    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: [])

    def _boom_init(*a, **kw):
        raise AssertionError("init_rag should not be called when state is ready")

    monkeypatch.setattr(qe, "init_rag", _boom_init)

    events = _drain(
        qe.stream_chat_api(
            message="anything",
            model_name="dummy-model",
            threshold=0.0,
        )
    )

    types = [e.get("type") for e in events]
    assert "status" in types
    functional_events = [e for e in events if e.get("type") != "pipeline_metrics"]
    assert [e.get("type") for e in functional_events[-2:]] == ["token", "sources"]

    sources_event = functional_events[-1]
    assert sources_event["type"] == "sources"
    assert sources_event["data"] == []
    assert sources_event.get("answer_origin") == "rag"

    token_event = functional_events[-2]
    assert token_event["type"] == "token"
    fallback_text = token_event["text"].lower()
    assert (
        "ne trouve pas" in fallback_text
        or "filtres" in fallback_text
        or "cannot find" in fallback_text
        or "filters" in fallback_text
    )


def test_stream_chat_api_no_results_explains_missing_paper_index(
    query_engine_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    qe = query_engine_module
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: [])
    # The wording follows the UI language saved on the machine running the tests.
    monkeypatch.setattr(qe, "_current_ui_language", lambda: "en")
    events = _drain(
        qe.stream_chat_api(
            message="describe this paper",
            model_name="dummy-model",
            threshold=0.0,
            source_paths=["papers/unreadable.pdf"],
        )
    )

    token_event = next(event for event in events if event.get("type") == "token")
    assert "no indexed text is available" in token_event["text"].lower()
    assert "rephrasing" not in token_event["text"].lower()


def test_stream_chat_api_streams_local_draft_then_replaces_guarded_answer(
    query_engine_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qe = query_engine_module
    final_answer = "Final grounded answer [S1]."
    _prepare_streaming_rag(qe, monkeypatch, guarded_text=final_answer)
    monkeypatch.setattr(qe, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(qe, "is_remote_provider", lambda: False)
    monkeypatch.setattr(qe, "resolve_chat_think_mode", lambda *args, **kwargs: True)

    def _fake_stream(*args: Any, **kwargs: Any):
        yield {"thinking": "Reasoning", "content": ""}
        yield {"thinking": "", "content": "Draft "}
        yield {"thinking": "", "content": "answer [S1]."}

    monkeypatch.setattr(qe, "stream_ollama_chat", _fake_stream)

    events = _drain(
        qe.stream_chat_api(
            message="Question",
            model_name="local-model",
            threshold=0.0,
            thinking_enabled=True,
        )
    )

    token_texts = [event["text"] for event in events if event.get("type") == "token"]
    assert token_texts == [
        "<think>",
        "Reasoning",
        "</think>\n\n",
        "Draft ",
        "answer [S1].",
    ]
    replacement = next(event for event in events if event.get("type") == "answer_replace")
    assert replacement["text"] == f"<think>Reasoning</think>\n\n{final_answer}"
    assert [event["type"] for event in events].index("answer_replace") < [
        event["type"] for event in events
    ].index("sources")


def test_stream_chat_api_streams_openai_compatible_provider_without_structured_buffering(
    query_engine_module,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qe = query_engine_module
    final_answer = "Remote answer [S1]."
    _prepare_streaming_rag(qe, monkeypatch, guarded_text=final_answer)
    monkeypatch.setattr(qe, "is_ollama_provider", lambda: False)
    monkeypatch.setattr(qe, "is_remote_provider", lambda: True)
    monkeypatch.setattr(qe, "create_chat_llm", lambda model_name: object())

    captured: dict[str, Any] = {}

    def _fake_stream_text(llm: Any, prompt: Any, *, abort_signal: Any = None):
        captured["prompt"] = prompt
        yield "Remote "
        yield "answer [S1]."

    monkeypatch.setattr(qe, "stream_text", _fake_stream_text)
    monkeypatch.setattr(
        qe,
        "invoke_structured_chat",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("structured blocking generation must not be used")
        ),
    )

    events = _drain(
        qe.stream_chat_api(
            message="Question",
            model_name="remote-model",
            threshold=0.0,
        )
    )

    assert [event["text"] for event in events if event.get("type") == "token"] == [
        "Remote ",
        "answer [S1].",
    ]
    assert not any(event.get("type") == "answer_replace" for event in events)
    assert captured["prompt"][0]["role"] == "system"
    assert captured["prompt"][1]["role"] == "user"


def test_stream_chat_api_init_lock_serializes_concurrent_calls(
    query_engine_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent generators must not both invoke init_rag (TOCTOU fix #6)."""
    qe = query_engine_module
    from src.rag import state

    call_count = {"n": 0}

    def _fake_init(model_name, threshold, force_reload=False):
        call_count["n"] += 1
        with state._state_lock:
            state.document_chain = object()
            state.vectorstore = object()
        return True, ""

    monkeypatch.setattr(qe, "init_rag", _fake_init)
    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: [])

    # Reset state so first generator triggers init.
    with state._state_lock:
        state.document_chain = None
        state.vectorstore = None

    gen1 = qe.stream_chat_api(message="a", model_name="m", threshold=0.0)
    gen2 = qe.stream_chat_api(message="b", model_name="m", threshold=0.0)

    # Drive both generators interleaved.
    _drain(gen1)
    _drain(gen2)

    assert call_count["n"] == 1, "init_rag should run exactly once across concurrent calls"


def test_extract_inline_filters_keeps_explicit_author_query(query_engine_module) -> None:
    qe = query_engine_module

    cleaned, filters = qe._extract_inline_filters("auteur: Grilli pertes AC")

    assert cleaned == "pertes AC"
    assert filters["authors"] == ["Grilli"]


def test_extract_inline_filters_detects_natural_written_by_author(query_engine_module) -> None:
    qe = query_engine_module

    cleaned, filters = qe._extract_inline_filters(
        "je cherche des articles qui parlent de pertes AC écrit par Grilli"
    )

    assert "pertes AC" in cleaned
    assert "Grilli" not in cleaned
    assert filters["authors"] == ["Grilli"]


def test_extract_inline_filters_detects_lowercase_written_by_author(
    query_engine_module,
) -> None:
    qe = query_engine_module

    cleaned, filters = qe._extract_inline_filters("Cherche des articles écrits par moreau")

    assert cleaned == "Cherche des articles"
    assert filters["authors"] == ["moreau"]


def test_extract_inline_filters_detects_natural_author_noun(query_engine_module) -> None:
    qe = query_engine_module

    cleaned, filters = qe._extract_inline_filters(
        "je cherche un fit de l'auteur Fleiter sur les rubans HTS Fujikura"
    )

    assert "rubans HTS Fujikura" in cleaned
    assert "Fleiter" not in cleaned
    assert filters["authors"] == ["Fleiter"]


def test_extract_inline_filters_detects_author_worked_on_topic(query_engine_module) -> None:
    qe = query_engine_module

    cleaned, filters = qe._extract_inline_filters(
        "est-ce que Moreau à travailler sur les bulk HTS ?"
    )

    assert "bulk HTS" in cleaned
    assert "Moreau" not in cleaned
    assert filters["authors"] == ["Moreau"]


def test_extract_inline_filters_detects_multiple_natural_authors(query_engine_module) -> None:
    qe = query_engine_module

    cleaned, filters = qe._extract_inline_filters("articles sur les pertes AC écrit par Grilli et Moreau")

    assert "pertes AC" in cleaned
    assert filters["authors"] == ["Grilli", "Moreau"]


def test_extract_inline_filters_keeps_full_author_name(query_engine_module) -> None:
    qe = query_engine_module

    cleaned, filters = qe._extract_inline_filters(
        "Quels sont les articles publiés par Etienne Rochepault ?"
    )

    assert filters["authors"] == ["Etienne Rochepault"]
    assert "Rochepault" not in cleaned


def test_extract_inline_filters_reads_name_after_self_pronoun(query_engine_module) -> None:
    qe = query_engine_module

    cleaned, filters = qe._extract_inline_filters(
        "Quels sont les articles publiés par moi (Etienne Rochepault) ?"
    )

    assert filters["authors"] == ["Etienne Rochepault"]
    assert "moi" not in cleaned


def test_doc_matches_full_author_name_in_last_first_order(query_engine_module) -> None:
    qe = query_engine_module
    metadata = {"zotero_authors": "Rochepault, Etienne; Devaux, Michel"}

    assert qe._doc_matches_filters(metadata, {"authors": ["Etienne Rochepault"]})
    assert not qe._doc_matches_filters(metadata, {"authors": ["Etienne Durand"]})


def test_extract_inline_filters_ignores_non_author_par_clause(query_engine_module) -> None:
    qe = query_engine_module

    cleaned, filters = qe._extract_inline_filters("modelisation par elements finis des pertes AC")

    assert cleaned == "modelisation par elements finis des pertes AC"
    assert filters["authors"] == []


def test_doc_matches_author_filters_across_all_authors(query_engine_module) -> None:
    qe = query_engine_module
    metadata = {
        "zotero_first_author_lc": "grilli",
        "zotero_authors": "Grilli, Rossi, Garcia et al.",
        "zotero_authors_lc": "grilli | rossi | garcía | de moreau",
    }

    assert qe._doc_matches_filters(metadata, {"authors": ["Grilli"]})
    assert qe._doc_matches_filters(metadata, {"authors": ["Moreau"]})
    assert qe._doc_matches_filters(metadata, {"authors": ["garcia"]})
    assert not qe._doc_matches_filters(metadata, {"authors": ["Brandt"]})


def test_doc_matches_exact_selected_article_path(query_engine_module) -> None:
    qe = query_engine_module
    metadata = {
        "source": "X:\\Zotero\\storage\\ABCD1234\\Paper.pdf",
        "rel_path": "ABCD1234\\Paper.pdf",
    }

    assert qe._doc_matches_filters(metadata, {"sources": ["abcd1234/Paper.pdf"]})
    assert not qe._doc_matches_filters(metadata, {"sources": ["OTHER/Paper.pdf"]})


def test_stream_chat_api_forwards_selected_article_filter(
    query_engine_module, monkeypatch: pytest.MonkeyPatch
) -> None:
    qe = query_engine_module
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    captured: dict[str, Any] = {}

    def _fake_retrieve(query, threshold, max_chunks, filters, **kwargs):
        captured["filters"] = filters
        return []

    monkeypatch.setattr(qe, "_retrieve_documents", _fake_retrieve)

    _drain(
        qe.stream_chat_api(
            message="Quelle méthode est utilisée ?",
            model_name="dummy-model",
            threshold=0.3,
            source_paths=["ABCD1234/Paper.pdf"],
        )
    )

    assert captured["filters"]["sources"] == ["ABCD1234/Paper.pdf"]


@pytest.mark.parametrize(("selected", "expected"), [(1, 6), (3, 6), (9, 9), (30, 12)])
def test_stream_chat_api_gives_one_passage_per_selected_article(
    query_engine_module, monkeypatch: pytest.MonkeyPatch, selected: int, expected: int
) -> None:
    qe = query_engine_module
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()
    monkeypatch.setattr(state, "_max_chunks_default", 6)
    monkeypatch.setattr(state, "_CHAT_MAX_CHUNKS_CAP", 8)

    captured: dict[str, Any] = {}

    def _fake_retrieve(query, threshold, max_chunks, filters, **kwargs):
        captured["max_chunks"] = max_chunks
        return []

    monkeypatch.setattr(qe, "_retrieve_documents", _fake_retrieve)

    _drain(
        qe.stream_chat_api(
            message="Compare the methods",
            model_name="dummy-model",
            threshold=0.3,
            source_paths=[f"P{i}/Paper.pdf" for i in range(selected)],
        )
    )

    assert captured["max_chunks"] == expected


def test_selected_article_vector_search_accepts_browser_path_separators(
    query_engine_module,
) -> None:
    qe = query_engine_module
    from src.rag import state

    class FilteredStore:
        def __init__(self) -> None:
            self.filters: list[dict[str, str]] = []

        def similarity_search_with_relevance_scores(self, query, k, filter):
            self.filters.append(filter)
            if filter == {"rel_path": "ABCD1234\\Paper.pdf"}:
                return [
                    (
                        qe.Document(
                            page_content="Focused content",
                            metadata={"rel_path": "ABCD1234\\Paper.pdf", "page": 1},
                        ),
                        0.8,
                    )
                ]
            return []

    store = FilteredStore()
    with state._state_lock:
        state.vectorstore = store

    rows = qe._collect_source_scored_docs(
        "method",
        k=4,
        source_paths=["ABCD1234/Paper.pdf"],
    )

    assert len(rows) == 1
    assert {"rel_path": "ABCD1234\\Paper.pdf"} in store.filters


def test_zotero_metadata_stores_full_author_filter_key() -> None:
    from src.rag.zotero_integration import _build_item_metadata
    from src.zotero_db import ZoteroItem

    item = ZoteroItem(
        title="Many Authors",
        authors=["Grilli", "Rossi", "Garcia", "De Moreau"],
    )

    metadata = _build_item_metadata("paper.pdf", "X:/paper.pdf", item)

    assert metadata["zotero_authors"] == "Grilli, Rossi, Garcia et al."
    assert metadata["zotero_authors_lc"] == "grilli | rossi | garcia | de moreau"
