"""Regression tests for the hybrid retrieval defects found on 2026-10-03.

Each test pins one way the lexical side used to push good semantic matches
out of the results: unscaled score fusion, hard anchors from ordinary
hyphenated words, Greek letters read inside words, and function words
counted as query terms.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.documents import Document

from src.rag import lexical_index
from src.rag import query_engine as qe


def _doc(text: str, **meta: Any) -> Document:
    return Document(page_content=text, metadata=meta)


def test_best_vector_match_reaches_the_reranker_despite_boosted_keyword_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vector = [
        (_doc(f"semantic passage {i}", source=f"v{i}.pdf", page="1"), 0.72 - i * 0.01)
        for i in range(20)
    ]
    keyword = [
        (_doc(f"keyword passage {i}", source=f"k{i}.pdf", page="1"), 1.5)
        for i in range(60)
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k: vector[:k])
    monkeypatch.setattr(qe, "_collect_lexical_metadata_docs", lambda *args, **kwargs: keyword)
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: True)
    monkeypatch.setattr(qe, "_reranker_candidate_multiplier", lambda: 4)
    seen: list[str] = []

    def fake_rerank(query, docs, top_k, **kwargs):
        seen.extend(doc.metadata["source"] for doc in docs)
        return docs[:top_k]

    monkeypatch.setattr(qe, "_rerank_documents", fake_rerank)

    qe._retrieve_documents("screening currents in coils", threshold=0.3, max_chunks=5, filters={})

    assert "v0.pdf" in seen
    assert "k0.pdf" in seen
    assert len(seen) == len(set(seen))


def test_interleave_alternates_vector_and_adjusted_orders() -> None:
    a, b, c = (_doc(name, source=f"{name}.pdf", page="1") for name in "abc")
    ranked = [(2.0, c), (1.0, b), (0.6, a)]
    vector = [(a, 0.7), (b, 0.6)]

    order = [doc.page_content for _score, doc in qe._interleave_vector_order(ranked, vector)]

    assert order == ["c", "a", "b"]


@pytest.mark.parametrize(
    "query",
    [
        "Why does an eddy current shield make voltage-based quench detection difficult?",
        "How efficient is a multi-megawatt HTS ship motor?",
        "Quels effets ont-ils sur le champ magnétique ?",
        "Voltage-based detection: does it fail?",
        "le modèle kim-anderson",
    ],
)
def test_ordinary_hyphenated_words_are_not_hard_anchors(query: str) -> None:
    assert qe._named_query_anchor_groups(query) == []


def test_named_models_stay_anchors() -> None:
    assert qe._named_query_anchor_groups("Explique-moi le modèle de Bean-Livingston") == [
        ("bean", "livingston")
    ]
    assert qe._named_query_anchor_groups("Which values does the Kim-anderson fit use?") == [
        ("kim", "anderson")
    ]


_PASTED_SENTENCE = (
    "est-ce que ce papier valide cette phrase ? Within the CERN HFM program, the "
    "exploration of Metal-Insulated (MI) HTS magnets has emerged as a promising way"
)


def _anchor_scenario(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Document, float]]:
    """Every chunk says "metal-insulation" or "MI", never "metal insulated"."""
    chunks = [
        (_doc("Metal-insulation (MI) coils tested at CERN.", rel_path="mi.pdf", source="mi.pdf", page="1"), 0.6),
        (_doc("MI HTS magnets reach high fields.", rel_path="mi.pdf", source="mi.pdf", page="2"), 0.5),
    ]
    monkeypatch.setattr(qe, "_collect_scored_docs", lambda query, k, **kwargs: chunks)
    monkeypatch.setattr(qe, "_collect_source_scored_docs", lambda query, k, source_paths: chunks)
    monkeypatch.setattr(qe, "_collect_lexical_metadata_docs", lambda *args, **kwargs: [])
    monkeypatch.setattr(qe, "_is_reranker_enabled", lambda: False)
    monkeypatch.setattr(qe, "_english_retrieval_query", lambda query: "")
    monkeypatch.setattr(qe, "excluded_rel_paths", lambda: [])
    monkeypatch.setattr(qe, "excluded_path_keys", lambda: frozenset())
    return chunks


def test_anchors_do_not_empty_a_selected_paper(monkeypatch: pytest.MonkeyPatch) -> None:
    assert qe._named_query_anchor_groups(_PASTED_SENTENCE) == [("metal", "insulated")]
    _anchor_scenario(monkeypatch)

    docs = qe._retrieve_documents(
        _PASTED_SENTENCE, threshold=0.3, max_chunks=6, filters={"sources": ["mi.pdf"]}
    )

    assert {doc.metadata["page"] for doc in docs} == {"1", "2"}


def test_anchors_that_reject_every_passage_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    _anchor_scenario(monkeypatch)

    docs = qe._retrieve_documents(_PASTED_SENTENCE, threshold=0.3, max_chunks=6, filters={})

    assert docs


@pytest.mark.parametrize(
    "query",
    [
        "Génère la bibliographie des sources citées",
        "Quelle est la formule chimique des REBCO ?",
        "le chiffre exact de la puissance",
        "How efficient is a multi-megawatt motor?",
        "pick-up coil pin number",
        "a meta-analysis through an API",
    ],
)
def test_words_containing_greek_names_are_not_symbols(query: str) -> None:
    assert qe._symbolic_alias_terms_for_query(query) == set()
    assert qe._expand_symbolic_retrieval_query(query) == query


def test_real_greek_notation_is_still_expanded() -> None:
    aliases = qe._symbolic_alias_terms_for_query("Tphi formulation, mu0 and the T-φ method")
    assert {"tphi", "t-φ", "mu0", "μ0"} <= aliases
    assert {"μ", "mu"} <= qe._symbolic_alias_terms_for_query("Valeur de μ0")


def test_function_words_are_not_query_terms() -> None:
    terms, _domain = qe._retrieval_query_terms(
        "Why does the shield and the coil make detection difficult when ramping?"
    )
    assert {"why", "does", "and", "make", "when"}.isdisjoint(terms)
    assert {"shield", "coil", "detection"} <= terms


def test_overlap_terms_must_start_a_word() -> None:
    assert qe._term_starts_word("magnet", "magnetization of the stack")
    assert not qe._term_starts_word("and", "a standard sample")
    assert not qe._term_starts_word("hts", "copyrights reserved")
    assert not qe._term_starts_word("tc", "coils, etc.")


def test_critical_quantities_are_not_critique_requests() -> None:
    assert "critique" not in qe._without_critical_quantities(
        "comment la densité de courant critique dépend-elle de la température critique ?"
    )
    assert "critique" in qe._without_critical_quantities("quelle critique fait-on de ce modèle ?")


def test_lexical_search_drops_function_words_before_its_token_cap(tmp_path) -> None:
    index_path = str(tmp_path / "index")
    target = Document(
        page_content="Laser striation of coated conductors",
        metadata={"rel_path": "striation.pdf"},
    )
    function_words = sorted(word for word in qe._LEXICAL_SEARCH_STOPWORDS if word.isascii())[:30]
    filler = Document(page_content=" ".join(function_words), metadata={"rel_path": "filler.pdf"})
    lexical_index.replace_paths(index_path, ["striation.pdf", "filler.pdf"], [target, filler])
    # 30 distinct function words fill the 24-token cap before "striation".
    query = " ".join(function_words) + " striation"

    unfiltered = lexical_index.search(index_path, query, limit=10)
    filtered = lexical_index.search(
        index_path, query, limit=10, stopwords=qe._LEXICAL_SEARCH_STOPWORDS
    )

    assert [doc.metadata["rel_path"] for doc in filtered] == ["striation.pdf"]
    assert "striation.pdf" not in [doc.metadata["rel_path"] for doc in unfiltered]
