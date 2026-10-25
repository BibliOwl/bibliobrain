"""Questions about one named paper: which paper, and which of its passages.

"Que dit Uchida 2015 sur la supraconductivité à haute température ?" names a
book of the library, but only the "Uchida (2015)" spelling was recognised.
The search then took twelve passages from anywhere, a 3B model answered from
the abstract of one chapter and invented "iodine" from "Fe". Here the name
and year are matched against the library, the question is scoped to that
paper, and a broad question gets the paper's abstract, chapter openings and
conclusion, spread over the whole document, instead of twelve neighbours.

Nothing here calls a model.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from typing import Any

_NAME = r"[A-ZÀ-Ý][A-Za-zÀ-ÿ'’-]+"
_STUDY_RE = re.compile(
    rf"(?<![\w-])(?P<author>{_NAME})"
    rf"(?:\s+(?:et\s+al|and\s+al|et\s+coll|et\s+collaborateurs)\.?"
    rf"|\s+(?:et|and|&)\s+{_NAME})?"
    r"\s*,?\s*\(?\s*(?P<year>(?:19|20)\d{2})[a-z]?\s*\)?"
)
# More than this many papers for one name and year is not a reference to one.
_MAX_PAPERS_PER_STUDY = 2
_MAX_SCOPED_PAPERS = 3


def _plain(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def named_studies(message: str) -> list[tuple[str, str]]:
    """``(author, year)`` pairs written as "Uchida 2015", "Kim et al. (2019)"..."""
    found: list[tuple[str, str]] = []
    for match in _STUDY_RE.finditer(str(message or "")):
        author = _plain(match.group("author"))
        pair = (author, match.group("year"))
        if author and pair not in found:
            found.append(pair)
    return found


def _family_names(first_author: Any) -> set[str]:
    """ "Uchida, Shin-ichi" is Uchida; "Shin-ichi Uchida" too."""
    raw = str(first_author or "").strip()
    if not raw:
        return set()
    family = raw.split(",", 1)[0] if "," in raw else raw.split()[-1]
    words = _plain(family).split()
    names = set(words)
    if len(words) > 1:
        names.add(" ".join(words))
    return names


def resolve_study_sources(message: str, papers: Iterable[Any]) -> list[dict[str, Any]]:
    """The library papers the question names by first author and year.

    ``papers`` holds one record per paper (two copies of a PDF are one
    paper), each with a ``metadata`` dict. A name and year that match no
    paper, or too many, are left to the normal search.
    """
    studies = named_studies(message)
    if not studies:
        return []
    records = [dict(getattr(paper, "metadata", paper) or {}) for paper in papers]
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for author, year in studies:
        matches = [
            record
            for record in records
            if str(record.get("zotero_year") or "").strip() == year
            and author in _family_names(record.get("zotero_first_author"))
        ]
        if not matches or len(matches) > _MAX_PAPERS_PER_STUDY:
            continue
        for record in matches:
            path = str(record.get("rel_path") or record.get("source") or "").strip()
            if path and path not in seen:
                seen.add(path)
                resolved.append(record)
    return resolved[:_MAX_SCOPED_PAPERS]


_BROAD_MARKERS = (
    "que dit",
    "qu est ce que dit",
    "que raconte",
    "de quoi parle",
    "resume",
    "resumer",
    "synthese",
    "idees principales",
    "idees cles",
    "points cles",
    "principaux points",
    "principaux resultats",
    "apports",
    "qu apporte",
    "que retenir",
    "message principal",
    "presente moi",
    "what does",
    "what is it about",
    "summar",
    "overview",
    "main ideas",
    "key points",
    "key findings",
    "takeaways",
)
_TOPIC_RE = re.compile(r"\b(?:sur|about|concernant|regarding|on)\b(?P<topic>.+)$")
_TOPIC_STOPWORDS = frozenset(
    "le la les l de du des d un une et ou a au aux en sur dans pour par avec son sa ses "
    "leur leurs ce cette ces the of a an in on about and or to with for its their this "
    "these is are".split()
)
# French words of a question against the English title of a paper.
_TOPIC_ALIASES = {
    "haute": "high",
    "hautes": "high",
    "basse": "low",
    "basses": "low",
    "supraconduct": "superconduct",
    "supraconducteur": "superconductor",
    "aimant": "magnet",
    "bobine": "coil",
    "courant": "current",
    "champ": "field",
    "perte": "loss",
    "pertes": "losses",
    "ruban": "tape",
    "fil": "wire",
}


def _topic_words(text: str) -> list[str]:
    return [word for word in _plain(text).split() if word not in _TOPIC_STOPWORDS]


def _alias(word: str) -> str:
    for prefix, english in _TOPIC_ALIASES.items():
        if word.startswith(prefix):
            return english + word[len(prefix) :] if len(prefix) > 6 else english
    return word


def _same_word(left: str, right: str) -> bool:
    if left == right:
        return True
    size = min(len(left), len(right))
    return size >= 5 and left[:5] == right[:5]


def overview_share(message: str, title: str, *, studies: Sequence[tuple[str, str]] = ()) -> float:
    """Part of the passages that should cover the whole paper (0 when none).

    "Que dit Uchida 2015 sur la supraconductivité à haute température ?" asks
    about the book's own subject: two thirds. "Que dit Uchida 2015 sur le
    dopage ?" names a point of it: a third, the rest from the search.
    """
    text = _plain(message)
    if not any(marker in text for marker in _BROAD_MARKERS):
        return 0.0
    topic_match = _TOPIC_RE.search(text)
    topic = _topic_words(topic_match.group("topic")) if topic_match else []
    named = {author for author, _year in studies} | {year for _author, year in studies}
    topic = [word for word in topic if word not in named]
    if not topic:
        return 2 / 3
    title_words = [_alias(word) for word in _topic_words(title)]
    hits = sum(
        1 for word in topic if any(_same_word(_alias(word), candidate) for candidate in title_words)
    )
    return 2 / 3 if hits * 2 >= len(topic) else 1 / 3


_HEADING_RE = re.compile(
    r"(?im)^\s*(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(abstract|r[ée]sum[ée]|summary|preface|pr[ée]face|foreword|avant-propos|introduction"
    r"|conclusions?|concluding remarks|summary and outlook|outlook|perspectives"
    r"|discussion and conclusions?)\b"
)
_CHAPTER_RE = re.compile(r"(?im)^\s*(?:chapter|chapitre)\s+\d+\b")
_FRONT_MATTER_RE = re.compile(
    r"\bISBN\b|all rights reserved|tous droits r[ée]serv[ée]s|library of congress"
    r"|series editors|editorial board|acid-free paper",
    re.IGNORECASE,
)
_REFERENCE_HEADING_RE = re.compile(r"(?im)^\s*(references|bibliography|bibliographie)\s*$")
_CITATION_YEAR_RE = re.compile(r"\((?:19|20)\d{2}\)")
_DOT_LEADER_RE = re.compile(r"\.{4,}|(?:\.\s){4,}")
_STRONG = frozenset({"abstract", "chapter"})
_ENDINGS = ("conclusion", "concluding", "summary", "outlook", "perspectives", "discussion and")
_LEADS = ("abstract", "resume", "summary", "preface", "foreword", "avant-propos", "introduction")


def _page_number(metadata: dict[str, Any]) -> int:
    try:
        return int(str(metadata.get("page", "")).strip())
    except ValueError:
        return 10**9


def _chunk_number(metadata: dict[str, Any]) -> int:
    parts = str(metadata.get("chunk_uid") or "").split(":")
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    chunk_id = metadata.get("chunk_id")
    return int(chunk_id) if isinstance(chunk_id, int) else 10**9


def _is_noise(text: str) -> bool:
    if len(text.strip()) < 200:
        return True
    if _FRONT_MATTER_RE.search(text) or len(_DOT_LEADER_RE.findall(text)) >= 3:
        return True
    head = text[:400]
    if _REFERENCE_HEADING_RE.search(head):
        return True
    return len(_CITATION_YEAR_RE.findall(text)) >= 5


def _structure_label(text: str) -> str:
    head = text[:600]
    if _CHAPTER_RE.search(head):
        return "chapter"
    match = _HEADING_RE.search(head)
    return _plain(match.group(1)) if match else ""


def _is_ending(text: str) -> bool:
    match = _HEADING_RE.search(text[:600])
    return match is not None and _plain(match.group(1)).startswith(_ENDINGS)


def _spread(items: list[Any], count: int) -> list[Any]:
    """``count`` items evenly spaced over ``items``, first and last included."""
    if count <= 0 or not items:
        return []
    if count >= len(items):
        return list(items)
    if count == 1:
        return [items[0]]
    step = (len(items) - 1) / (count - 1)
    return [items[round(index * step)] for index in range(count)]


def overview_passages(chunks: Sequence[Any], count: int) -> list[Any]:
    """The passages that show a whole paper: its opening, chapters and end.

    ``chunks`` are the paper's indexed chunks (Documents). Each kept passage
    opens with a heading (abstract, introduction, chapter, conclusion); when
    there are more than ``count`` the first opening and the last conclusion
    stay and the rest is spread over the document. Short papers with few
    headings are filled with passages spread evenly. The result is in
    document order.
    """
    if count <= 0:
        return []
    usable = [
        chunk
        for chunk in chunks
        if str((chunk.metadata or {}).get("content_type") or "pdf_page")
        not in {"zotero_note", "wiki_page"}
    ]
    # An OCR pass re-indexes pages the text layer already had: keep one reading.
    text_pages = {
        _page_number(chunk.metadata or {})
        for chunk in usable
        if not str((chunk.metadata or {}).get("chunk_uid") or "").startswith("ocr-")
    }
    usable = [
        chunk
        for chunk in usable
        if not (
            str((chunk.metadata or {}).get("chunk_uid") or "").startswith("ocr-")
            and _page_number(chunk.metadata or {}) in text_pages
        )
    ]
    ordered = sorted(
        usable,
        key=lambda chunk: (
            _page_number(chunk.metadata or {}),
            _chunk_number(chunk.metadata or {}),
        ),
    )
    content = [chunk for chunk in ordered if not _is_noise(str(chunk.page_content or ""))]
    if not content:
        return []
    labelled = [(chunk, _structure_label(str(chunk.page_content or ""))) for chunk in content]
    chosen: list[Any] = []

    def take(candidates: list[Any], room: int) -> None:
        fresh = [chunk for chunk in candidates if all(chunk is not kept for kept in chosen)]
        chosen.extend(_spread(fresh, room))

    lead = next((chunk for chunk, label in labelled if label in _LEADS), content[0])
    chosen.append(lead)
    # The last chapter or the conclusion, not the page header that repeats
    # "Summary and Perspectives" on each of its pages.
    endings = [
        chunk for chunk, _label in labelled[len(labelled) // 2 :] if _is_ending(chunk.page_content)
    ]
    ending = next(
        (chunk for chunk in endings if _structure_label(chunk.page_content) == "chapter"),
        endings[0] if endings else None,
    )
    if ending is not None and ending is not lead and count > 1:
        chosen.append(ending)
    # Chapter openings and abstracts first; then the first passage under each
    # other heading, since a page header repeats "Introduction" on every page.
    take([chunk for chunk, label in labelled if label in _STRONG], count - len(chosen))
    first_per_label: dict[str, Any] = {}
    for chunk, label in labelled:
        if label and label not in _STRONG:
            first_per_label.setdefault(label, chunk)
    take(list(first_per_label.values()), count - len(chosen))
    take(content, count - len(chosen))
    position = {id(chunk): index for index, chunk in enumerate(content)}
    return sorted(chosen[:count], key=lambda chunk: position.get(id(chunk), 0))


__all__ = [
    "named_studies",
    "overview_passages",
    "overview_share",
    "resolve_study_sources",
]
