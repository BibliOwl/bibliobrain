from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import src.config as config


def _load_notebooks_module(workspace_tmp_dir: Path) -> Any:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    config.STORAGE_DIR = str(storage_dir)
    config.DATA_PATH = str(data_dir)

    module = cast(Any, importlib.import_module("src.notebooks"))
    return cast(Any, importlib.reload(module))


def test_concurrent_message_updates_are_not_lost(workspace_tmp_dir: Path) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    notebook = notebooks.create_notebook("Concurrent")

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda index: notebooks.add_message(notebook["id"], "user", str(index)),
                range(32),
            )
        )

    persisted = notebooks.get_notebook(notebook["id"])
    assert persisted is not None
    assert sorted(message["content"] for message in persisted["messages"]) == sorted(
        str(index) for index in range(32)
    )


def test_notebook_id_cannot_escape_storage_directory(workspace_tmp_dir: Path) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    outside = Path(config.STORAGE_DIR) / "secret.json"
    outside.write_text('{"secret": true}', encoding="utf-8")

    assert notebooks.get_notebook("..\\secret") is None
    assert notebooks.delete_notebook("..\\secret") is False
    assert outside.exists()


def test_notebook_response_style_guide_includes_model_profile_note(
    workspace_tmp_dir: Path,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)

    guide = notebooks._notebook_response_style_guide(
        "je cherche des formulations HTS sur COMSOL",
        model_name="gemma4:e2b",
    )

    assert "Consignes specifiques au modele:" in guide
    assert "N'utilise pas de sections imposees" in guide
    assert "Utilise exactement cette structure" not in guide
    assert "Do not use mandatory wrapper sections" in guide
    assert "Never emit Gemma channel markers" in guide


def test_add_sources_persists_batch_with_one_write(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    notebook = notebooks.create_notebook("Import groupe")
    writes = 0
    original_write = notebooks._write

    def counted_write(payload: dict) -> None:
        nonlocal writes
        writes += 1
        original_write(payload)

    monkeypatch.setattr(notebooks, "_write", counted_write)
    added = notebooks.add_sources(
        notebook["id"],
        [
            {"title": "A", "content": "Texte A", "source_path": "a.pdf"},
            {"title": "B", "content": "Texte B", "source_path": "b.pdf"},
        ],
    )

    assert added is not None
    assert len(added) == 2
    assert writes == 1
    assert [source["title"] for source in notebooks.get_notebook(notebook["id"])["sources"]] == [
        "A",
        "B",
    ]


def test_notebook_context_preserves_visual_evidence_when_text_is_long(
    workspace_tmp_dir: Path,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)

    snippets = notebooks._fit_source_snippets(
        [
            {
                "title": "Article illustré",
                "content": "texte " * 1000,
                "visual_content": "Figure 4 : la courbe rouge atteint 12 teslas.",
            }
        ],
        per_source_limit=1200,
        total_limit=2000,
    )

    assert len(snippets) == 1
    assert "[Texte extrait]" in snippets[0]
    assert "[Figures analysées]" in snippets[0]
    assert "12 teslas" in snippets[0]


def test_notebook_chat_can_auto_analyze_and_persist_pdf_figures(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    pdf_path = Path(config.DATA_PATH) / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-1.7\n")
    nb = notebooks.create_notebook("Figures")
    source = notebooks.add_source(
        nb["id"],
        "Article HTS",
        "Le texte mentionne la figure 2.",
        "paper.pdf",
    )
    assert source is not None

    monkeypatch.setattr(
        notebooks,
        "get_app_settings",
        lambda: {
            "visual_analysis_mode": "on_demand",
            "vision_model": "vision:test",
            "visual_analysis_auto_chat": True,
        },
    )
    monkeypatch.setattr(
        notebooks,
        "analyze_pdf_visuals",
        lambda *_args, **_kwargs: {
            "model": "vision:test",
            "results": [{"page": 2, "text": "La courbe rouge atteint 12 teslas."}],
        },
    )
    query_engine = importlib.import_module("src.rag.query_engine")
    monkeypatch.setattr(
        query_engine,
        "_decide_auto_visual_analysis",
        lambda *_args, **_kwargs: SimpleNamespace(analyze=True, source_index=1),
    )
    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda _model: object())
    captured: dict[str, str] = {}

    def fake_stream_text(_llm: object, prompt: str, abort_signal=None) -> list[str]:
        captured["prompt"] = prompt
        return ["La figure atteint 12 teslas [1]."]

    monkeypatch.setattr(notebooks, "stream_text", fake_stream_text)

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "Que montre la courbe de la figure 2 ?",
            "chat:test",
        )
    )

    assert any(event.get("type") == "status" for event in events)
    assert "[Figures analysées]" in captured["prompt"]
    assert "12 teslas" in captured["prompt"]
    saved_source = notebooks.get_notebook(nb["id"])["sources"][0]
    assert saved_source["visual_model"] == "vision:test"
    assert saved_source["visual_pages"] == [2]
    assert "12 teslas" in saved_source["visual_content"]


def test_stream_notebook_chat_sanitizes_remote_stream_output(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
    ) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Carnet test")
    notebooks.add_source(nb["id"], "Source test", "Contenu source pour la question.")

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())
    monkeypatch.setattr(
        notebooks,
        "stream_text",
        lambda llm, prompt, abort_signal=None: [
            "<think>raisonnement",
            " cache</think>\n<|channel|>thought bruit cache<channel|>",
            "\nSortie propre avec citation [1].",
        ],
    )

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "question",
            "gemma4:e2b",
            thinking_enabled=False,
        )
    )

    tokens = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    assert "<think>" not in tokens
    assert "<|channel|>" not in tokens
    assert "raisonnement" not in tokens
    assert tokens.startswith("Sortie propre")


def test_stream_notebook_chat_expands_context_for_detailed_single_article_request(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Article unique")
    long_content = " ".join(f"detail-{idx}" for idx in range(1200))
    notebooks.add_source(nb["id"], "Article HTS", long_content)
    captured: dict[str, str] = {}

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())

    def fake_stream_text(llm: object, prompt: str, abort_signal: Any = None) -> list[str]:
        captured["prompt"] = prompt
        return ["Analyse naturelle ok [1]."]

    monkeypatch.setattr(notebooks, "stream_text", fake_stream_text)

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "fais moi une analyse détaillée de cet article",
            "demo-model",
        )
    )

    tokens = "".join(str(event.get("text") or "") for event in events if event.get("type") == "token")
    prompt = captured["prompt"]
    assert "traite la source [1] comme l'article demande" in prompt
    assert "detail-800" in prompt
    assert "pas d'un resume allonge" in prompt
    assert "700 a 1 200 mots" in prompt
    assert "resultats quantitatifs" in prompt
    assert "Prefere 1 ou 2 courts paragraphes" not in prompt
    assert "N'utilise pas de sections imposees" in prompt
    assert "Utilise exactement cette structure" not in prompt
    assert tokens.startswith("Analyse naturelle ok")


def test_stream_notebook_chat_expands_context_for_detailed_summary_request(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Article avec page de garde")
    front_matter = "Related content et metadonnees. " * 120
    article_body = "Abstract, methode et resultats. " * 900 + "RESULTAT_FINAL_1_63_T"
    notebooks.add_source(nb["id"], "Article HTS", front_matter + article_body)
    captured: dict[str, str] = {}

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())

    def fake_stream_text(llm: object, prompt: str, abort_signal: Any = None) -> list[str]:
        captured["prompt"] = prompt
        return ["Resume detaille ok [1]."]

    monkeypatch.setattr(notebooks, "stream_text", fake_stream_text)

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "fais un résumé détaillé",
            "demo-model",
        )
    )

    tokens = "".join(str(event.get("text") or "") for event in events if event.get("type") == "token")
    prompt = captured["prompt"]
    assert "RESULTAT_FINAL_1_63_T" in prompt
    assert "fond du document" in prompt
    assert "corps de l'article" in prompt
    assert "pas d'un resume allonge" not in prompt
    assert "Prefere 1 ou 2 courts paragraphes" not in prompt
    assert tokens.startswith("Resume detaille ok")


def test_tiny_notebook_model_allows_longer_output_for_deep_analysis(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Petit modele")
    notebooks.add_source(nb["id"], "Article", "evidence " * 1000)
    captured: dict[str, Any] = {}

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: False)
    monkeypatch.setattr(notebooks, "is_tiny_model", lambda model_name: True)

    def fake_stream_ollama_chat(model_name: str, messages: list[dict], **kwargs: Any):
        captured["messages"] = messages
        captured["options"] = kwargs.get("options_override")
        return [{"content": "Analyse structurée [1].", "thinking": ""}]

    monkeypatch.setattr(notebooks, "stream_ollama_chat", fake_stream_ollama_chat)

    list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "Fais une analyse approfondie de cet article",
            "tiny-demo",
        )
    )

    system_prompt = captured["messages"][0]["content"]
    assert captured["options"]["num_predict"] == notebooks._NB_TINY_MODEL_DEEP_MAX_PREDICT
    assert "pas d'un resume allonge" in system_prompt
    assert "plus de trois phrases" not in system_prompt
    assert "evidence evidence" in system_prompt


def test_stream_notebook_chat_uses_notebook_notes_when_no_sources(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Notes seules")
    notebooks.update_notes(nb["id"], "Analyse copiee: contribution, methode et limites.")
    captured: dict[str, str] = {}

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())

    def fake_stream_text(llm: object, prompt: str, abort_signal: Any = None) -> list[str]:
        captured["prompt"] = prompt
        return ["Notes ok."]

    monkeypatch.setattr(notebooks, "stream_text", fake_stream_text)

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "resume les notes du carnet",
            "demo-model",
        )
    )

    tokens = "".join(str(event.get("text") or "") for event in events if event.get("type") == "token")
    assert "[Notes du carnet]" in captured["prompt"]
    assert "Analyse copiee" in captured["prompt"]
    assert "aucune source" not in tokens.lower()
    assert tokens.startswith("Notes ok")


def test_stream_notebook_chat_can_run_without_history_or_persistence(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Recherche rédaction")
    notebooks.add_source(nb["id"], "Source test", "Résultat étayé pour le brouillon.")
    notebooks.update_notes(nb["id"], "Notes initiales.")
    notebooks.add_message(nb["id"], "user", "Ancienne question sans rapport")
    notebooks.add_message(nb["id"], "assistant", "Ancienne réponse sans rapport")
    before_notebook = notebooks.get_notebook(nb["id"])
    before = before_notebook["messages"]
    captured: dict[str, str] = {}

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())

    def fake_stream_text(llm: object, prompt: str, abort_signal: Any = None) -> list[str]:
        captured["prompt"] = prompt
        return ["Réponse sourcée [1]."]

    monkeypatch.setattr(notebooks, "stream_text", fake_stream_text)

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "Que dit le carnet sur ce passage ? Ajoute la réponse aux notes.",
            "demo-model",
            persist_history=False,
            read_only=True,
        )
    )

    tokens = "".join(str(event.get("text") or "") for event in events if event.get("type") == "token")
    assert tokens == "Réponse sourcée [1]."
    assert "Ancienne question sans rapport" not in captured["prompt"]
    assert notebooks.get_notebook(nb["id"])["messages"] == before
    assert notebooks.get_notebook(nb["id"])["notes"] == before_notebook["notes"]
    assert not any(event.get("type") == "notes_updated" for event in events)


def test_stream_notebook_chat_appends_requested_answer_to_notes(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Notes auto")
    notebooks.add_source(nb["id"], "Source test", "La source explique le phenomene.")
    notebooks.update_notes(nb["id"], "Note existante.")

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())

    def fake_stream_text(llm: object, prompt: str, abort_signal: Any = None) -> list[str]:
        assert "directement reutilisable comme note" in prompt
        assert "[Notes du carnet]" in prompt
        return ["Explication a conserver [1]."]

    monkeypatch.setattr(notebooks, "stream_text", fake_stream_text)

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "explique ce point et ajoute le dans les notes de ce carnet",
            "demo-model",
        )
    )

    notes_events = [event for event in events if event.get("type") == "notes_updated"]
    assert len(notes_events) == 1
    updated_notes = str(notes_events[0].get("notes") or "")
    assert "Note existante." in updated_notes
    assert "### Note du chat -" not in updated_notes
    assert "explique ce point" not in updated_notes
    assert "Explication a conserver [1]." in updated_notes

    saved = notebooks.get_notebook(nb["id"])
    assert saved["notes"] == updated_notes
    assert saved["messages"][-1]["content"] == "Explication a conserver [1]."


def test_stream_notebook_chat_extracts_note_section_only(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Notes propres")
    notebooks.add_source(nb["id"], "Source test", "Contexte source.")

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())
    monkeypatch.setattr(
        notebooks,
        "stream_text",
        lambda llm, prompt, abort_signal=None: [
            "Explication visible.\n\n**Notes pour le carnet :**\nA conserver seulement [1]."
        ],
    )

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "explique et mets le dans les notes",
            "demo-model",
        )
    )

    notes_event = next(event for event in events if event.get("type") == "notes_updated")
    assert notes_event["notes"] == "A conserver seulement [1]."
    saved = notebooks.get_notebook(nb["id"])
    assert saved["notes"] == "A conserver seulement [1]."
    assert saved["messages"][-1]["content"].startswith("Explication visible.")


def test_stream_notebook_chat_appends_exact_user_note_request(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("WCPM")
    notebooks.add_source(nb["id"], "Article WCPM", "La WCPM controle la forme d'onde.")

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())
    monkeypatch.setattr(
        notebooks,
        "stream_text",
        lambda llm, prompt, abort_signal=None: ["Principe WCPM a noter [1]."],
    )

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "explique le principe de la WCPM et ajoute le dans les notes de ce carnet",
            "demo-model",
        )
    )

    assert any(event.get("type") == "notes_updated" for event in events)
    saved = notebooks.get_notebook(nb["id"])
    assert "Principe WCPM a noter [1]." in saved["notes"]


def test_stream_notebook_chat_appends_when_request_flag_is_set(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Flag notes")
    notebooks.add_source(nb["id"], "Source test", "Contexte source.")

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())
    monkeypatch.setattr(
        notebooks,
        "stream_text",
        lambda llm, prompt, abort_signal=None: ["Reponse ajoutee par flag [1]."],
    )

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "resume ce passage",
            "demo-model",
            append_to_notes=True,
        )
    )

    assert any(event.get("type") == "notes_updated" for event in events)
    saved = notebooks.get_notebook(nb["id"])
    assert "Reponse ajoutee par flag [1]." in saved["notes"]


def test_notebook_chat_refuses_general_answer_without_sources(workspace_tmp_dir: Path) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Carnet vide")

    events = list(
        notebooks.stream_notebook_chat(
            nb["id"],
            "Explique les pertes AC dans les aimants HTS",
            "test-model",
        )
    )

    text = "".join(str(event.get("text") or "") for event in events if event.get("type") == "token")
    assert "exploitable" in text
    assert "aucune source" in text.lower()
    assert "Answer:" not in text
    assert "Explanation:" not in text
    assert "Response note:" not in text
    saved = notebooks.get_notebook(nb["id"])
    assert saved["messages"][0]["role"] == "user"
    assert saved["messages"][1]["role"] == "assistant"


def test_remove_notebook_message_by_index(workspace_tmp_dir: Path) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Carnet test")
    notebooks.add_message(nb["id"], "user", "Question")
    notebooks.add_message(nb["id"], "assistant", "Reponse")

    assert notebooks.remove_message(nb["id"], 0) is True
    saved = notebooks.get_notebook(nb["id"])
    assert [msg["content"] for msg in saved["messages"]] == ["Reponse"]
    assert notebooks.remove_message(nb["id"], 99) is False


def test_stream_notebook_chat_keeps_notes_apart_from_numbered_sources(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Notes et sources")
    notebooks.add_source(nb["id"], "Article A", "Le taux observe est de 12 pour cent.")
    notebooks.update_notes(nb["id"], "Mon hypothese: le taux varie selon la saison.")
    captured: dict[str, str] = {}

    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())

    def fake_stream_text(llm: object, prompt: str, abort_signal: Any = None) -> list[str]:
        captured["prompt"] = prompt
        return ["Ok [1]."]

    monkeypatch.setattr(notebooks, "stream_text", fake_stream_text)

    list(notebooks.stream_notebook_chat(nb["id"], "que sait-on du taux ?", "demo-model"))

    prompt = captured["prompt"]
    sources_at = prompt.index("[1] Article A")
    notes_at = prompt.index("[Notes du carnet]")
    assert sources_at < notes_at
    # The notes are introduced as non-citable, after the numbered sources.
    assert "Ce ne sont PAS des sources" in prompt[sources_at:notes_at]
    assert "ne se citent jamais avec [x]" in prompt
    assert "d'apres vos notes" in prompt
    assert "les sources numerotees et les notes du carnet" in prompt


def test_long_source_shows_its_start_and_its_conclusion(workspace_tmp_dir: Path) -> None:
    # The notebook chat used to send only the first 1200 characters of a PDF.
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    body = " ".join(f"method-{idx}" for idx in range(3000))
    text = f"Abstract. We model screening currents.\n{body}\n5. Conclusion\nThe T-A model is faster."

    excerpt = notebooks._source_excerpt(text, 1200, "Sur quoi ces sources sont-elles en désaccord ?")

    assert len(excerpt) <= 1210
    assert excerpt.startswith("Abstract. We model screening currents.")
    assert "Conclusion The T-A model is faster." in excerpt


def test_long_source_prefers_the_passage_matching_the_question(workspace_tmp_dir: Path) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    filler = " ".join(f"word{idx}" for idx in range(1500))
    text = f"Intro. {filler} The hysteresis losses reach 3 J per cycle. {filler} Conclusion. Done."

    excerpt = notebooks._source_excerpt(text, 1200, "What are the hysteresis losses?")

    assert "hysteresis losses reach 3 J" in excerpt


def _prompt_for(notebooks: Any, monkeypatch: Any, nb_id: str, message: str) -> str:
    captured: dict[str, str] = {}
    monkeypatch.setattr(notebooks, "is_remote_provider", lambda: True)
    monkeypatch.setattr(notebooks, "create_chat_llm", lambda model_name: object())

    def fake_stream_text(llm: object, prompt: str, abort_signal: Any = None) -> list[str]:
        captured["prompt"] = prompt
        return ["ok [1]."]

    monkeypatch.setattr(notebooks, "stream_text", fake_stream_text)
    list(notebooks.stream_notebook_chat(nb_id, message, "demo-model", persist_history=False))
    return captured["prompt"]


def test_every_suggested_question_gets_its_own_task(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("REBCO")
    notebooks.add_source(nb["id"], "T-A homogeneous model", "Abstract. Screening currents.")
    notebooks.add_source(nb["id"], "MI magnet model", "Abstract. Metal-insulated coils.")
    expected = {
        "Sur quoi ces sources sont-elles en désaccord ?": "Ne conclus pas seulement qu'aucun desaccord",
        "What do these sources disagree on?": "Ne conclus pas seulement qu'aucun desaccord",
        "Quelles sont les idées principales de ces sources ?": "l'idee principale de chaque source",
        "What are the main ideas of these sources?": "l'idee principale de chaque source",
        "Quelles questions ces sources laissent-elles ouvertes ?": "Releve les limites, questions ouvertes",
        "Which questions do these sources leave open?": "Releve les limites, questions ouvertes",
    }
    for message, task in expected.items():
        prompt = _prompt_for(notebooks, monkeypatch, nb["id"], message)
        assert task in prompt, message
        assert "Prefere 1 ou 2 courts paragraphes" not in prompt
    plain = _prompt_for(notebooks, monkeypatch, nb["id"], "Quelle est la divergence du champ ?")
    assert "Ne conclus pas seulement" not in plain


def test_text_only_sources_are_marked_for_the_model(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Notes")
    notebooks.add_source(nb["id"], "Pasted abstract", "Title. Abstract only.")

    prompt = _prompt_for(notebooks, monkeypatch, nb["id"], "De quoi parle cet article ?")

    assert "[1] Pasted abstract (texte ajoute a la main, sans PDF)" in prompt
    assert "ne lui prete pas de details absents" in prompt
