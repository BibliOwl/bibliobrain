from __future__ import annotations

from typing import Any

import pytest

from src.chat_intents import (
    detect_chat_intent,
    detect_retrieval_depth,
    is_draft_citation_followup,
    is_multi_paper_comparison_request,
    needs_llm_tool_planner,
    paper_list_followup,
    question_may_need_visuals,
    requests_library_citations,
    resolve_retrieval_depth,
)


@pytest.mark.parametrize(("message", "expected"), [
    ("tu peux réécrire un abstract avec des citations pertinentes de ma bibliothèque ?", True),
    ("Rédige une introduction avec des références.", True),
    ("Write an abstract citing sources from my library", True),
    ("Rédige une introduction. Plan : discussion et références.", False),
    ("Rédige une introduction sans citations.", False),
    ("Reformule ce passage.", False),
])
def test_library_citation_request_reads_only_the_request(message: str, expected: bool) -> None:
    assert requests_library_citations(message) is expected


def test_citation_followup_after_draft_routes_back_to_drafting() -> None:
    draft: list[dict[str, Any]] = [
        {"role": "user", "content": "Rédige une introduction."},
        {"role": "assistant", "content": "Introduction...", "meta": {"mode": "drafting"}},
    ]
    rag: list[dict[str, Any]] = [
        {"role": "assistant", "content": "Réponse", "meta": {"mode": "rag"}}
    ]
    assert detect_chat_intent("tu peux rajouter des citations ?").mode == "rag"
    assert is_draft_citation_followup("tu peux rajouter des citations ?", draft)
    assert not is_draft_citation_followup("tu peux rajouter des citations ?", rag)
    assert not is_draft_citation_followup("quelles sont les sources ?", draft)
    assert not is_draft_citation_followup("tu peux rajouter des citations ?", [])


@pytest.mark.parametrize("message", [
    "ok est-ce que tu peux chercher des nouveaux articles ?",
    "Tu peux rechercher d'autres publications ?",
    "Trouve de nouveaux articles écrits par Grilli",
    "Can you find more papers?",
    "Search for recent studies",
])
def test_contextual_discovery_without_repeated_topic(message: str) -> None:
    assert detect_chat_intent(message).mode == "article_discovery"


@pytest.mark.parametrize("message", [
    "avec ce contexte, tu peux me rédiger une introduction pour l'article ?\n"
    "Titre proposé\nHTS coils\nObjectif : Valider la conception.\nDiscussion\nRéférences",
    "Rédige une introduction. Plan : discussion et références.",
    "Rédige une introduction. Contexte : valider la conception.",
    "Reformule ce passage : valider les résultats de la comparaison.",
    "Write an introduction using this outline. Discussion and references.",
    "Traduis ce texte en anglais : compare les modèles.",
    "Tu peux me faire une introduction avec ces résultats ?",
    "Rédige une introduction qui présente ces résultats sans rien inventer.",
    "Rédige une introduction mais ne parle pas des pertes AC.",
    "D'accord. Rédige une introduction avec ce contexte.",
])
def test_drafting_keeps_context_out_of_tool_routing(message: str) -> None:
    intent = detect_chat_intent(message)
    assert intent.mode == "drafting"
    assert intent.query == message


@pytest.mark.parametrize(("message", "mode"), [
    ("Comment rédiger une introduction ?", "rag"),
    ("Comment chercher des nouveaux articles ?", "rag"),
    ("Who will write this paper?", "rag"),
    ("Explique ce passage. Contexte : valider les résultats.", "rag"),
    ("Rédige une revue de littérature sur les HTS", "literature_review"),
    ("/biblio Rédige une introduction\nRéférences", "bibliography"),
    ("/rag Rédige une introduction\nPlan : validation", "rag"),
    ("/draft Introduction\nPlan : validation", "drafting"),
    ("Vérifie ce passage. Les résultats sont comparables.", "fact_check"),
])
def test_routing_preserves_questions_and_explicit_commands(message: str, mode: str) -> None:
    assert detect_chat_intent(message).mode == mode


def test_infinitive_search_can_reach_fallback_planner() -> None:
    assert needs_llm_tool_planner("tu peux chercher des publications ?")
    assert not needs_llm_tool_planner("Explique ce résultat. Texte : cherche des articles.")


@pytest.mark.parametrize("message", [
    "comment modéliser les pertes AC dans les aimants supra",
    "quel est le moteur d'une aventador ?",
    "Explique les résultats de cet article",
    "Quels sont les thèmes principaux de ma bibliothèque ?",
    "Donne une définition de la supraconductivité",
    "What do these papers say about AC losses?",
    "Merci",
])
def test_ordinary_questions_skip_llm_tool_planner(message: str) -> None:
    assert not needs_llm_tool_planner(message)


@pytest.mark.parametrize("message", [
    "prepare un panorama des publications consacrees aux pertes AC",
    "Rassemble les sources pertinentes",
    "Can you gather papers about HTS magnets?",
    "Show those from 2025",
    "Check this claim against the library",
])
def test_ambiguous_actions_keep_llm_tool_planner(message: str) -> None:
    assert needs_llm_tool_planner(message)


def test_detect_literature_review_from_natural_language() -> None:
    intent = detect_chat_intent("fais une revue de litterature sur les pertes AC HTS")

    assert intent.mode == "literature_review"
    assert intent.explicit is False


def test_detect_literature_review_from_command() -> None:
    intent = detect_chat_intent("/review AC losses in HTS magnets")

    assert intent.mode == "literature_review"
    assert intent.query == "AC losses in HTS magnets"
    assert intent.explicit is True


def test_detect_paper_list_from_exhaustive_request() -> None:
    intent = detect_chat_intent("liste tous les papers qui parlent des pertes AC dans les HTS")

    assert intent.mode == "paper_list"
    assert intent.explicit is False


def test_detect_paper_list_without_exhaustive_marker() -> None:
    intent = detect_chat_intent("fais une liste des papers sur les pertes AC")

    assert intent.mode == "paper_list"
    assert intent.explicit is False


def test_detect_paper_list_from_command_without_exhaustive_words() -> None:
    intent = detect_chat_intent("/liste pertes AC dans les HTS")

    assert intent.mode == "paper_list"
    assert intent.query == "pertes AC dans les HTS"
    assert intent.explicit is True


def test_rag_command_forces_normal_chat() -> None:
    intent = detect_chat_intent("/rag liste tous les papers qui parlent des pertes AC")

    assert intent.mode == "rag"
    assert intent.query == "liste tous les papers qui parlent des pertes AC"
    assert intent.explicit is True


def test_detect_markdown_export_command_defaults_to_previous() -> None:
    intent = detect_chat_intent("/md fais un document propre")

    assert intent.mode == "document_export"
    assert intent.export_format == "markdown"
    assert intent.export_scope == "previous"
    assert intent.query == "fais un document propre"
    assert intent.explicit is True


def test_detect_latex_export_command_for_chat_scope() -> None:
    intent = detect_chat_intent("/latex exporte tout le chat")

    assert intent.mode == "document_export"
    assert intent.export_format == "latex"
    assert intent.export_scope == "chat"
    assert intent.explicit is True


def test_detect_natural_previous_message_export() -> None:
    intent = detect_chat_intent("cree un fichier md de la reponse precedente")

    assert intent.mode == "document_export"
    assert intent.export_format == "markdown"
    assert intent.export_scope == "previous"


def test_detect_natural_chat_export() -> None:
    intent = detect_chat_intent("exporte ce chat en markdown")

    assert intent.mode == "document_export"
    assert intent.export_format == "markdown"
    assert intent.export_scope == "chat"


def test_non_exhaustive_paper_question_stays_rag() -> None:
    intent = detect_chat_intent("cite quelques papers importants sur les pertes AC")

    assert intent.mode == "rag"


def test_detect_article_discovery_from_natural_language_with_explicit_topic() -> None:
    intent = detect_chat_intent("cherche des articles sur les pertes AC dans les aimants HTS")

    assert intent.mode == "article_discovery"
    assert intent.explicit is False


def test_detect_author_filtered_article_search_as_local_paper_list() -> None:
    intent = detect_chat_intent("je cherche des articles qui parlent de pertes AC écrit par Grilli")

    assert intent.mode == "paper_list"
    assert intent.explicit is False


def test_detect_author_only_article_search_as_local_paper_list() -> None:
    intent = detect_chat_intent("Cherche des articles écrits par moreau")

    assert intent.mode == "paper_list"
    assert intent.explicit is False


def test_detect_author_question_without_list_verb_as_paper_list() -> None:
    # Reported 2026-10-01: routed to plain RAG, which cited the same paper as [S2][S4][S5].
    for message in (
        "Quels sont les articles publiés par moi (Etienne Rochepault) ?",
        "Which papers were published by Etienne Rochepault?",
    ):
        assert detect_chat_intent(message).mode == "paper_list", message


def test_cite_the_papers_of_an_author_as_paper_list() -> None:
    # Reported 2026-10-16: answered from the previous answer's two SPARC sources.
    for message in (
        "cite moi tout les articles de Camilla",
        "Cite-moi les articles de Moreau",
    ):
        assert detect_chat_intent(message).mode == "paper_list", message


def test_article_search_with_non_author_par_stays_discovery() -> None:
    intent = detect_chat_intent("cherche des articles par methode sur les pertes AC")

    assert intent.mode == "article_discovery"


def test_detect_article_discovery_from_natural_language_with_referential_topic() -> None:
    intent = detect_chat_intent("cherche des articles en rapport avec ce sujet")

    assert intent.mode == "article_discovery"
    assert intent.explicit is False


def test_detect_article_discovery_from_command() -> None:
    intent = detect_chat_intent("/discover modelling losses in HTS magnets")

    assert intent.mode == "article_discovery"
    assert intent.query == "modelling losses in HTS magnets"
    assert intent.explicit is True


def test_detect_paper_compare_from_command() -> None:
    intent = detect_chat_intent("/compare H formulation vs T-A formulation")

    assert intent.mode == "paper_compare"
    assert intent.query == "H formulation vs T-A formulation"
    assert intent.explicit is True


def test_detect_paper_compare_from_natural_language() -> None:
    intent = detect_chat_intent("compare les papers sur les pertes AC HTS")

    assert intent.mode == "paper_compare"


def test_detect_evidence_matrix_from_natural_language() -> None:
    intent = detect_chat_intent("fais une matrice de preuves sur les pertes AC")

    assert intent.mode == "evidence_matrix"


def test_detect_bibliography_from_cited_sources_request() -> None:
    intent = detect_chat_intent("genere la bibliographie des sources citees dans ce chat")

    assert intent.mode == "bibliography"
    assert intent.export_scope == "chat"


def test_detect_fact_check_request() -> None:
    intent = detect_chat_intent("verifie ce passage avec ma base: les pertes AC augmentent")

    assert intent.mode == "fact_check"


def test_detect_notebook_create_request() -> None:
    intent = detect_chat_intent("cree un carnet avec les sources de la reponse precedente")

    assert intent.mode == "notebook_create"


def test_general_article_question_stays_rag() -> None:
    intent = detect_chat_intent("quels articles sont les plus cites sur les pertes AC ?")

    assert intent.mode == "rag"


def test_detect_retrieval_depth_flags_synthesis_queries() -> None:
    assert detect_retrieval_depth("fais une revue de litterature sur les pertes AC") == "deep"
    assert detect_retrieval_depth("compare les approches de modelisation") == "deep"
    assert detect_retrieval_depth("donne une synthèse approfondie du sujet") == "deep"
    assert detect_retrieval_depth("What are the main themes in my library?") == "deep"
    assert detect_retrieval_depth("Quels sont les thèmes principaux ?") == "deep"
    assert detect_retrieval_depth("c'est quoi le modele de Kim ?") == "quick"
    assert detect_retrieval_depth("quelle est la Tc du REBCO ?") == "quick"


def test_resolve_retrieval_depth_manual_override_wins() -> None:
    # Manual choice overrides auto-detection in both directions.
    assert resolve_retrieval_depth("fais une revue de litterature", "quick") == "quick"
    assert resolve_retrieval_depth("c'est quoi la Tc ?", "deep") == "deep"
    # "auto"/empty fall back to detection.
    assert resolve_retrieval_depth("fais une revue de litterature", "auto") == "deep"
    assert resolve_retrieval_depth("c'est quoi la Tc ?", "") == "quick"
    assert resolve_retrieval_depth("c'est quoi la Tc ?", None) == "quick"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("compare ces articles", True),
        ("Compare these papers", True),
        ("quelles différences entre ces études ?", True),
        ("compare les deux formulations", False),
        ("résume cet article", False),
    ],
)
def test_multi_paper_comparison_request(message: str, expected: bool) -> None:
    assert is_multi_paper_comparison_request(message) is expected


@pytest.mark.parametrize("message", [
    "ok et quels articles parlent de LTS ?",
    "Which articles talk about HTS magnets?",
    "Quels sont les articles qui traitent du quench ?",
    "What papers discuss screening currents?",
])
def test_which_papers_question_is_a_paper_list(message: str) -> None:
    assert detect_chat_intent(message).mode == "paper_list"


@pytest.mark.parametrize("message", [
    "Quels articles récents parlent de LTS ?",
    "Quel article explique le mieux le quench ?",
    "What is the critical current of REBCO?",
])
def test_which_papers_rule_leaves_other_questions_alone(message: str) -> None:
    assert detect_chat_intent(message).mode != "paper_list"


def _after_paper_list(query: str, sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": query},
        {
            "role": "assistant",
            "content": "1. **A** [S1]",
            "meta": {
                "mode": "paper_list",
                "sources": sources,
                "tool_plan": {
                    "action": "execute",
                    "calls": [{"name": "search.local_articles", "arguments": {"query": query}}],
                },
            },
        },
    ]


@pytest.mark.parametrize("message", [
    "est-ce qu'il y en a d'autres ?",
    "tu peux tout citer ?",
    "Any others?",
    "et les autres ?",
    # 2026-10-21 report: "seulement 3 ?" went to plain RAG, which contradicted the list.
    "seulement 3 ?",
    "il n'y en a que 3 ?",
    "only three?",
    "just 3 papers?",
])
def test_continuation_after_paper_list_repeats_the_query_without_listed_papers(
    message: str,
) -> None:
    first = _after_paper_list("Which articles talk about HTS magnets?", [{"article_id": "a1"}])
    second = _after_paper_list("Which articles talk about HTS magnets?", [{"article_id": "a2"}])
    followup = paper_list_followup(message, first + second)

    assert detect_chat_intent(message).mode == "rag"
    assert followup is not None
    assert followup.query == "Which articles talk about HTS magnets?"
    assert [source["article_id"] for source in followup.excluded_sources] == ["a2", "a1"]


@pytest.mark.parametrize(("message", "topic"), [
    ("non je voulais dire de low temperature superconductors", "low temperature superconductors"),
    ("Non, je parlais des LTS.", "LTS"),
    ("I meant the Nb3Sn coils", "Nb3Sn coils"),
])
def test_correction_after_paper_list_lists_the_corrected_topic(message: str, topic: str) -> None:
    followup = paper_list_followup(message, _after_paper_list("quels articles parlent de LTS ?", []))

    assert followup is not None
    assert followup.query == topic
    assert followup.excluded_sources == ()


def test_paper_list_followup_needs_a_previous_list_and_no_new_topic() -> None:
    rag_history: list[dict[str, Any]] = [
        {"role": "assistant", "content": "Réponse", "meta": {"mode": "rag", "tool_plan": {}}}
    ]
    listed = _after_paper_list("liste les articles sur le quench", [])

    assert paper_list_followup("il y en a d'autres ?", rag_history) is None
    assert paper_list_followup("il y en a d'autres ?", []) is None
    assert paper_list_followup("d'autres articles sur le refroidissement ?", listed) is None
    assert paper_list_followup("tu peux tout résumer ?", listed) is None
    assert paper_list_followup("seulement ceux de 2024 sur le quench ?", listed) is None
    assert paper_list_followup("only the review papers?", listed) is None


@pytest.mark.parametrize("message", [
    "Que montre la courbe de la figure 2 ?",
    "Quelles valeurs montre la courbe ?",
    "Que contient le tableau 3 ?",
    "Compare la fig. 4 et le texte.",
    "What value does the red series reach?",
    "What does Table 2 report?",
    "Lis le graphique de l'article.",
])
def test_question_may_need_visuals_for_figure_questions(message: str) -> None:
    assert question_may_need_visuals(message) is True


@pytest.mark.parametrize("message", [
    "Résume cet article.",
    "Quels sont les auteurs ?",
    "Quel est le courant critique à 77 K ?",
    "Quels axes de recherche ressortent de ma bibliothèque ?",
    "Fais un tableau comparatif des méthodes.",
    "Présente les résultats sous forme de tableau.",
    "Make a table of the results.",
    "",
])
def test_question_may_need_visuals_skips_text_questions(message: str) -> None:
    assert question_may_need_visuals(message) is False
