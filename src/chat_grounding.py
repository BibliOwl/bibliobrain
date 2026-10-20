"""Deterministic check of the figures of a chat answer against its passages.

Small local models add a value they computed or remembered far more often
than they break the output format. Nothing here calls a model: each figure of
the answer is looked up in the passages, and the ones found nowhere are
listed. Citations are left as the model wrote them, apart from the labels it
piles up after its last sentence.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation

_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
# Space, no-break space and narrow no-break space group thousands in French.
_THOUSANDS_SPACES = " " + chr(0xA0) + chr(0x202F)
# A number glued to a preceding letter is a name ("NMC811", "S1", "B0").
_NUMBER_RE = re.compile(
    rf"(?<![A-Za-zÀ-ÿ\d_.,])\d+(?:[{_THOUSANDS_SPACES}]\d{{3}}(?!\d))*(?:[.,]\d+)*"
)
_SOURCE_GROUP_RE = re.compile(r"\[\s*S\d+(?:\s*,\s*S\d+)*\s*\]", flags=re.IGNORECASE)
_BRACKET_GROUP_RE = re.compile(r"\[[^\]\n]*\]")
_LIST_MARKER_RE = re.compile(r"(?m)^\s*\d{1,2}[.)]\s")
# "p. 12", "pp. 12-14", "page 12": where a passage is, not what it reports.
_PAGE_REFERENCE_RE = re.compile(
    r"\b(?:pp?\.\s*|pages?\s+)\d+(?:\s*[-–]\s*\d+)?", flags=re.IGNORECASE
)


def number_forms(token: str) -> frozenset[str]:
    """Canonical spellings a written number may stand for.

    "6,204" is six thousand in English and six point two in French, so both
    readings are kept; a figure is grounded when one of its readings is.
    """
    value = re.sub(rf"[{_THOUSANDS_SPACES}]", "", str(token or "")).strip(".,")
    if not value:
        return frozenset()
    forms: set[str] = set()
    grouped = re.fullmatch(r"[1-9]\d{0,2}(?:([.,])\d{3})+", value)
    if grouped and len(set(re.findall(r"[.,]", value))) == 1:
        forms.add(_canonical_decimal(re.sub(r"[.,]", "", value)))
    separators = re.findall(r"[.,]", value)
    if len(separators) <= 1:
        forms.add(_canonical_decimal(value.replace(",", ".")))
    elif not forms:
        forms.add(value)
    return frozenset(form for form in forms if form)


def _canonical_decimal(value: str) -> str:
    integer, _, fraction = value.partition(".")
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    return f"{integer}.{fraction}" if fraction else integer


def _is_checked(token: str) -> bool:
    """Single digits are counts and list positions, not reported values."""
    return bool(re.search(r"[.,]\d", token)) or len(re.sub(r"\D", "", token)) >= 2


@dataclass(frozen=True)
class _Number:
    raw: str
    forms: frozenset[str]


def _numbers(text: str, *, checked_only: bool) -> list[_Number]:
    value = str(text or "").translate(_SUPERSCRIPTS)
    found: list[_Number] = []
    for match in _NUMBER_RE.finditer(value):
        raw = match.group(0).strip(".,")
        if checked_only and not _is_checked(raw):
            continue
        forms = number_forms(raw)
        if forms:
            found.append(_Number(raw=raw, forms=forms))
    return found


class _NumberPool:
    """The figures written in a text, with the roundings they allow."""

    def __init__(self, texts: Iterable[str]) -> None:
        self.forms: set[str] = set()
        for text in texts:
            for number in _numbers(text, checked_only=False):
                self.forms |= number.forms
                # A table row extracted from a PDF reads "31 178 44": three
                # cells, not thirty-one thousand. Keep each cell as well.
                for cell in re.split(rf"[{_THOUSANDS_SPACES}]", number.raw)[1:]:
                    self.forms |= number_forms(cell)
                self.forms |= number_forms(re.split(rf"[{_THOUSANDS_SPACES}]", number.raw)[0])
        self._decimals: list[Decimal] = []
        for form in self.forms:
            if "." in form:
                try:
                    self._decimals.append(Decimal(form))
                except InvalidOperation:
                    continue

    def holds(self, number: _Number) -> bool:
        if number.forms & self.forms:
            return True
        # "91 %" for a passage that says 91.3 % is a rounding, not an invention.
        for form in number.forms:
            try:
                value = Decimal(form)
            except InvalidOperation:
                continue
            exponent = value.as_tuple().exponent
            quantum = Decimal(1).scaleb(exponent if isinstance(exponent, int) else 0)
            for candidate in self._decimals:
                for rounding in (ROUND_HALF_UP, ROUND_HALF_EVEN):
                    if candidate.quantize(quantum, rounding=rounding) == value:
                        return True
        return False


def _mask(text: str, pattern: re.Pattern[str]) -> str:
    """Blank what the pattern matches, keeping every offset."""
    return pattern.sub(lambda match: " " * len(match.group(0)), text)


def _answer_numbers(text: str) -> list[_Number]:
    masked = text
    for pattern in (_BRACKET_GROUP_RE, _LIST_MARKER_RE, _PAGE_REFERENCE_RE):
        masked = _mask(masked, pattern)
    return _numbers(masked, checked_only=True)


@dataclass(frozen=True)
class GroundingReport:
    text: str
    ungrounded_values: list[str] = field(default_factory=list)


_DANGLING_TAIL_RE = re.compile(
    r"(?:(?<=[.!?…])|\n)[ \t]*((?:\[\s*S\d+(?:\s*,\s*S\d+)*\s*\][\s,;.]*(?:(?:et|and)\s+)?)+)\s*\Z",
    flags=re.IGNORECASE,
)


def strip_dangling_citations(text: str) -> str:
    """Drop the labels a small model piles up after its last sentence.

    "…sans extrapolation. [S4] [S6]" cites nothing: the labels follow the
    full stop and would only add unrelated passages to the listed sources.
    A single label after the full stop is the citation of that sentence, and
    an answer whose only citations sit there keeps them.
    """
    match = _DANGLING_TAIL_RE.search(text)
    if not match:
        return text
    tail = match.group(1)
    own_line = text[: match.start(1)].endswith("\n")
    if len(_SOURCE_GROUP_RE.findall(tail)) < 2 and not own_line:
        return text
    kept = text[: match.start(1)].rstrip()
    return kept if _SOURCE_GROUP_RE.search(kept) else text


def check_answer_grounding(
    answer: str | None,
    passages: Sequence[str],
    *,
    question: str = "",
    extra_texts: Sequence[str] = (),
) -> GroundingReport:
    """Drop the labels piled after the answer and list the figures found nowhere.

    ``passages`` are the texts the model answered from, each with the title,
    authors and year shown above it. ``extra_texts`` are other places a figure
    may legitimately come from, such as the conversation.
    """
    text = str(answer or "")
    shown = [str(passage or "") for passage in passages]
    if not text.strip() or not any(passage.strip() for passage in shown):
        return GroundingReport(text=text)
    text = strip_dangling_citations(text)

    known = _NumberPool([*shown, question, *extra_texts])
    ungrounded: list[str] = []
    for number in _answer_numbers(text):
        if not known.holds(number) and number.raw not in ungrounded:
            ungrounded.append(number.raw)
    return GroundingReport(text=text, ungrounded_values=ungrounded)


def contains_value(text: str | None, value: str) -> bool:
    """Whether ``text`` states the figure ``value``, whatever its spelling."""
    wanted = number_forms(value)
    return any(number.forms & wanted for number in _numbers(str(text or ""), checked_only=False))


__all__ = [
    "GroundingReport",
    "check_answer_grounding",
    "contains_value",
    "number_forms",
    "strip_dangling_citations",
]
