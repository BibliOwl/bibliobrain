"""Retrieval changes measured on 2026-10-14 with src/retrieval_benchmark.py.

One pool per bilingual question and one rerank, copies of a passage kept out
of the reranker's pool, the previous question added only to follow-ups, and
the lexical scan's pure helpers cached.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.documents import Document

from src.rag import query_engine as qe


def _doc(text: str, **meta: Any) -> Document:
    return Document(page_content=text, metadata=meta)


@pytest.mark.parametrize(
    "question",
    [
        "Et quels résultats expérimentaux obtiennent-ils ?",
        "And what current speed did they assume?",
        "Quelle poussée ont-ils mesurée ?",
        "What are its limitations?",
        "Pourquoi ?",
        "Des exemples ?",
        "Que dit cet article sur les pertes ?",
        "Does it change the critical current?",
    ],
)
def test_follow_ups_are_recognised(question: str) -> None:
    assert qe._is_follow_up_question(question)


@pytest.mark.parametrize(
    "question",
    [
        "Comment réduire l'aimantation des bobinages réalisés en rubans HTS ?",
        "Qu'est-ce que le quench ?",
        "Définis la formulation H",
        "Résume ce que dit Grilli sur les formulations",
        "Un plasma d'air généré par micro-ondes peut-il servir à la propulsion ?",
        "How does an HTS magnet without turn insulation behave when it is cooled by conduction?",
        "Existe-t-il une méthode rapide pour calculer le champ de conducteurs en arc ?",
    ],
)
def test_questions_naming_their_subject_stand_alone(question: str) -> None:
    assert not qe._is_follow_up_question(question)


@pytest.mark.parametrize("message", ["test", "Merci !", "ok", "Bonjour"])
def test_greetings_and_tests_are_not_follow_ups(message: str) -> None:
    # "test" after the library overview used to re-run it with the old
    # question, and the model repeated its previous answer.
    assert not qe._is_follow_up_question(message)


def test_previous_question_is_added_to_follow_ups_only() -> None:
    history = [
        {"role": "user", "content": "Comment fonctionne la pompe de flux cylindrique ?"},
        {"role": "assistant", "content": "Elle ..."},
    ]

    follow_up = qe._build_retrieval_query("Et quels résultats obtiennent-ils ?", history)
    switch = qe._build_retrieval_query(
        "Comment réduire l'aimantation des bobinages en rubans HTS ?", history
    )

    assert "pompe de flux cylindrique" in follow_up
    assert switch == "Comment réduire l'aimantation des bobinages en rubans HTS ?"


def test_french_question_reranks_both_pools_once(monkeypatch: pytest.MonkeyPatch) -> None:
    pools = {
        "Qu'est-ce que l'écrantage ?": [_doc(f"fr {i}", source=f"fr{i}.pdf") for i in range(12)],
        "What is screening?": [_doc(f"en {i}", source=f"en{i}.pdf") for i in range(12)],
    }
    singles: list[tuple[str, bool]] = []
    reranks: list[list[str]] = []

    def single(query, threshold, max_chunks, filters, *, pool_only=False, **kwargs):
        singles.append((query, pool_only))
        return list(pools[query])

    def rerank(query, docs, top_k, **kwargs):
        reranks.append([doc.page_content for doc in docs])
        return docs[:top_k]

    monkeypatch.setattr(qe, "_retrieve_documents_single", single)
    monkeypatch.setattr(qe, "_needs_english_retrieval_query", lambda query: True)
    monkeypatch.setattr(qe, "_english_retrieval_query", lambda query: "What is screening?")
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_rerank_documents", rerank)

    docs = qe._retrieve_documents("Qu'est-ce que l'écrantage ?", 0.3, 6, {}, depth="quick")

    assert singles == [("Qu'est-ce que l'écrantage ?", True), ("What is screening?", True)]
    assert len(reranks) == 1
    # Whole pools: the 12th French candidate is judged too.
    assert len(reranks[0]) == 24 and "fr 11" in reranks[0]
    assert len(docs) == 6


def test_copies_of_a_passage_take_one_place_in_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "Screening currents reduce the central field of the magnet."
    vector = [
        (_doc(text, source="copy1/paper.pdf", page="3"), 0.8),
        (_doc(text, source="copy2/paper.pdf", page="3"), 0.79),
        *[(_doc(f"other passage {i}", source=f"o{i}.pdf", page="1"), 0.7) for i in range(10)],
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: vector[:k])
    monkeypatch.setattr(qe, "_collect_lexical_metadata_docs", lambda *args, **kwargs: [])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_needs_english_retrieval_query", lambda query: False)
    seen: list[str] = []

    def rerank(query, docs, top_k, **kwargs):
        seen.extend(doc.page_content for doc in docs)
        return docs[:top_k]

    monkeypatch.setattr(qe, "_rerank_documents", rerank)

    qe._retrieve_documents("screening currents", 0.3, 4, {})

    assert seen.count(text) == 1


def test_fuzzy_overlap_matches_near_spellings_only() -> None:
    query = frozenset({"homogenization", "contact", "resistivity"})

    assert qe._fuzzy_token_overlap(query, frozenset({"homogenisation", "model"})) == 1
    assert qe._fuzzy_token_overlap(query, frozenset({"magnet", "quench"})) == 0
