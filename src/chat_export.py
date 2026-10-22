"""Chat-triggered Markdown / LaTeX export generation."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import unicodedata
import uuid
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

import src.config as config
from src.llm_provider import (
    create_chat_llm,
    invoke_structured_chat,
    invoke_text,
    strip_thinking_blocks,
)

_logger = logging.getLogger(__name__)

ExportFormat = Literal["markdown", "latex"]
ExportScope = Literal["previous", "chat"]


class ChatExportDocument(BaseModel):
    title: str = Field("", description="Short document title.")
    content: str = Field(..., description="Complete Markdown or LaTeX document content.")


def export_dir() -> Path:
    return Path(config.STORAGE_DIR) / "chat_exports"


def _message_content(message: dict[str, Any]) -> str:
    return strip_thinking_blocks(str(message.get("content", "") or "")).strip()


def _is_export_message(message: dict[str, Any]) -> bool:
    meta = message.get("meta")
    return isinstance(meta, dict) and str(meta.get("mode") or "") == "document_export"


def _selected_export_messages(
    messages: list[dict[str, Any]],
    *,
    scope: ExportScope,
) -> list[dict[str, Any]]:
    rows = [row for row in messages if isinstance(row, dict)]
    if scope == "chat":
        return [
            row
            for row in rows
            if str(row.get("role") or "").strip().lower() in {"user", "assistant"}
            and not _is_export_message(row)
            and bool(_message_content(row))
        ]

    for preferred_role in ("assistant", "user"):
        for row in reversed(rows):
            role = str(row.get("role") or "").strip().lower()
            if role != preferred_role or _is_export_message(row):
                continue
            if _message_content(row):
                return [row]
    return []


def select_export_source(
    messages: list[dict[str, Any]],
    *,
    scope: ExportScope,
) -> tuple[str, str]:
    """Return ``(source_text, default_title)`` for the requested export scope."""
    rows = _selected_export_messages(messages, scope=scope)
    if scope == "chat":
        parts: list[str] = []
        for row in rows:
            role = str(row.get("role") or "").strip().lower()
            content = _message_content(row)
            label = "Utilisateur" if role == "user" else "Assistant"
            parts.append(f"{label}:\n{content}")
        return "\n\n---\n\n".join(parts).strip(), "chat"

    if rows:
        row = rows[0]
        role = str(row.get("role") or "").strip().lower()
        title = "message-assistant" if role == "assistant" else "message-utilisateur"
        return _message_content(row), title
    return "", "message"


def _clean_bibliography_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _normalize_bibliography_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi:\s*", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .;,")


def _source_value(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean_bibliography_text(source.get(key))
        if value:
            return value
    return ""


def _bibliography_dedupe_key(source: dict[str, Any]) -> tuple[str, str]:
    doi = _normalize_bibliography_key(_source_value(source, "doi", "zotero_doi"))
    if doi:
        return "doi", doi

    title = _normalize_bibliography_key(_source_value(source, "title", "zotero_title"))
    authors = _normalize_bibliography_key(_source_value(source, "authors", "author", "zotero_authors"))
    year = _normalize_bibliography_key(_source_value(source, "year", "zotero_year"))
    if title or authors or year:
        return "work", "|".join([title, authors, year])

    file_value = _normalize_bibliography_key(_source_value(source, "file", "source"))
    page = _normalize_bibliography_key(_source_value(source, "page"))
    return "file", "|".join([file_value, page])


def collect_export_bibliography_sources(
    messages: list[dict[str, Any]],
    *,
    scope: ExportScope,
) -> list[dict[str, Any]]:
    """Return deduplicated source metadata for the messages included in the export."""
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in _selected_export_messages(messages, scope=scope):
        meta = row.get("meta")
        sources = meta.get("sources") if isinstance(meta, dict) else None
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, dict):
                continue
            entry = _format_bibliography_entry(source)
            if not entry:
                continue
            key = _bibliography_dedupe_key(source)
            if key in seen:
                continue
            seen.add(key)
            result.append(dict(source))
    return result


def _format_bibliography_entry(source: dict[str, Any]) -> str:
    citation = _source_value(source, "citation", "zotero_citation")
    if citation:
        return citation

    authors = _source_value(source, "authors", "author", "zotero_authors")
    year = _source_value(source, "year", "zotero_year")
    title = _source_value(source, "title", "zotero_title")
    journal = _source_value(source, "journal", "zotero_journal")
    doi = _source_value(source, "doi", "zotero_doi")

    parts: list[str] = []
    if authors and year:
        parts.append(f"{authors} ({year}).")
    elif authors:
        parts.append(f"{authors}.")
    elif year:
        parts.append(f"({year}).")
    if title:
        parts.append(f"{title}.")
    if journal:
        parts.append(f"{journal}.")
    if doi:
        parts.append(f"DOI: {doi}.")
    rendered = " ".join(parts).strip()
    if rendered:
        return rendered

    file_value = _source_value(source, "file", "source")
    page = _source_value(source, "page")
    fallback_parts = []
    if file_value:
        fallback_parts.append(file_value)
    if page and page != "?":
        fallback_parts.append(f"p. {page}")
    return ", ".join(fallback_parts).strip()


def _render_bibliography_section(
    sources: list[dict[str, Any]],
    *,
    export_format: ExportFormat,
) -> str:
    entries = [_format_bibliography_entry(source) for source in sources]
    entries = [entry for entry in entries if entry]
    if not entries:
        return ""

    if export_format == "latex":
        items = "\n".join(f"\\item {_inline_latex(entry)}" for entry in entries)
        return f"\\section*{{Bibliographie}}\n\\begin{{enumerate}}\n{items}\n\\end{{enumerate}}\n"

    items = "\n".join(f"{index}. {entry}" for index, entry in enumerate(entries, start=1))
    return f"## Bibliographie\n\n{items}\n"


def append_bibliography_section(
    content: str,
    sources: list[dict[str, Any]],
    *,
    export_format: ExportFormat,
) -> str:
    section = _render_bibliography_section(sources, export_format=export_format)
    if not section:
        return content

    if export_format == "latex":
        text = str(content or "").rstrip()
        match = re.search(r"\\end\{document\}\s*$", text)
        if match:
            return f"{text[: match.start()].rstrip()}\n\n{section}\n\\end{{document}}\n"
        return f"{text}\n\n{section}"

    return f"{str(content or '').rstrip()}\n\n{section}"


def build_text_export_document(
    *,
    title: str,
    body: str,
    export_format: ExportFormat,
    bibliography_sources: list[dict[str, Any]] | None = None,
) -> ChatExportDocument:
    resolved_title = _coerce_export_title(title, body)
    content = _ensure_document_shape(body, title=resolved_title, export_format=export_format)
    if bibliography_sources:
        content = append_bibliography_section(
            content,
            bibliography_sources,
            export_format=export_format,
        )
    return ChatExportDocument(title=resolved_title, content=content)


def _slugify(value: str, *, fallback: str = "export") -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-").lower()
    if not text:
        text = fallback
    return text[:60].strip("-") or fallback


def safe_export_filename(
    *,
    title: str,
    export_format: ExportFormat,
    now: datetime | None = None,
) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    suffix = "tex" if export_format == "latex" else "md"
    slug = _slugify(title, fallback="chat-export")
    return f"{stamp}-{slug}-{uuid.uuid4().hex[:8]}.{suffix}"


_CODE_FENCE_LANGUAGE_RE = re.compile(r"(?:markdown|md|latex|tex)?", re.I)


def _strip_code_fence(content: str) -> str:
    value = str(content or "").strip()
    # Plain string checks: a regex over the whole body backtracks quadratically.
    if len(value) >= 6 and value.startswith("```") and value.endswith("```"):
        inner = value[3:-3]
        language = _CODE_FENCE_LANGUAGE_RE.match(inner)
        return inner[language.end() if language else 0 :].strip()
    return value


def _coerce_export_title(title: str, body: str, *, fallback: str = "Export du document") -> str:
    explicit = str(title or "").strip()
    if explicit:
        return explicit[:120].strip() or fallback

    raw_body = _strip_code_fence(body)
    for raw_line in raw_body.splitlines():
        stripped = raw_line.strip()
        if len(stripped) > 1 and stripped[0] == "#" and stripped[1].isspace():
            return stripped[1:].strip()[:120].strip()

    for raw_line in raw_body.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        line = re.sub(r"^\[[^\]]+\]\s*", "", line)
        line = re.sub(r"(?<!\s)\s*\[[A-Za-z]?\d+(?:,\s*[A-Za-z]?\d+)*\][\s.,;:!?-]*$", "", line).strip()
        if line:
            return line[:120].rstrip(" .,:;|-") or fallback
    return fallback


_LATEX_BEGIN_DOCUMENT_RE = re.compile(r"\\begin\{document\}", re.I)
_LATEX_END_DOCUMENT_RE = re.compile(r"\\end\{document\}", re.I)


def _extract_latex_body(value: str) -> str:
    text = str(value or "").strip()
    # Two literal searches instead of one lazy regex, which is quadratic on
    # a text full of \begin{document} without a matching \end{document}.
    begin = _LATEX_BEGIN_DOCUMENT_RE.search(text)
    if begin:
        end = _LATEX_END_DOCUMENT_RE.search(text, begin.end())
        if end:
            text = text[begin.end() : end.start()]
    text = re.sub(r"(?m)^\s*\\maketitle\s*$", "", text)
    text = re.sub(r"(?m)^\s*\\(?:documentclass|usepackage|title|author|date)\b.*$", "", text)
    return text.strip()


def _latex_document_preamble(title: str) -> str:
    clean_title = str(title or "").strip() or "Export du chat"
    return (
        "\\documentclass{article}\n"
        "\\usepackage[utf8]{inputenc}\n"
        "\\usepackage[T1]{fontenc}\n"
        "\\usepackage{hyperref}\n"
        f"\\title{{{_escape_latex(clean_title)}}}\n"
        "\\author{}\n"
        "\\date{}\n\n"
        "\\begin{document}\n\n"
        "\\maketitle\n\n"
    )


def _ensure_document_shape(content: str, *, title: str, export_format: ExportFormat) -> str:
    value = _strip_code_fence(content)
    clean_title = str(title or "").strip() or "Export du chat"
    if export_format == "markdown":
        if not re.search(r"(?m)^\s*#\s+", value):
            value = f"# {clean_title}\n\n{value.strip()}"
        return value.strip() + "\n"

    body = _latex_body_from_markdownish_text(_extract_latex_body(value) or value)
    return f"{_latex_document_preamble(clean_title)}{body.strip()}\n\n\\end{{document}}\n"


def _escape_latex(value: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in str(value or ""))


def _split_latex_command_span(text: str, command: str) -> tuple[str, str, str] | None:
    needle = f"\\{command}" + "{"
    start = text.find(needle)
    if start < 0:
        return None
    content_start = start + len(needle)
    depth = 1
    index = content_start
    while index < len(text):
        char = text[index]
        if char == "{" and (index == 0 or text[index - 1] != "\\"):
            depth += 1
        elif char == "}" and (index == 0 or text[index - 1] != "\\"):
            depth -= 1
            if depth == 0:
                return text[:start], text[content_start:index], text[index + 1 :]
        index += 1
    return None


def _inline_latex(text: str) -> str:
    value = str(text or "")
    if not value:
        return ""

    placeholders: list[str] = []

    def protect(rendered: str) -> str:
        token = f"\uE000{len(placeholders)}\uE001"
        placeholders.append(rendered)
        return token

    def replace_code(match: re.Match[str]) -> str:
        return protect(r"\texttt{" + _escape_latex(match.group(1)) + "}")

    value = re.sub(r"`([^`]+)`", replace_code, value)

    for source_cmd, target_cmd in (("textit", "emph"), ("emph", "emph"), ("textbf", "textbf")):
        while True:
            span = _split_latex_command_span(value, source_cmd)
            if span is None:
                break
            before, inner, after = span
            value = before + protect(f"\\{target_cmd}" + "{" + _inline_latex(inner) + "}") + after

    def replace_markdown_bold_italic(match: re.Match[str]) -> str:
        return protect(r"\textbf{\emph{" + _inline_latex(match.group(1)) + "}}")

    def replace_markdown_bold(match: re.Match[str]) -> str:
        return protect(r"\textbf{" + _inline_latex(match.group(1)) + "}")

    def replace_markdown_italic(match: re.Match[str]) -> str:
        return protect(r"\emph{" + _inline_latex(match.group(1)) + "}")

    value = re.sub(r"\*\*\*([^*\n]+)\*\*\*", replace_markdown_bold_italic, value)
    value = re.sub(r"\*\*([^\n]+?)\*\*", replace_markdown_bold, value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", replace_markdown_italic, value)

    escaped = _escape_latex(value)
    for index, rendered in enumerate(placeholders):
        escaped = escaped.replace(_escape_latex(f"\uE000{index}\uE001"), rendered)
    return escaped


def _latex_body_from_markdownish_text(text: str) -> str:
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_enumerate = False
    in_itemize = False

    def close_itemize() -> None:
        nonlocal in_itemize
        if in_itemize:
            out.append("\\end{itemize}")
            in_itemize = False

    def close_enumerate() -> None:
        nonlocal in_enumerate
        close_itemize()
        if in_enumerate:
            out.append("\\end{enumerate}")
            in_enumerate = False

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            close_itemize()
            out.append("")
            continue

        heading = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        latex_heading = re.match(r"^\\(section|subsection|subsubsection)\*?\{(.+)\}\s*$", stripped)
        if heading or latex_heading:
            close_enumerate()
            if heading:
                level = len(heading.group(1))
                text_value = heading.group(2)
                command = {1: "section", 2: "subsection"}.get(level, "subsubsection")
            else:
                assert latex_heading is not None
                command = latex_heading.group(1)
                text_value = latex_heading.group(2)
            out.append(f"\\{command}*{{{_inline_latex(text_value)}}}")
            continue

        numbered = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if numbered:
            close_itemize()
            if not in_enumerate:
                out.append("\\begin{enumerate}")
                in_enumerate = True
            out.append(f"\\item {_inline_latex(numbered.group(1))}")
            continue

        bullet = re.match(r"^[-*]\s+(.+)$", stripped)
        if bullet:
            if not in_itemize:
                out.append("\\begin{itemize}")
                in_itemize = True
            out.append(f"\\item {_inline_latex(bullet.group(1))}")
            continue

        if line.startswith((" ", "\t")) and in_enumerate:
            nested = re.match(r"^\s*[-*]\s+(.+)$", line)
            if nested:
                if not in_itemize:
                    out.append("\\begin{itemize}")
                    in_itemize = True
                out.append(f"\\item {_inline_latex(nested.group(1))}")
                continue

        close_enumerate()
        out.append(_inline_latex(stripped))

    close_enumerate()
    return "\n".join(out).strip()


def _find_latex_compiler() -> tuple[str, str]:
    for name, kind in (("tectonic", "tectonic"), ("tecto", "tectonic"), ("pdflatex", "pdflatex")):
        path = shutil.which(name)
        if path:
            return path, kind
    return "", ""


def validate_latex_document(path: Path, *, timeout_seconds: int | None = None) -> dict[str, Any]:
    compiler, kind = _find_latex_compiler()
    if timeout_seconds is None:
        # Tectonic downloads its fonts and packages on first use (~30 s); an
        # earlier kill leaves the cache cold, so every export timed out.
        # Once cached, a compile takes about 2 s.
        timeout_seconds = 90 if kind == "tectonic" else 25
    if not compiler:
        return {
            "ok": None,
            "compiler": "",
            "msg": "Aucun compilateur LaTeX detecte.",
        }

    source = Path(path)
    if not source.is_file() or source.suffix.lower() != ".tex":
        return {
            "ok": False,
            "compiler": kind,
            "msg": "Fichier LaTeX introuvable.",
        }

    if kind == "tectonic":
        command = [compiler, "-X", "compile", source.name]
    else:
        command = [
            compiler,
            "-interaction=nonstopmode",
            "-halt-on-error",
            source.name,
        ]
    try:
        completed = subprocess.run(
            command,
            cwd=str(source.parent),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "compiler": kind,
            "msg": f"Validation LaTeX interrompue apres {timeout_seconds}s.",
        }
    finally:
        _cleanup_latex_validation_outputs(source)
    output = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if str(part or "").strip()
    )
    if completed.returncode == 0:
        return {
            "ok": True,
            "compiler": kind,
            "msg": "Compilation LaTeX OK.",
        }
    return {
        "ok": False,
        "compiler": kind,
        "msg": _short_latex_log(output),
    }


def _cleanup_latex_validation_outputs(source: Path) -> None:
    for suffix in (".pdf", ".aux", ".log", ".out", ".toc", ".synctex.gz"):
        candidate = source.with_suffix(suffix)
        if candidate == source:
            continue
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError:
            pass


def _short_latex_log(output: str, *, limit: int = 900) -> str:
    text = str(output or "").strip()
    if not text:
        return "Compilation LaTeX echouee sans log."
    error_match = re.search(r"(?m)^! .+(?:\n.*){0,8}", text)
    if error_match:
        text = error_match.group(0).strip()
    text = re.sub(r"\s+\n", "\n", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "..."
    return text


def _build_export_prompt(
    *,
    source_text: str,
    export_format: ExportFormat,
    scope: ExportScope,
) -> str:
    if export_format == "latex":
        format_rules = (
            "Produit un document structure simple en Markdown lisible, avec titres et listes. "
            "N'utilise pas de commandes LaTeX brutes: le backend convertira ce contenu en LaTeX "
            "compilable et echappera les caracteres speciaux. Ne mets pas de bloc ```."
        )
    else:
        format_rules = (
            "Produit un fichier Markdown propre avec un titre H1 et des sections lisibles. "
            "Conserve les labels de citation existants comme [S1]. Ne mets pas de front matter "
            "et ne mets pas de bloc ```."
        )
    scope_label = "conversation complete" if scope == "chat" else "message precedent"
    return (
        "Tu transformes un contenu de chat en document. Ne rajoute aucun fait absent du contenu source. "
        "Tu peux seulement restructurer, titrer, clarifier la mise en forme et retirer les artefacts de chat.\n\n"
        f"Format attendu: {export_format}.\n"
        f"Cible: {scope_label}.\n"
        f"Regles de format: {format_rules}\n\n"
        "Contenu source:\n"
        f"{source_text.strip()}"
    )


def _fallback_export_document(
    *,
    source_text: str,
    default_title: str,
    export_format: ExportFormat,
) -> ChatExportDocument:
    title = "Export du chat" if default_title == "chat" else "Message exporte"
    title = str(default_title or title).replace("-", " ").strip().title() or title
    body = strip_thinking_blocks(source_text).strip()
    if export_format == "latex":
        content = _ensure_document_shape(body, title=title, export_format="latex")
    else:
        content = _ensure_document_shape(body, title=title, export_format="markdown")
    return ChatExportDocument(title=title, content=content)


def generate_export_document_unstructured(
    *,
    source_text: str,
    model_name: str,
    export_format: ExportFormat,
    scope: ExportScope,
) -> ChatExportDocument:
    prompt = _build_export_prompt(
        source_text=source_text,
        export_format=export_format,
        scope=scope,
    )
    raw = invoke_text(create_chat_llm(model_name, temperature=0.0, top_p=1.0), prompt)
    title = "Export du chat" if scope == "chat" else "Message exporte"
    heading_match = re.search(r"(?m)^\s*#\s+(.+?)\s*$", raw)
    if heading_match:
        title = heading_match.group(1).strip() or title
    latex_title_match = re.search(r"\\title\{(.+?)\}", raw)
    if latex_title_match:
        title = latex_title_match.group(1).strip() or title
    return ChatExportDocument(
        title=title,
        content=_ensure_document_shape(raw, title=title, export_format=export_format),
    )


def generate_export_document(
    *,
    source_text: str,
    model_name: str,
    export_format: ExportFormat,
    scope: ExportScope,
) -> ChatExportDocument:
    prompt = _build_export_prompt(
        source_text=source_text,
        export_format=export_format,
        scope=scope,
    )
    generated = invoke_structured_chat(
        model_name,
        [{"role": "user", "content": prompt}],
        response_model=ChatExportDocument,
        temperature=0.0,
        top_p=1.0,
    )
    if not isinstance(generated, ChatExportDocument):
        raise RuntimeError("LLM returned an unexpected type for ChatExportDocument")
    title = (generated.title or "").strip() or ("Export du chat" if scope == "chat" else "Message exporte")
    content = _ensure_document_shape(
        generated.content,
        title=title,
        export_format=export_format,
    )
    return ChatExportDocument(title=title, content=content)


def generate_export_document_resilient(
    *,
    source_text: str,
    model_name: str,
    export_format: ExportFormat,
    scope: ExportScope,
    default_title: str,
) -> tuple[ChatExportDocument, str]:
    try:
        return (
            generate_export_document(
                source_text=source_text,
                model_name=model_name,
                export_format=export_format,
                scope=scope,
            ),
            "",
        )
    except Exception:
        _logger.debug(
            "Recovered from an error in generate_export_document_resilient.", exc_info=True
        )
        try:
            return (
                generate_export_document_unstructured(
                    source_text=source_text,
                    model_name=model_name,
                    export_format=export_format,
                    scope=scope,
                ),
                "structured_output_fallback",
            )
        except Exception:
            _logger.debug(
                "Recovered from an error in generate_export_document_resilient.", exc_info=True
            )
            return (
                _fallback_export_document(
                    source_text=source_text,
                    default_title=default_title,
                    export_format=export_format,
                ),
                "deterministic_fallback",
            )


def save_export_document(
    document: ChatExportDocument,
    *,
    export_format: ExportFormat,
) -> dict[str, Any]:
    out_dir = export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    filename = safe_export_filename(title=document.title, export_format=export_format)
    path = out_dir / filename
    path.write_text(document.content, encoding="utf-8")
    saved: dict[str, Any] = {
        "filename": filename,
        "path": str(path),
        "url": f"/api/chat-exports/{filename}",
        "format": export_format,
    }
    if export_format == "latex":
        saved["latex_validation"] = validate_latex_document(path)
    return saved


def stream_chat_export(
    *,
    user_query: str,
    model_name: str,
    previous_messages: list[dict[str, Any]],
    export_format: ExportFormat,
    scope: ExportScope,
):
    del user_query
    source_text, default_title = select_export_source(previous_messages, scope=scope)
    if not source_text:
        text = (
            "Je n'ai pas trouve de message precedent a exporter. "
            "Generez d'abord une reponse ou ouvrez une conversation existante."
        )
        yield {"type": "token", "text": text}
        yield {"type": "sources", "data": [], "answer_origin": "document_export"}
        return

    yield {"type": "status", "msg": "Preparation du document..."}
    document, fallback_reason = generate_export_document_resilient(
        source_text=source_text,
        model_name=model_name,
        export_format=export_format,
        scope=scope,
        default_title=default_title,
    )
    if not document.title.strip():
        document = ChatExportDocument(title=default_title, content=document.content)
    bibliography_sources = collect_export_bibliography_sources(previous_messages, scope=scope)
    if bibliography_sources:
        document = ChatExportDocument(
            title=document.title,
            content=append_bibliography_section(
                document.content,
                bibliography_sources,
                export_format=export_format,
            ),
        )
    saved = save_export_document(document, export_format=export_format)
    saved["bibliography_count"] = len(bibliography_sources)
    if fallback_reason:
        saved["fallback"] = fallback_reason
    response = f"Fichier cree : [{saved['filename']}]({saved['url']})"
    yield {"type": "token", "text": response}
    yield {"type": "export_file", "data": saved}
    yield {"type": "sources", "data": [], "answer_origin": "document_export"}


def collect_export_filenames_from_messages(messages: Iterable[dict[str, Any]]) -> list[str]:
    filenames: list[str] = []
    seen: set[str] = set()
    for message in messages:
        meta = message.get("meta") if isinstance(message, dict) else None
        if not isinstance(meta, dict):
            continue
        export_file = meta.get("export_file")
        if not isinstance(export_file, dict):
            continue
        filename = str(export_file.get("filename") or "").strip()
        if not filename or filename in seen:
            continue
        if resolve_export_file(filename) is None:
            continue
        seen.add(filename)
        filenames.append(filename)
    return filenames


def delete_export_files(filenames: Iterable[str]) -> dict[str, Any]:
    deleted: list[str] = []
    missing: list[str] = []
    bytes_deleted = 0
    seen: set[str] = set()

    for filename in filenames:
        name = str(filename or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        path = resolve_export_file(name)
        if path is None or not path.exists():
            missing.append(name)
            continue
        try:
            bytes_deleted += path.stat().st_size
            path.unlink()
            deleted.append(name)
        except OSError:
            missing.append(name)

    return {
        "deleted": deleted,
        "missing": missing,
        "files_deleted": len(deleted),
        "bytes_deleted": bytes_deleted,
    }


def clear_all_export_files() -> dict[str, Any]:
    root = export_dir()
    if not root.exists():
        return {"deleted": [], "missing": [], "files_deleted": 0, "bytes_deleted": 0}
    filenames = [
        path.name
        for path in root.iterdir()
        if path.is_file() and re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:md|tex)", path.name)
    ]
    return delete_export_files(filenames)


def resolve_export_file(filename: str) -> Path | None:
    name = str(filename or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:md|tex)", name):
        return None
    root = export_dir().resolve()
    candidate = (root / name).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


__all__ = [
    "append_bibliography_section",
    "build_text_export_document",
    "ChatExportDocument",
    "clear_all_export_files",
    "collect_export_bibliography_sources",
    "collect_export_filenames_from_messages",
    "delete_export_files",
    "export_dir",
    "generate_export_document",
    "validate_latex_document",
    "resolve_export_file",
    "safe_export_filename",
    "save_export_document",
    "select_export_source",
    "stream_chat_export",
]
