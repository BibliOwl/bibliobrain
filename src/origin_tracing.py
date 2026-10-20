"""Does a cited paper attribute an idea to an earlier source? (one hop, local).

Given a draft claim and the library paper cited for it, find the passage that
supports the claim in the paper's own text, read the citation markers of that
passage, map them to the paper's reference list, and ask the model which
citations the passage puts on the claim itself.

Everything runs on the local library: no network. The model only answers a
closed question per citation group on the passage it is shown; it never names
a source of its own. See docs/3-ARCHITECTURE/origin-tracing-plan.md.

Measured on 26 hand-labelled claims / 63 citations from the user's library
(2026-10-19, prompt "variant C"): qwen3.5:4b 92 % accuracy, ornith-1.5:9b
86 %, granite4.2:3b 65 %, qwen3.5:2b 71 %. Models under 4B are flagged as a
weak indication.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from src import config as _cfg
from src.citation_markers import (
    CitationMarker,
    ReferenceEntry,
    ReferenceList,
    extract_markers,
    normalize_text,
    parse_reference_list,
    resolve_marker,
    uses_glued_superscripts,
)

logger = logging.getLogger(__name__)

FOR_THIS_IDEA = "for_this_idea"
FOR_ANOTHER_POINT = "for_another_point"
NOT_CHECKED = "not_checked"

# Verdicts, from the most to the least informative.
VERDICT_ATTRIBUTES_ELSEWHERE = "attributes_elsewhere"
VERDICT_NO_ATTRIBUTION = "no_attribution"
VERDICT_UNCHECKED = "unchecked"
VERDICT_UNDETERMINED = "undetermined"

_MAX_CLAIM_CHARS = 600
_MAX_PASSAGE_CHARS = 1600
_MAX_GROUPS = 8
_CONTEXT_SENTENCES = 1
_WEAK_MODEL_SIZE_B = 4.0
_LIBRARY_CACHE_SECONDS = 300.0
_MIN_TITLE_CHARS = 25
_RETRIEVED_CHUNKS = 3
# Without a validated quote, the best passages are tried in order and the
# first one that cites something wins: an abstract states the idea with no
# citation, the introduction states it again with its sources.
_PASSAGE_CANDIDATES = 5
# Reranked sentences: the first two only; a third is already much further
# from the claim (measured on "critical state model": a generic intro won).
_RETRIEVAL_CANDIDATES = 2
_MIN_SHARED_WORDS = 0.4
_CLOSE_SHARED_WORDS = 0.15
_STEM_CHARS = 4
_MIN_RETRIEVED_SENTENCE = 40
_MAX_RETRIEVED_SENTENCE = 800

_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\u00C0-\u00DE(\[])")
_WORD_RE = re.compile(r"[^\W\d_]{3,}")
_GROUP_CONNECTORS_RE = re.compile(r"\b(?:and|or|et|ou|see|also|voir|aussi)\b", re.IGNORECASE)
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
_SELF_CLAIM_RE = re.compile(
    r"\b(?:we\s+(?:propose|introduce|present|report|demonstrate)|here\s+we\s+(?:show|report)|"
    r"for\s+the\s+first\s+time|this\s+(?:paper|work|letter)\s+(?:proposes|introduces|presents)|"
    r"nous\s+(?:proposons|introduisons|présentons)|pour\s+la\s+première\s+fois)\b",
    re.IGNORECASE,
)

_LIGATURES = {
    0xFB00: "ff",
    0xFB01: "fi",
    0xFB02: "fl",
    0xFB03: "ffi",
    0xFB04: "ffl",
    0xFB05: "st",
    0xFB06: "st",
}

_FRONT_MATTER_RE = re.compile(
    r"https?://|doi\.org|©|\bkeywords?\b|\bmots[- ]clés\b|all rights reserved|table of contents",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "You check citations in scientific articles. You answer only from the passage given. "
    "Return the requested structured answer only."
)


class OriginTraceError(Exception):
    """A request that cannot be traced, with a code the API maps to a message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _Located:
    start: int
    end: int
    quote_found: bool


@dataclass
class _Group:
    markers: list[CitationMarker]
    references: list[ReferenceEntry]


# ---------------------------------------------------------------------------
# Passage location
# ---------------------------------------------------------------------------


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    """Lowercase text with collapsed whitespace and an index back to ``text``."""

    chars: list[str] = []
    positions: list[int] = []
    previous_space = True
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        # PDF line-break hyphenation: "super-\nconducting" reads "superconducting".
        if char == "-" and index + 1 < length and text[index + 1] in "\r\n" and chars and chars[-1].isalpha():
            index += 1
            while index < length and text[index].isspace():
                index += 1
            continue
        if char == "\u00ad":
            index += 1
            continue
        expanded = _LIGATURES.get(ord(char))
        if expanded:
            # "\ufb02ux" reads "flux"; both letters point back to the ligature.
            for letter in expanded:
                chars.append(letter)
                positions.append(index)
            previous_space = False
            index += 1
            continue
        if char.isspace():
            if not previous_space:
                chars.append(" ")
                positions.append(index)
            previous_space = True
        else:
            chars.append(char.lower())
            positions.append(index)
            previous_space = False
        index += 1
    return "".join(chars), positions


def _locate_quote(body: str, quote: str) -> _Located | None:
    """Span of ``quote`` in ``body``, tolerant to PDF spacing and hyphenation."""

    from src.unicode_utils import repair_pdf_text_artifacts

    needle = normalize_text(repair_pdf_text_artifacts(quote))
    if len(needle) < 20:
        return None
    # Offsets must point into ``body``, so its ligatures are expanded in the
    # map; the quote comes from repaired index text.
    haystack, positions = _normalized_with_map(body)
    probes = [needle, needle[:120], needle[-120:]]
    words = needle.split()
    probes += [" ".join(words[i:i + 8]) for i in range(0, max(1, len(words) - 7), 4)]
    for probe in probes:
        if len(probe) < 20:
            continue
        found = haystack.find(probe)
        if found < 0:
            continue
        if probe is needle:
            end = found + len(needle) - 1
        else:
            end = min(len(haystack) - 1, found + len(probe) - 1)
        return _Located(positions[found], positions[end] + 1, True)
    return None


def _stems(text: str) -> set[str]:
    """Crude stems: "trapped field" and "trap fields" share "trap" and "fiel"."""

    return {word.lower()[:_STEM_CHARS] for word in _WORD_RE.findall(text) if len(word) >= _STEM_CHARS}


def _best_sentences_for_claim(body: str, claim: str) -> list[_Located]:
    """Fallback without a quote: the sentences sharing the most words with the claim, best first."""

    wanted = _stems(claim)
    if not wanted:
        return []
    scored: list[tuple[float, int, int]] = []
    start = 0
    for match in list(_SENTENCE_END_RE.finditer(body)) + [None]:
        end = match.start() if match else len(body)
        words = _stems(body[start:end].translate(_LIGATURES))
        # Running headers, keywords and copyright lines repeat the title words.
        if words and not _FRONT_MATTER_RE.search(body[start:end]):
            score = len(wanted & words) / len(wanted)
            if score >= _MIN_SHARED_WORDS:
                scored.append((score, start, end))
        start = match.end() if match else end
    scored.sort(key=lambda item: (-item[0], item[1]))
    # Only sentences nearly as close as the best one compete: a far weaker
    # match that happens to cite something is a generic introduction.
    best = scored[0][0] if scored else 0.0
    close = [item for item in scored if item[0] >= best - _CLOSE_SHARED_WORDS]
    return [_Located(start, end, False) for _score, start, end in close[:_PASSAGE_CANDIDATES]]


def _retrieved_quotes(claim: str, rel_path: str, model_name: str | None) -> list[str]:
    """Sentences of ``rel_path`` closest to the claim, best first: RAG search in that article, then reranker.

    Returns [] when the index is not available; the caller then falls back
    to shared words.
    """

    from src.drafting_citations import ensure_drafting_rag

    try:
        if ensure_drafting_rag(model_name):
            return []
        from src.rag.query_engine import _prepare_explicit_filters, retrieve_for_question
        from src.rag.reranker import rerank_texts

        documents = retrieve_for_question(
            claim,
            None,
            0.0,
            _RETRIEVED_CHUNKS,
            _prepare_explicit_filters(source_paths=[rel_path]),
        )
        sentences = [
            sentence.strip()
            for document in documents
            for sentence in _SENTENCE_END_RE.split(re.sub(r"\s+", " ", str(document.page_content or "")))
            if _MIN_RETRIEVED_SENTENCE <= len(sentence.strip()) <= _MAX_RETRIEVED_SENTENCE
        ]
        if not sentences:
            return []
        ranked = rerank_texts(claim, sentences, top_k=_RETRIEVAL_CANDIDATES)
    except Exception:  # noqa: BLE001 - the shared-words fallback still works
        logger.debug("Retrieval inside %s failed for origin tracing.", rel_path, exc_info=True)
        return []
    if ranked and any(score != 0.0 for _index, score in ranked):
        return [sentences[index] for index, _score in ranked]
    # Reranker disabled: keep the sentences sharing the most words with the claim.
    wanted = _stems(claim)
    return sorted(sentences, key=lambda sentence: -len(wanted & _stems(sentence)))[:_RETRIEVAL_CANDIDATES]


def _expand_to_sentences(body: str, located: _Located) -> tuple[int, int]:
    """Widen a span to whole sentences, plus one sentence on each side.

    The marker often sits just outside the quote the validator kept.
    """

    bounds = [0] + [match.end() for match in _SENTENCE_END_RE.finditer(body)] + [len(body)]
    first = max(index for index, value in enumerate(bounds) if value <= located.start)
    last = min(index for index, value in enumerate(bounds) if value >= located.end)
    first = max(0, first - _CONTEXT_SENTENCES)
    last = min(len(bounds) - 1, last + _CONTEXT_SENTENCES)
    start, end = bounds[first], bounds[last]
    if end - start > _MAX_PASSAGE_CHARS:
        # Keep the located span and trim the context around it evenly.
        spare = max(0, _MAX_PASSAGE_CHARS - (located.end - located.start)) // 2
        start = max(start, located.start - spare)
        end = min(end, located.end + spare)
    return start, end


# ---------------------------------------------------------------------------
# Citation groups
# ---------------------------------------------------------------------------


def _group_markers(passage: str, markers: list[CitationMarker], references: ReferenceList) -> list[_Group]:
    """Adjacent markers ("[1], [2] and [3]") cite one statement: one group."""

    groups: list[_Group] = []
    previous: CitationMarker | None = None
    for marker in markers:
        resolved = resolve_marker(marker, references)
        gap = _GROUP_CONNECTORS_RE.sub(" ", passage[previous.end:marker.start]) if previous else "x"
        if groups and previous is not None and not _WORD_RE.search(gap):
            groups[-1].markers.append(marker)
            groups[-1].references.extend(resolved)
        else:
            groups.append(_Group(markers=[marker], references=list(resolved)))
        previous = marker
    return groups


def _annotate_passage(passage: str, groups: list[_Group]) -> str:
    """Tag each group after its last marker: "... [3] <c1> ..."."""

    pieces: list[str] = []
    last = 0
    for index, group in enumerate(groups, 1):
        end = group.markers[-1].end
        pieces.append(passage[last:end])
        pieces.append(f" <c{index}>")
        last = end
    pieces.append(passage[last:])
    return re.sub(r"\s+", " ", "".join(pieces)).strip()


def build_attribution_prompt(claim: str, annotated_passage: str, group_labels: list[str]) -> str:
    lines = [
        "IDEA (from the user's draft):",
        claim,
        "",
        "PASSAGE (from a cited article; citation markers are tagged <c1>, <c2>...):",
        annotated_passage,
        "",
        "For each tagged citation, answer:",
        f'- "{FOR_THIS_IDEA}" if the passage puts this citation on a statement of the IDEA '
        "(same idea or same result, even reworded);",
        f'- "{FOR_ANOTHER_POINT}" if the citation supports something else in the passage '
        "(another result, another application, another method, a review for further reading).",
        "Judge each citation by the words it is attached to, not by the whole sentence.",
        "",
        "Citations:",
    ]
    lines += [f"- c{index}: {label}" for index, label in enumerate(group_labels, 1)]
    return "\n".join(lines)


def _answer_model(count: int) -> type[BaseModel]:
    """One required field per citation group: Ollama's schema mode answers each."""

    fields: dict[str, Any] = {
        f"c{index}": (Literal["for_this_idea", "for_another_point"], Field(...))
        for index in range(1, count + 1)
    }
    return create_model("OriginAttributionAnswer", **fields)


def _ask_model(model_name: str, prompt: str, count: int) -> dict[str, str]:
    from src.llm_provider import invoke_structured_chat

    result = invoke_structured_chat(
        model_name,
        [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        response_model=_answer_model(count),
        temperature=0.0,
        top_p=0.2,
        context_tokens_hint=len(prompt) // 3 + 512,
    )
    return {key: str(value) for key, value in result.model_dump().items()}


def model_reliability(model_name: str | None) -> str:
    """"low" for models measured as near chance on this task (under 4B)."""

    from src.model_profiles import estimate_model_size_b

    size = estimate_model_size_b(model_name)
    if size is not None and size < _WEAK_MODEL_SIZE_B:
        return "low"
    return "normal"


# ---------------------------------------------------------------------------
# Library matching
# ---------------------------------------------------------------------------

_library_cache: tuple[float, list[dict[str, Any]]] | None = None


def _normalize_title(value: str) -> str:
    text = normalize_text(value)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _library_index() -> list[dict[str, Any]]:
    """One record per indexed library article, cached a few minutes."""

    global _library_cache
    now = time.monotonic()
    if _library_cache and now - _library_cache[0] < _LIBRARY_CACHE_SECONDS:
        return _library_cache[1]
    from src.drafting_interop import citation_record_from_item
    from src.library import _lookup_zotero, list_library

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        # Not only indexed files: a scan without text layer still opens in the reader.
        _rows, rel_paths = list_library(indexed_only=False)
    except Exception:
        logger.debug("Library listing failed for origin tracing.", exc_info=True)
        rel_paths = []
    for rel_path in rel_paths:
        item = _lookup_zotero(rel_path)
        if item is None:
            continue
        identity = str(getattr(item, "item_key", "") or rel_path).lower()
        if identity in seen:
            continue
        seen.add(identity)
        record = citation_record_from_item(item)
        record["rel_path"] = rel_path
        record["_title_key"] = _normalize_title(item.title or "")
        record["_doi_key"] = str(item.doi or "").strip().lower()
        record["_surname_key"] = _surname_key(item.first_author() or "")
        record["_year_key"] = str(item.year or "").strip()[:4]
        records.append(record)
    _library_cache = (now, records)
    return records


def library_record(rel_path: str) -> dict[str, Any] | None:
    """Citation record of a library article (label, title, year...), or None."""

    wanted = str(rel_path or "").replace("\\", "/").lower()
    for record in _library_index():
        if str(record.get("rel_path") or "").replace("\\", "/").lower() == wanted:
            return _public_record(record)
    return None


def clear_library_cache() -> None:
    global _library_cache
    _library_cache = None


def _surname_key(author: str) -> str:
    """Family name of a Zotero author string ("Tomita, M." or "M. Tomita")."""

    value = str(author or "").strip()
    if "," in value:
        value = value.split(",", 1)[0]
    else:
        value = value.split()[-1] if value.split() else ""
    return normalize_text(value)


def _match_library(entry: ReferenceEntry, library: list[dict[str, Any]], exclude_path: str) -> dict[str, Any] | None:
    """Library record for a reference entry: DOI, then title, then a unique first author + year."""

    candidates = [record for record in library if record.get("rel_path") != exclude_path]
    entry_dois = {match.rstrip(".,;)").lower() for match in _DOI_RE.findall(entry.text)}
    for record in candidates:
        if record["_doi_key"] and record["_doi_key"] in entry_dois:
            return record
    entry_key = _normalize_title(entry.text)
    for record in candidates:
        title = record["_title_key"]
        if len(title) >= _MIN_TITLE_CHARS and title in entry_key:
            return record
    # Physics styles drop the title: "Tomita M and Murakami M 2003 Nature 421 517".
    head = normalize_text(entry.text)
    first_author = head.split(",", 1)[0] if "," in head[:60] else head[:40]
    years = set(re.findall(r"\b(?:1[89]|20)\d{2}\b", head))
    matches = [
        record
        for record in candidates
        if len(record["_surname_key"]) >= 3
        and record["_year_key"] in years
        and re.search(rf"\b{re.escape(record['_surname_key'])}\b", first_author)
    ]
    identities = {record.get("citation_id") for record in matches}
    return matches[0] if len(identities) == 1 else None


def _public_record(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {key: value for key, value in record.items() if not key.startswith("_")}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _load_document_text(rel_path: str) -> tuple[str, list[tuple[int, str]]]:
    """Full text of a library document and the offset where each page starts."""

    from src.document_formats import load_document_units
    from src.file_access import resolve_document_path

    full_path = resolve_document_path(_cfg.DATA_PATH, rel_path)
    if full_path is None or not full_path.exists():
        raise OriginTraceError("document_not_found", "Article introuvable dans la bibliothèque.")
    units = load_document_units(full_path)
    pieces: list[str] = []
    pages: list[tuple[int, str]] = []
    offset = 0
    for unit in units:
        text = str(unit.text or "")
        pages.append((offset, str(unit.location_label or unit.page)))
        pieces.append(text)
        offset += len(text) + 1
    return "\n".join(pieces), pages


def _page_label(pages: list[tuple[int, str]], position: int) -> str:
    label = ""
    for start, value in pages:
        if start > position:
            break
        label = value
    return label


def trace_origin(
    claim: str,
    rel_path: str,
    *,
    evidence_quote: str = "",
    model_name: str | None = None,
) -> dict[str, Any]:
    """Which sources the cited paper credits for ``claim``, in the passage that supports it."""

    claim = re.sub(r"\s+", " ", str(claim or "")).strip()[:_MAX_CLAIM_CHARS]
    rel_path = str(rel_path or "").strip()
    if not claim or not rel_path:
        raise OriginTraceError("invalid_request", "Phrase ou article manquant.")

    text, pages = _load_document_text(rel_path)
    references = parse_reference_list(text)
    body = text[: references.start] if references.start >= 0 else text
    payload: dict[str, Any] = {
        "status": "ok",
        "claim": claim,
        "rel_path": rel_path,
        "reference_list": {
            "found_by": references.found_by,
            "style": references.style,
            "entries": len(references.entries),
        },
        "passage": None,
        "citations": [],
        "self_claim": False,
        "model": model_name or "",
        "model_reliability": model_reliability(model_name) if model_name else "",
        "notes": [],
    }

    candidates: list[tuple[_Located, str]] = []
    if evidence_quote:
        located = _locate_quote(body, evidence_quote)
        if located is not None:
            candidates.append((located, "quote"))
    else:
        # A source reached by following the chain has no validated quote yet.
        for quote in _retrieved_quotes(claim, rel_path, model_name):
            located = _locate_quote(body, quote)
            if located is not None:
                candidates.append((located, "retrieval"))
    if not candidates:
        candidates = [(located, "words") for located in _best_sentences_for_claim(body, claim)]
    if not candidates:
        payload["verdict"] = VERDICT_UNDETERMINED
        payload["notes"].append("passage_not_found")
        return payload

    glued = uses_glued_superscripts(body, references)
    spans = [(candidate, how, *_expand_to_sentences(body, candidate)) for candidate, how in candidates]
    # First a passage that cites something, then one where the article claims
    # the idea as its own ("we report..."), else the best-ranked one.
    chosen = next(
        (span for span in spans if extract_markers(body[span[2]:span[3]], glued_superscripts=glued)),
        None,
    ) or next(
        (span for span in spans if _SELF_CLAIM_RE.search(body[span[0].start:span[0].end])),
        spans[0],
    )
    located, located_by, start, end = chosen
    passage = body[start:end]
    payload["passage"] = {
        "text": re.sub(r"\s+", " ", passage).strip(),
        "page": _page_label(pages, located.start),
        "quote_found": located.quote_found,
        "located_by": located_by,
    }
    payload["self_claim"] = bool(_SELF_CLAIM_RE.search(passage))

    markers = extract_markers(passage, glued_superscripts=glued)
    if not markers:
        payload["verdict"] = VERDICT_NO_ATTRIBUTION
        payload["notes"].append("no_marker_in_passage")
        return payload
    if not references.entries:
        payload["verdict"] = VERDICT_UNDETERMINED
        payload["notes"].append("reference_list_not_found")
        payload["citations"] = [
            {"markers": [marker.text], "references": [], "attribution": NOT_CHECKED} for marker in markers
        ]
        return payload

    groups = _group_markers(passage, markers, references)[:_MAX_GROUPS]
    resolved_groups = [group for group in groups if group.references]
    attributions: dict[int, str] = {}
    if resolved_groups and model_name:
        prompt = build_attribution_prompt(
            claim,
            _annotate_passage(passage, resolved_groups),
            [", ".join(marker.text for marker in group.markers) for group in resolved_groups],
        )
        try:
            answers = _ask_model(model_name, prompt, len(resolved_groups))
            attributions = {
                index: answers.get(f"c{index + 1}", NOT_CHECKED) for index in range(len(resolved_groups))
            }
        except Exception as exc:  # noqa: BLE001 - the citations stay useful without a verdict
            logger.warning("Origin attribution check failed: %s", exc)
            payload["notes"].append("model_failed")

    library = _library_index()
    citations: list[dict[str, Any]] = []
    for group in groups:
        index = resolved_groups.index(group) if group in resolved_groups else -1
        references_out = []
        for entry in group.references:
            references_out.append({
                "number": entry.number,
                "text": entry.text,
                "library": _public_record(_match_library(entry, library, rel_path)),
            })
        citations.append({
            "markers": [marker.text for marker in group.markers],
            "references": references_out,
            "attribution": attributions.get(index, NOT_CHECKED) if index >= 0 else NOT_CHECKED,
        })
    # Credited sources first, then the other resolved ones, unresolved last.
    order = {FOR_THIS_IDEA: 0, NOT_CHECKED: 1, FOR_ANOTHER_POINT: 2}
    citations.sort(key=lambda item: (not item["references"], order.get(item["attribution"], 1)))
    payload["citations"] = citations

    if not resolved_groups:
        payload["verdict"] = VERDICT_UNDETERMINED
        payload["notes"].append("markers_not_resolved")
    elif not attributions:
        payload["verdict"] = VERDICT_UNCHECKED
    elif any(value == FOR_THIS_IDEA for value in attributions.values()):
        payload["verdict"] = VERDICT_ATTRIBUTES_ELSEWHERE
    else:
        payload["verdict"] = VERDICT_NO_ATTRIBUTION
    return payload


# ---------------------------------------------------------------------------
# Sources outside the library (network, only when the user allows it)
# ---------------------------------------------------------------------------

_MAX_REFERENCE_CHARS = 600
_CROSSREF_CANDIDATES = 3


def _identified_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
    from src.pdf_metadata import _crossref_item

    item = _crossref_item(raw)
    if item is None:
        return None
    doi = str(item.doi or "").strip()
    try:
        cited_by = int(raw.get("is-referenced-by-count") or 0)
    except (TypeError, ValueError):
        cited_by = 0
    return {
        "title": item.title,
        "authors_display": item.authors_str(),
        "first_author": item.first_author(),
        "year": item.year,
        "journal": item.journal,
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else "",
        "cited_by_count": cited_by,
        "_volume": str(raw.get("volume") or "").strip(),
        "_first_page": str(raw.get("page") or "").split("-")[0].strip(),
    }


def _plausible_match(candidate: dict[str, Any], reference_text: str) -> bool:
    """A bibliographic search hit must be the same work, not a namesake of the same year.

    Year and first author must match, then either most of the title words
    or, for the title-less physics styles, the volume plus the first page or
    a journal word.
    """

    text = normalize_text(reference_text)
    surname = _surname_key(candidate.get("first_author") or "")
    year = str(candidate.get("year") or "")
    if not year or year not in text or len(surname) < 2:
        return False
    if re.search(rf"\b{re.escape(surname)}\b", text) is None:
        return False
    words = set(_WORD_RE.findall(text))
    title_words = {word for word in _WORD_RE.findall(normalize_text(candidate.get("title") or "")) if len(word) >= 4}
    if title_words and len(title_words & words) >= 0.6 * len(title_words):
        return True
    numbers = set(re.findall(r"\d+", text))
    if not candidate.get("_volume") or candidate["_volume"] not in numbers:
        return False
    journal_words = {word for word in _WORD_RE.findall(normalize_text(candidate.get("journal") or "")) if len(word) >= 4}
    first_page = candidate.get("_first_page") or ""
    return (bool(first_page) and first_page in numbers) or any(
        word[:4] in text for word in journal_words
    )


def identify_reference(reference_text: str) -> dict[str, Any] | None:
    """Crossref record for a raw reference entry, or None when nothing matches safely.

    Sends only the reference entry (authors, title, journal of a published
    work), never the user's draft. Raises :class:`OriginTraceError` when
    Crossref cannot be reached.
    """

    from urllib.parse import quote

    from src.pdf_metadata import _crossref_get_json

    text = re.sub(r"\s+", " ", str(reference_text or "")).strip()[:_MAX_REFERENCE_CHARS]
    if len(text) < 12:
        raise OriginTraceError("invalid_request", "Référence trop courte.")
    dois = [match.rstrip(".,;)") for match in _DOI_RE.findall(text)]
    if dois:
        payload = _crossref_get_json(f"/works/{quote(dois[0], safe='')}")
        rows = [payload.get("message")] if isinstance(payload, dict) else None
    else:
        # "[12] A. Author, ..." searches better without its list number.
        query = re.sub(r"^\s*\[?\d{1,3}\]?\.?\s*", "", text)
        payload = _crossref_get_json(
            "/works", params={"query.bibliographic": query, "rows": _CROSSREF_CANDIDATES}
        )
        message = payload.get("message") if isinstance(payload, dict) else None
        rows = message.get("items") if isinstance(message, dict) else None
    if payload is None:
        raise OriginTraceError("lookup_failed", "Crossref ne répond pas pour le moment.")
    for raw in rows or []:
        if not isinstance(raw, dict):
            continue
        candidate = _identified_payload(raw)
        if candidate and (dois or _plausible_match(candidate, text)):
            return {key: value for key, value in candidate.items() if key != "first_author" and not key.startswith("_")}
    return None
