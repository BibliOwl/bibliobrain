from __future__ import annotations

import pytest

from src.chat_grounding import (
    check_answer_grounding,
    contains_value,
    number_forms,
    strip_dangling_citations,
)

PASSAGES = [
    "SCIF in a REBCO insert Tanaka 2021\n"
    "At 4.2 K the SCIF after the first ramp was −0.42 T, i.e. 2.9 % of the insert field.",
    "SCIF in a REBCO insert Tanaka 2021\n"
    "At 20 K the SCIF after the first ramp was −0.19 T. Capacity retention was 91.3 %.",
    "Cohorte Petit 2020\n"
    "Dans cette cohorte de 1 846 patients, le risque était réduit de 27 % (HR 0,73).",
]
QUESTION = "Quelle est la valeur du SCIF à 20 K ?"


def _check(answer: str):
    return check_answer_grounding(answer, PASSAGES, question=QUESTION)


def test_grounded_answer_is_left_untouched() -> None:
    answer = "À 20 K, le SCIF vaut −0,19 T [S2]."

    report = _check(answer)

    assert report.text == answer
    assert report.ungrounded_values == []


def test_citation_is_left_as_the_model_wrote_it() -> None:
    # −0.19 T is in the second passage: the label is not corrected or completed.
    answer = "À 20 K, le SCIF vaut −0.19 T [S1]."

    report = _check(answer)

    assert report.text == answer
    assert report.ungrounded_values == []


def test_year_shown_above_a_passage_is_a_known_figure() -> None:
    assert _check("L'étude date de 2021.").ungrounded_values == []


def test_page_references_are_not_reported_values() -> None:
    report = _check("Le SCIF vaut 0.19 T [S2], voir p. 64 et pages 70-71.")

    assert report.ungrounded_values == []


def test_invented_figure_is_reported() -> None:
    report = _check("Le SCIF vaut 0.25 T à 20 K [S2].")

    assert report.ungrounded_values == ["0.25"]
    assert report.text == "Le SCIF vaut 0.25 T à 20 K [S2]."


def test_figure_of_the_conversation_is_not_reported() -> None:
    answer = "Comme indiqué plus haut, 72 % reste la valeur retenue."

    assert _check(answer).ungrounded_values == ["72"]
    report = check_answer_grounding(
        answer, PASSAGES, question=QUESTION, extra_texts=["Assistant: la valeur est 72 %"]
    )
    assert report.ungrounded_values == []


@pytest.mark.parametrize(
    "answer",
    [
        "La rétention est de 91 % [S2].",  # rounding of 91.3
        "La cohorte compte 1846 patients [S3].",  # French thousands separator
        "Le rapport de risque est de 0.73 [S3].",  # decimal comma in the passage
        "1. Le SCIF vaut 0.19 T [S2].\n2. Il vaut 0.42 T à 4.2 K [S1].",  # list markers
        "Deux mesures sont rapportées, voir [S1, S2].",
    ],
)
def test_legitimate_spellings_are_grounded(answer: str) -> None:
    assert _check(answer).ungrounded_values == []


@pytest.mark.parametrize("passages", [[], ["", "  "]])
def test_nothing_is_checked_without_passages(passages: list[str]) -> None:
    report = check_answer_grounding("Le SCIF vaut 0.25 T.", passages, question=QUESTION)

    assert report.ungrounded_values == []


def test_number_forms_keep_both_readings_of_an_ambiguous_separator() -> None:
    assert number_forms("6,204") == {"6204", "6.204"}
    assert number_forms("1 846") == {"1846"}
    assert number_forms("0,730") == {"0.73"}


def test_contains_value_needs_the_whole_number() -> None:
    assert contains_value("soit 78,6 % de la capacité", "78.6")
    assert not contains_value("un courant de 231 A", "31")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (
            "Le SCIF vaut 0.19 T [S2]. Aucune comparaison n'est possible. [S1] [S3]",
            "Le SCIF vaut 0.19 T [S2]. Aucune comparaison n'est possible.",
        ),
        ("Le SCIF vaut 0.19 T [S2].\n\n[S2] [S2]", "Le SCIF vaut 0.19 T [S2]."),
        # One label after the full stop is that sentence's citation.
        ("Le SCIF vaut 0.19 T. [S2]", "Le SCIF vaut 0.19 T. [S2]"),
        # Nothing else is cited: these labels are all the answer has.
        ("Aucune mesure n'est rapportée. [S1], [S2]", "Aucune mesure n'est rapportée. [S1], [S2]"),
    ],
)
def test_labels_piled_after_the_last_sentence_are_dropped(answer: str, expected: str) -> None:
    assert strip_dangling_citations(answer) == expected
    assert _check(answer).text == expected


def test_cells_of_an_extracted_table_row_are_separate_figures() -> None:
    passages = ["T (K) Tape A Tape B\n77 152 31 178 44\n50 590 214 655 262"]

    report = check_answer_grounding("Le ruban B atteint 178 A [S1].", passages, question="")

    assert report.ungrounded_values == []
