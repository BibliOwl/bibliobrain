from __future__ import annotations

import asyncio

import pytest

from src import response_language


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How does no-insulation winding affect the charging delay of HTS coils?", "en"),
        ("Explain screening currents", "en"),
        ("Qu'est-ce que les courants d'écrantage dans les bobines REBCO ?", "fr"),
        ("Quel sera le prix du ruban REBCO en 2030 selon mes articles ?", "fr"),
        ("Résume cet article", "fr"),
        ("REBCO ?", ""),
    ],
)
def test_question_language_is_detected(question: str, expected: str) -> None:
    assert response_language.detect_text_language(question) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        (
            "est-ce que ce papier valide cette phrase  ? Within the framework of the CERN "
            "High Field Magnets (HFM) R&D Program, the exploration of Metal-Insulated (MI) "
            "High Temperature Superconducting (HTS) magnets has emerged as a promising way",
            "fr",
        ),
        ("Translate this: la bobine est isolée par une couche de métal", "en"),
        ('Que veut dire "the coil quenches when the current exceeds Ic and the" ?', "fr"),
        ("REBCO ? How do they behave at 20 K in the coil", "en"),
        ("How does no-insulation winding affect the charging delay of HTS coils?", "en"),
    ],
)
def test_the_request_not_the_quoted_text_sets_the_language(question: str, expected: str) -> None:
    assert response_language.detect_question_language(question) == expected


def test_chat_language_follows_the_question_and_falls_back_to_the_ui(monkeypatch) -> None:
    from src.rag import query_engine

    monkeypatch.setattr(query_engine, "get_app_settings", lambda: {"ui_language": "fr"})
    with response_language.response_language_for("How do NI coils charge?", fallback="fr"):
        assert query_engine._current_ui_language() == "en"
        assert "English" in query_engine._response_language_instruction()
    with response_language.response_language_for("REBCO ?", fallback="fr"):
        assert query_engine._current_ui_language() == "fr"
    # Outside a chat request the UI language applies again.
    assert query_engine._current_ui_language() == "fr"


def test_the_language_reaches_generation_threads() -> None:
    async def request() -> str:
        response_language.set_response_language("en")
        return await asyncio.to_thread(response_language.current_response_language)

    assert asyncio.run(request()) == "en"
    assert response_language.current_response_language() == ""
