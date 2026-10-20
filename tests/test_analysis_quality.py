from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.analysis_prompts import (
    build_analysis_cache_fingerprint,
    get_analysis_prompt,
    list_analysis_prompt_payloads,
    list_analysis_types,
    set_analysis_prompt_override,
)
from src.analysis_quality import (
    deep_analysis_guidance,
    detailed_summary_guidance,
    is_deep_article_analysis_request,
    is_detailed_article_summary_request,
)
from src.library import _build_analysis_document_text


@dataclass
class _Page:
    page_content: str


def test_deep_analysis_intent_accepts_direct_request_and_short_followup() -> None:
    assert is_deep_article_analysis_request(
        "Tu peux faire une analyse détaillée de l'article ?"
    )
    assert is_deep_article_analysis_request("Analyse cet article")
    assert is_deep_article_analysis_request("Détaille plus")
    assert not is_deep_article_analysis_request("Résume cet article")


def test_detailed_summary_intent_is_distinct_from_a_short_summary() -> None:
    assert is_detailed_article_summary_request("fais un résumé détaillé")
    assert is_detailed_article_summary_request("Give me a comprehensive summary")
    assert not is_detailed_article_summary_request("Résume cet article")


def test_detailed_summary_guidance_prioritizes_the_article_body() -> None:
    guidance = detailed_summary_guidance(language="fr", citation_format="[Sx]")

    assert "fond du document" in guidance
    assert "corps de l'article" in guidance
    assert "resultats quantitatifs" in guidance
    assert "[Sx]" in guidance


def test_deep_analysis_guidance_demands_evidence_not_a_longer_summary() -> None:
    guidance = deep_analysis_guidance(language="fr", citation_format="[Sx]")

    assert "pas d'un resume allonge" in guidance
    assert "700 a 1 200 mots" in guidance
    assert "resultats quantitatifs" in guidance
    assert "limites" in guidance
    assert "[Sx]" in guidance


def test_library_exposes_a_dedicated_critical_analysis_prompt() -> None:
    assert "Analyse critique approfondie" in list_analysis_types()

    prompt = get_analysis_prompt("Analyse critique approfondie")
    assert "simple résumé" in prompt
    assert "Contribution originale" in prompt
    assert "Validation et résultats" in prompt
    assert "900 à 1 500 mots" in prompt
    assert "{text}" in prompt


def test_critical_analysis_prompt_hunts_inconsistencies_with_a_verdict() -> None:
    french = get_analysis_prompt("Analyse critique approfondie", language="fr")
    english = get_analysis_prompt("Analyse critique approfondie", language="en")

    assert "## Cohérence interne et incohérences détectées" in french
    assert "Verdict de robustesse" in french
    # A truncated excerpt must not turn missing pages into "inconsistencies".
    assert "jamais une incohérence" in french
    assert "## Internal Consistency and Detected Inconsistencies" in english
    assert "Robustness verdict" in english
    assert "never an inconsistency" in english
    # The new section sits between the limitations and the final judgment.
    assert (
        french.index("## Limites et points de vigilance")
        < french.index("## Cohérence interne")
        < french.index("## Appréciation globale")
    )


def test_deep_analysis_guidance_checks_internal_consistency() -> None:
    assert "coherence interne" in deep_analysis_guidance(language="fr")
    assert "internal consistency" in deep_analysis_guidance(language="en")


def test_analysis_prompts_follow_ui_language() -> None:
    french_prompt = get_analysis_prompt("Résumé rapide", language="fr")
    english_prompt = get_analysis_prompt("Résumé rapide", language="en")

    assert "# IDENTITÉ ET OBJECTIF" in french_prompt
    assert "# IDENTITY AND OBJECTIVE" in english_prompt
    assert "Text:\n{text}" in english_prompt
    assert "Language rule: write the entire visible answer in English" in english_prompt


def test_analysis_prompt_editor_payload_follows_ui_language(monkeypatch) -> None:
    monkeypatch.setattr("src.analysis_prompts._read_overrides", lambda: {})

    french_payload = list_analysis_prompt_payloads(language="fr")[0]
    english_payload = list_analysis_prompt_payloads(language="en")[0]

    assert french_payload["type"] == english_payload["type"] == "Résumé rapide"
    assert french_payload["label"] == "Résumé rapide"
    assert english_payload["label"] == "Quick summary"
    assert "# IDENTITÉ ET OBJECTIF" in french_payload["default_prompt"]
    assert "# IDENTITY AND OBJECTIVE" in english_payload["default_prompt"]
    assert english_payload["prompt"] == english_payload["default_prompt"]
    assert english_payload["language"] == "en"
    assert french_payload["fingerprint"] != english_payload["fingerprint"]


def test_saving_english_default_does_not_create_custom_override(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.analysis_prompts._PROMPTS_PATH",
        tmp_path / "analysis_prompts.json",
    )
    english_default = list_analysis_prompt_payloads(language="en")[0]["default_prompt"]

    payload = set_analysis_prompt_override(
        "Résumé rapide",
        english_default,
        language="en",
    )

    assert payload["custom"] is False
    assert payload["prompt"] == english_default


def test_analysis_cache_fingerprint_includes_language() -> None:
    french_fingerprint = build_analysis_cache_fingerprint("file-fp", "Résumé rapide", "fr")
    english_fingerprint = build_analysis_cache_fingerprint("file-fp", "Résumé rapide", "en")

    assert french_fingerprint != english_fingerprint
    assert ":prompt:fr:" in french_fingerprint
    assert ":prompt:en:" in english_fingerprint


def test_analysis_document_sampling_keeps_front_methods_and_conclusion() -> None:
    pages = [
        _Page(f"Contenu générique de la page {index}. " + ("x" * 5000))
        for index in range(20)
    ]
    pages[0] = _Page("INTRODUCTION objectif et contexte. " + ("i" * 5000))
    pages[8] = _Page("METHODOLOGY protocole et hypothèses. " + ("m" * 5000))
    pages[14] = _Page("RESULTS validation quantitative. " + ("r" * 5000))
    pages[19] = _Page("CONCLUSION limites et perspectives. " + ("c" * 5000))

    text = _build_analysis_document_text(pages, max_chars=22000)

    assert "[Page 1]" in text
    assert "INTRODUCTION" in text
    assert "[Page 9]" in text
    assert "METHODOLOGY" in text
    assert "[Page 15]" in text
    assert "RESULTS" in text
    assert "[Page 20]" in text
    assert "CONCLUSION" in text
    assert len(text) <= 22000
