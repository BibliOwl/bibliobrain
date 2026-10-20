"""Citation markers in article text and the reference entries they point to.

Pure text functions, without network or model: they read the text extracted
from a PDF, cut its reference list into entries, find the in-text markers
(``[12]``, ``(Smith, 1972)``, AIP superscripts flattened to ``windings,2``)
and map each marker to its entries.

Measured on a 612-PDF library (2026-10-19): 96 % of numeric markers and 65 %
of author-year markers resolve; see docs/3-ARCHITECTURE/origin-tracing-plan.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LETTERS = "A-Za-z\u00C0-\u024F"
_UPPER = "A-Z\u00C0-\u00DE"
_NAME = rf"[{_UPPER}][{_LETTERS}'’\-]+"
_PARTICLES = r"(?:(?:van|von|de|der|den|del|della|di|da|du|le|la|dos|das)\s+)*"

_REFERENCES_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:[\dIVX]+\.?[ \t]*)?"
    r"(?:references(?: and notes)?|reference list|bibliography|bibliographie|"
    r"références(?: bibliographiques)?|literature cited|works cited|notes and references)"
    r"[ \t]*:?[ \t]*$"
)
# Some PDFs glue the heading to the first entry: "References 1. A. Author".
_REFERENCES_INLINE_RE = re.compile(r"(?i)\breferences\s*(?=\[?1[\].\s])")
# The bibliography sits in the second part of an article; an earlier
# "References" line is a table of contents or a running header.
_REFERENCES_MIN_SHARE = 0.4

_NUMBERED_ENTRY_RES = (
    ("bracket", re.compile(r"(?m)^\s*\[(\d{1,3})\]\s*\S")),
    ("dot", re.compile(rf"(?m)^\s*(\d{{1,3}})\.\s+[{_UPPER}]")),
    ("space", re.compile(rf"(?m)^\s*(\d{{1,3}})\s+[{_UPPER}][{_LETTERS}'’\-]*[,.]")),
    # AIP/APS PDFs lose the superscript: "17T. Ogasawara, K. Yasukochi, ..."
    ("glued", re.compile(rf"(?:^|(?<=\s))(\d{{1,3}})(?=(?:[{_UPPER}]\.\s?){{1,3}}[{_UPPER}][{_LETTERS}'’\-]+,)", re.M)),
)
_MIN_NUMBERED_ENTRIES = 3
_MAX_ENTRY_CHARS = 600
_CAPTION_RE = re.compile(r"\s(?:Figure|Fig\.|Table|Tableau)\s+\d+\s*[.:]")

_AUTHOR_YEAR_ENTRY_START_RE = re.compile(
    rf"^\s*{_PARTICLES}{_NAME},?\s+(?:[{_UPPER}]\.|[{_UPPER}]{{1,3}}[,\s]|[{_UPPER}][{_LETTERS}]+,)"
)
_YEAR_RE = re.compile(r"\b(?:1[5-9]|20)\d{2}[a-z]?\b")

# PDF extraction sometimes pads the brackets: "[ 12]", "[5 ]".
_BRACKET_RE = re.compile(r"\[\s?(\d{1,3}(?:\s*[-–,]\s*\d{1,3})*)\s?\]")
_BRACKET_RANGE_RE = re.compile(r"\[\s?(\d{1,3})\s?\]\s*[-–]\s*\[\s?(\d{1,3})\s?\]")
_UNICODE_SUPERSCRIPT_RE = re.compile(r"(?<=[a-z\).,])([¹²³⁴⁵⁶⁷⁸⁹⁰]+(?:[,–⁻-][¹²³⁴⁵⁶⁷⁸⁹⁰]+)*)")
# Three lowercase letters keep chemical formulas (YBa2Cu3O7, Nb3Sn, cm2) out.
_GLUED_RE = re.compile(r"(?<=[a-z]{3})([,.]?)(\d{1,3}(?:[,–-]\d{1,3})*)(?=[\s.,;:)]|$)")
_PAREN_RE = re.compile(r"\(([^()]{0,240}?(?:1[5-9]|20)\d{2}[a-z]?[^()]{0,80})\)")
_AUTHOR_YEAR_RE = re.compile(
    rf"{_PARTICLES}({_NAME})(?:\s+(?:et\s+al\.?|and\s+{_NAME}|&\s*{_NAME}))?,?\s*((?:1[5-9]|20)\d{{2}})[a-z]?"
)
_NARRATIVE_RE = re.compile(
    rf"({_NAME})(?:\s+(?:et\s+al\.?|and\s+{_NAME}|&\s*{_NAME}))?\s*\(((?:1[5-9]|20)\d{{2}})[a-z]?\)"
)
_NOT_SURNAMES = frozenset({
    "Fig", "Figs", "Figure", "Table", "Eq", "Eqs", "Section", "Chapter", "In", "The", "This", "See",
    "Ref", "Refs", "Since", "From", "After", "Before", "During", "Until", "Between", "Copyright",
    "Received", "Accepted", "Published", "Conference", "Workshop", "Report", "Proc", "Vol",
    "January", "February", "March", "April", "May", "June", "July", "August", "September",
    "October", "November", "December", "Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
    "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre",
})
_SUPERSCRIPT_DIGITS = str.maketrans("¹²³⁴⁵⁶⁷⁸⁹⁰⁻", "1234567890-")
_MAX_RANGE = 40
# A document uses flattened superscripts when it has almost no bracket marker
# but many glued numbers that match its numbered entries.
_MIN_GLUED_MARKERS = 5


@dataclass(frozen=True)
class CitationMarker:
    text: str
    start: int
    end: int
    kind: str
    numbers: tuple[int, ...] = ()
    surname: str = ""
    year: str = ""


@dataclass(frozen=True)
class ReferenceEntry:
    text: str
    number: int | None = None


@dataclass
class ReferenceList:
    entries: list[ReferenceEntry] = field(default_factory=list)
    style: str = ""
    found_by: str = "none"
    start: int = -1
    text: str = ""

    @property
    def numbered(self) -> bool:
        return any(entry.number is not None for entry in self.entries)

    def entry(self, number: int) -> ReferenceEntry | None:
        for entry in self.entries:
            if entry.number == number:
                return entry
        return None


def normalize_text(text: str) -> str:
    """Lowercase text with PDF line-break hyphens and soft hyphens removed."""

    value = str(text or "").replace("\u00ad", "")
    value = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", value)
    return re.sub(r"\s+", " ", value).strip().lower()


def split_references(text: str) -> tuple[int, str]:
    """Offset where the reference list starts, and how it was found.

    Returns ``(-1, "none")`` when the document has no recognizable list.
    """

    value = str(text or "")
    total = len(value)
    for match in _REFERENCES_HEADING_RE.finditer(value):
        if match.start() >= _REFERENCES_MIN_SHARE * total:
            return match.end(), "heading"
    inline = [m for m in _REFERENCES_INLINE_RE.finditer(value) if m.start() >= _REFERENCES_MIN_SHARE * total]
    if inline:
        return inline[-1].end(), "inline"
    for _style, pattern in _NUMBERED_ENTRY_RES:
        for match in pattern.finditer(value):
            if match.group(1) != "1" or match.start() < 0.5 * total:
                continue
            numbers = [int(m.group(1)) for m in pattern.finditer(value, match.start())]
            if len(numbers) >= 5 and numbers[:3] == [1, 2, 3]:
                return match.start(), "numbered_run"
    return -1, "none"


def _increasing_run(matches: list[re.Match[str]]) -> list[tuple[int, int]]:
    """Numbered entries in order; page numbers and volumes break the run."""

    kept: list[tuple[int, int]] = []
    expected = 1
    for match in matches:
        number = int(match.group(1))
        if expected <= number <= expected + 3:
            kept.append((number, match.start()))
            expected = number + 1
    return kept


def _numbered_entries(refs: str) -> tuple[list[ReferenceEntry], str]:
    best: tuple[list[ReferenceEntry], str] = ([], "")
    for style, pattern in _NUMBERED_ENTRY_RES:
        kept = _increasing_run(list(pattern.finditer(refs)))
        if len(kept) < _MIN_NUMBERED_ENTRIES or len(kept) <= len(best[0]):
            continue
        entries = []
        for index, (number, start) in enumerate(kept):
            end = kept[index + 1][1] if index + 1 < len(kept) else start + _MAX_ENTRY_CHARS
            entries.append(ReferenceEntry(text=_clean_entry(refs[start:end]), number=number))
        best = (entries, style)
    return best


def _author_year_entries(refs: str) -> list[ReferenceEntry]:
    entries: list[str] = []
    current: list[str] = []
    for line in refs.splitlines():
        if not line.strip():
            continue
        starts_entry = bool(_AUTHOR_YEAR_ENTRY_START_RE.match(line))
        if starts_entry and current and _YEAR_RE.search(" ".join(current)):
            entries.append(" ".join(current))
            current = []
        current.append(line.strip())
    if current:
        entries.append(" ".join(current))
    return [ReferenceEntry(text=_clean_entry(entry)) for entry in entries if _YEAR_RE.search(entry)]


def _clean_entry(text: str) -> str:
    value = re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", str(text or ""))
    # Figure captions printed between two entries are not part of the first one.
    caption = _CAPTION_RE.search(value)
    if caption:
        value = value[: caption.start()]
    return re.sub(r"\s+", " ", value).strip()[:_MAX_ENTRY_CHARS]


def parse_reference_list(text: str) -> ReferenceList:
    """Cut the reference list of an article into entries."""

    start, found_by = split_references(text)
    if start < 0:
        return ReferenceList()
    refs = str(text)[start:]
    entries, style = _numbered_entries(refs)
    if not entries:
        entries, style = _author_year_entries(refs), "author_year"
    if not entries:
        style = ""
    return ReferenceList(entries=entries, style=style, found_by=found_by, start=start, text=refs)


def _expand_numbers(group: str) -> tuple[int, ...]:
    numbers: list[int] = []
    for part in re.split(r"\s*,\s*", group):
        bounds = [bound for bound in re.split(r"\s*[-–]\s*", part) if bound]
        try:
            if len(bounds) == 2:
                low, high = int(bounds[0]), int(bounds[1])
                if 0 < low <= high and high - low <= _MAX_RANGE:
                    numbers.extend(range(low, high + 1))
            elif len(bounds) == 1:
                numbers.append(int(bounds[0]))
        except ValueError:
            continue
    return tuple(number for number in numbers if number > 0)


def _is_surname(value: str) -> bool:
    # All-caps words are acronyms (ITER, YBCO, PSV), not authors.
    return value not in _NOT_SURNAMES and not value.isupper()


def extract_markers(text: str, *, glued_superscripts: bool = False) -> list[CitationMarker]:
    """In-text citation markers, in reading order.

    ``glued_superscripts`` also reads digits glued to a word (``windings,2``):
    only pass it for a document whose numbered entries confirm that style
    (see :func:`uses_glued_superscripts`), since a bare digit is ambiguous.
    """

    value = str(text or "")
    markers: list[CitationMarker] = []
    position = 0
    while match := _BRACKET_RE.search(value, position):
        numbers = _expand_numbers(match.group(1))
        start, end = match.start(), match.end()
        # IEEE writes a range across brackets: "[2]–[7]" cites 2 to 7.
        span = _BRACKET_RANGE_RE.match(value, start)
        merged = _expand_numbers(f"{span.group(1)}-{span.group(2)}") if span else ()
        if span and merged:
            numbers, end = merged, span.end()
        if numbers:
            markers.append(CitationMarker(value[start:end], start, end, "numeric", numbers=numbers))
        position = end
    for match in _UNICODE_SUPERSCRIPT_RE.finditer(value):
        numbers = _expand_numbers(match.group(1).translate(_SUPERSCRIPT_DIGITS))
        if numbers:
            markers.append(CitationMarker(match.group(0), match.start(), match.end(), "superscript", numbers=numbers))
    if glued_superscripts:
        for match in _GLUED_RE.finditer(value):
            numbers = _expand_numbers(match.group(2))
            if numbers:
                markers.append(
                    CitationMarker(match.group(2), match.start(2), match.end(2), "superscript", numbers=numbers)
                )
    taken: set[tuple[int, str]] = set()
    for match in _PAREN_RE.finditer(value):
        offset = match.start(1)
        for part in re.finditer(r"[^;]+", match.group(1)):
            found = _AUTHOR_YEAR_RE.search(part.group(0))
            if not found or not _is_surname(found.group(1)):
                continue
            start = offset + part.start() + found.start()
            end = offset + part.start() + found.end()
            taken.add((start, found.group(2)))
            markers.append(
                CitationMarker(found.group(0).strip(), start, end, "author_year", surname=found.group(1), year=found.group(2))
            )
    for match in _NARRATIVE_RE.finditer(value):
        if not _is_surname(match.group(1)) or (match.start(), match.group(2)) in taken:
            continue
        markers.append(
            CitationMarker(match.group(0), match.start(), match.end(), "author_year", surname=match.group(1), year=match.group(2))
        )
    markers.sort(key=lambda marker: (marker.start, marker.end))
    return markers


def uses_glued_superscripts(body: str, references: ReferenceList) -> bool:
    """True when flattened superscripts are this document's citation style."""

    if not references.numbered:
        return False
    if len(_BRACKET_RE.findall(body)) >= 3:
        return False
    known = {entry.number for entry in references.entries}
    glued = [
        number
        for match in _GLUED_RE.finditer(body)
        for number in _expand_numbers(match.group(2))
    ]
    matching = [number for number in glued if number in known]
    return len(matching) >= _MIN_GLUED_MARKERS and len(matching) >= 0.6 * len(glued)


def resolve_marker(marker: CitationMarker, references: ReferenceList) -> list[ReferenceEntry]:
    """Reference entries a marker points to; empty when it cannot be resolved."""

    if marker.numbers:
        if not references.numbered:
            return []
        resolved = [references.entry(number) for number in marker.numbers]
        # A partly unknown group ("[3, 250]") is a misread, not a citation.
        if any(entry is None for entry in resolved):
            return []
        return [entry for entry in resolved if entry is not None]
    if not marker.surname or not marker.year:
        return []
    surname = normalize_text(marker.surname)
    first_author: list[ReferenceEntry] = []
    co_author: list[ReferenceEntry] = []
    for entry in references.entries:
        entry_text = normalize_text(entry.text)
        if marker.year not in entry_text:
            continue
        if surname not in entry_text:
            continue
        # "Smith, J., ..." and "[3] J. Smith, ...": the first author ends at the first comma.
        (first_author if surname in entry_text.split(",", 1)[0] else co_author).append(entry)
    candidates = first_author or co_author
    if not candidates:
        found = _raw_author_year_entry(marker.surname, marker.year, references.text)
        return [found] if found else []
    # Several entries (Smith 1972a/1972b) stay ambiguous: keep at most two.
    return candidates[:2]


def _raw_author_year_entry(surname: str, year: str, refs: str) -> ReferenceEntry | None:
    """Entry cut around a surname followed by its year, when entries did not split."""

    # Some styles print surnames in capitals: "OKONTA R. and PELL S."
    for match in re.finditer(rf"\b{re.escape(surname)}\b", refs, re.IGNORECASE):
        line_start = refs.rfind("\n", 0, match.start()) + 1
        window = refs[line_start:match.end() + 400]
        if year in window[: (match.end() - line_start) + 250]:
            end = window.find("\n\n")
            return ReferenceEntry(text=_clean_entry(window[: end if end > 0 else len(window)]))
    return None
