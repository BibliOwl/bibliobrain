from __future__ import annotations

import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import pytest

import src.config as config


@pytest.fixture(autouse=True)
def _restore_app_settings_storage():
    """Put STORAGE_DIR and the settings module back after each test.

    The tests below point them at a temporary folder that is deleted at the
    end; later tests then failed on that missing folder.
    """
    original = config.STORAGE_DIR
    yield
    config.STORAGE_DIR = original
    importlib.reload(importlib.import_module("src.app_settings"))


def _load_app_settings_module(storage_dir: Path) -> Any:
    """Recharge le module avec un STORAGE_DIR local au test pour isoler le fichier JSON."""
    config.STORAGE_DIR = str(storage_dir)
    module = cast(Any, importlib.import_module("src.app_settings"))
    return cast(Any, importlib.reload(module))


def test_app_settings_persists_normalized_values(workspace_tmp_dir: Path) -> None:
    """Les valeurs utilisateur doivent etre normalisees puis relues avec les settings LLM merges."""
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.get_generation_settings = lambda: {
        "temperature": 0.3,
        "repeat_penalty": 1.1,
        "top_p": 0.7,
    }
    app_settings.get_ollama_context_length = lambda: 16384

    calls: list[dict[str, Any]] = []

    def fake_set_generation_settings(**kwargs: Any) -> None:
        calls.append(dict(kwargs))

    app_settings.set_generation_settings = fake_set_generation_settings
    context_calls: list[int] = []
    app_settings.set_ollama_context_length = lambda value: context_calls.append(int(value))

    payload = app_settings.set_app_settings(
        llm_thinking="true",
        threshold="9.0",
        max_chunks="99",
        data_path="  /tmp/data  ",
        zotero_path="  /tmp/zotero.sqlite  ",
        metadata_source="zotero",
        auto_enrich_pdf_metadata="true",
        allow_external_api_requests="yes",
        research_scope_enabled="false",
        discovery_query_planner="true",
        discovery_candidate_multiplier="7",
        discovery_llm_validation_multiplier="6",
        unpaywall_email="  test@example.com  ",
        debug_metrics="on",
        first_rag_init_done="true",
        feature_tour_done_v1="true",
        reranker_profile="ettin_cpu",
        reranker_translate_french="false",
        selected_model="  qwen3:8b  ",
        visual_analysis_mode="index",
        vision_model="  glm-ocr:q8_0  ",
        visual_description_model="  vision:9b  ",
        visual_analysis_auto_chat="true",
        ui_language="en",
        temperature=0.5,
        repeat_penalty=1.3,
        top_p=0.9,
        ollama_context_length=32768,
    )

    assert payload["llm_thinking"] is True
    assert payload["threshold"] == 1.0
    assert payload["max_chunks"] == 20
    assert payload["data_path"] == "/tmp/data"
    assert payload["zotero_path"] == "/tmp/zotero.sqlite"
    assert payload["metadata_source"] == "zotero"
    assert payload["auto_enrich_pdf_metadata"] is False
    assert payload["allow_external_api_requests"] is True
    assert payload["allow_external_discovery"] is True
    assert payload["research_scope_enabled"] is False
    assert payload["discovery_query_planner"] is True
    assert payload["discovery_candidate_multiplier"] == 7
    assert payload["discovery_llm_validation_multiplier"] == 6
    assert payload["unpaywall_email"] == "test@example.com"
    assert payload["debug_metrics"] is True
    assert payload["first_rag_init_done"] is True
    assert payload["feature_tour_done_v1"] is True
    assert payload["reranker_profile"] == "ettin_cpu"
    assert payload["reranker_translate_french"] is False
    assert payload["selected_model"] == "qwen3:8b"
    assert payload["visual_analysis_mode"] == "index"
    assert payload["vision_model"] == "glm-ocr:q8_0"
    assert payload["visual_description_model"] == "vision:9b"
    assert payload["visual_analysis_auto_chat"] is True
    assert payload["ui_language"] == "en"
    assert payload["temperature"] == 0.3
    assert payload["ollama_context_length"] == 16384
    assert calls == [{"temperature": 0.5, "repeat_penalty": 1.3, "top_p": 0.9}]
    assert context_calls == [32768]

    payload = app_settings.set_app_settings(ui_language="de")
    assert payload["ui_language"] == ""


def test_app_settings_accepts_legacy_external_discovery_alias(workspace_tmp_dir: Path) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.get_generation_settings = lambda: {}

    payload = app_settings.set_app_settings(allow_external_discovery="true")

    assert payload["allow_external_api_requests"] is True
    assert payload["allow_external_discovery"] is True


def test_research_scope_feature_is_enabled_by_default(workspace_tmp_dir: Path) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.get_generation_settings = lambda: {}

    assert app_settings.get_app_settings()["research_scope_enabled"] is True


def test_app_settings_infers_metadata_source_for_legacy_settings(
    workspace_tmp_dir: Path,
) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.get_generation_settings = lambda: {}

    assert app_settings.set_app_settings(zotero_path="references.bib")["metadata_source"] == "bibtex"
    assert app_settings.set_app_settings(
        zotero_path="C:/Zotero/zotero.sqlite"
    )["metadata_source"] == "zotero"
    assert app_settings.set_app_settings(
        zotero_path="",
        metadata_source="none",
    )["metadata_source"] == "none"


def test_pdf_metadata_enrichment_is_opt_in_and_disabled_only_for_zotero(
    workspace_tmp_dir: Path,
) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.get_generation_settings = lambda: {}

    assert app_settings.get_app_settings()["auto_enrich_pdf_metadata"] is False

    standalone = app_settings.set_app_settings(
        metadata_source="none",
        auto_enrich_pdf_metadata=True,
    )
    assert standalone["auto_enrich_pdf_metadata"] is True

    zotero = app_settings.set_app_settings(metadata_source="zotero")
    assert zotero["auto_enrich_pdf_metadata"] is False

    bibtex = app_settings.set_app_settings(
        metadata_source="bibtex",
        auto_enrich_pdf_metadata=True,
    )
    assert bibtex["auto_enrich_pdf_metadata"] is True


def test_app_settings_falls_back_when_json_is_invalid(workspace_tmp_dir: Path) -> None:
    """Un fichier JSON corrompu ne doit pas casser la lecture des settings applicatifs."""
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.get_generation_settings = lambda: {
        "temperature": 0.1,
        "repeat_penalty": 1.2,
        "top_p": 0.5,
    }

    settings_path = Path(config.STORAGE_DIR) / "app_settings.json"
    settings_path.write_text("{invalid", encoding="utf-8")

    payload = app_settings.get_app_settings()

    assert payload["threshold"] == 0.3
    assert payload["max_chunks"] == 6
    assert payload["discovery_query_planner"] is False
    assert payload["discovery_candidate_multiplier"] == 4
    assert payload["discovery_llm_validation_multiplier"] == 3
    assert payload["reranker_enabled"] is True
    assert payload["reranker_profile"] == "auto"
    assert payload["reranker_translate_french"] is True
    assert payload["selected_model"] == ""
    assert payload["visual_analysis_mode"] == "off"
    assert payload["vision_model"] == ""
    assert payload["visual_description_model"] == ""
    assert payload["visual_analysis_auto_chat"] is False
    assert payload["ui_language"] == ""
    assert payload["temperature"] == 0.1


def test_app_settings_rejects_unknown_visual_mode(workspace_tmp_dir: Path) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.get_generation_settings = lambda: {}

    payload = app_settings.set_app_settings(
        visual_analysis_mode="always_and_download_everything",
        vision_model=" model ",
    )

    assert payload["visual_analysis_mode"] == "off"
    assert payload["vision_model"] == "model"


def test_app_settings_rejects_unknown_reranker_profile(workspace_tmp_dir: Path) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.get_generation_settings = lambda: {}

    payload = app_settings.set_app_settings(reranker_profile="foreign_python_model")

    assert payload["reranker_profile"] == "auto"


def test_app_settings_accepts_reviewed_optional_reranker_profile(
    workspace_tmp_dir: Path,
) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.get_generation_settings = lambda: {}

    payload = app_settings.set_app_settings(reranker_profile="gte_modernbert_cpu")

    assert payload["reranker_profile"] == "gte_modernbert_cpu"


def test_app_settings_preserves_concurrent_partial_updates(workspace_tmp_dir: Path) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda index: app_settings._save_app_settings_payload({f"key_{index}": index}),
                range(24),
            )
        )

    payload = app_settings._load_app_settings_payload()
    assert payload == {f"key_{index}": index for index in range(24)}


def test_onboarding_progress_keeps_only_known_entries(workspace_tmp_dir: Path) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)

    fresh = app_settings.get_app_settings()["onboarding_progress"]
    assert fresh == {
        "tutorial_done": False,
        "tips_seen": [],
        "milestones": [],
        "checklist_hidden": False,
    }

    saved = app_settings.set_app_settings(
        onboarding_progress={
            "tutorial_done": "true",
            "tips_seen": ["drafting", "unknown", "library", "drafting"],
            "milestones": ["source-opened", "rm -rf", "question-asked"],
            "checklist_hidden": False,
            "extra": "dropped",
        }
    )["onboarding_progress"]

    assert saved == {
        "tutorial_done": True,
        "tips_seen": ["library", "drafting"],
        "milestones": ["question-asked", "source-opened"],
        "checklist_hidden": False,
    }


def test_onboarding_progress_is_dismissed_for_installs_that_finished_the_old_tour(
    workspace_tmp_dir: Path,
) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir)
    app_settings.set_app_settings(feature_tour_done_v1=True)

    progress = app_settings.get_app_settings()["onboarding_progress"]

    assert progress["tutorial_done"] is True
    assert progress["checklist_hidden"] is True
    assert progress["tips_seen"] == list(app_settings._ONBOARDING_TIPS)


def test_app_settings_reports_the_pdf_folder_as_unset_until_one_is_in_use(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    """The first-run assistant must not offer Bibliobrain's own empty folder."""
    app_settings = _load_app_settings_module(workspace_tmp_dir / "storage")
    app_settings.get_generation_settings = lambda: {}
    default_folder = workspace_tmp_dir / "data"
    monkeypatch.setattr(config, "DATA_PATH", str(default_folder))
    monkeypatch.delenv("DATA_PATH", raising=False)

    assert app_settings.get_app_settings()["data_path_configured"] is False

    default_folder.mkdir()
    assert app_settings.get_app_settings()["data_path_configured"] is False

    (default_folder / "article.pdf").write_bytes(b"%PDF")
    assert app_settings.get_app_settings()["data_path_configured"] is True


def test_app_settings_reports_a_chosen_pdf_folder_as_set(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    app_settings = _load_app_settings_module(workspace_tmp_dir / "storage")
    app_settings.get_generation_settings = lambda: {}
    monkeypatch.setattr(config, "DATA_PATH", str(workspace_tmp_dir / "data"))
    monkeypatch.delenv("DATA_PATH", raising=False)

    payload = app_settings.set_app_settings(data_path=str(workspace_tmp_dir / "articles"))

    assert payload["data_path_configured"] is True


def test_standalone_pdfs_are_indexed_unless_the_user_opts_out(
    monkeypatch: Any, workspace_tmp_dir: Path
) -> None:
    from src import app_settings

    monkeypatch.setattr(app_settings, "_app_settings_path", lambda: workspace_tmp_dir / "s.json")
    assert app_settings.get_app_settings()["index_standalone_pdfs"] is True
    assert app_settings.index_standalone_pdfs_enabled() is True
    app_settings.set_app_settings(index_standalone_pdfs=False)
    assert app_settings.index_standalone_pdfs_enabled() is False
