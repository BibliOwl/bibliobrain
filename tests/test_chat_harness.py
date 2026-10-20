from __future__ import annotations

from pathlib import Path

import pytest
from conftest import read_app_javascript

from src.chat_harness import (
    build_llm_tool_planner_prompt,
    get_tool_spec,
    intent_from_llm_tool_choice,
    intent_from_ui_hint,
    parse_llm_tool_choice,
    plan_chat_tools,
    plan_notebook_tools,
    requests_notebook_notes_update,
    tool_event,
)
from src.chat_intents import ChatIntent, detect_chat_intent


def test_plan_local_article_search_executes_without_confirmation() -> None:
    intent = detect_chat_intent("cherche des articles sur les pertes AC")

    plan = plan_chat_tools(intent, article_search_scope="local")

    assert plan.action == "execute"
    assert plan.primary_tool_name == "search.local_articles"
    assert plan.primary_call is not None
    assert plan.primary_call.arguments["query"] == intent.query


def test_plan_ambiguous_article_search_requests_scope_confirmation() -> None:
    intent = detect_chat_intent("cherche des articles sur les pertes AC")

    plan = plan_chat_tools(intent)

    assert plan.action == "confirm"
    assert plan.calls == ()
    assert plan.confirmation is not None
    assert plan.confirmation.kind == "article_search_scope"
    assert [choice["id"] for choice in plan.confirmation.choices] == ["local", "external"]


def test_plan_external_search_requires_explicit_selection_or_ack() -> None:
    intent = detect_chat_intent("cherche des articles sur les pertes AC")

    selected = plan_chat_tools(intent, article_search_scope="external")
    acknowledged = plan_chat_tools(
        intent,
        external_search_confirmed=True,
        privacy_ack=True,
    )
    incomplete_ack = plan_chat_tools(intent, external_search_confirmed=True)

    assert selected.primary_tool_name == "search.external_articles"
    assert acknowledged.primary_tool_name == "search.external_articles"
    assert incomplete_ack.action == "confirm"


@pytest.mark.parametrize(
    ("message", "tool_name"),
    [
        ("fais une revue de litterature sur les HTS", "research.literature_review"),
        ("liste tous les papers sur les HTS", "search.local_articles"),
        ("compare les papers sur les HTS", "research.compare_papers"),
        ("fais une matrice de preuves sur les HTS", "research.evidence_matrix"),
        ("verifie cette affirmation", "research.fact_check"),
        ("cree un carnet avec les sources precedentes", "notebook.create"),
        ("/md reponse precedente", "chat.export"),
    ],
)
def test_plan_existing_chat_intents(message: str, tool_name: str) -> None:
    plan = plan_chat_tools(detect_chat_intent(message))

    assert plan.action == "execute"
    assert plan.primary_tool_name == tool_name


def test_notebook_plan_adds_write_tool_only_when_requested() -> None:
    analysis_only = plan_notebook_tools(
        notebook_id="nb-1",
        message="analyse cette source",
        append_to_notes=False,
    )
    with_notes = plan_notebook_tools(
        notebook_id="nb-1",
        message="analyse et ajoute aux notes",
        append_to_notes=True,
    )

    assert [call.name for call in analysis_only.calls] == ["notebook.analyze"]
    assert [call.name for call in with_notes.calls] == [
        "notebook.analyze",
        "notebook.append_notes",
    ]
    assert get_tool_spec("notebook.append_notes").risk == "write"


def test_notebook_write_request_is_detected_without_model_tool_call() -> None:
    plan = plan_notebook_tools(
        notebook_id="nb-1",
        message="analyse cet article et mets la synthese dans les notes",
        append_to_notes=False,
    )

    assert [call.name for call in plan.calls] == [
        "notebook.analyze",
        "notebook.append_notes",
    ]


@pytest.mark.parametrize(
    "message",
    [
        "ok est-ce que tu rajouter tout ça dans les Notes ?",
        "Peux-tu rajouter tout cela aux notes du carnet ?",
        "Rajoutez cette synthèse dans les notes.",
    ],
)
def test_notebook_write_request_accepts_rajouter_variants(message: str) -> None:
    assert requests_notebook_notes_update(message) is True

    plan = plan_notebook_tools(
        notebook_id="nb-1",
        message=message,
        append_to_notes=False,
    )

    assert [call.name for call in plan.calls] == [
        "notebook.analyze",
        "notebook.append_notes",
    ]


def test_tiny_model_does_not_change_deterministic_tool_plan() -> None:
    intent = detect_chat_intent("/compare formulation H et T-A")

    tiny_model_plan = plan_chat_tools(intent)

    assert tiny_model_plan.primary_tool_name == "research.compare_papers"


def test_french_papier_plural_routes_compare_without_llm() -> None:
    intent = detect_chat_intent("Compare les papiers sur les pertes AC")

    assert intent.mode == "paper_compare"
    assert plan_chat_tools(intent).primary_tool_name == "research.compare_papers"


def test_formulation_comparison_routes_without_llm() -> None:
    intent = detect_chat_intent("Compare les formulations H et T-A")

    assert intent.mode == "paper_compare"
    assert plan_chat_tools(intent).primary_tool_name == "research.compare_papers"


@pytest.mark.parametrize(
    ("action", "message", "tool_name"),
    [
        ("answer", "Quels sont les themes principaux ?", "chat.answer"),
        ("local_search", "Liste les articles sur les HTS", "search.local_articles"),
        ("literature_review", "Fais une revue sur les HTS", "research.literature_review"),
        ("compare", "Compare les papiers sur les HTS", "research.compare_papers"),
        ("evidence_matrix", "Fais une matrice de preuves", "research.evidence_matrix"),
        ("fact_check", "Verifie ce passage", "research.fact_check"),
        ("bibliography", "Genere la bibliographie du chat", "chat.bibliography"),
        ("create_notebook", "Cree un carnet", "notebook.create"),
        ("export_markdown", "Exporte le message", "chat.export"),
        ("export_latex", "Exporte le chat", "chat.export"),
    ],
)
def test_ui_hint_actions_have_model_independent_tool_contract(
    action: str,
    message: str,
    tool_name: str,
) -> None:
    intent = intent_from_ui_hint(message, action)

    assert intent is not None
    assert intent.explicit is True
    assert plan_chat_tools(intent).primary_tool_name == tool_name


def test_ui_related_articles_hint_keeps_scope_confirmation() -> None:
    intent = intent_from_ui_hint("Cherche des articles lies", "article_search")

    assert intent is not None
    plan = plan_chat_tools(intent)
    assert plan.action == "confirm"
    assert plan.confirmation is not None
    assert plan.confirmation.kind == "article_search_scope"


def test_unknown_ui_hint_action_is_ignored() -> None:
    assert intent_from_ui_hint("question", "unknown_action") is None


def test_tiny_model_tool_choice_parser_accepts_short_label() -> None:
    assert parse_llm_tool_choice("LOCAL_SEARCH") == "LOCAL_SEARCH"
    assert parse_llm_tool_choice("Tool: LOCAL_SEARCH") == "LOCAL_SEARCH"
    assert parse_llm_tool_choice("I would use LOCAL_SEARCH for this.") == "LOCAL_SEARCH"


def test_tool_choice_parser_rejects_ambiguous_or_unknown_output() -> None:
    assert parse_llm_tool_choice("LOCAL_SEARCH or EXTERNAL_SEARCH") is None
    assert parse_llm_tool_choice("I am not sure.") is None


def test_llm_tool_choice_is_converted_to_existing_safe_intent() -> None:
    intent = intent_from_llm_tool_choice(
        "prepare un panorama des publications sur les pertes AC",
        "LOCAL_SEARCH",
    )

    assert intent is not None
    assert intent.mode == "paper_list"
    assert intent.explicit is True


@pytest.mark.parametrize("choice", ["CREATE_NOTEBOOK", "EXPORT"])
def test_llm_planner_cannot_trigger_write_tools(choice: str) -> None:
    assert intent_from_llm_tool_choice("Quels sont les themes ?", choice) is None


def test_explicit_write_choice_can_be_resolved_only_when_opted_in() -> None:
    intent = intent_from_llm_tool_choice(
        "Cree un carnet avec ces sources",
        "CREATE_NOTEBOOK",
        allow_write=True,
    )

    assert intent is not None
    assert plan_chat_tools(intent).primary_tool_name == "notebook.create"


def test_llm_planner_prompt_is_closed_choice_and_keeps_short_context() -> None:
    prompt = build_llm_tool_planner_prompt(
        "fais-en une liste",
        previous_messages=[
            {"role": "user", "content": "Nous parlons des pertes AC."},
            {"role": "assistant", "content": "Voici un premier resume."},
        ],
    )

    assert "Return exactly ONE code" in prompt
    assert "LOCAL_SEARCH" in prompt
    assert "Nous parlons des pertes AC." in prompt
    assert prompt.endswith("CODE:")


def test_tool_event_has_stable_serializable_shape() -> None:
    plan = plan_chat_tools(ChatIntent(mode="rag", query="question"))
    call = plan.primary_call

    assert call is not None
    assert tool_event("tool_started", call, status="running") == {
        "type": "tool_started",
        "tool": {"name": "chat.answer", "arguments": {"query": "question"}},
        "status": "running",
    }


def test_unknown_tool_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown chat tool"):
        get_tool_spec("unknown.tool")


def test_static_chat_suggestions_declare_explicit_actions_and_i18n_prompts() -> None:
    root = Path(__file__).resolve().parents[1]
    i18n = (root / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
    app_js = read_app_javascript()
    static_bindings = (
        root / "static" / "js" / "static-event-bindings.js"
    ).read_text(encoding="utf-8")
    actions = (
        "local_search",
        "article_search",
        "literature_review",
        "compare",
        "evidence_matrix",
        "fact_check",
        "bibliography",
        "create_notebook",
        "export_markdown",
        "export_latex",
    )

    assert static_bindings.count("applyChatHintByKey(") == 11
    for action in actions:
        assert f"'{action}'" in static_bindings
    for suffix in (
        "List",
        "Author",
        "Related",
        "Review",
        "Compare",
        "Evidence",
        "Verify",
        "Bibliography",
        "Notebook",
        "Markdown",
        "Latex",
    ):
        assert f"'chat.hintPrompt{suffix}'" in i18n
    for event_type in ("tool_started", "tool_result", "tool_confirmation"):
        assert f"event.type === '{event_type}'" in app_js
