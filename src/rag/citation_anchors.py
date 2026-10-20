"""Sentence-level citation anchors for RAG answers.

Given an answer text peppered with citations like ``[S1]``, ``[S2,S3]``,
return for each citation label the most likely supporting sentence inside
the cited chunk, plus character offsets so the frontend can highlight it.

Pure stdlib (re, difflib) to avoid a new dependency. ``difflib`` is
slower than ``rapidfuzz`` but accurate enough for typical chunk sizes
(< 4000 chars).

Public API:
    - extract_citation_anchors(answer_text, sources) -> dict[label, list[Anchor]]
    - SENTENCE_ANCHOR_VERSION
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from typing import Any

_logger = logging.getLogger(__name__)

SENTENCE_ANCHOR_VERSION = 1

# Match `[S1]`, `[S1,S2]`, `[1]`, `[1, 3]`, `[A:1]`, `[A:1,B:2]`.
_CITATION_RE = re.compile(r"\[([^\]\n]+)\]")
_LABEL_RE = re.compile(r"^(?:S\d+|\d+|[A-Za-z0-9_\-.]+:\d+)$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9«\"'(])")
_WHITESPACE_RE = re.compile(r"\s+")

_MAX_CHUNK_SCAN = 6000  # cap per-chunk text considered for matching
_MIN_FUZZY_SCORE = 0.45
_MAX_ANCHOR_CHARS = 320


@dataclass
class Anchor:
    chunk_uid: str
    text: str
    score: float
    char_start: int
    char_end: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_label(raw: str) -> str:
    label = raw.strip().upper()
    if not label:
        return ""
    if not _LABEL_RE.match(label):
        # tolerate lowercase 's' or stray spaces, already uppered.
        return ""
    return label


def _expand_label_aliases(label: str) -> set[str]:
    out = {label}
    if label.startswith("S") and label[1:].isdigit():
        out.add(label[1:])
    elif label.isdigit():
        out.add(f"S{int(label)}")
    return out


def _split_sentences(text: str) -> list[tuple[str, int]]:
    """Return list of (sentence, char_offset_in_text)."""
    if not text:
        return []
    out: list[tuple[str, int]] = []
    cursor = 0
    for piece in _SENTENCE_SPLIT_RE.split(text):
        offset = text.find(piece, cursor)
        if offset < 0:
            offset = cursor
        cursor = offset + len(piece)
        cleaned = piece.strip()
        if cleaned:
            out.append((cleaned, offset))
    return out


def _enclosing_sentence(answer_text: str, citation_pos: int) -> str:
    """Return the sentence in ``answer_text`` containing ``citation_pos``.

    Treats common abbreviations ("et al.", "e.g.", "i.e.", "cf.", "vs.",
    "Dr.", "Mr.", initials like "J. F. Doe") as non-terminal so we don't
    over-fragment the sentence.
    """
    if not answer_text:
        return ""
    boundaries: list[int] = []
    for match in re.finditer(r"[.!?\n]", answer_text):
        end = match.end()
        char = match.group(0)
        if char in {"!", "?", "\n"}:
            boundaries.append(end)
            continue
        # '.' boundary — reject if preceded by a known abbreviation,
        # by a single uppercase initial, or followed by a lowercase letter.
        prefix = answer_text[max(0, end - 8):end - 1].lower()
        if prefix.endswith(("et al", "e.g", "i.e", " cf", "vs", " dr", " mr", " ms", " mrs", " st", " no", " fig", " eq")):
            continue
        # single-letter initial like " J."
        if end >= 3 and answer_text[end - 3] == " " and answer_text[end - 2].isalpha() and answer_text[end - 2].isupper():
            continue
        # lowercase or digit follows → likely not a real boundary.
        if end < len(answer_text):
            next_char = answer_text[end]
            if next_char.isspace() and end + 1 < len(answer_text):
                next_char = answer_text[end + 1]
            if next_char.islower():
                continue
        boundaries.append(end)
    start = 0
    for end in boundaries:
        if end > citation_pos:
            return answer_text[start:end].strip()
        start = end
    return answer_text[start:].strip()


def _normalize_for_match(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _best_match_in_chunk(needle: str, chunk_text: str) -> tuple[str, float, int, int]:
    """Find the best matching sentence in chunk_text for ``needle``.

    Strategy:
      1. Strip citations from needle.
      2. Split chunk into sentences.
      3. Score each sentence with SequenceMatcher (normalized text).
      4. Return best (raw_sentence, score, char_start, char_end).
    """
    chunk_text = chunk_text[:_MAX_CHUNK_SCAN]
    needle_clean = _CITATION_RE.sub("", needle)
    needle_norm = _normalize_for_match(needle_clean)
    if not needle_norm or not chunk_text:
        return ("", 0.0, 0, 0)

    sentences = _split_sentences(chunk_text)
    if not sentences:
        return (chunk_text[:_MAX_ANCHOR_CHARS], 0.0, 0, min(_MAX_ANCHOR_CHARS, len(chunk_text)))

    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(needle_norm)
    best = ("", 0.0, 0, 0)
    for sentence, offset in sentences:
        candidate_norm = _normalize_for_match(sentence)
        if not candidate_norm:
            continue
        matcher.set_seq1(candidate_norm)
        try:
            ratio = matcher.ratio()
        except Exception:
            _logger.debug("Recovered from an error in _best_match_in_chunk.", exc_info=True)
            ratio = 0.0
        if ratio > best[1]:
            sentence_cap = sentence[:_MAX_ANCHOR_CHARS]
            best = (sentence_cap, ratio, offset, offset + len(sentence_cap))
    return best


def _index_sources_by_labels(sources: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_label: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(sources or []):
        if not isinstance(source, dict):
            continue
        labels: set[str] = set()
        ref_label = str(source.get("ref_label", "")).strip().upper()
        if ref_label:
            labels.add(ref_label)
        numeric_index = source.get("index")
        try:
            numeric_int = int(numeric_index) if numeric_index is not None else (index + 1)
        except (TypeError, ValueError):
            numeric_int = index + 1
        if numeric_int >= 1:
            labels.add(str(numeric_int))
            labels.add(f"S{numeric_int}")
        for label in labels:
            by_label.setdefault(label, source)
    return by_label


def _resolve_source_for_label(
    label: str, by_label: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    for alias in _expand_label_aliases(label):
        candidate = by_label.get(alias)
        if candidate is not None:
            return candidate
    return None


def _source_chunk_text(source: dict[str, Any]) -> str:
    for key in ("content", "page_content", "snippet"):
        text = str(source.get(key, "") or "")
        if text:
            return text
    return ""


def extract_citation_anchors(
    answer_text: str,
    sources: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{label: [anchor_dict, ...]}`` for each citation in the answer.

    - ``answer_text``: the LLM response (possibly streamed and concatenated).
    - ``sources``: the list returned by ``format_sources_api`` (with
      ``index``, ``ref_label``, ``snippet`` and optionally ``content``).

    If matching fails for a label (no valid source / low fuzzy score), no
    entry is emitted for that label — the frontend keeps the legacy
    behaviour (highlight the entire chunk).
    """
    if not answer_text or not sources:
        return {}

    by_label = _index_sources_by_labels(sources)
    out: dict[str, list[dict[str, Any]]] = {}
    seen_pairs: set[tuple[str, str]] = set()

    for match in _CITATION_RE.finditer(answer_text):
        raw_block = match.group(1)
        sentence = _enclosing_sentence(answer_text, match.start())
        if not sentence:
            continue
        for token in raw_block.split(","):
            label = _normalize_label(token)
            if not label:
                continue
            source = _resolve_source_for_label(label, by_label)
            if source is None:
                continue
            chunk_text = _source_chunk_text(source)
            if not chunk_text:
                continue
            chunk_uid = str(source.get("chunk_uid", "")).strip() or str(
                source.get("ref_label", label)
            )
            pair_key = (label, chunk_uid)
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            text, score, start, end = _best_match_in_chunk(sentence, chunk_text)
            if score < _MIN_FUZZY_SCORE or not text:
                continue
            anchor = Anchor(
                chunk_uid=chunk_uid,
                text=text,
                score=round(score, 3),
                char_start=start,
                char_end=end,
            )
            out.setdefault(label, []).append(anchor.to_dict())

    return out


__all__ = ["SENTENCE_ANCHOR_VERSION", "Anchor", "extract_citation_anchors"]
