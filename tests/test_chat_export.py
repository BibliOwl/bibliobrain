from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def test_select_export_source_prefers_previous_assistant() -> None:
    from src.chat_export import select_export_source

    source, title = select_export_source(
        [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Reponse importante"},
            {"role": "user", "content": "cree un md"},
        ],
        scope="previous",
    )

    assert source == "Reponse importante"
    assert title == "message-assistant"


def test_select_export_source_chat_scope_formats_roles() -> None:
    from src.chat_export import select_export_source

    source, title = select_export_source(
        [
            {"role": "user", "content": "Question"},
            {"role": "assistant", "content": "Reponse"},
            {
                "role": "assistant",
                "content": "Fichier cree : [x](x)",
                "meta": {"mode": "document_export"},
            },
        ],
        scope="chat",
    )

    assert title == "chat"
    assert "Utilisateur:\nQuestion" in source
    assert "Assistant:\nReponse" in source
    assert "Fichier cree" not in source


def test_collect_export_bibliography_sources_previous_scope_uses_selected_message() -> None:
    from src.chat_export import collect_export_bibliography_sources

    sources = collect_export_bibliography_sources(
        [
            {
                "role": "user",
                "content": "Question",
                "meta": {"sources": [{"title": "Question source", "doi": "10.1/user"}]},
            },
            {
                "role": "assistant",
                "content": "Reponse [S1]",
                "meta": {"sources": [{"title": "Assistant source", "doi": "10.1/assistant"}]},
            },
        ],
        scope="previous",
    )

    assert [source["title"] for source in sources] == ["Assistant source"]


def test_collect_export_bibliography_sources_chat_scope_deduplicates_and_skips_exports() -> None:
    from src.chat_export import collect_export_bibliography_sources

    sources = collect_export_bibliography_sources(
        [
            {
                "role": "user",
                "content": "Question",
                "meta": {"sources": [{"title": "First", "doi": "https://doi.org/10.42/demo"}]},
            },
            {
                "role": "assistant",
                "content": "Reponse [S1]",
                "meta": {
                    "sources": [
                        {"title": "Duplicate", "doi": "10.42/demo"},
                        {"title": "Second", "authors": "Smith", "year": "2024"},
                    ]
                },
            },
            {
                "role": "assistant",
                "content": "Fichier cree : [x](x)",
                "meta": {
                    "mode": "document_export",
                    "sources": [{"title": "Export source", "doi": "10.1/export"}],
                },
            },
        ],
        scope="chat",
    )

    assert [source["title"] for source in sources] == ["First", "Second"]


def test_append_markdown_bibliography_section_uses_zotero_citation() -> None:
    from src.chat_export import append_bibliography_section

    markdown = append_bibliography_section(
        "# Demo\n\nCorps avec citation [S1].\n",
        [{"citation": "Doe, J. (2024). Article HTS. Superconductivity Journal."}],
        export_format="markdown",
    )

    assert markdown.endswith("Superconductivity Journal.\n")
    assert "## Bibliographie" in markdown
    assert "1. Doe, J. (2024). Article HTS." in markdown


def test_append_latex_bibliography_section_escapes_entries_before_document_end() -> None:
    from src.chat_export import append_bibliography_section

    tex = append_bibliography_section(
        "\\documentclass{article}\n\\begin{document}\nCorps [S1]\n\\end{document}\n",
        [
            {
                "authors": "Doe & Smith",
                "year": "2024",
                "title": "AC losses in HTS_1",
                "journal": "TASC",
                "doi": "10.123/abc_def",
            }
        ],
        export_format="latex",
    )

    assert "\\section*{Bibliographie}" in tex
    assert "\\begin{enumerate}" in tex
    assert "Doe \\& Smith" in tex
    assert "HTS\\_1" in tex
    assert "10.123/abc\\_def" in tex
    assert tex.rstrip().endswith("\\end{document}")
    assert tex.index("\\section*{Bibliographie}") < tex.index("\\end{document}")


def test_append_bibliography_section_skips_empty_sources() -> None:
    from src.chat_export import append_bibliography_section

    content = "# Demo\n\nSans source.\n"

    assert append_bibliography_section(content, [], export_format="markdown") == content


def test_build_text_export_document_derives_title_and_appends_markdown_bibliography() -> None:
    from src.chat_export import build_text_export_document

    document = build_text_export_document(
        title="",
        body="AC losses in HTS magnets remain a key design constraint [1].",
        export_format="markdown",
        bibliography_sources=[
            {
                "authors": "Dupont",
                "year": "2026",
                "title": "AC losses in HTS magnets",
                "doi": "10.123/demo",
            }
        ],
    )

    assert document.title == "AC losses in HTS magnets remain a key design constraint"
    assert document.content.startswith("# AC losses in HTS magnets remain a key design constraint")
    assert "## Bibliographie" in document.content
    assert "1. Dupont (2026). AC losses in HTS magnets. DOI: 10.123/demo." in document.content


def test_safe_export_filename_uses_expected_extension() -> None:
    from src.chat_export import safe_export_filename

    name = safe_export_filename(
        title="Pertes AC / aimants HTS",
        export_format="latex",
        now=datetime(2026, 5, 7, 12, 30, 0),
    )

    assert name.startswith("20260507-123000-pertes-ac-aimants-hts-")
    assert name.endswith(".tex")
    assert "/" not in name


def test_generate_export_document_uses_structured_llm(monkeypatch: Any) -> None:
    from src import chat_export
    from src.chat_export import ChatExportDocument

    captured: dict[str, Any] = {}

    def fake_invoke(model_name, messages, response_model=None, **kwargs):
        captured["model_name"] = model_name
        captured["messages"] = messages
        captured["response_model"] = response_model
        captured["kwargs"] = kwargs
        return ChatExportDocument(title="Mon document", content="Contenu reformate")

    monkeypatch.setattr(chat_export, "invoke_structured_chat", fake_invoke)

    document = chat_export.generate_export_document(
        source_text="source",
        model_name="demo-model",
        export_format="markdown",
        scope="previous",
    )

    assert captured["model_name"] == "demo-model"
    assert captured["response_model"] is ChatExportDocument
    assert captured["kwargs"]["temperature"] == 0.0
    assert document.content.startswith("# Mon document")


def test_generate_export_document_resilient_falls_back_to_unstructured(
    monkeypatch: Any,
) -> None:
    from src import chat_export

    captured: dict[str, Any] = {}

    def broken_structured(*args: Any, **kwargs: Any):
        raise RuntimeError("Invalid JSON")

    def fake_create_chat_llm(model_name: str, **kwargs: Any) -> object:
        captured["model_name"] = model_name
        captured["kwargs"] = kwargs
        return object()

    def fake_invoke_text(llm: object, prompt: str) -> str:
        captured["prompt"] = prompt
        return "# Export propre\n\nContenu reformate."

    monkeypatch.setattr(chat_export, "invoke_structured_chat", broken_structured)
    monkeypatch.setattr(chat_export, "create_chat_llm", fake_create_chat_llm)
    monkeypatch.setattr(chat_export, "invoke_text", fake_invoke_text)

    document, fallback = chat_export.generate_export_document_resilient(
        source_text="Contenu source",
        model_name="demo-model",
        export_format="markdown",
        scope="previous",
        default_title="message-assistant",
    )

    assert fallback == "structured_output_fallback"
    assert document.title == "Export propre"
    assert document.content.startswith("# Export propre")
    assert captured["model_name"] == "demo-model"
    assert "Contenu source" in captured["prompt"]


def test_generate_export_document_resilient_deterministic_latex_fallback(
    monkeypatch: Any,
) -> None:
    from src import chat_export

    def broken(*args: Any, **kwargs: Any):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(chat_export, "invoke_structured_chat", broken)
    monkeypatch.setattr(chat_export, "create_chat_llm", broken)

    document, fallback = chat_export.generate_export_document_resilient(
        source_text="Pertes AC & HTS_1 [S1]",
        model_name="demo-model",
        export_format="latex",
        scope="previous",
        default_title="message-assistant",
    )

    assert fallback == "deterministic_fallback"
    assert "\\documentclass{article}" in document.content
    assert "Pertes AC \\& HTS\\_1 [S1]" in document.content


def test_latex_export_sanitizes_markdown_like_llm_output() -> None:
    from src.chat_export import _ensure_document_shape

    raw = r"""
\documentclass{article}
\usepackage[french]{babel}
\title{Liste des articles sur les pertes dans les HTS}
\begin{document}
\maketitle

\section*{Articles}

1. \textit{C:\Users\user\Zotero\storage\KRPEZ2X3\CERN-THESIS-2016-142.pdf} [S2]
   - fichier: `KRPEZ2X3 / file_with_under_score.pdf`; DOI: `10.1109/TASC.2013.2259827`
   - Preuve: pertes AC & HTS_1.

2. \textit{Advanced electromagnetic modeling of T–A formulations} [S9]
   - fichier: `79RWG35C / Berrospe-Juarez.pdf`

\end{document}
"""

    tex = _ensure_document_shape(raw, title="Liste", export_format="latex")

    assert "\\begin{enumerate}" in tex
    assert "\\begin{itemize}" in tex
    assert "\\item \\emph{C:\\textbackslash{}Users" in tex
    assert "\\texttt{KRPEZ2X3 / file\\_with\\_under\\_score.pdf}" in tex
    assert "pertes AC \\& HTS\\_1" in tex
    assert "`" not in tex
    assert "\\usepackage[french]{babel}" not in tex
    assert "\\end{document}" in tex


def test_save_export_document_writes_under_chat_exports(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    import src.config as config
    from src.chat_export import ChatExportDocument, export_dir, save_export_document

    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))

    saved = save_export_document(
        ChatExportDocument(title="Demo", content="# Demo\n"),
        export_format="markdown",
    )

    path = Path(saved["path"])
    assert path.parent == export_dir()
    assert path.suffix == ".md"
    assert path.read_text(encoding="utf-8") == "# Demo\n"


def test_save_latex_export_records_missing_compiler_validation(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    import src.config as config
    from src import chat_export
    from src.chat_export import ChatExportDocument, save_export_document

    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    monkeypatch.setattr(chat_export.shutil, "which", lambda name: None)

    saved = save_export_document(
        ChatExportDocument(
            title="Demo",
            content="\\documentclass{article}\n\\begin{document}\nDemo\n\\end{document}\n",
        ),
        export_format="latex",
    )

    assert saved["filename"].endswith(".tex")
    assert saved["latex_validation"]["ok"] is None
    assert "Aucun compilateur" in saved["latex_validation"]["msg"]


def test_validate_latex_document_uses_tectonic_when_available(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    from src import chat_export

    tex_path = workspace_tmp_dir / "demo.tex"
    tex_path.write_text("\\documentclass{article}\n\\begin{document}\nDemo\n\\end{document}\n", encoding="utf-8")
    captured: dict[str, Any] = {}

    def fake_which(name: str) -> str | None:
        return "C:\\bin\\tectonic.exe" if name == "tectonic" else None

    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command: list[str], **kwargs: Any) -> Completed:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr(chat_export.shutil, "which", fake_which)
    monkeypatch.setattr(chat_export.subprocess, "run", fake_run)

    result = chat_export.validate_latex_document(tex_path)

    assert result["ok"] is True
    assert result["compiler"] == "tectonic"
    assert captured["command"][1:3] == ["-X", "compile"]
    assert captured["command"][-1] == "demo.tex"
    # Room for Tectonic's first-use downloads, which a 25 s limit cut short.
    assert captured["kwargs"]["timeout"] == 90


def test_collect_export_filenames_from_messages_deduplicates(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    import src.config as config
    from src.chat_export import collect_export_filenames_from_messages, export_dir

    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    export_dir().mkdir(parents=True, exist_ok=True)
    (export_dir() / "demo.md").write_text("# Demo", encoding="utf-8")

    names = collect_export_filenames_from_messages(
        [
            {"meta": {"export_file": {"filename": "demo.md"}}},
            {"meta": {"export_file": {"filename": "demo.md"}}},
            {"meta": {"export_file": {"filename": "../secret.md"}}},
        ]
    )

    assert names == ["demo.md"]


def test_delete_and_clear_export_files_stay_under_chat_exports(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    import src.config as config
    from src.chat_export import clear_all_export_files, delete_export_files, export_dir

    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    export_dir().mkdir(parents=True, exist_ok=True)
    first = export_dir() / "first.md"
    second = export_dir() / "second.tex"
    ignored = export_dir() / "ignored.tmp"
    first.write_text("# First", encoding="utf-8")
    second.write_text("\\documentclass{article}", encoding="utf-8")
    ignored.write_text("tmp", encoding="utf-8")

    deleted = delete_export_files(["first.md", "../secret.md"])

    assert deleted["files_deleted"] == 1
    assert not first.exists()
    assert second.exists()

    cleared = clear_all_export_files()

    assert cleared["files_deleted"] == 1
    assert not second.exists()
    assert ignored.exists()


def test_stream_chat_export_no_previous_message() -> None:
    from src.chat_export import stream_chat_export

    events = list(
        stream_chat_export(
            user_query="/md",
            model_name="m",
            previous_messages=[],
            export_format="markdown",
            scope="previous",
        )
    )

    assert any("message precedent" in event.get("text", "") for event in events)
    sources = next(event for event in events if event.get("type") == "sources")
    assert sources["answer_origin"] == "document_export"


def test_stream_chat_export_saves_file_when_structured_json_fails(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    import src.config as config
    from src import chat_export

    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))

    def fake_resilient(**kwargs: Any):
        return (
            chat_export.ChatExportDocument(
                title="Latex ok",
                content="\\documentclass{article}\n\\begin{document}\nOK\n\\end{document}\n",
            ),
            "structured_output_fallback",
        )

    monkeypatch.setattr(chat_export, "generate_export_document_resilient", fake_resilient)

    events = list(
        chat_export.stream_chat_export(
            user_query="/latex",
            model_name="demo-model",
            previous_messages=[{"role": "assistant", "content": "Source"}],
            export_format="latex",
            scope="previous",
        )
    )

    export_event = next(event for event in events if event.get("type") == "export_file")
    path = Path(export_event["data"]["path"])
    assert path.suffix == ".tex"
    assert path.exists()
    assert export_event["data"]["fallback"] == "structured_output_fallback"


def test_stream_chat_export_writes_bibliography_into_saved_markdown(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    import src.config as config
    from src import chat_export

    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))

    def fake_resilient(**kwargs: Any):
        return (
            chat_export.ChatExportDocument(title="Markdown ok", content="# Markdown ok\n\nReponse [S1].\n"),
            "",
        )

    monkeypatch.setattr(chat_export, "generate_export_document_resilient", fake_resilient)

    events = list(
        chat_export.stream_chat_export(
            user_query="/md",
            model_name="demo-model",
            previous_messages=[
                {
                    "role": "assistant",
                    "content": "Source [S1]",
                    "meta": {
                        "sources": [
                            {
                                "authors": "Dupont",
                                "year": "2026",
                                "title": "Pertes AC dans les aimants HTS",
                                "doi": "10.555/hts",
                            }
                        ]
                    },
                }
            ],
            export_format="markdown",
            scope="previous",
        )
    )

    export_event = next(event for event in events if event.get("type") == "export_file")
    path = Path(export_event["data"]["path"])
    content = path.read_text(encoding="utf-8")

    assert export_event["data"]["bibliography_count"] == 1
    assert "## Bibliographie" in content
    assert "1. Dupont (2026). Pertes AC dans les aimants HTS. DOI: 10.555/hts." in content
