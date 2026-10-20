"""Thematic synthesis of a notebook: every source is read, then grouped into chapters.

The notebook chat puts all sources in one prompt capped at a few thousand
characters, which silently drops every article past the first few and keeps
only their front matter. This pipeline instead:

  1. **Reads each source on its own** — one structured LLM call per article
     extracts its key points, each backed by a verbatim quote.
  2. **Checks every quote** against the article text with the tolerant
     matcher used for drafting citations; a quote that is not found is
     dropped (the point stays, cited by its source number only).
  3. **Groups the points into chapters** — a plan call names the chapters,
     short calls file each point under a chapter number, and one call per
     chapter writes its opening paragraph. The model only ever sees the point
     statements, never the quotes, and the Markdown is rendered here, so a
     quote cannot be rewritten or moved to another source.

Citations use ``[n]``, the 1-based position of the source in the notebook,
like the notebook chat.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

import src.config as _cfg

_logger = logging.getLogger(__name__)

# Each article is a full LLM round-trip: the cap bounds latency on a local model.
MAX_SYNTHESIS_ARTICLES = 40
_MAX_ARTICLE_PAGES = 30
_CHARS_PER_TOKEN = 3.2
# Enough for the body of a typical journal paper without its references.
_TARGET_ARTICLE_TOKENS = 9000
_NON_OLLAMA_CONTEXT_TOKENS = 8192
_FALLBACK_CONTEXT_TOKENS = 4096
_CONTEXT_INPUT_RATIO = 0.85
_OUTPUT_RESERVE_TOKENS = 1500
_PROMPT_OVERHEAD_TOKENS = 600
_MIN_ARTICLE_CHARS = 4000
_MAX_ARTICLE_CHARS = 40000
# When an article does not fit, keep its opening and its conclusion.
_HEAD_SHARE = 0.7
_MAX_IDEA_CHARS = 240
_MAX_QUOTE_CHARS = 450
_MAX_CHAPTERS = 7
_MAX_SCOPE_CHARS = 160
# Past ~10 points a small model stops answering part-way through the list.
_CLASSIFY_BATCH_SIZE = 8

_REFERENCES_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:\d+\.?[ \t]*)?"
    r"(?:references|reference list|bibliography|bibliographie|références|"
    r"literature cited|works cited)[ \t]*:?[ \t]*$"
)
_WORD_RE = re.compile(r"[^\W\d_]{3,}")
_CITATION_GROUP_RE = re.compile(r"\[\s*S?\d+(?:\s*[,;]\s*S?\d+)*\s*\]")

_STRINGS = {
    "fr": {
        "reading": "Lecture de l'article {index}/{total} : {title}",
        "planning": "Plan des chapitres à partir de {count} points clés…",
        "classifying": "Classement des points dans les chapitres ({done}/{total} classés)…",
        "writing": "Rédaction du chapitre {index}/{total} : {title}",
        "heading": "Synthèse thématique",
        "stats": (
            "{articles} article(s) analysé(s) · {chapters} chapitre(s) · "
            "{points} point(s) clé(s) · {quotes} citation(s) vérifiée(s) dans le texte"
        ),
        "other": "Autres points",
        "sources": "Sources",
        "skipped": "Articles non exploités",
        "unreadable": "texte illisible ou absent",
        "failed": "échec de l'analyse",
        "no_points": "aucun point clé extrait",
        "over_limit": "au-delà des {limit} premiers articles",
        "nothing": (
            "Aucun point clé n'a pu être extrait des sources du carnet. Vérifiez que les "
            "articles contiennent du texte lisible, puis relancez la synthèse."
        ),
        "no_sources": (
            "Le carnet ne contient aucune source exploitable. Ajoutez des articles ou une "
            "collection Zotero, puis relancez la synthèse."
        ),
        "language": "Rédige title, summary et idea en français.",
    },
    "en": {
        "reading": "Reading article {index}/{total}: {title}",
        "planning": "Planning chapters from {count} key points…",
        "classifying": "Filing the points into chapters ({done}/{total} filed)…",
        "writing": "Writing chapter {index}/{total}: {title}",
        "heading": "Thematic synthesis",
        "stats": (
            "{articles} article(s) analysed · {chapters} chapter(s) · "
            "{points} key point(s) · {quotes} quote(s) verified in the text"
        ),
        "other": "Other points",
        "sources": "Sources",
        "skipped": "Articles not used",
        "unreadable": "unreadable or missing text",
        "failed": "analysis failed",
        "no_points": "no key point extracted",
        "over_limit": "beyond the first {limit} articles",
        "nothing": (
            "No key point could be extracted from the notebook sources. Check that the "
            "articles contain readable text, then run the synthesis again."
        ),
        "no_sources": (
            "The notebook has no usable source. Add articles or a Zotero collection, "
            "then run the synthesis again."
        ),
        "language": "Write title, summary and idea in English.",
    },
}


# --- Request detection ------------------------------------------------------

_STRONG_MARKERS = (
    "synthese thematique",
    "plan thematique",
    "thematic synthesis",
    "thematic review",
    "thematic outline",
)
_THEME_MARKERS = (
    "par theme",
    "en themes",
    "des themes",
    "les themes",
    "grands themes",
    "thematique",
    "chapitre",
    "by theme",
    "into themes",
    "themes",
    "chapter",
)
_GROUPING_VERB_RE = re.compile(
    r"\b(?:regroup|group|class|organis|organiz|structur|ordonn|rassembl|cluster|categori)"
)


def _normalize(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(text or "").strip().lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


def is_thematic_synthesis_request(message: str) -> bool:
    """Whether the user asks to organise the notebook's ideas into themes or chapters."""
    text = _normalize(message)
    if not text:
        return False
    if any(marker in text for marker in _STRONG_MARKERS):
        return True
    has_theme = any(marker in text for marker in _THEME_MARKERS)
    return has_theme and bool(_GROUPING_VERB_RE.search(text))


# --- Article text ------------------------------------------------------------


@dataclass
class ArticlePage:
    label: str
    text: str


@dataclass
class KeyPoint:
    point_id: str
    source_number: int
    idea: str
    quote: str = ""
    page: str = ""


@dataclass
class Chapter:
    title: str
    summary: str
    points: list[KeyPoint] = field(default_factory=list)


def _page_label(unit: Any) -> str:
    # ``unit.page`` is 0-based for PDFs; the label carries the printed number.
    label = str(getattr(unit, "location_label", "") or "").strip()
    match = re.fullmatch(r"Page (\d+)", label)
    return f"p. {match.group(1)}" if match else label


def _looks_unreadable(text: str) -> bool:
    """Font-encoded PDFs yield strings like ``/C77/C101/C108`` instead of words."""
    value = str(text or "").strip()
    if len(value) < 200:
        return True
    return len(_WORD_RE.findall(value)) < len(value) / 40


def _drop_references(pages: list[ArticlePage]) -> list[ArticlePage]:
    """Cut the bibliography: it is long, and every idea in it belongs to other papers."""
    total = sum(len(page.text) for page in pages)
    offset = 0
    for index, page in enumerate(pages):
        for match in _REFERENCES_HEADING_RE.finditer(page.text):
            if offset + match.start() >= 0.4 * total:
                kept = page.text[: match.start()].rstrip()
                head = pages[:index]
                return head + [ArticlePage(page.label, kept)] if kept else head
        offset += len(page.text)
    return pages


def source_pages(source: dict) -> list[ArticlePage]:
    """Page-labelled text of a notebook source, re-read from its file when possible.

    The notebook stores a flat excerpt without page boundaries; the original
    document gives page numbers for the quotes and more than the stored
    excerpt. Pasted sources fall back to their stored content.
    """
    from src.unicode_utils import repair_pdf_text_artifacts

    rel_path = str(source.get("source_path") or "").strip()
    if rel_path:
        try:
            from src.document_formats import load_document_units
            from src.file_access import resolve_document_path

            full_path = resolve_document_path(_cfg.DATA_PATH, rel_path)
            if full_path is not None:
                units = load_document_units(full_path)[:_MAX_ARTICLE_PAGES]
                pages = [
                    ArticlePage(_page_label(unit), repair_pdf_text_artifacts(unit.text).strip())
                    for unit in units
                    if str(unit.text or "").strip()
                ]
                if pages and not _looks_unreadable("\n".join(page.text for page in pages)):
                    return _drop_references(pages)
        except Exception:
            _logger.debug("Notebook source %s could not be re-read", rel_path, exc_info=True)
    content = repair_pdf_text_artifacts(source.get("content") or "").strip()
    if not content or _looks_unreadable(content):
        return []
    return _drop_references([ArticlePage("", content)])


def fit_article_text(pages: list[ArticlePage], char_budget: int) -> str:
    """Article text with ``[p. N]`` markers, keeping its start and its end."""
    text = "\n\n".join(
        f"[{page.label}]\n{page.text}" if page.label else page.text for page in pages
    ).strip()
    if len(text) <= char_budget:
        return text
    head_end = int(char_budget * _HEAD_SHARE)
    space = text.rfind(" ", 0, head_end)
    head_end = space if space > 0 else head_end
    tail_start = len(text) - (char_budget - head_end)
    space = text.find(" ", tail_start)
    tail_start = space + 1 if 0 <= space < tail_start + 80 else tail_start
    return f"{text[:head_end].rstrip()}\n\n[…]\n\n{text[tail_start:].lstrip()}"


def _article_char_budget(model_name: str) -> int:
    """Characters of one article that fit the context window next to the instructions."""
    try:
        from src.llm_provider import is_ollama_provider, resolve_ollama_context_length

        if is_ollama_provider():
            context_tokens = resolve_ollama_context_length(
                model_name,
                required_tokens=_TARGET_ARTICLE_TOKENS
                + _OUTPUT_RESERVE_TOKENS
                + _PROMPT_OVERHEAD_TOKENS,
            )
            context_tokens = context_tokens or _FALLBACK_CONTEXT_TOKENS
        else:
            context_tokens = _NON_OLLAMA_CONTEXT_TOKENS
    except Exception:
        _logger.debug("Recovered from an error in _article_char_budget.", exc_info=True)
        context_tokens = _FALLBACK_CONTEXT_TOKENS
    usable = int(context_tokens * _CONTEXT_INPUT_RATIO) - _OUTPUT_RESERVE_TOKENS - _PROMPT_OVERHEAD_TOKENS
    return max(_MIN_ARTICLE_CHARS, min(_MAX_ARTICLE_CHARS, int(usable * _CHARS_PER_TOKEN)))


# --- LLM steps ---------------------------------------------------------------


class _ExtractedPoint(BaseModel):
    idea: str = Field(..., description="One self-contained sentence stating the key point.")
    quote: str = Field(
        default="",
        description="Verbatim passage (1-2 sentences) copied from the article text, in its original language.",
    )
    page: str = Field(default="", description="The [p. N] marker preceding the quoted passage.")


class _ExtractedPoints(BaseModel):
    points: list[_ExtractedPoint] = Field(
        default_factory=list,
        description="3 to 6 key points of this article.",
    )


class _PlannedChapter(BaseModel):
    title: str = Field(..., description="Short, explicit chapter title.")
    scope: str = Field(default="", description="One sentence saying which points belong here.")


class _ChapterPlan(BaseModel):
    chapters: list[_PlannedChapter] = Field(
        default_factory=list,
        description="3 to 7 thematic chapters.",
    )


def _point_key(point_id: str) -> str:
    return "p" + point_id.replace(".", "_")


def _assignment_model(points: list[KeyPoint], chapter_count: int) -> type[BaseModel]:
    """One required chapter number per point.

    Asked for a list of assignments, a small model stops after about three
    entries. Required keys make Ollama's schema-constrained decoding emit an
    answer for every point of the batch, and the enum keeps it in range.
    """
    chapter_number = Literal[tuple(range(1, chapter_count + 1))]  # type: ignore[valid-type]
    fields: dict[str, Any] = {
        _point_key(point.point_id): (
            chapter_number,
            Field(..., description=f"Chapter number for key point {point.point_id}."),
        )
        for point in points
    }
    return create_model("PointChapters", **fields)


class _ChapterText(BaseModel):
    summary: str = Field(..., description="3-5 sentences synthesising the chapter, citing sources as [n].")


def _invoke_structured(
    model_name: str,
    prompt: str,
    response_model: type[BaseModel],
) -> BaseModel:
    from src.llm_provider import invoke_structured_chat

    return invoke_structured_chat(
        model_name,
        [{"role": "user", "content": prompt}],
        response_model=response_model,
        context_tokens_hint=int(len(prompt) / _CHARS_PER_TOKEN) + _PROMPT_OVERHEAD_TOKENS,
    )


def _truncate(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _point_lines(points: list[KeyPoint]) -> str:
    return "\n".join(
        f"{point.point_id} [{point.source_number}] {_truncate(point.idea, _MAX_IDEA_CHARS)}"
        for point in points
    )


def _extraction_prompt(
    message: str,
    number: int,
    title: str,
    article_text: str,
    language: str,
) -> str:
    return (
        "Tu prépares une synthèse thématique de plusieurs articles scientifiques. "
        f"Voici l'article [{number}] : {title}\n"
        f"Demande de l'utilisateur : {message.strip()}\n\n"
        "Extrais de 3 à 6 points clés propres à cet article : question étudiée, méthode, "
        "résultats principaux, conclusions, limites.\n"
        "Pour chaque point :\n"
        "- idea : une phrase autonome qui énonce le point, sans formule du type "
        "« l'article montre ».\n"
        "- quote : un passage copié mot pour mot dans le texte ci-dessous (une ou deux "
        "phrases) qui appuie le point, dans sa langue d'origine. Ne traduis pas et ne "
        "reformule pas. Laisse vide si aucun passage ne convient.\n"
        "- page : le repère [p. N] qui précède ce passage, ou vide.\n"
        "Ignore l'en-tête de l'éditeur, les affiliations, les remerciements et la bibliographie. "
        f"{_STRINGS[language]['language']}\n\n"
        f"Texte de l'article [{number}] :\n{article_text}"
    )


def _plan_prompt(
    message: str,
    titles: dict[int, str],
    points: list[KeyPoint],
    language: str,
) -> str:
    index = "\n".join(f"[{number}] {title}" for number, title in titles.items())
    return (
        "Voici les points clés extraits de plusieurs articles scientifiques. Propose de 3 à 7 "
        "chapitres thématiques qui couvrent l'ensemble de ces points, pour une synthèse "
        "structurée.\n"
        f"Demande de l'utilisateur : {message.strip()}\n\n"
        "Regroupe par idée (phénomène, méthode, résultat, application, limite), pas par article : "
        "un bon chapitre réunit des points venant de plusieurs articles.\n"
        "Pour chaque chapitre :\n"
        "- title : un titre court et explicite.\n"
        "- scope : une courte phrase (moins de 20 mots) qui dit quels points y vont.\n"
        "Ordonne les chapitres du plus général au plus spécifique. "
        f"{_STRINGS[language]['language']}\n\n"
        f"Articles :\n{index}\n\n"
        f"Points clés (identifiant [source] énoncé) :\n{_point_lines(points)}"
    )


def _classification_prompt(plan: list[_PlannedChapter], points: list[KeyPoint]) -> str:
    chapters = "\n".join(
        f"{number}. {chapter.title}" + (f" — {chapter.scope}" if chapter.scope else "")
        for number, chapter in enumerate(plan, start=1)
    )
    lines = "\n".join(
        f"{_point_key(point.point_id)} : {_truncate(point.idea, _MAX_IDEA_CHARS)}"
        for point in points
    )
    return (
        "Classe des points clés dans les chapitres d'une synthèse thématique.\n\n"
        f"Chapitres :\n{chapters}\n\n"
        "Pour chaque point ci-dessous, donne sous sa clé le numéro du chapitre "
        f"(de 1 à {len(plan)}) qui lui correspond le mieux, même approximativement.\n\n"
        f"Points clés :\n{lines}"
    )


def _chapter_prompt(message: str, chapter: Chapter, language: str) -> str:
    return (
        f"Tu rédiges le chapitre « {chapter.title} » d'une synthèse thématique d'articles "
        "scientifiques.\n"
        f"Demande de l'utilisateur : {message.strip()}\n\n"
        "Voici les points clés que ce chapitre regroupe, chacun suivi de sa source [n] :\n"
        + "\n".join(
            f"- {_truncate(point.idea, _MAX_IDEA_CHARS)} [{point.source_number}]"
            for point in chapter.points
        )
        + "\n\nRédige dans summary un paragraphe de 3 à 5 phrases qui synthétise ce chapitre : "
        "idées communes, différences et complémentarités entre les articles. Cite la source [n] "
        "après chaque affirmation. N'ajoute aucun fait absent de ces points. "
        f"{_STRINGS[language]['language']}"
    )


def _clean_quote(quote: str) -> str:
    value = " ".join(str(quote or "").split())
    return value.strip(" \"'«»“”‘’")


def _normalize_page(value: str) -> str:
    page = str(value or "").strip().strip("[]()").strip()
    match = re.fullmatch(r"(?i)(?:p\.?|page)?\s*(\d+)", page)
    return f"p. {match.group(1)}" if match else page


def verify_quote(quote: str, pages: list[ArticlePage], claimed_page: str) -> tuple[bool, str]:
    """Whether ``quote`` comes from the article, and the page it was found on."""
    from src.drafting_citations import _quote_matches_evidence

    if not quote:
        return False, ""
    for page in pages:
        if _quote_matches_evidence(quote, page.text):
            return True, page.label
    # A quote may straddle a page break.
    if _quote_matches_evidence(quote, "\n".join(page.text for page in pages)):
        return True, _normalize_page(claimed_page)
    return False, ""


def extract_key_points(
    message: str,
    number: int,
    title: str,
    pages: list[ArticlePage],
    model_name: str,
    *,
    char_budget: int,
    language: str,
) -> list[KeyPoint]:
    """Key points of one article; unverifiable quotes are dropped."""
    prompt = _extraction_prompt(
        message, number, title, fit_article_text(pages, char_budget), language
    )
    result = _invoke_structured(model_name, prompt, _ExtractedPoints)
    if not isinstance(result, _ExtractedPoints):
        raise RuntimeError("unexpected structured output for key points")
    points: list[KeyPoint] = []
    for extracted in result.points:
        idea = " ".join(str(extracted.idea or "").split())
        if not idea:
            continue
        quote = _clean_quote(extracted.quote)
        verified, page = verify_quote(quote, pages, extracted.page)
        if verified and len(quote) > _MAX_QUOTE_CHARS:
            quote = _truncate(quote, _MAX_QUOTE_CHARS)
        points.append(
            KeyPoint(
                point_id=f"{number}.{len(points) + 1}",
                source_number=number,
                idea=idea,
                quote=quote if verified else "",
                page=page if verified else "",
            )
        )
    return points


def clean_citations(text: str, valid_numbers: set[int]) -> str:
    """Keep ``[n]`` markers that name an analysed source; ``[S3]`` becomes ``[3]``."""

    def replace(match: re.Match[str]) -> str:
        numbers = [int(value) for value in re.findall(r"\d+", match.group(0))]
        kept = [number for number in dict.fromkeys(numbers) if number in valid_numbers]
        return "".join(f"[{number}]" for number in kept)

    cleaned = _CITATION_GROUP_RE.sub(replace, str(text or ""))
    return re.sub(r"[ \t]+([.,;:])", r"\1", re.sub(r"[ \t]{2,}", " ", cleaned)).strip()


def plan_chapters(
    message: str,
    titles: dict[int, str],
    points: list[KeyPoint],
    model_name: str,
    *,
    language: str,
) -> list[_PlannedChapter]:
    """Chapter titles and scopes, without any point yet."""
    plan = _invoke_structured(model_name, _plan_prompt(message, titles, points, language), _ChapterPlan)
    if not isinstance(plan, _ChapterPlan):
        raise RuntimeError("unexpected structured output for the chapter plan")
    chapters: list[_PlannedChapter] = []
    seen: set[str] = set()
    for chapter in plan.chapters:
        title = " ".join(str(chapter.title or "").split()).strip(" #*")
        if title and title.casefold() not in seen:
            seen.add(title.casefold())
            # Scopes are prompts for the classifier, not text for the reader:
            # source markers in them would only bias the choice by article.
            # A long scope also makes the classifier stop part-way through a batch.
            scope = " ".join(_CITATION_GROUP_RE.sub("", str(chapter.scope or "")).split())
            scope = _truncate(re.split(r"(?<=[.!?])\s", scope, maxsplit=1)[0], _MAX_SCOPE_CHARS)
            chapters.append(_PlannedChapter(title=title, scope=scope))
    return chapters[:_MAX_CHAPTERS]


def classify_points(
    plan: list[_PlannedChapter],
    points: list[KeyPoint],
    model_name: str,
) -> Generator[int, None, dict[str, int]]:
    """Map point ids to 1-based chapter numbers, one short call per batch.

    Choosing a number for each point is far more reliable on a small model
    than listing ids under each chapter, which silently leaves most points out.
    A batch that fails, or answers out of range, is asked again once. Yields
    the number of points already classified before each call.
    """
    assignments: dict[str, int] = {}
    pending = list(points)
    for _attempt in range(2):
        for start in range(0, len(pending), _CLASSIFY_BATCH_SIZE):
            yield len(assignments)
            batch = pending[start : start + _CLASSIFY_BATCH_SIZE]
            try:
                result = _invoke_structured(
                    model_name,
                    _classification_prompt(plan, batch),
                    _assignment_model(batch, len(plan)),
                )
            except Exception:
                _logger.exception("Notebook synthesis could not classify a batch of key points")
                continue
            for point in batch:
                chapter = getattr(result, _point_key(point.point_id), None)
                if isinstance(chapter, int) and 1 <= chapter <= len(plan):
                    assignments[point.point_id] = chapter
        pending = [point for point in points if point.point_id not in assignments]
        if not pending:
            break
    return assignments


def assign_points(
    plan: list[_PlannedChapter],
    assignments: dict[str, int],
    points: list[KeyPoint],
    *,
    other_title: str,
) -> list[Chapter]:
    """Chapters in plan order, each point exactly once; empty chapters are dropped."""
    members: dict[int, list[KeyPoint]] = {}
    leftovers: list[KeyPoint] = []
    for point in points:
        number = assignments.get(point.point_id, 0)
        if 1 <= number <= len(plan):
            members.setdefault(number, []).append(point)
        else:
            leftovers.append(point)
    chapters = [
        Chapter(chapter.title, "", members[number])
        for number, chapter in enumerate(plan, start=1)
        if members.get(number)
    ]
    if leftovers:
        chapters.append(Chapter(other_title, "", leftovers))
    return chapters


def chapters_by_article(titles: dict[int, str], points: list[KeyPoint]) -> list[Chapter]:
    """Fallback when no thematic plan could be built: one chapter per article."""
    by_source: dict[int, list[KeyPoint]] = {}
    for point in points:
        by_source.setdefault(point.source_number, []).append(point)
    return [
        Chapter(titles.get(number, f"[{number}]"), "", members)
        for number, members in by_source.items()
    ]


def write_chapter_summary(
    message: str,
    chapter: Chapter,
    model_name: str,
    *,
    language: str,
) -> str:
    """Opening paragraph of a chapter, citing only the sources of its points."""
    result = _invoke_structured(model_name, _chapter_prompt(message, chapter, language), _ChapterText)
    if not isinstance(result, _ChapterText):
        return ""
    valid_numbers = {point.source_number for point in chapter.points}
    # Small models often wrap the paragraph in quotation marks, or leak the
    # closing ``"}`` of the JSON into it.
    summary = " ".join(str(result.summary or "").split()).strip("\"'«»“”{} ")
    return clean_citations(summary, valid_numbers)


# --- Rendering ---------------------------------------------------------------


def render_synthesis_markdown(
    chapters: list[Chapter],
    titles: dict[int, str],
    skipped: list[tuple[int, str, str]],
    *,
    language: str,
) -> str:
    """Markdown that the chat's fallback renderer also displays correctly.

    That renderer (used unless math loads ``marked``) only builds a list when
    every line of a block is a ``- `` item and knows no blockquote, so each
    point and its quote share one line.
    """
    strings = _STRINGS[language]
    points = [point for chapter in chapters for point in chapter.points]
    cited = sorted({point.source_number for point in points})
    stats = strings["stats"].format(
        articles=len(cited),
        chapters=len(chapters),
        points=len(points),
        quotes=sum(1 for point in points if point.quote),
    )
    lines = [f"## {strings['heading']}", "", f"*{stats}*"]
    for index, chapter in enumerate(chapters, start=1):
        lines.extend(["", f"### {index}. {chapter.title}"])
        if chapter.summary:
            lines.extend(["", chapter.summary])
        lines.append("")
        for point in chapter.points:
            line = f"- {_without_emphasis(point.idea)} [{point.source_number}]"
            if point.quote:
                where = f" ({point.page})" if point.page else ""
                line += f" — *« {_without_emphasis(point.quote)} »*{where}"
            lines.append(line)
    lines.extend(["", f"### {strings['sources']}", ""])
    lines.extend(f"- [{number}] {titles[number]}" for number in cited)
    if skipped:
        lines.extend(["", f"*{strings['skipped']} :*", ""])
        lines.extend(f"- [{number}] {title} — {reason}" for number, title, reason in skipped)
    return "\n".join(lines).strip() + "\n"


def _without_emphasis(text: str) -> str:
    # A stray asterisk would open an emphasis span over the rest of the line.
    return str(text or "").replace("*", "")


# --- Pipeline ----------------------------------------------------------------


def synthesis_language(message: str) -> str:
    try:
        from src.rag.literature_review import resolve_review_language

        language = resolve_review_language(message)
    except Exception:
        _logger.debug("Recovered from an error in synthesis_language.", exc_info=True)
        language = "fr"
    return language if language in _STRINGS else "fr"


def generate_thematic_synthesis(
    sources: list[dict],
    message: str,
    model_name: str,
    *,
    abort_signal: Any = None,
) -> Generator[dict[str, Any], None, str]:
    """Yield ``status`` events; return the synthesis Markdown.

    Raises ``RuntimeError`` with a user-facing message when nothing usable
    comes out, and ``InterruptedError`` when the user stops the generation.
    """
    language = synthesis_language(message)
    strings = _STRINGS[language]

    def check_abort() -> None:
        if abort_signal is not None and abort_signal.is_aborted():
            raise InterruptedError("Generation aborted.")

    numbered = [
        (number, source)
        for number, source in enumerate(sources, start=1)
        if isinstance(source, dict)
        and (str(source.get("content") or "").strip() or str(source.get("source_path") or "").strip())
    ]
    if not numbered:
        raise RuntimeError(strings["no_sources"])

    titles = {
        number: str(source.get("title") or f"Source {number}").strip()
        for number, source in numbered
    }
    skipped: list[tuple[int, str, str]] = [
        (number, titles[number], strings["over_limit"].format(limit=MAX_SYNTHESIS_ARTICLES))
        for number, _source in numbered[MAX_SYNTHESIS_ARTICLES:]
    ]
    selected = numbered[:MAX_SYNTHESIS_ARTICLES]
    char_budget = _article_char_budget(model_name)

    points: list[KeyPoint] = []
    for index, (number, source) in enumerate(selected, start=1):
        check_abort()
        title = titles[number]
        yield {
            "type": "status",
            "msg": strings["reading"].format(
                index=index, total=len(selected), title=_truncate(title, 80)
            ),
        }
        pages = source_pages(source)
        if not pages:
            skipped.append((number, title, strings["unreadable"]))
            continue
        try:
            article_points = extract_key_points(
                message,
                number,
                title,
                pages,
                model_name,
                char_budget=char_budget,
                language=language,
            )
        except InterruptedError:
            raise
        except Exception:
            _logger.exception("Notebook synthesis failed on source %s", number)
            skipped.append((number, title, strings["failed"]))
            continue
        if not article_points:
            skipped.append((number, title, strings["no_points"]))
        points.extend(article_points)

    check_abort()
    if not points:
        raise RuntimeError(strings["nothing"])
    skipped.sort(key=lambda row: row[0])
    analysed = {point.source_number for point in points}
    analysed_titles = {number: title for number, title in titles.items() if number in analysed}

    yield {"type": "status", "msg": strings["planning"].format(count=len(points))}
    try:
        plan = plan_chapters(message, analysed_titles, points, model_name, language=language)
    except Exception:
        _logger.exception("Notebook synthesis could not plan the chapters")
        plan = []
    chapters: list[Chapter] = []
    if plan:
        classification = classify_points(plan, points, model_name)
        try:
            while True:
                classified = next(classification)
                check_abort()
                yield {
                    "type": "status",
                    "msg": strings["classifying"].format(done=classified, total=len(points)),
                }
        except StopIteration as stop:
            assignments = stop.value
        chapters = assign_points(plan, assignments, points, other_title=strings["other"])
    if not any(chapter.title != strings["other"] for chapter in chapters):
        chapters = chapters_by_article(analysed_titles, points)
    else:
        thematic = [chapter for chapter in chapters if chapter.title != strings["other"]]
        for index, chapter in enumerate(thematic, start=1):
            check_abort()
            yield {
                "type": "status",
                "msg": strings["writing"].format(
                    index=index, total=len(thematic), title=_truncate(chapter.title, 80)
                ),
            }
            try:
                chapter.summary = write_chapter_summary(
                    message, chapter, model_name, language=language
                )
            except Exception:
                _logger.exception("Notebook synthesis could not write chapter %s", index)
    check_abort()
    return render_synthesis_markdown(chapters, titles, skipped, language=language)


__all__ = [
    "MAX_SYNTHESIS_ARTICLES",
    "ArticlePage",
    "Chapter",
    "KeyPoint",
    "assign_points",
    "chapters_by_article",
    "classify_points",
    "clean_citations",
    "extract_key_points",
    "fit_article_text",
    "generate_thematic_synthesis",
    "is_thematic_synthesis_request",
    "plan_chapters",
    "render_synthesis_markdown",
    "source_pages",
    "synthesis_language",
    "verify_quote",
    "write_chapter_summary",
]
