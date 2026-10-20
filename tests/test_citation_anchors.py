"""Tests for sentence-level citation anchor extraction."""

from __future__ import annotations

from src.rag.citation_anchors import (
    _enclosing_sentence,
    _normalize_label,
    extract_citation_anchors,
)


def test_normalize_label_accepts_standard_forms() -> None:
    assert _normalize_label("S1") == "S1"
    assert _normalize_label("s1") == "S1"
    assert _normalize_label(" s1 ") == "S1"
    assert _normalize_label("3") == "3"
    assert _normalize_label("A:7") == "A:7"


def test_normalize_label_rejects_garbage() -> None:
    assert _normalize_label("") == ""
    assert _normalize_label("hello") == ""
    assert _normalize_label("S") == ""
    assert _normalize_label("1S") == ""


def test_enclosing_sentence_returns_correct_span() -> None:
    text = "First sentence. Second sentence with [S1]. Third sentence."
    pos = text.index("[S1]")
    sentence = _enclosing_sentence(text, pos)
    assert "Second sentence" in sentence
    assert "First sentence" not in sentence
    assert "Third sentence" not in sentence


def test_extract_anchors_matches_sentence_in_source() -> None:
    answer = (
        "Les transformers ont été introduits par Vaswani et al. en 2017 [S1]. "
        "Ils reposent sur l'attention multi-têtes."
    )
    sources = [
        {
            "index": 1,
            "ref_label": "S1",
            "chunk_uid": "paper:1",
            "content": (
                "Introduction. Les transformers ont été introduits par Vaswani et al. en 2017. "
                "Ils ont remplacé les RNN sur de nombreuses tâches."
            ),
        },
    ]
    anchors = extract_citation_anchors(answer, sources)
    assert "S1" in anchors
    assert len(anchors["S1"]) == 1
    found = anchors["S1"][0]
    assert "Vaswani" in found["text"]
    assert found["score"] >= 0.5
    assert found["chunk_uid"] == "paper:1"
    assert found["char_start"] >= 0
    assert found["char_end"] > found["char_start"]


def test_extract_anchors_handles_multiple_citations_in_one_block() -> None:
    answer = "Plusieurs études confirment ce résultat [S1, S2]."
    sources = [
        {
            "index": 1,
            "ref_label": "S1",
            "chunk_uid": "p1",
            "content": "Plusieurs études confirment ce résultat dans des contextes variés.",
        },
        {
            "index": 2,
            "ref_label": "S2",
            "chunk_uid": "p2",
            "content": "D'autres travaux montrent que plusieurs études confirment ce résultat.",
        },
    ]
    anchors = extract_citation_anchors(answer, sources)
    assert "S1" in anchors
    assert "S2" in anchors


def test_extract_anchors_skips_low_match_scores() -> None:
    answer = "Le ciel est bleu [S1]."
    sources = [
        {
            "index": 1,
            "ref_label": "S1",
            "chunk_uid": "p1",
            "content": "Quantum chromodynamics describes the strong nuclear interaction between quarks.",
        },
    ]
    anchors = extract_citation_anchors(answer, sources)
    assert anchors == {} or "S1" not in anchors


def test_extract_anchors_resolves_numeric_alias() -> None:
    answer = "Cette assertion est attestée [1]."
    sources = [
        {
            "index": 1,
            "ref_label": "S1",
            "chunk_uid": "p1",
            "content": "Cette assertion est attestée par plusieurs auteurs indépendants.",
        },
    ]
    anchors = extract_citation_anchors(answer, sources)
    assert "1" in anchors


def test_extract_anchors_handles_empty_inputs() -> None:
    assert extract_citation_anchors("", []) == {}
    assert extract_citation_anchors("hello [S1]", []) == {}
    assert extract_citation_anchors("", [{"index": 1, "content": "x"}]) == {}


def test_extract_anchors_dedupes_label_chunk_pairs() -> None:
    answer = "Voici un fait [S1]. Voici le même fait répété [S1]."
    sources = [
        {
            "index": 1,
            "ref_label": "S1",
            "chunk_uid": "p1",
            "content": "Voici un fait scientifique bien documenté.",
        },
    ]
    anchors = extract_citation_anchors(answer, sources)
    assert "S1" in anchors
    assert len(anchors["S1"]) == 1, "same (label, chunk) pair should not duplicate"
