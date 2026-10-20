"""Spot library records with doubtful metadata and propose a correction.

Zotero's automatic PDF import sometimes keeps a thesis cover-page line as the
title ("discipline : MECANIQUE ENERGETIQUE Ecole Doctorale ...") or leaves the
year empty. Bibliobrain reads zotero.sqlite read-only, so it only flags these
records and proposes the correct values, found by DOI or from the title printed
on the PDF's first page; the user applies them in Zotero.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from pypdf import PdfReader

from src.document_guard import check_document
from src.zotero_db import ZoteroItem

_logger = logging.getLogger(__name__)

# Cover-page and administrative lines that are never an article title.
_BOILERPLATE_RE = re.compile(
    r"(?i)\b(?:discipline|[ée]cole doctorale|doctoral school|pour obtenir le grade|"
    r"grade de docteur|docteur de l|doctor of philosophy|in partial fulfil+ment|"
    r"th[èe]se (?:de doctorat|pr[ée]sent[ée]e)|thesis submitted|submitted to|"
    r"pr[ée]sent[ée]e? (?:par|et soutenue)|soutenue (?:publiquement|le)|defended|"
    r"universit\w*|polytechni\w*|d[ée]partement|department|institut\w*|laboratoire|laboratory|umr\s*\d|cnrs|"
    r"table (?:of contents|des mati[èe]res)|copyright|all rights reserved|jury|"
    r"rapporteur|examinateur|directeur de th[èe]se|hal id|archive ouverte|"
    r"multi-disciplinary open access|submitted on|hal-\d|tel-\d)\b"
)
_TITLE_ISSUES = {
    "titre manquant",
    "le titre ressemble à une page de garde",
    "le titre reprend le nom du fichier",
    "titre très court",
}
_THESIS_COVER_RE = re.compile(r"(?i)\b(?:th[èe]se|thesis|doctorat|dissertation)\b")
_FILENAME_TITLE_RE = re.compile(r"^[^-]{2,60} - (?:\d{4} - )?.+")
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")
# Words that continue a title across a line break ("... OF QUENCH / IN THE ITER").
_CONTINUATION_WORDS = {
    "in", "of", "the", "and", "for", "with", "on", "to", "at", "by", "a", "an", "from",
    "under", "via", "using", "during",
}


def _looks_like_filename(title: str) -> bool:
    lowered = title.lower()
    return (
        lowered.endswith((".pdf", ".docx", ".epub"))
        or "_" in title and " " not in title.strip()
        or bool(_FILENAME_TITLE_RE.match(title)) and bool(_YEAR_RE.search(title.split(" - ")[1] if " - " in title else ""))
    )


def metadata_issues(item: ZoteroItem | None, rel_path: str) -> list[str]:
    """Reasons, in French, why this record's metadata looks wrong."""
    if item is None:
        return ["aucune notice Zotero associée"]
    issues: list[str] = []
    title = (item.title or "").strip()
    if not title:
        issues.append("titre manquant")
    elif _BOILERPLATE_RE.search(title):
        issues.append("le titre ressemble à une page de garde")
    elif _looks_like_filename(title):
        # A file named after its (correct) title is common; only a title that
        # still carries file-name traits is suspicious.
        issues.append("le titre reprend le nom du fichier")
    elif len(title) < 12:
        issues.append("titre très court")
    if not str(item.year or "").strip():
        issues.append("année manquante")
    if not item.authors:
        issues.append("auteurs manquants")
    return issues


def _first_page_lines(full_path: str) -> list[str]:
    try:
        check_document(full_path)
        reader = PdfReader(full_path, strict=False)
        text = reader.pages[0].extract_text() if reader.pages else ""
    except Exception:
        _logger.debug("Recovered from an error in _first_page_lines.", exc_info=True)
        return []
    return [re.sub(r"\s+", " ", line).strip() for line in str(text or "").splitlines() if line.strip()]


def _title_candidate(lines: list[str]) -> str:
    """The first title-like block, skipping cover-page boilerplate.

    Consecutive lines are joined only when the next one continues the title
    ("IN THE ITER MAGNET SYSTEMS"), so a translated title printed right after
    is not glued to it.
    """
    for index, line in enumerate(lines[:40]):
        words = line.split()
        if (
            len(words) < 3
            or len(line) > 220
            or _BOILERPLATE_RE.search(line)
            or _YEAR_RE.search(line) and len(words) < 6
            or "@" in line
        ):
            continue
        title = line
        for following in lines[index + 1 : index + 3]:
            first_word = following.split()[0].lower() if following.split() else ""
            if first_word in _CONTINUATION_WORDS and not _BOILERPLATE_RE.search(following):
                title = f"{title} {following}"
            else:
                break
        return title
    return ""


def cover_page_metadata(full_path: str) -> dict[str, Any]:
    """Title, author and year read from a thesis-like first page."""
    lines = _first_page_lines(full_path)
    if not lines:
        return {}
    is_cover = any(_THESIS_COVER_RE.search(line) for line in lines[:25])
    author = ""
    for index, line in enumerate(lines[:30]):
        if re.search(r"(?i)\b(?:pr[ée]sent[ée]e?|soutenue|submitted)\b.*\b(?:par|by)\s*$", line):
            if index + 1 < len(lines):
                author = lines[index + 1]
            break
    year_match = next((match for line in lines[:30] if (match := _YEAR_RE.search(line))), None)
    return {
        "title": _title_candidate(lines),
        "author": author,
        "year": year_match.group(1) if year_match else "",
        "is_thesis_cover": is_cover,
    }


def propose_metadata_fix(item: ZoteroItem | None, full_path: str) -> dict[str, Any]:
    """Best correction available: DOI match first, then the cover-page title.

    Returns ``{"title", "year", "authors", "doi", "source"}``; ``source`` is
    ``openalex``/``crossref`` for a confirmed external match, ``page`` when only
    the first page could be read, or empty when nothing better was found.
    """
    from src.pdf_metadata import _crossref_lookup, _openalex_lookup

    local = item or ZoteroItem()
    cover = cover_page_metadata(full_path) if full_path and os.path.exists(full_path) else {}
    # A sound Zotero title beats anything read on the page (a HAL cover sheet
    # would otherwise replace it); the page title is only for broken titles.
    title_is_broken = bool(_TITLE_ISSUES & set(metadata_issues(item, full_path or "")))
    probe = ZoteroItem(
        title=(cover.get("title") or local.title) if title_is_broken else local.title,
        authors=list(local.authors) or ([cover["author"]] if cover.get("author") else []),
        year=local.year or cover.get("year", ""),
        doi=local.doi,
    )
    for source, lookup in (("openalex", _openalex_lookup), ("crossref", _crossref_lookup)):
        try:
            match, _definitive = lookup(probe)
        except Exception:
            _logger.debug("Recovered from an error in propose_metadata_fix.", exc_info=True)
            match = None
        if match is not None and match.title:
            return {
                "title": match.title,
                "year": match.year or probe.year,
                "authors": list(match.authors[:4]),
                "doi": match.doi,
                "source": source,
            }
    if title_is_broken and cover.get("title") and cover["title"] != local.title:
        return {
            "title": cover["title"],
            "year": probe.year,
            "authors": probe.authors[:4],
            "doi": local.doi,
            "source": "page",
        }
    return {"title": "", "year": "", "authors": [], "doi": "", "source": ""}
