from __future__ import annotations

from typing import Any

import pytest

from src import app_help
from src.chat_harness import plan_chat_tools
from src.chat_intents import detect_chat_intent, is_app_help_followup
from src.llm_provider import StreamAbortSignal


@pytest.mark.parametrize("message", [
    "Comment utiliser Bibliobrain ?", "À quoi sert Bibliobrain ?",
    "How do I use Bibliobrain?", "Comment connecter Zotero ?",
    "Comment créer un carnet ?", "Comment importer mes PDF ?",
    "Comment ajouter des articles ?", "Comment fonctionne le chat ?",
    "How do I use notebooks?", "Comment accéder aux paramètres ?",
    "Where can I configure Ollama?", "/help", "/aide recherche dans le chat",
    "Comment puis-je créer un nouveau carnet ?", "How do I add papers to my library?",
    "Comment importer des articles dans Zotero ?", "How does the chat work?",
    "Où sont stockés mes PDF ?", "Where are my PDFs stored?", "Où trouver les paramètres ?",
    "Comment ouvrir l'onglet Découverte ?", "Comment indexer mes PDF ?",
    "Explique-moi comment connecter LM Studio", "What are notebooks for?",
    "How can I connect Zotero to Bibliobrain?", "Pourquoi l'indexation est lente ?",
    "Ça ne marche pas, Zotero n'est pas détecté", "Ollama ne répond pas",
])
def test_help_routes_without_a_model_planner(message: str) -> None:
    intent = detect_chat_intent(message)
    assert intent.mode == "app_help"
    assert plan_chat_tools(intent).primary_tool_name == "app.help"


# A help answer has no sources and no warning: a scientific question routed to
# it is silently lost. Every word that names a feature is also a research word.
@pytest.mark.parametrize("message", [
    "How do experimental settings affect the results?",
    "How does climate change affect library usage?",
    "How do students use chat-based tutors?",
    "Comment fonctionne l'indexation des documents dans Elasticsearch ?",
    "How does indexing of documents work in search engines?",
    "Comment les chercheurs utilisent-ils les carnets de laboratoire ?",
    "How can OCR change archival research?",
    "How do clinicians use notebooks in practice?",
    "How can I use lab notebooks for reproducibility?",
    "How can I use notebooks for reproducibility?",
    "How do I change the settings of a PID controller?",
    "How do we index documents in a vector database?",
    "How to add documents to a corpus?",
    "Comment fonctionne le chat domestique ?",
    "Comment configurer les paramètres d'un modèle de régression ?",
    "How do libraries support open science?",
    "Where is the library of Alexandria?",
    "Comment créer un index de vulnérabilité ?",
    "How does OCR work?",
    "How to choose a model?",
    "Comment changer de modèle ?",
    "What does the literature say about Bibliobrain?",
    "How do Jupyter notebooks affect reproducibility?",
    "Comment les bibliothèques numériques organisent-elles leurs collections ?",
    "How do I find papers on transformers?",
    "Comment trouver des articles sur le climat ?",
    "Comment importer des données dans R ?",
    "How to import data into pandas?",
    "Where are the settings for the simulation described?",
    "Comment la découverte de la pénicilline a-t-elle changé la médecine ?",
    "How can I find the parameters in my papers?",
    "Comment fonctionne la rédaction scientifique ?",
    "How does Zotero handle citations in the papers?",
    "Comment organiser mes articles par thème ?",
])
def test_help_never_takes_a_scientific_question(message: str) -> None:
    assert detect_chat_intent(message).mode != "app_help"


@pytest.mark.parametrize(("message", "mode"), [
    ("Comment modéliser les pertes AC ?", "rag"),
    ("Comment créer un modèle de diffusion ?", "rag"),
    ("How do neural networks work?", "rag"),
    ("Que disent mes articles sur Bibliobrain ?", "rag"),
    ("Crée un carnet depuis ces sources", "notebook_create"),
    ("Exporte la conversation en Markdown", "document_export"),
    ("/rag Comment utiliser Bibliobrain ?", "rag"),
    ("Résume ceci. Texte : comment utiliser Bibliobrain ?", "rag"),
])
def test_help_preserves_scientific_questions_and_actions(message: str, mode: str) -> None:
    assert detect_chat_intent(message).mode == mode


def test_help_followup_requires_previous_help_and_stays_narrow() -> None:
    history = [{"role": "assistant", "meta": {"mode": "app_help"}}]
    assert is_app_help_followup("Et ensuite ?", history)
    assert is_app_help_followup("Je ne trouve pas", history)
    assert not is_app_help_followup("Et comment modéliser les pertes AC ?", history)
    assert not is_app_help_followup("Et ensuite ?", [])
    assert not is_app_help_followup("Et ensuite ?", [{"role": "assistant", "meta": {"mode": "rag"}}])


def test_help_reads_only_bundled_docs_once_and_bounds_context(monkeypatch, workspace_tmp_dir) -> None:
    (workspace_tmp_dir / "Home.md").write_text("# Accueil\nGuide local", encoding="utf-8")
    (workspace_tmp_dir / "Parametres.md").write_text(
        "# Paramètres\n## Connexion Zotero\n" + "Connecter Zotero. " * 2000, encoding="utf-8",
    )
    (workspace_tmp_dir / "secret.md").write_text("PRIVATE_ARTICLE", encoding="utf-8")
    monkeypatch.setattr(app_help, "_WIKI_ROOT", workspace_tmp_dir)
    app_help._load_sections.cache_clear()
    try:
        messages, pages = app_help._help_messages("connecter Zotero", [], "fr")
        assert pages == ["Parametres"]
        context = messages[-1]["content"].split("<documentation>\n")[1].split("\n</documentation>")[0]
        assert len(context) <= app_help._MAX_CONTEXT_CHARS
        assert "PRIVATE_ARTICLE" not in str(messages)
        (workspace_tmp_dir / "Parametres.md").unlink()
        assert app_help._help_messages("connecter Zotero", [], "en")[1] == pages
        assert app_help._load_sections.cache_info().hits > 0
    finally:
        app_help._load_sections.cache_clear()


@pytest.mark.parametrize(("query", "page"), [
    ("How to configure Ollama settings?", "Parametres"),
    ("How to create notebooks?", "Carnets"),
    ("Comment connecter Zotero ?", "Parametres"),
])
def test_real_wiki_selection_in_both_languages(query: str, page: str) -> None:
    _, pages = app_help._help_messages(query, [], "en")
    assert page in pages


@pytest.mark.parametrize("query", [
    "Comment sauvegarder mes données ?", "How do I back up my data?",
    "How do I restore a backup?", "Comment désinstaller Bibliobrain ?",
])
def test_backup_questions_reach_the_maintenance_page(query: str) -> None:
    _, pages = app_help._help_messages(query, [], "fr")
    assert "Maintenance" in pages


def test_documentation_links_point_to_wiki_files_in_the_answer_language() -> None:
    url = app_help._WIKI_URL

    assert app_help._documentation_links(["Maintenance", "Premier-lancement"], "fr") == (
        f"[Maintenance]({url}Maintenance.md) · [Premier lancement]({url}Premier-lancement.md)"
    )
    assert app_help._documentation_links(["Premier-lancement"], "en") == (
        f"[First launch]({url}First-launch-en.md)"
    )
    # Every page the help reads exists, in both languages: a link never 404s.
    for page in app_help._PAGES:
        assert (app_help._WIKI_ROOT / f"{page}.md").is_file()
        assert (app_help._WIKI_ROOT / f"{app_help._ENGLISH_PAGES[page]}.md").is_file()


@pytest.mark.parametrize("ollama", [True, False])
def test_help_streams_one_model_call_and_keeps_docs_out_of_academic_sources(monkeypatch, ollama) -> None:
    calls: list[Any] = []

    def fake_stream(*args, **kwargs):
        calls.append((args, kwargs))
        yield {"content": "Ouvrez les Paramètres."} if ollama else "Ouvrez les Paramètres."

    monkeypatch.setattr(app_help, "is_ollama_provider", lambda: ollama)
    monkeypatch.setattr(app_help, "stream_ollama_chat", fake_stream)
    monkeypatch.setattr(app_help, "create_chat_llm", lambda model: object())
    monkeypatch.setattr(app_help, "stream_text", fake_stream)
    history = [{"role": "assistant", "content": "PRIVATE_PAPER", "meta": {"mode": "rag"}}]
    events = list(app_help.stream_app_help(
        "Comment connecter Zotero ?", "demo", previous_messages=history,
    ))
    assert len(calls) == 1
    assert "PRIVATE_PAPER" not in str(calls)
    assert "Zotero" in str(calls)
    assert events[-1] == {"type": "sources", "data": [], "answer_origin": "app_help"}
    assert "Documentation :" in "".join(event.get("text", "") for event in events)


def test_missing_documentation_does_not_call_model(monkeypatch, workspace_tmp_dir) -> None:
    monkeypatch.setattr(app_help, "_WIKI_ROOT", workspace_tmp_dir)
    app_help._load_sections.cache_clear()
    monkeypatch.setattr(app_help, "is_ollama_provider", lambda: pytest.fail("No model without docs"))
    try:
        events = list(app_help.stream_app_help("/help", "demo", previous_messages=[]))
        assert "indisponible" in str(events)
    finally:
        app_help._load_sections.cache_clear()


def test_help_honors_stop_before_reading_documentation(monkeypatch) -> None:
    signal = StreamAbortSignal()
    signal.abort("user_stop")
    monkeypatch.setattr(app_help, "_load_sections", lambda: pytest.fail("Aborted request read docs"))
    with pytest.raises(InterruptedError):
        list(app_help.stream_app_help("/help", "demo", previous_messages=[], abort_signal=signal))
