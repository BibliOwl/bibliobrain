from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from src import library


def _prepare_analysis(
    monkeypatch: pytest.MonkeyPatch,
    workspace_tmp_dir,
) -> tuple[dict[str, Any], str]:
    rel_path = "paper.pdf"
    (workspace_tmp_dir / rel_path).write_bytes(b"%PDF-test")
    monkeypatch.setattr(library.config, "DATA_PATH", str(workspace_tmp_dir))
    monkeypatch.setattr(library, "get_app_settings", lambda: {"ui_language": "fr"})
    monkeypatch.setattr(library, "get_cached_analysis", lambda *args: None)
    monkeypatch.setattr(
        library,
        "build_analysis_cache_fingerprint",
        lambda *args: "fingerprint",
    )
    monkeypatch.setattr(
        library,
        "get_analysis_prompt",
        lambda *args, **kwargs: "Analyse ce document et réponds directement.\n{text}",
    )

    import langchain_community.document_loaders as document_loaders

    class FakeLoader:
        def __init__(self, path: str) -> None:
            self.path = path

        def load(self) -> list[Any]:
            return [SimpleNamespace(page_content="Résultat scientifique important.")]

    monkeypatch.setattr(document_loaders, "PyPDFLoader", FakeLoader)
    captured: dict[str, Any] = {}

    def fake_create_chat_llm(model_name: str, **kwargs: Any) -> object:
        captured["model_name"] = model_name
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(library, "create_chat_llm", fake_create_chat_llm)
    return captured, rel_path


def test_generate_summary_uses_plain_text_with_analysis_specific_limit(
    monkeypatch: pytest.MonkeyPatch,
    workspace_tmp_dir,
) -> None:
    captured, rel_path = _prepare_analysis(monkeypatch, workspace_tmp_dir)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(library, "set_cached_analysis", lambda **kwargs: saved.update(kwargs))

    def fake_invoke_text(llm: object, prompt: str) -> str:
        assert "Résultat scientifique important" in prompt
        return "<think>raisonnement interne</think>\n\nRésumé scientifique utile."

    monkeypatch.setattr(library, "invoke_text", fake_invoke_text)

    result = library.generate_summary(
        rel_path,
        "Résumé rapide",
        "granite4.1:8b",
    )

    assert result == "Résumé scientifique utile."
    assert captured == {
        "model_name": "granite4.1:8b",
        "max_output_tokens": 512,
        "thinking_enabled": False,
    }
    assert saved["content"] == result


def test_generate_summary_retries_without_reasoning_after_inline_thinking(
    monkeypatch: pytest.MonkeyPatch,
    workspace_tmp_dir,
) -> None:
    _captured, rel_path = _prepare_analysis(monkeypatch, workspace_tmp_dir)
    monkeypatch.setattr(library, "set_cached_analysis", lambda **kwargs: None)
    llm_thinking: list[bool] = []

    def fake_create_chat_llm(model_name: str, **kwargs: Any) -> object:
        llm_thinking.append(kwargs["thinking_enabled"])
        return SimpleNamespace(thinking=kwargs["thinking_enabled"])

    def fake_invoke_text(llm: Any, prompt: str) -> str:
        if llm.thinking:
            # granite4.2 reasons in the answer text itself when thinking is on.
            return "We need a concise paragraph. " * 60 + "Le résumé."
        return "Résumé scientifique utile."

    monkeypatch.setattr(library, "create_chat_llm", fake_create_chat_llm)
    monkeypatch.setattr(library, "invoke_text", fake_invoke_text)

    result = library.generate_summary(
        rel_path, "Résumé rapide", "granite4.2:8b", thinking_enabled=True
    )

    assert result == "Résumé scientifique utile."
    assert llm_thinking == [True, False]


def test_generate_summary_raises_and_does_not_cache_llm_failures(
    monkeypatch: pytest.MonkeyPatch,
    workspace_tmp_dir,
) -> None:
    _captured, rel_path = _prepare_analysis(monkeypatch, workspace_tmp_dir)
    cached: list[dict[str, Any]] = []
    monkeypatch.setattr(library, "set_cached_analysis", lambda **kwargs: cached.append(kwargs))

    def broken_invoke_text(llm: object, prompt: str) -> str:
        raise RuntimeError("max_tokens length limit")

    monkeypatch.setattr(library, "invoke_text", broken_invoke_text)

    with pytest.raises(RuntimeError, match="Échec de la génération.*max_tokens"):
        library.generate_summary(rel_path, "Résumé rapide", "granite4.1:8b")

    assert cached == []


def test_generate_summary_rejects_pdf_outside_library(
    monkeypatch: pytest.MonkeyPatch,
    workspace_tmp_dir,
) -> None:
    data_dir = workspace_tmp_dir / "data"
    data_dir.mkdir()
    (workspace_tmp_dir / "secret.pdf").write_bytes(b"%PDF-secret")
    monkeypatch.setattr(library.config, "DATA_PATH", str(data_dir))
    monkeypatch.setattr(library, "get_app_settings", lambda: {"ui_language": "en"})

    result = library.generate_summary("../secret.pdf", "summary", "model")

    assert result == "File not found or outside the configured library."


def test_analysis_document_text_excludes_reference_section() -> None:
    pages = [
        SimpleNamespace(
            page_content=(
                "Conclusion\nLe résultat principal est confirmé.\n\n"
                "REFERENCES\nTitle Example\nAuthors Example"
            )
        )
    ]

    result = library._build_analysis_document_text(pages)

    assert "résultat principal" in result
    assert "REFERENCES" not in result
    assert "Authors Example" not in result


def test_generate_summary_retries_a_reference_dump_with_compact_evidence(
    monkeypatch: pytest.MonkeyPatch,
    workspace_tmp_dir,
) -> None:
    _captured, rel_path = _prepare_analysis(monkeypatch, workspace_tmp_dir)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(library, "set_cached_analysis", lambda **kwargs: saved.update(kwargs))
    outputs = iter(
        [
            "The references you've provided are a comprehensive list of papers. " * 20,
            "Cette étude développe un modèle multiphysique du câble CORC. "
            "Elle évalue sa stabilité lors d'un quench localisé et montre que la "
            "résistance de contact terminale influence fortement la propagation thermique.",
        ]
    )
    calls: list[str] = []

    def fake_invoke_text(llm: object, prompt: str) -> str:
        calls.append(prompt)
        return next(outputs)

    monkeypatch.setattr(library, "invoke_text", fake_invoke_text)

    result = library.generate_summary(rel_path, "Résumé rapide", "granite4.1:8b")

    assert result.startswith("Cette étude développe")
    assert len(calls) == 2
    assert "tentative précédente" in calls[1]
    assert saved["content"] == result
