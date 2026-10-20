from __future__ import annotations

import time
from typing import Any

import pytest
from langchain_core.documents import Document


def _doc(content: str, **meta: Any) -> Document:
    return Document(page_content=content, metadata=meta)


def test_dedupe_docs_by_paper_prefers_article_identity() -> None:
    from src.rag.paper_list import dedupe_docs_by_paper

    docs = [
        _doc("a1", article_id="p1", zotero_title="Title A", source="a.pdf", page=1),
        _doc("a2", article_id="p1", zotero_title="Title A", source="a.pdf", page=2),
        _doc("b1", zotero_doi="10.123/ABC", zotero_title="Title B", source="b.pdf"),
        _doc("b2", zotero_doi="10.123/abc", zotero_title="Other B", source="b2.pdf"),
        _doc("c1", zotero_title="A Normalized: Title!", source="c.pdf"),
        _doc("c2", zotero_title="a normalized title", source="c2.pdf"),
    ]

    deduped = dedupe_docs_by_paper(docs)

    assert [doc.page_content for doc in deduped] == ["a1", "b1", "c1"]


def test_stream_timed_operation_emits_elapsed_status(monkeypatch) -> None:
    from src.rag import paper_list as pl

    monkeypatch.setattr(pl, "PAPER_LIST_STATUS_INTERVAL_SECONDS", 0.06)

    def slow_operation() -> str:
        time.sleep(0.13)
        return "done"

    operation = pl._stream_timed_operation(
        slow_operation,
        stage="test",
        initial_message="Starting",
        waiting_message="Still running",
        language="en",
    )
    events: list[dict[str, Any]] = []
    while True:
        try:
            events.append(next(operation))
        except StopIteration as stop:
            result, elapsed = stop.value
            break

    assert result == "done"
    assert elapsed >= 0.12
    assert events[0]["msg"] == "Starting"
    assert any("Still running" in event["msg"] for event in events[1:])
    assert any("elapsed" in event["msg"] for event in events[1:])
    assert any(event["elapsed_seconds"] > 0 for event in events[1:])


def test_build_paper_list_markdown_is_deterministic() -> None:
    from src.rag.paper_list import build_paper_list_markdown

    docs = [
        _doc(
            "This paper discusses AC losses in superconducting magnets with H formulation.",
            article_id="p1",
            zotero_title="AC Losses in HTS Magnets",
            zotero_authors="Doe; Smith",
            zotero_year="2024",
            zotero_doi="10.123/hts",
            source="papers/hts.pdf",
            page=3,
        )
    ]

    markdown = build_paper_list_markdown("HTS AC losses", docs)

    assert "1. **AC Losses in HTS Magnets**" in markdown
    assert "[S1]" in markdown
    assert "Preuve:" in markdown
    assert "fichier:" not in markdown
    assert "DOI:" not in markdown


def test_build_paper_list_markdown_uses_llm_summary() -> None:
    from src.rag.paper_list import build_paper_list_markdown

    docs = [
        _doc(
            "This paper discusses AC losses in superconducting magnets.",
            article_id="p1",
            ref_label="S1",
            zotero_title="AC Losses in HTS Magnets",
            source="papers/hts.pdf",
            page=3,
        )
    ]

    markdown = build_paper_list_markdown(
        "HTS AC losses",
        docs,
        summaries={"S1": "S1 parle des pertes AC dans les aimants HTS."},
    )

    assert "S1 parle des pertes AC" in markdown
    assert "Preuve:" not in markdown
    assert "fichier:" not in markdown
    assert "page: 3" not in markdown


def test_build_paper_summary_prompt_includes_all_labels_and_query() -> None:
    from src.rag.paper_list import build_paper_summary_prompt

    docs = [
        _doc("content one", ref_label="S1", zotero_title="Paper One", source="p1.pdf"),
        _doc("content two", ref_label="S2", zotero_title="Paper Two", source="p2.pdf"),
    ]

    prompt, labels = build_paper_summary_prompt("HTS AC losses", docs)

    assert labels == ["S1", "S2"]
    assert "HTS AC losses" in prompt
    assert "Respecte exactement les labels fournis: S1, S2" in prompt
    assert "relevant=true" in prompt
    assert "energies renouvelables" in prompt
    assert "[S1]" in prompt
    assert "[S2]" in prompt


def test_build_paper_summary_prompt_uses_requested_english_language() -> None:
    from src.rag.paper_list import build_paper_summary_prompt

    docs = [
        _doc("content one", ref_label="S1", zotero_title="Paper One", source="p1.pdf"),
    ]

    prompt, labels = build_paper_summary_prompt("HTS AC losses", docs, language="en")

    assert labels == ["S1"]
    assert "Strict rules:" in prompt
    assert "Write the summary sentence in English" in prompt
    assert "Requete utilisateur" not in prompt


def test_incomplete_paper_list_request_detects_missing_author() -> None:
    from src.rag.paper_list import incomplete_paper_list_request_message

    message = incomplete_paper_list_request_message("Cherche des articles écrits par ...")

    assert "auteur" in message.lower()
    assert "exemple" not in message.lower()


def test_incomplete_paper_list_request_detects_missing_topic() -> None:
    from src.rag.paper_list import incomplete_paper_list_request_message

    message = incomplete_paper_list_request_message("Liste tous les articles qui parlent de ...")

    assert "sujet" in message.lower()


def test_incomplete_paper_list_request_accepts_author_only_search() -> None:
    from src.rag.paper_list import incomplete_paper_list_request_message

    assert incomplete_paper_list_request_message("Cherche des articles ecrits par Grilli") == ""


def test_author_only_paper_list_keeps_original_query_for_retrieval() -> None:
    from src.rag.paper_list import _retrieval_query_for_filters

    query = _retrieval_query_for_filters(
        user_query="Cherche des articles écrits par Moreau*Copy\nAI",
        cleaned_message="Cherche des articles *Copy\nAI",
        filters={"authors": ["Moreau"], "tags": [], "collections": [], "years": [], "dois": []},
    )

    assert query == "Cherche des articles écrits par Moreau*Copy\nAI"


def test_direct_query_evidence_handles_typo_and_cross_language_topic_aliases() -> None:
    from src.rag.paper_list import _has_direct_query_evidence

    solar = _doc(
        "Photovoltaic generation for a renewable power grid.",
        zotero_title="Solar power systems",
    )
    hts = _doc(
        "Optimisation et protection de bobines supraconductrices.",
        zotero_title="NI-MI HTS coils",
    )

    query = "Liste tous les articles sur les energies renouvlables"
    assert _has_direct_query_evidence(query, solar) is True
    assert _has_direct_query_evidence(query, hts) is False


def test_validate_paper_summaries_rejects_missing_or_unknown_labels() -> None:
    from src.rag.paper_list import (
        PaperListSummaryItem,
        PaperListSummaryResponse,
        validate_paper_summaries,
    )

    ok = validate_paper_summaries(
        PaperListSummaryResponse(
            items=[
                PaperListSummaryItem(label="S1", relevant=True, summary="S1 parle du sujet."),
                PaperListSummaryItem(label="S2", relevant=True, summary="S2 mentionne le sujet."),
            ]
        ),
        ["S1", "S2"],
    )
    assert ok == {"S1": "S1 parle du sujet.", "S2": "S2 mentionne le sujet."}

    missing = validate_paper_summaries(
        PaperListSummaryResponse(
            items=[PaperListSummaryItem(label="S1", relevant=True, summary="ok")]
        ),
        ["S1", "S2"],
    )
    assert missing is None

    unknown = validate_paper_summaries(
        PaperListSummaryResponse(
            items=[
                PaperListSummaryItem(label="S1", relevant=True, summary="ok"),
                PaperListSummaryItem(label="S3", relevant=True, summary="invented"),
            ]
        ),
        ["S1", "S2"],
    )
    assert unknown is None


def test_validate_paper_summaries_excludes_candidates_marked_irrelevant() -> None:
    from src.rag.paper_list import (
        PaperListSummaryItem,
        PaperListSummaryResponse,
        validate_paper_summaries,
    )

    summaries = validate_paper_summaries(
        PaperListSummaryResponse(
            items=[
                PaperListSummaryItem(
                    label="S1",
                    relevant=False,
                    summary="Le texte parle d'energie, sans lien avec les renouvelables.",
                ),
                PaperListSummaryItem(
                    label="S2",
                    relevant=True,
                    summary="L'article etudie directement la production solaire.",
                ),
            ]
        ),
        ["S1", "S2"],
    )

    assert summaries == {"S2": "L'article etudie directement la production solaire."}


def test_select_relevant_papers_relabels_filtered_sources_without_gaps() -> None:
    from src.rag.paper_list import _select_relevant_papers

    docs = [
        _doc("irrelevant", ref_label="S1", zotero_title="Other"),
        _doc("solar", ref_label="S2", zotero_title="Solar systems"),
    ]

    selected, summaries = _select_relevant_papers(
        docs, {"S2": "S2: etudie directement la production solaire."}
    )

    assert [doc.metadata["zotero_title"] for doc in selected] == ["Solar systems"]
    assert selected[0].metadata["ref_label"] == "S1"
    assert summaries == {"S1": "etudie directement la production solaire."}


def test_generate_paper_summaries_uses_structured_llm(monkeypatch: Any) -> None:
    from src.rag import paper_list as pl
    from src.rag.paper_list import PaperListSummaryItem, PaperListSummaryResponse

    captured: dict[str, Any] = {}

    def fake_invoke(model_name, messages, response_model=None, **kwargs):
        captured["model_name"] = model_name
        captured["messages"] = messages
        captured["response_model"] = response_model
        captured["kwargs"] = kwargs
        return PaperListSummaryResponse(
            items=[
                PaperListSummaryItem(label="S1", relevant=True, summary="S1 parle des pertes AC.")
            ]
        )

    monkeypatch.setattr("src.llm_provider.is_ollama_provider", lambda: False)
    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", fake_invoke)

    summaries = pl.generate_paper_summaries(
        "HTS AC losses",
        [_doc("content", ref_label="S1", zotero_title="Paper", source="p.pdf")],
        model_name="demo-model",
    )

    assert summaries == {"S1": "S1 parle des pertes AC."}
    assert captured["model_name"] == "demo-model"
    assert captured["response_model"] is PaperListSummaryResponse
    assert captured["kwargs"]["temperature"] == 0.0
    assert "N'omets aucune source" in captured["messages"][0]["content"]


def test_generate_paper_summaries_uses_bounded_abortable_ollama_stream(
    monkeypatch: Any,
) -> None:
    from src.llm_provider import StreamAbortSignal
    from src.rag import paper_list as pl

    captured: dict[str, Any] = {}
    signal = StreamAbortSignal()

    def fake_stream(model_name, messages, **kwargs):
        captured["model_name"] = model_name
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        yield {
            "content": (
                '{"items":[{"label":"S1","relevant":true,'
                '"summary":"Le paper traite des pertes AC."}]}'
            )
        }

    monkeypatch.setattr("src.llm_provider.is_ollama_provider", lambda: True)
    monkeypatch.setattr("src.llm_provider.stream_ollama_chat", fake_stream)

    summaries = pl.generate_paper_summaries(
        "HTS AC losses",
        [_doc("content", ref_label="S1", zotero_title="Paper", source="p.pdf")],
        model_name="gemma4:12b",
        abort_signal=signal,
    )

    assert summaries == {"S1": "Le paper traite des pertes AC."}
    assert captured["model_name"] == "gemma4:12b"
    assert captured["kwargs"]["abort_signal"] is signal
    assert captured["kwargs"]["format_schema"]["type"] == "object"
    assert captured["kwargs"]["options_override"]["num_predict"] == 280


def _batch_docs(count: int) -> list[Document]:
    return [
        _doc("content", ref_label=f"S{i}", zotero_title=f"Paper {i}", source=f"p{i}.pdf")
        for i in range(1, count + 1)
    ]


def _answer_for(prompt: str, *, skip: frozenset[str] = frozenset()) -> str:
    import json
    import re

    match = re.search(r"labels fournis: ([^.\n]+)\.", prompt)
    assert match is not None
    labels = match.group(1).split(", ")
    return json.dumps(
        {
            "items": [
                {"label": label, "relevant": True, "summary": f"{label} ok."}
                for label in labels
                if label not in skip
            ]
        }
    )


def test_batch_summary_schema_forces_one_item_per_label() -> None:
    from src.rag import paper_list as pl

    schema = pl._batch_summary_schema(["S5", "S6", "S7"])

    items = schema["properties"]["items"]
    assert items["minItems"] == items["maxItems"] == 3
    label = schema["$defs"]["PaperListSummaryItem"]["properties"]["label"]
    assert label["enum"] == ["S5", "S6", "S7"]
    # The shared model schema stays untouched for other callers.
    assert "minItems" not in pl.PaperListSummaryResponse.model_json_schema()["properties"]["items"]


def test_generate_paper_summaries_assesses_small_batches(monkeypatch: Any) -> None:
    from src.rag import paper_list as pl

    prompts: list[str] = []

    def fake_stream(model_name, messages, **kwargs):
        prompts.append(messages[0]["content"])
        yield {"content": _answer_for(messages[0]["content"])}

    monkeypatch.setattr("src.llm_provider.is_ollama_provider", lambda: True)
    monkeypatch.setattr("src.llm_provider.stream_ollama_chat", fake_stream)

    summaries = pl.generate_paper_summaries("HTS", _batch_docs(10), model_name="granite4.2:3b")

    assert len(prompts) == 3  # batches of 4, 4 and 2
    assert summaries is not None
    assert sorted(summaries) == sorted(f"S{i}" for i in range(1, 11))
    assert summaries.unverified == []


def test_generate_paper_summaries_keeps_verified_batches_when_one_fails(monkeypatch: Any) -> None:
    from src.rag import paper_list as pl

    def fake_stream(model_name, messages, **kwargs):
        prompt = messages[0]["content"]
        # The model keeps skipping S6: that batch stays incomplete after the retry.
        yield {"content": _answer_for(prompt, skip=frozenset({"S6"}))}

    monkeypatch.setattr("src.llm_provider.is_ollama_provider", lambda: True)
    monkeypatch.setattr("src.llm_provider.stream_ollama_chat", fake_stream)

    summaries = pl.generate_paper_summaries("HTS", _batch_docs(8), model_name="granite4.2:3b")

    assert summaries is not None
    assert sorted(summaries) == ["S1", "S2", "S3", "S4"]
    assert summaries.unverified == ["S5", "S6", "S7", "S8"]
    assert "could not assess 4 candidate(s)" in pl._unverified_note(summaries, "en")


def test_generate_paper_summaries_falls_back_on_llm_error(monkeypatch: Any) -> None:
    from src.rag import paper_list as pl

    def fake_invoke(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("src.llm_provider.is_ollama_provider", lambda: False)
    monkeypatch.setattr("src.llm_provider.invoke_structured_chat", fake_invoke)

    summaries = pl.generate_paper_summaries(
        "HTS AC losses",
        [_doc("content", ref_label="S1", zotero_title="Paper", source="p.pdf")],
        model_name="demo-model",
    )

    assert summaries is None


def test_stream_paper_list_full_flow(monkeypatch) -> None:
    from src.llm_provider import StreamAbortSignal
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    captured: dict[str, Any] = {}
    fake_docs = [
        _doc(
            "HTS chunk 1",
            article_id="p1",
            zotero_title="Paper One",
            source="p1.pdf",
            page=1,
        ),
        _doc(
            "HTS chunk 2",
            article_id="p1",
            zotero_title="Paper One",
            source="p1.pdf",
            page=2,
        ),
        _doc(
            "HTS chunk 3",
            article_id="p2",
            zotero_title="Paper Two",
            source="p2.pdf",
            page=5,
        ),
    ]

    def fake_retrieve(query, threshold, max_chunks, filters, **kwargs):
        captured["query"] = query
        captured["threshold"] = threshold
        captured["max_chunks"] = max_chunks
        captured["filters"] = filters
        captured["use_reranker"] = kwargs.get("use_reranker")
        return fake_docs

    monkeypatch.setattr(qe, "_retrieve_documents", fake_retrieve)
    monkeypatch.setattr(qe, "_stream_reranker_warmup_status", lambda: iter(()))
    monkeypatch.setattr(qe, "_build_reranker_metrics_event", lambda: None)
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    def fake_generate_summaries(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured["summary_abort_signal"] = kwargs.get("abort_signal")
        return {"S1": "S1 parle de HTS.", "S2": "S2 mentionne HTS."}

    monkeypatch.setattr(pl, "generate_paper_summaries", fake_generate_summaries)
    abort_signal = StreamAbortSignal()

    events = list(
        pl.stream_paper_list(
            "liste tous les papers HTS",
            model_name="dummy",
            threshold=0.2,
            target_chunks=80,
            abort_signal=abort_signal,
        )
    )

    assert captured["max_chunks"] == 40
    assert captured.get("use_reranker") is False
    assert any(event.get("type") == "token" for event in events)
    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "S1 parle de HTS" in text
    assert "S2 mentionne HTS" in text
    sources_event = next(event for event in events if event.get("type") == "sources")
    assert sources_event["answer_origin"] == "paper_list"
    assert len(sources_event["data"]) == 2
    assert captured["summary_abort_signal"] is abort_signal


def test_stream_paper_list_limits_llm_summaries_to_first_twelve(monkeypatch) -> None:
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    fake_docs = [
        _doc(
            f"HTS chunk {index}",
            article_id=f"p{index}",
            zotero_title=f"Paper {index}",
            source=f"p{index}.pdf",
            page=1,
        )
        for index in range(15)
    ]
    captured: dict[str, Any] = {}

    def fake_retrieve(*args, **kwargs):
        captured["use_reranker"] = kwargs.get("use_reranker")
        return fake_docs

    monkeypatch.setattr(qe, "_retrieve_documents", fake_retrieve)
    monkeypatch.setattr(qe, "_stream_reranker_warmup_status", lambda: iter(()))
    monkeypatch.setattr(qe, "_build_reranker_metrics_event", lambda: None)
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    def fake_generate_summaries(_query: str, docs: list[Any], **_kwargs: Any):
        captured["summary_doc_count"] = len(docs)
        return {
            f"S{index}": f"S{index} traite directement de HTS." for index in range(1, len(docs) + 1)
        }

    monkeypatch.setattr(pl, "generate_paper_summaries", fake_generate_summaries)

    events = list(
        pl.stream_paper_list(
            "liste les papers HTS",
            model_name="dummy",
            threshold=0.2,
            target_chunks=32,
        )
    )

    assert captured["summary_doc_count"] == 12
    assert captured["use_reranker"] is False
    sources_event = next(event for event in events if event.get("type") == "sources")
    assert len(sources_event["data"]) == 12
    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "liste prudente n'est donc pas exhaustive" in text


def _topic_list_setup(monkeypatch, chunks, scores):
    """A reranker scoring each candidate paper; vector search returns ``chunks``."""
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe
    from src.rag import reranker, state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()
        state.runtime_db_path = ""
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_has_reranker_predict_error", lambda: False)
    monkeypatch.setattr(qe, "_build_reranker_metrics_event", lambda: None)
    monkeypatch.setattr(
        qe, "_collect_scored_docs", lambda query, k, excluded_paths=None: [(c, 0.7) for c in chunks]
    )
    seen: dict[str, Any] = {}

    def fake_rerank_texts(query, texts, top_k=None):
        seen["query"] = query
        seen["texts"] = list(texts)
        ranked = [(index, scores[text.split("\n")[0]]) for index, text in enumerate(texts)]
        reranker._record_metrics({"applied": True})
        return sorted(ranked, key=lambda item: item[1], reverse=True)

    monkeypatch.setattr(reranker, "rerank_texts", fake_rerank_texts)
    monkeypatch.setattr(pl, "english_topic", lambda topic, model, titles=(): "")
    return seen


def test_topic_list_keeps_every_relevant_paper_once_in_two_tiers(monkeypatch) -> None:
    from src.rag import paper_list as pl

    chunks = [
        _doc("pump p1", article_id="a1", zotero_title="Flux Pump One", source="1.pdf"),
        # A second copy of the same paper: one entry.
        _doc("pump p1", article_id="a1b", zotero_title="Flux Pump One", source="1b.pdf"),
        _doc("pump p2", article_id="a2", zotero_title="Flux Pump Two", source="2.pdf"),
        _doc("dynamo", article_id="a3", zotero_title="Superconducting Dynamo", source="3.pdf"),
        _doc("other", article_id="a4", zotero_title="Cryostat Design", source="4.pdf"),
    ] + [
        _doc(f"pump {i}", article_id=f"x{i}", zotero_title=f"Flux Pump {i}", source=f"x{i}.pdf")
        for i in range(3, 20)
    ]
    scores = {
        "Flux Pump One": 8.0,
        "Flux Pump Two": 7.5,
        "Superconducting Dynamo": 4.2,
        "Cryostat Design": -1.0,
        **{f"Flux Pump {i}": 6.0 for i in range(3, 20)},
    }
    _topic_list_setup(monkeypatch, chunks, scores)

    events = list(
        pl.stream_paper_list(
            "List all papers about flux pumps", model_name="m", threshold=0.3, language="en"
        )
    )

    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    titles = [
        source["title"] for source in next(e for e in events if e["type"] == "sources")["data"]
    ]
    # No cap at 12: the 19 relevant papers, then the possibly related one.
    assert len(titles) == 20
    assert titles[:2] == ["Flux Pump One", "Flux Pump Two"]
    assert titles[-1] == "Superconducting Dynamo"
    assert titles.count("Flux Pump One") == 1
    assert "Cryostat Design" not in titles
    assert "19 papers of your library deal with « flux pumps »" in text
    assert "Possibly related (1)" in text


def test_topic_absent_from_the_library_lists_nothing(monkeypatch) -> None:
    from src.rag import paper_list as pl

    chunks = [_doc("magnets", article_id="a1", zotero_title="Magnet Design", source="1.pdf")]
    _topic_list_setup(monkeypatch, chunks, {"Magnet Design": -1.75})

    events = list(
        pl.stream_paper_list(
            "Liste tous les articles sur le machine learning", model_name="m", threshold=0.3
        )
    )

    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "ne semble traiter de « machine learning »" in text
    assert next(e for e in events if e["type"] == "sources")["data"] == []


def test_french_topic_is_reranked_in_the_library_wording(monkeypatch) -> None:
    from src.rag import paper_list as pl

    chunks = [
        _doc("pump", article_id="a1", zotero_title="An Overview of Flux Pumps", source="1.pdf")
    ]
    seen = _topic_list_setup(monkeypatch, chunks, {"An Overview of Flux Pumps": 8.0})
    asked: dict[str, Any] = {}

    def fake_english_topic(topic, model, titles=()):
        asked.update(topic=topic, titles=list(titles))
        return "flux pumps"

    monkeypatch.setattr(pl, "english_topic", fake_english_topic)

    list(pl.stream_paper_list("Liste tous les articles sur les pompes de flux", "m", 0.3))

    assert asked == {"topic": "pompes de flux", "titles": ["An Overview of Flux Pumps"]}
    assert seen["query"] == "flux pumps"


def test_stream_paper_list_author_only_without_zotero_lists_indexed_papers(monkeypatch) -> None:
    from src import library
    from src.rag import lexical_index, state
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe

    # No library metadata: the list must come from the index. Set it here rather than
    # rely on earlier tests having left no Zotero library behind.
    monkeypatch.setattr(
        library, "find_library_articles_by_authors", lambda *_args, **_kwargs: ([], False)
    )
    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()
        state.runtime_db_path = "index"
    monkeypatch.setattr(
        lexical_index,
        "file_metadata",
        lambda path: [
            {
                "zotero_title": "Paper by Moreau",
                "zotero_authors": "De Moreau",
                "zotero_year": "2020",
            },
            {"zotero_title": "Paper by Smith", "zotero_authors": "Smith", "zotero_year": "2021"},
        ],
    )

    def no_retrieval(*args, **kwargs):
        raise AssertionError("the index metadata answers an author list")

    monkeypatch.setattr(qe, "_retrieve_documents", no_retrieval)

    events = list(
        pl.stream_paper_list(
            "Cherche des articles écrits par Moreau", model_name="m", threshold=0.0
        )
    )

    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "Paper by Moreau" in text
    assert "Paper by Smith" not in text


def test_whole_library_is_listed_once_per_paper_newest_first(monkeypatch) -> None:
    from src.rag import lexical_index, state
    from src.rag import paper_list as pl

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()
        state.runtime_db_path = "index"
    monkeypatch.setattr(
        lexical_index,
        "file_metadata",
        lambda path: [
            {"zotero_title": "Old Paper", "zotero_year": "2001", "rel_path": "a.pdf"},
            {"zotero_title": "New Paper", "zotero_year": "2024", "rel_path": "b.pdf"},
            {"zotero_title": "New Paper", "zotero_year": "2024", "rel_path": "b-copy.pdf"},
            {"zotero_title": "A note", "content_type": "zotero_note", "rel_path": "n"},
        ],
    )

    events = list(pl.stream_paper_list("Liste tous mes articles", model_name="m", threshold=0.3))
    only_2024 = list(pl.stream_paper_list("Liste tous mes articles de 2024", "m", 0.3))

    titles = [s["title"] for s in next(e for e in events if e["type"] == "sources")["data"]]
    assert titles == ["New Paper", "Old Paper"]
    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "contient 2 articles" in text
    assert [s["title"] for s in next(e for e in only_2024 if e["type"] == "sources")["data"]] == [
        "New Paper"
    ]


def test_stream_paper_list_author_only_uses_metadata_without_initializing_rag(monkeypatch) -> None:
    from src import library
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = None
        state.vectorstore = None
        state.last_sources = []

    monkeypatch.setattr(
        library,
        "find_library_articles_by_authors",
        lambda authors, indexed_only=True: (
            [
                {
                    "source": "papers/moreau.pdf",
                    "title": "Paper by Moreau",
                    "authors": ["Claire Moreau", "Alice Example"],
                    "year": "2026",
                    "journal": "Journal",
                    "doi": "10.1234/moreau",
                    "tags": [],
                    "collections": [],
                    "citation": "Claire Moreau (2026). Paper by Moreau. Journal.",
                    "article_id": "ITEM1",
                }
            ],
            True,
        ),
    )

    def fail_init(*_args, **_kwargs):
        raise AssertionError("author-only metadata lookup must not initialize RAG")

    monkeypatch.setattr(qe, "init_rag", fail_init)

    events = list(
        pl.stream_paper_list(
            "Cherche des articles écrits par moreau",
            model_name="ornith:9b",
            threshold=0.3,
        )
    )

    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "Paper by Moreau" in text
    sources = next(event for event in events if event.get("type") == "sources")
    assert sources["data"][0]["authors"] == "Claire Moreau, Alice Example"
    metrics = next(event for event in events if event.get("type") == "pipeline_metrics")
    assert metrics["data"]["metadata_only"] is True


def test_stream_paper_list_author_followup_leaves_out_papers_already_listed(monkeypatch) -> None:
    from src import library
    from src.rag import paper_list as pl

    records = [
        {
            "source": f"papers/{key}.pdf",
            "title": title,
            "authors": ["Claire Moreau"],
            "article_id": key,
        }
        for key, title in (("ITEM1", "First paper"), ("ITEM2", "Second paper"))
    ]
    monkeypatch.setattr(
        library,
        "find_library_articles_by_authors",
        lambda authors, indexed_only=True: (records, True),
    )

    def answer(exclude_sources):
        events = list(
            pl.stream_paper_list(
                "Find papers written by moreau",
                model_name="ornith:9b",
                threshold=0.3,
                language="en",
                exclude_sources=exclude_sources,
            )
        )
        text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
        sources = next(event for event in events if event.get("type") == "sources")["data"]
        return text, sources

    text, sources = answer([{"article_id": "ITEM1", "title": "First paper"}])
    assert "First paper" not in text
    assert "Second paper" in text and "[S1]" in text
    assert "1 paper(s) already listed above were left out." in text
    assert [source["article_id"] for source in sources] == ["ITEM2"]

    text, sources = answer([{"article_id": "ITEM1"}, {"article_id": "ITEM2"}])
    assert "No other paper by this author" in text
    assert "First paper" not in text and "Second paper" not in text
    assert sources == []


def test_stream_paper_list_excludes_semantically_close_but_irrelevant_candidates(
    monkeypatch,
) -> None:
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()
        state.last_sources = [_doc("stale source")]

    candidates = [
        _doc(
            "Echanges d'energie en physique fondamentale.",
            article_id="p1",
            zotero_title="Les analogies en physique",
            source="analogies.pdf",
        ),
        _doc(
            "Caracterisation electrique de supraconducteurs.",
            article_id="p2",
            zotero_title="Methodes de caracterisation des supraconducteurs",
            source="hts.pdf",
        ),
    ]
    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: candidates)
    monkeypatch.setattr(qe, "_stream_reranker_warmup_status", lambda: iter(()))
    monkeypatch.setattr(qe, "_build_reranker_metrics_event", lambda: None)
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)
    monkeypatch.setattr(pl, "generate_paper_summaries", lambda *a, **kw: {})

    events = list(
        pl.stream_paper_list(
            "Liste tous les articles qui parlent d'energies renouvlables",
            model_name="ornith:9b",
            threshold=0.3,
        )
    )

    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    sources_event = next(event for event in events if event.get("type") == "sources")
    metrics = next(
        event
        for event in events
        if event.get("type") == "pipeline_metrics" and event.get("stage") == "paper_list"
    )
    assert "Aucun article directement pertinent" in text
    assert "Les analogies en physique" not in text
    assert sources_event["data"] == []
    assert metrics["data"]["candidate_papers"] == 2
    assert metrics["data"]["unique_papers"] == 0
    assert metrics["data"]["relevance_verified"] is True
    with state._state_lock:
        assert state.last_sources == []


def test_stream_paper_list_fails_closed_when_relevance_check_fails(monkeypatch) -> None:
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    monkeypatch.setattr(
        qe,
        "_retrieve_documents",
        lambda *a, **kw: [
            _doc(
                "production solaire",
                article_id="p1",
                zotero_title="Candidate solaire",
                source="p.pdf",
            )
        ],
    )
    monkeypatch.setattr(qe, "_stream_reranker_warmup_status", lambda: iter(()))
    monkeypatch.setattr(qe, "_build_reranker_metrics_event", lambda: None)
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)
    monkeypatch.setattr(pl, "generate_paper_summaries", lambda *a, **kw: None)

    events = list(pl.stream_paper_list("articles solaires", model_name="m", threshold=0.3))

    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    sources_event = next(event for event in events if event.get("type") == "sources")
    assert "pertinence n'a pas pu etre verifiee" in text
    assert sources_event["data"] == []


def test_stream_paper_list_failure_message_uses_requested_english_language(monkeypatch) -> None:
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    monkeypatch.setattr(
        qe,
        "_retrieve_documents",
        lambda *a, **kw: [
            _doc(
                "coated conductor manufacturing",
                article_id="p1",
                zotero_title="Candidate paper",
                source="p.pdf",
            )
        ],
    )
    monkeypatch.setattr(qe, "_stream_reranker_warmup_status", lambda: iter(()))
    monkeypatch.setattr(qe, "_build_reranker_metrics_event", lambda: None)
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)
    monkeypatch.setattr(pl, "generate_paper_summaries", lambda *a, **kw: None)

    events = list(
        pl.stream_paper_list(
            "List all papers about coated conductors",
            model_name="m",
            threshold=0.3,
            language="en",
        )
    )

    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    statuses = [event.get("msg", "") for event in events if event.get("type") == "status"]
    assert "Candidate papers were found, but their relevance could not be verified." in text
    assert "Des articles candidats" not in text
    assert any("Verification complete" in message for message in statuses)


def test_stream_paper_list_no_docs_returns_friendly_message(monkeypatch) -> None:
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    monkeypatch.setattr(qe, "_retrieve_documents", lambda *a, **kw: [])
    monkeypatch.setattr(qe, "_stream_reranker_warmup_status", lambda: iter(()))
    monkeypatch.setattr(qe, "_build_reranker_metrics_event", lambda: None)
    # The LLM-checked list, used when no reranker is available.
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)

    events = list(pl.stream_paper_list("unknown topic", model_name="m", threshold=0.0))

    assert any("Aucun article" in event.get("text", "") for event in events)
    sources_event = next(event for event in events if event.get("type") == "sources")
    assert sources_event["data"] == []
    assert sources_event["answer_origin"] == "paper_list"


def test_stream_paper_list_incomplete_request_skips_retrieval(monkeypatch) -> None:
    from src.rag import paper_list as pl
    from src.rag import query_engine as qe
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    def boom_retrieve(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("retrieval should be skipped for incomplete prompts")

    monkeypatch.setattr(qe, "_retrieve_documents", boom_retrieve)

    events = list(
        pl.stream_paper_list(
            "Cherche des articles ecrits par ...",
            model_name="m",
            threshold=0.0,
        )
    )

    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "auteur" in text.lower()
    sources_event = next(event for event in events if event.get("type") == "sources")
    assert sources_event["data"] == []


def test_exclude_listed_papers_matches_any_identity() -> None:
    from src.rag.paper_list import _exclude_listed_papers

    docs = [
        _doc("a", article_id="p1", zotero_title="Paper One"),
        _doc("b", zotero_doi="10.1/X", zotero_title="Paper Two"),
        _doc("c", zotero_title="Paper: Three"),
        _doc("d", article_id="p4", zotero_title="Paper Four"),
    ]
    listed = [{"article_id": "p1"}, {"doi": "10.1/x"}, {"title": "paper three"}]

    kept, dropped = _exclude_listed_papers(docs, listed)

    assert [doc.page_content for doc in kept] == ["d"]
    assert dropped == 3
    assert _exclude_listed_papers(docs, None) == (docs, 0)


def test_lts_query_accepts_papers_that_only_name_the_conductor() -> None:
    from src.rag.paper_list import _has_direct_query_evidence

    nb3sn = _doc("Training of Nb₃Sn Rutherford cables", zotero_title="Nb3Sn training")
    rebco = _doc("REBCO pancake coil", zotero_title="REBCO coils")

    assert _has_direct_query_evidence("quels articles parlent de LTS ?", nb3sn)
    assert not _has_direct_query_evidence("quels articles parlent de LTS ?", rebco)
    assert _has_direct_query_evidence("papers about HTS", rebco)


@pytest.mark.parametrize(
    "message",
    [
        "Quels articles ai-je dans ma bibliothèque ?",
        "Combien d'articles ai-je ?",
        "Montre-moi ma bibliothèque",
        "What papers do I have?",
        "How many papers are in my library?",
    ],
)
def test_questions_about_the_library_itself_are_paper_lists(message: str) -> None:
    from src.chat_intents import detect_chat_intent

    assert detect_chat_intent(message).mode == "paper_list"


@pytest.mark.parametrize(
    ("message", "topic", "years"),
    [
        ("Liste tous mes articles", set(), []),
        ("Quels articles ai-je dans ma bibliothèque ?", set(), []),
        ("Liste tous mes articles de 2024", set(), ["2024"]),
        ("Liste tous les articles sur le quench", {"quench"}, []),
    ],
)
def test_library_request_topic(message: str, topic: set[str], years: list[str]) -> None:
    from src.rag import paper_list as pl

    assert pl.library_request_topic(message) == (topic, years)


@pytest.mark.parametrize(
    ("message", "topic"),
    [
        ("Liste tous les articles sur les pompes de flux", "pompes de flux"),
        ("List all my papers about bulk superconductors", "bulk superconductors"),
        ("Quels articles parlent du quench ?", "quench"),
    ],
)
def test_paper_list_topic(message: str, topic: str) -> None:
    from src.rag import paper_list as pl

    assert pl.paper_list_topic(message) == topic


def test_topic_translation_keeps_one_short_line() -> None:
    from src.rag import paper_list as pl

    assert pl._valid_topic_translation('"Flux pumps".\nBecause the titles say so.') == "Flux pumps"
    assert (
        pl._valid_topic_translation("Translation: bulk superconductors") == "bulk superconductors"
    )
    assert pl._valid_topic_translation(" ".join(["word"] * 20)) == ""
