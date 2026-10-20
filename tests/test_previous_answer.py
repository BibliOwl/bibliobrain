"""Questions about the previous answer are answered from its sources."""

from __future__ import annotations

from typing import Any

import pytest

from src.chat_intents import refers_to_previous_answer
from src.rag import query_engine as qe


def _history(sources: list[dict[str, Any]], *, mode: str = "paper_list") -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "Liste tous les articles sur les pompes de flux"},
        {"role": "assistant", "content": "...", "meta": {"mode": mode, "sources": sources}},
    ]


def _sources(count: int, *, snippet: str = "A passage.") -> list[dict[str, Any]]:
    return [
        {
            "ref_label": f"S{index}",
            "title": f"Flux pump paper {index}",
            "authors": "Coombs, Tim",
            "author": "Coombs, Tim",
            "year": "2016",
            "snippet": snippet,
            "file": f"p{index}.pdf",
        }
        for index in range(1, count + 1)
    ]


@pytest.mark.parametrize(
    "question",
    [
        "Extrais les sujets de ces articles et classe-les",
        "Classe ces articles par type d'approche",
        "Lesquels parmi eux sont expérimentaux ?",
        "Résume la liste",
        "Liste les thèmes de ces articles",
        "Summarize these papers",
        "Group them by year",
        # Right after a list, no need to name it.
        "Fais un résumé global",
        "Quels sont les grands thèmes ?",
        "tu peux faire un résumé des sujets abordés ?",
        "Résume les articles cités",
        "Classe les articles que tu m'as listés",
    ],
)
def test_questions_about_the_previous_answer(question: str) -> None:
    assert refers_to_previous_answer(question, _history(_sources(3)))


@pytest.mark.parametrize(
    "question",
    [
        "Et sur le quench ?",
        "Est-ce qu'il y en a d'autres ?",
        "Résume l'article de Grilli sur les formulations",
        "Comment fonctionne une pompe de flux ?",
        # Without accents the imperative reads like "cités"/"listés".
        "cite moi tout les articles de Camilla",
        "Liste les articles de Moreau",
        "Donne-moi la liste des articles de Moreau",
    ],
)
def test_new_questions_after_a_list(question: str) -> None:
    assert not refers_to_previous_answer(question, _history(_sources(3)))


def test_only_an_answer_with_sources_can_be_referred_to() -> None:
    assert not refers_to_previous_answer("Classe ces articles", _history([]))
    # An analysis without "ces articles" only follows a list.
    assert not refers_to_previous_answer("Fais un résumé global", _history(_sources(3), mode="rag"))
    assert refers_to_previous_answer("Classe ces articles", _history(_sources(3), mode="rag"))


def test_context_holds_every_source_with_its_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "_previous_answer_char_budget", lambda model: 100_000)

    docs, note, omitted = qe._previous_answer_context(
        "Classe ces articles", _history(_sources(40)), "m"
    )

    assert [doc.metadata["ref_label"] for doc in docs] == [f"S{i}" for i in range(1, 41)]
    assert "A passage." in docs[0].page_content and omitted == 0
    assert "40 sources" in note
    guide = qe._build_source_citation_guide(qe._pin_source_ref_labels(docs))
    assert "[S40]" in guide and guide.index("[S1]") < guide.index("[S9]")


def test_passages_go_first_then_sources_when_the_window_is_small(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sources = _sources(40, snippet="x" * 300)
    monkeypatch.setattr(qe, "_previous_answer_char_budget", lambda model: 4000)

    docs, _note, omitted = qe._previous_answer_context(
        "Classe ces articles", _history(sources), "m"
    )

    # Passages dropped first: every source still there.
    assert len(docs) == 40 and omitted == 0
    assert all("x" * 300 not in doc.page_content for doc in docs)

    monkeypatch.setattr(qe, "_previous_answer_char_budget", lambda model: 1500)
    docs, note, omitted = qe._previous_answer_context("Classe ces articles", _history(sources), "m")

    assert 0 < len(docs) < 40 and omitted == 40 - len(docs)
    assert f"{len(docs)} premieres" in note


def test_chat_answers_from_the_previous_sources_without_searching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()

    def no_search(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("no new retrieval for a question about the previous answer")

    prompts: list[str] = []

    def fake_stream(model, messages, **kwargs):
        prompts.append(messages[-1]["content"])
        yield {"content": "Deux groupes : [S1] et [S12].", "done": True}

    monkeypatch.setattr(qe, "retrieve_for_question", no_search)
    monkeypatch.setattr(qe, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(qe, "stream_ollama_chat", fake_stream)
    monkeypatch.setattr(qe, "_chat_context_options", lambda *args: None)
    monkeypatch.setattr(qe, "_previous_answer_char_budget", lambda model: 100_000)

    events = list(
        qe.stream_chat_api(
            "Classe ces articles par thème", "m", 0.3, conversation_history=_history(_sources(12))
        )
    )

    assert "12 sources de ta reponse precedente" in prompts[0]
    sources = next(event for event in events if event.get("type") == "sources")["data"]
    assert [source["ref_label"] for source in sources] == ["S1", "S12"]
    assert any(
        event.get("stage") == "previous_answer"
        for event in events
        if event["type"] == "pipeline_metrics"
    )
