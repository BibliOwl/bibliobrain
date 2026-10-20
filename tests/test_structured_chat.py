from __future__ import annotations

from src.structured_chat import ChatAnswer, clean_chat_answer_markdown


def test_chat_answer_accepts_sectioned_analysis_json() -> None:
    structured = ChatAnswer.model_validate(
        {
            "CONTEXTE": {"texte": "Le document traite des pertes AC dans les aimants HTS."},
            "OBJECTIF": "Comparer les formulations numeriques disponibles.",
            "RESULTATS_PRINCIPAUX": [
                "La formulation H capture les transitoires.",
                "La formulation T-A reduit le cout de calcul.",
            ],
        }
    )

    assert "**CONTEXTE**\nLe document traite des pertes AC" in structured.answer
    assert "**OBJECTIF**\nComparer les formulations numeriques" in structured.answer
    assert "- La formulation H capture les transitoires." in structured.answer
    assert structured.explanation == ""
    assert structured.response_note == ""


def test_chat_answer_preserves_standard_fields_and_appends_extra_sections() -> None:
    structured = ChatAnswer.model_validate(
        {
            "Reponse": "Reponse directe.",
            "Explanation": "Justification courte.",
            "LIMITES": {"texte": "Le texte ne couvre pas les details experimentaux."},
        }
    )

    assert structured.answer.startswith("Reponse directe.")
    assert "**LIMITES**\nLe texte ne couvre pas" in structured.answer
    assert structured.explanation == "Justification courte."


def test_chat_answer_analysis_markdown_omits_empty_response_note() -> None:
    structured = ChatAnswer(answer="Résumé rapide.", explanation="", response_note="")

    assert structured.format_as_analysis_markdown() == "Résumé rapide."
    assert structured.format_as_markdown().rstrip().endswith("Response note:")


def test_clean_chat_answer_markdown_removes_empty_legacy_response_note() -> None:
    text = "Answer:\nRésumé rapide.\n\nExplanation:\n\nResponse note:"

    assert clean_chat_answer_markdown(text) == "Résumé rapide."
