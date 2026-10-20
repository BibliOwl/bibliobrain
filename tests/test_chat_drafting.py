from __future__ import annotations

from typing import Any

import pytest

from src import chat_drafting as drafting
from src.chat_harness import intent_from_llm_tool_choice, plan_chat_tools
from src.chat_intents import detect_chat_intent
from src.llm_provider import StreamAbortSignal


def test_drafting_plan_and_llm_choice() -> None:
    intent = detect_chat_intent("Rédige une introduction. Plan : discussion et références.")
    plan = plan_chat_tools(intent)
    assert plan.primary_tool_name == "chat.draft"
    assert plan.action == "execute"
    choice = intent_from_llm_tool_choice("Reformulate this text", "DRAFT")
    assert choice is not None and choice.mode == "drafting"


@pytest.mark.parametrize("local", [False, True])
def test_draft_streams_supplied_context_without_retrieval(
    monkeypatch: pytest.MonkeyPatch, local: bool
) -> None:
    prompt = "Rédige une introduction.\n" + "Données expérimentales. " * 250 + "59 quenches."
    history = [{"role": "user", "content": "Champ maximal 2.95 T."}]
    captured: dict[str, Any] = {}
    signal = StreamAbortSignal()

    def remote(llm, messages, **kwargs):
        captured.update(messages=messages, kwargs=kwargs)
        yield "Une introduction "
        yield "fondée sur 59 quenches."

    def ollama(model, messages, **kwargs):
        for content in remote(model, messages, **kwargs):
            yield {"thinking": "private reasoning", "content": content}

    monkeypatch.setattr(drafting, "is_ollama_provider", lambda: local)
    monkeypatch.setattr(drafting, "create_chat_llm", lambda name: object())
    monkeypatch.setattr(drafting, "stream_text", remote)
    monkeypatch.setattr(drafting, "stream_ollama_chat", ollama)
    events = list(drafting.stream_chat_drafting(
        prompt, "test-model", previous_messages=history, abort_signal=signal
    ))
    assert captured["messages"][-1]["content"] == prompt
    assert captured["messages"][1]["content"] == history[0]["content"]
    assert captured["kwargs"]["abort_signal"] is signal
    assert "private reasoning" not in str(events)
    assert "".join(e.get("text", "") for e in events if e["type"] == "token") == (
        "Une introduction fondée sur 59 quenches."
    )
    assert events[-1] == {"type": "sources", "data": [], "answer_origin": "drafting"}


def test_drafting_keeps_long_plan_in_followup_history() -> None:
    plan = "Plan détaillé\n" + "Mesures de champ. " * 300 + "59 quenches."
    messages, sources = drafting._draft_messages(
        "Reformule cette introduction", [{"role": "user", "content": plan}], "en"
    )
    assert messages[1]["content"] == plan
    assert sources == []


def test_drafting_preserves_known_citations_and_removes_invented_labels(monkeypatch) -> None:
    source = {"title": "HTS tests", "ref_label": "S3", "snippet": "59 quenches."}
    history: list[dict[str, Any]] = [
        {"role": "assistant", "content": "59 quenches [S3].", "meta": {"sources": [source]}}
    ]
    monkeypatch.setattr(drafting, "is_ollama_provider", lambda: False)
    monkeypatch.setattr(drafting, "create_chat_llm", lambda name: object())
    monkeypatch.setattr(drafting, "stream_text", lambda *a, **k: iter(["59 quenches [S3][S99]."]))
    events = list(drafting.stream_chat_drafting(
        "Rédige une introduction", "test-model", previous_messages=history
    ))
    replacement = next(e["text"] for e in events if e["type"] == "answer_replace")
    assert "[S3]" in replacement
    assert "[S99]" not in replacement
    assert events[-1]["data"] == [source]


def test_citation_request_searches_library_from_draft_material(monkeypatch) -> None:
    from langchain_core.documents import Document

    draft = (
        "Les aimants REBCO à isolation métallique limitent les risques lors des quenches [S2]. "
        "Les pertes AC dans les bobines racetrack restent un défi majeur pour les accélérateurs."
    )
    history: list[dict[str, Any]] = [
        {"role": "user", "content": 'Rédige une introduction.\nTitre proposé\n'
            '"Experimental Characterization of a 600 mm Metal-Insulated HTS Magnet"'},
        {"role": "assistant", "content": draft, "meta": {"mode": "drafting"}},
        {"role": "assistant", "content": "Je ne peux pas ajouter de citations [S1].",
            "meta": {"mode": "rag"}},
    ]
    docs = [
        Document(page_content="Metal-insulated REBCO coils survive quenches.", metadata={
            "source": "a.pdf", "article_id": "a", "chunk_uid": "a1", "ref_label": "S1",
            "zotero_title": "MI coils"}),
        Document(page_content="AC loss in racetrack coils.", metadata={
            "source": "b.pdf", "article_id": "b", "chunk_uid": "b1", "ref_label": "S2",
            "zotero_title": "AC loss"}),
    ]
    captured: dict[str, Any] = {}

    def search(queries, model_name, **kwargs):
        captured["queries"] = queries
        yield {"type": "status", "msg": "searching"}
        yield {"type": "_library_docs", "docs": docs}

    def generate(llm, messages, **kwargs):
        captured["messages"] = messages
        yield "Les pertes AC restent un défi [S2][S7]."

    monkeypatch.setattr(drafting, "_search_library", search)
    monkeypatch.setattr(drafting, "is_ollama_provider", lambda: False)
    monkeypatch.setattr(drafting, "create_chat_llm", lambda name: object())
    monkeypatch.setattr(drafting, "stream_text", generate)
    events = list(drafting.stream_chat_drafting(
        "tu peux réécrire un abstract avec des citations pertinentes de ma bibliothèque ?",
        "test-model", previous_messages=history,
    ))

    assert captured["queries"][0].startswith("Experimental Characterization")
    assert any("pertes AC" in query for query in captured["queries"])
    assert all("citations pertinentes" not in query for query in captured["queries"])
    system = captured["messages"][0]["content"]
    assert "AC loss in racetrack coils." in system
    assert all("[S" not in m["content"] for m in captured["messages"][1:-1])
    replacement = next(e["text"] for e in events if e["type"] == "answer_replace")
    assert "[S2]" in replacement and "[S7]" not in replacement
    sources = next(e for e in events if e["type"] == "sources")
    assert [s["ref_label"] for s in sources["data"]] == ["S2"]
    assert sources["answer_origin"] == "drafting"


def test_library_search_merges_claims_round_robin(monkeypatch) -> None:
    from langchain_core.documents import Document

    from src.rag import native_chat, query_engine

    def doc(article: str, chunk: int) -> Document:
        return Document(page_content=f"{article}{chunk}", metadata={
            "source": f"{article}.pdf", "article_id": article, "chunk_uid": f"{article}{chunk}"})

    hits = {"q1": [doc("a", 1), doc("a", 2)], "q2": [doc("b", 1), doc("a", 1)]}
    monkeypatch.setattr(native_chat, "ensure_rag_ready", lambda *a: None)
    monkeypatch.setattr(query_engine, "_retrieve_documents", lambda q, *a, **k: hits[q])
    events = list(drafting._search_library(
        ["q1", "q2"], "m", threshold=0.3, tags=None, collections=None,
        language="fr", abort_signal=None,
    ))
    docs = events[-1]["docs"]
    assert [d.metadata["chunk_uid"] for d in docs] == ["a1", "b1", "a2"]
    assert [d.metadata["ref_label"] for d in docs] == ["S1", "S2", "S3"]
    assert sum(e["type"] == "status" for e in events) == 2


def test_drafting_without_citation_request_skips_library(monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise AssertionError("library search must not run")

    monkeypatch.setattr(drafting, "_search_library", fail)
    monkeypatch.setattr(drafting, "is_ollama_provider", lambda: False)
    monkeypatch.setattr(drafting, "create_chat_llm", lambda name: object())
    monkeypatch.setattr(drafting, "stream_text", lambda *a, **k: iter(["Texte."]))
    events = list(drafting.stream_chat_drafting(
        "Rédige une introduction. Plan : discussion et références.",
        "test-model", previous_messages=[],
    ))
    assert events[-1] == {"type": "sources", "data": [], "answer_origin": "drafting"}


def test_drafting_aborts_without_emitting_more_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    signal = StreamAbortSignal()
    signal.abort()
    monkeypatch.setattr(drafting, "is_ollama_provider", lambda: False)
    monkeypatch.setattr(drafting, "create_chat_llm", lambda name: object())
    monkeypatch.setattr(drafting, "stream_text", lambda *a, **k: iter(["ignored"]))
    with pytest.raises(InterruptedError):
        list(drafting.stream_chat_drafting(
            "Rédige une introduction", "test-model", previous_messages=[], abort_signal=signal
        ))
