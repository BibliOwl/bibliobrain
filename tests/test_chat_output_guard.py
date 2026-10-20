from __future__ import annotations

import pytest
from langchain_core.documents import Document

from src.chat_output_guard import (
    collect_valid_source_labels,
    strip_invalid_citations,
    validate_chat_output,
)
from src.structured_chat import normalize_chunk_citation_markup


def _doc(label: str = "S1") -> Document:
    return Document(
        page_content="Les pertes AC incluent des pertes hysteretiques.",
        metadata={"ref_label": label, "chunk_uid": "PAPER1:3", "article_id": "PAPER1", "chunk_id": 3},
    )


def test_guard_accepts_valid_structured_answer_with_known_citation() -> None:
    text = (
        "Answer:\n"
        "Les pertes AC incluent des pertes hysteretiques [S1].\n\n"
        "Explanation:\n"
        "Le passage source mentionne ce mecanisme [S1].\n\n"
        "Response note:\n"
        "Aucune note utile."
    )

    report = validate_chat_output(text, sources=[_doc("S1")])

    assert report.ok
    assert report.severity == "ok"
    assert report.cited_labels == ["S1"]
    assert report.issues == []


def test_guard_rejects_unknown_source_citation() -> None:
    text = (
        "Answer:\nLa formulation H est la meilleure [S9].\n\n"
        "Explanation:\nLe contexte le montre [S9].\n\n"
        "Response note:\nAucune note utile."
    )

    report = validate_chat_output(text, sources=[_doc("S1")], context_text="La formulation H est mentionnee.")

    assert not report.ok
    assert report.severity == "error"
    assert report.missing_labels == ["S9"]
    assert "unknown_source_citation" in {issue.code for issue in report.issues}


def test_guard_accepts_unstructured_answer_with_valid_citations() -> None:
    text = (
        "Les pertes dans les aimants HTS sont surtout des pertes AC [S1].\n\n"
        "Les extraits citent les pertes d'hysteresis et de couplage [S1]."
    )

    report = validate_chat_output(text, sources=[_doc("S1")])

    assert report.ok
    assert report.severity == "ok"
    assert report.cited_labels == ["S1"]


def test_guard_can_still_require_legacy_structure() -> None:
    text = "Les pertes AC incluent des pertes hysteretiques [S1]."

    report = validate_chat_output(text, sources=[_doc("S1")], require_structure=True)

    assert not report.ok
    assert "missing_required_structure" in {issue.code for issue in report.issues}


def test_strip_invalid_citations_removes_unknown_source_without_dropping_sentence() -> None:
    text = "Les pertes AC incluent l'hysteresis [S1, S9] et le couplage [S9]."

    cleaned = strip_invalid_citations(text, sources=[_doc("S1")])

    assert cleaned == "Les pertes AC incluent l'hysteresis [S1] et le couplage."


def test_guard_rejects_numeric_empty_and_placeholder_citations() -> None:
    text = (
        "Answer:\nLes pertes AC sont importantes [3] [] ****.\n\n"
        "Explanation:\nLe passage est cite [S1].\n\n"
        "Response note:\nAucune note utile."
    )

    report = validate_chat_output(text, sources=[_doc("S1")])

    codes = {issue.code for issue in report.issues}
    assert not report.ok
    assert "raw_numeric_citation" in codes
    assert "empty_bracket_citation" in codes
    assert "placeholder_citation" in codes


def test_guard_detects_visible_reasoning_and_drafting_markers() -> None:
    text = (
        "<think>I should reason privately.</think>\n"
        "Answer:\nLes pertes incluent l'hysteresis [S1].\n\n"
        "Explanation:\nLe contexte le dit [S1].\n\n"
        "Response note:\nAucune note utile.\n\n"
        "Final Polish:\nDo not show this."
    )

    report = validate_chat_output(text, sources=[_doc("S1")])

    codes = {issue.code for issue in report.issues}
    assert not report.ok
    assert "hidden_reasoning_leak" in codes
    assert "drafting_marker" in codes


def test_guard_normalizes_malformed_source_citation() -> None:
    text = (
        "Answer:\nLes pertes incluent l'hysteresis [S'3].\n\n"
        "Explanation:\nLe contexte le dit [S'3].\n\n"
        "Response note:\nAucune note utile."
    )

    report = validate_chat_output(text, sources=[_doc("S3")])

    assert report.ok
    assert "[S'3]" not in report.normalized_text
    assert "[S3]" in report.normalized_text
    assert report.cited_labels == ["S3"]


def test_guard_rejects_raw_chunk_citation_markup() -> None:
    text = (
        "Answer:\nLes pertes de couplage sont mentionnees [CHUNK 23: Fukumoto Y et al.].\n\n"
        "Explanation:\nLe contexte cite ce passage [S1].\n\n"
        "Response note:\nAucune note utile."
    )

    report = validate_chat_output(text, sources=[_doc("S1")])

    assert not report.ok
    assert "raw_chunk_citation" in {issue.code for issue in report.issues}
    assert "[CHUNK" not in report.normalized_text
    assert "[23: Fukumoto Y et al.]" in report.normalized_text


def test_guard_rejects_unresolved_freeform_chunk_label_after_normalization() -> None:
    text = (
        "Answer:\nLes pertes de couplage sont mentionnees [23: Fukumoto Y et al.].\n\n"
        "Explanation:\nLe contexte cite ce passage [S1].\n\n"
        "Response note:\nAucune note utile."
    )

    report = validate_chat_output(text, sources=[_doc("S1")])

    assert not report.ok
    codes = {issue.code for issue in report.issues}
    assert "unresolved_citation_bracket" in codes


def test_normalize_chunk_citation_markup_accepts_freeform_chunk_labels() -> None:
    text = "Answer: Une piste [CHUNK 23: Fukumoto Y et al.] est citee."

    normalized = normalize_chunk_citation_markup(text)

    assert normalized == "Answer: Une piste [23: Fukumoto Y et al.] est citee."


def test_guard_warns_on_unsupported_ranking_but_accepts_prudent_refusal() -> None:
    ranked = (
        "Answer:\nLa formulation H est la plus rapide [S1].\n\n"
        "Explanation:\nLe contexte mentionne seulement la formulation H [S1].\n\n"
        "Response note:\nAucune note utile."
    )
    refused = (
        "Answer:\nLes sources ne permettent pas de determiner quelle formulation est la plus rapide [S1].\n\n"
        "Explanation:\nLe contexte mentionne seulement la formulation H [S1].\n\n"
        "Response note:\nInformation insuffisante."
    )

    ranked_report = validate_chat_output(ranked, sources=[_doc("S1")], context_text="La formulation H est robuste.")
    refused_report = validate_chat_output(refused, sources=[_doc("S1")], context_text="La formulation H est robuste.")

    assert ranked_report.ok
    assert ranked_report.severity == "warning"
    assert "unsupported_ranking_language" in {issue.code for issue in ranked_report.issues}
    assert refused_report.severity == "ok"


def test_guard_can_warn_on_uncited_technical_sentence() -> None:
    text = (
        "Answer:\nLes pertes AC dans les conducteurs HTS dependent de la frequence.\n\n"
        "Explanation:\nLe substrat metallique peut produire des pertes resistives [S1].\n\n"
        "Response note:\nAucune note utile."
    )

    report = validate_chat_output(
        text,
        sources=[_doc("S1")],
        flag_uncited_technical_sentences=True,
    )

    assert report.ok
    assert report.severity == "warning"
    assert "uncited_technical_sentence" in {issue.code for issue in report.issues}


def test_collect_valid_source_labels_includes_ref_label_and_chunk_uid() -> None:
    labels = collect_valid_source_labels([_doc("S2")])

    assert "S2" in labels
    assert "PAPER1:3" in labels


@pytest.mark.parametrize("source", [_doc("S9"), {"ref_label": "S9"}, "S9"])
def test_explicit_source_label_does_not_allow_positional_alias(source) -> None:
    assert "S9" in collect_valid_source_labels([source])
    assert "S1" not in collect_valid_source_labels([source])
    report = validate_chat_output("The current is 104 A [S1].", sources=[source])
    assert report.missing_labels == ["S1"]
    assert report.severity == "error"
    assert strip_invalid_citations("104 A [S9, S1].", sources=[source]) == "104 A [S9]."


def test_reordered_explicit_labels_are_preserved() -> None:
    sources = [{"ref_label": "S10"}, {"ref_label": "S9"}]
    assert collect_valid_source_labels(sources) == ["S10", "S9"]
    assert validate_chat_output("104 A [S9] and 262 A [S10].", sources=sources).ok


def test_unlabelled_sources_keep_positional_fallback() -> None:
    assert collect_valid_source_labels([{}, {}]) == ["S1", "S2"]


def test_wiki_provenance_label_does_not_allow_positional_alias() -> None:
    source = {"wiki_provenance": True, "article_id": "paper", "chunk_id": 3}
    assert collect_valid_source_labels([source]) == ["PAPER:3"]
    assert validate_chat_output("A supported claim [PAPER:3].", sources=[source]).ok


def test_strip_invalid_citations_removes_the_prompt_notation_copied_literally() -> None:
    text = "La valeur n'est pas fournie [Sx]. Voir aussi [S1]."

    assert strip_invalid_citations(text, sources=[{"ref_label": "S1"}]) == (
        "La valeur n'est pas fournie. Voir aussi [S1]."
    )
