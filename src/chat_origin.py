"""``/origine`` in the chat: which earlier sources the library credits for an idea.

The chat finds the library articles closest to the idea (or uses the
attached ones), runs :func:`src.origin_tracing.trace_origin` on each, then
follows every credited source that is itself in the library, a few steps
deep. The answer is written from those results, not by a model: each
traced article becomes an ``[Sx]`` source whose snippet is the passage read.

Local only. Article reference numbers are written "réf. 12", never "[12]":
the chat reads every bracket as a citation of its own sources.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from src.origin_tracing import (
    FOR_THIS_IDEA,
    VERDICT_ATTRIBUTES_ELSEWHERE,
    VERDICT_NO_ATTRIBUTION,
    VERDICT_UNCHECKED,
    OriginTraceError,
    library_record,
    trace_origin,
)

_logger = logging.getLogger(__name__)

_MAX_START_ARTICLES = 2
_MAX_DEPTH = 3
_MAX_BRANCHES = 2
_MAX_TRACES = 6
_RETRIEVED_CHUNKS = 8
_SNIPPET_CHARS = 320
_ENTRY_CHARS = 110

_TEXTS: dict[str, dict[str, str]] = {
    "empty": {
        "fr": "Indiquez l'idée dont vous cherchez l'origine, par exemple : `/origine le modèle de l'état critique`.",
        "en": "Give the idea whose origin you are looking for, for example: `/origin the critical state model`.",
    },
    "no_index": {
        "fr": "Je ne peux pas chercher dans la bibliothèque : l'index n'est pas disponible. {reason}",
        "en": "I cannot search the library: the index is not available. {reason}",
    },
    "no_article": {
        "fr": "Je n'ai trouvé aucun article de la bibliothèque qui parle de cette idée.",
        "en": "I found no library article about this idea.",
    },
    "status_search": {"fr": "Recherche des articles qui énoncent cette idée…", "en": "Looking for articles stating this idea…"},
    "status_read": {"fr": "Lecture de {label} et de sa bibliographie…", "en": "Reading {label} and its reference list…"},
    "heading": {"fr": "**Origine de l'idée** : *{claim}*", "en": "**Origin of the idea**: *{claim}*"},
    "earliest": {
        "fr": "**Plus ancienne source créditée trouvée : {label}**",
        "en": "**Oldest credited source found: {label}**",
    },
    "chain": {"fr": "Chaîne de citations :", "en": "Citation chain:"},
    "colon": {"fr": " : ", "en": ": "},
    "attributes": {"fr": "attribue l'idée à {refs}", "en": "credits the idea to {refs}"},
    "no_attribution": {
        "fr": "ne cite aucune autre source pour cette idée dans le passage retrouvé",
        "en": "cites no other source for this idea in the passage found",
    },
    "self_claim": {"fr": " et la présente comme son propre apport", "en": " and presents it as its own contribution"},
    "unchecked": {
        "fr": "cite {refs} (non vérifié : aucun modèle n'a pu trier les citations)",
        "en": "cites {refs} (not checked: no model could sort the citations)",
    },
    "passage_not_found": {"fr": "passage sur cette idée introuvable", "en": "no passage about this idea found"},
    "reference_list_not_found": {
        "fr": "bibliographie introuvable dans le PDF",
        "en": "reference list not found in the PDF",
    },
    "markers_not_resolved": {
        "fr": "citations du passage introuvables dans la bibliographie",
        "en": "the passage's citations are not in the reference list",
    },
    "unreadable": {"fr": "article illisible ({message})", "en": "unreadable article ({message})"},
    "outside": {"fr": "hors bibliothèque", "en": "outside the library"},
    "ref": {"fr": "réf. {number}", "en": "ref. {number}"},
    "caveat": {
        "fr": "*Ce n'est pas une preuve d'originalité : une idée peut être plus ancienne que les sources citées. Vérifiez les passages dans les Sources.*",
        "en": "*This does not prove originality: an idea can be older than the sources cited. Check the passages in Sources.*",
    },
    "weak_model": {
        "fr": "*Petit modèle : les attributions sont indicatives.*",
        "en": "*Small model: the attributions are only indicative.*",
    },
    "outside_hint": {
        "fr": "Pour remonter au-delà d'une source hors bibliothèque, ajoutez-la à Zotero, ou identifiez-la depuis la Rédaction (« Source d'origine ? »).",
        "en": "To go beyond a source outside the library, add it to Zotero, or identify it from Writing (\"Original source?\").",
    },
    "limit": {
        "fr": "Recherche arrêtée après {count} articles.",
        "en": "Search stopped after {count} articles.",
    },
}


def _lang() -> str:
    from src.rag.native_chat import _native_lang

    return _native_lang()


def _t(key: str, **kwargs: object) -> str:
    texts = _TEXTS[key]
    template = texts.get(_lang()) or texts["fr"]
    return template.format(**kwargs) if kwargs else template


@dataclass
class _Step:
    rel_path: str
    depth: int
    record: dict[str, Any] | None
    result: dict[str, Any] | None = None
    error: str = ""
    source_label: str = ""
    children: list[int] = field(default_factory=list)


def _article_label(rel_path: str, record: dict[str, Any] | None) -> str:
    if record and record.get("label"):
        return str(record["label"])
    name = re.sub(r"\.[a-z0-9]+$", "", str(rel_path).replace("\\", "/").rsplit("/", 1)[-1], flags=re.I)
    return name.strip()[:80] or rel_path


def _entry_text(reference: dict[str, Any]) -> str:
    text = str(reference.get("text") or "")
    # Brackets would read as chat citations: "[12] A. Author" -> "A. Author".
    text = re.sub(r"^\s*\[?\d{1,3}\]?\.?\s*", "", text)
    text = text.replace("[", "(").replace("]", ")")
    return text[: _ENTRY_CHARS - 1] + "…" if len(text) > _ENTRY_CHARS else text


def _reference_year(reference: dict[str, Any]) -> str:
    library = reference.get("library") or {}
    if library.get("year"):
        return str(library["year"])[:4]
    match = re.search(r"\b(?:1[5-9]|20)\d{2}\b", str(reference.get("text") or ""))
    return match.group(0) if match else ""


def _starting_articles(
    claim: str,
    model_name: str | None,
    threshold: float,
    *,
    tags: Any,
    collections: Any,
) -> tuple[list[str], str]:
    """Library articles closest to the claim, or ([], reason) without an index."""

    from src.drafting_citations import ensure_drafting_rag
    from src.rag.query_engine import _prepare_explicit_filters, _safe_relpath, retrieve_for_question

    reason = ensure_drafting_rag(model_name)
    if reason:
        return [], reason
    documents = retrieve_for_question(
        claim,
        None,
        threshold,
        _RETRIEVED_CHUNKS,
        _prepare_explicit_filters(tags=tags, collections=collections),
    )
    paths: list[str] = []
    for document in documents:
        source = str((document.metadata or {}).get("source") or "").strip()
        rel_path = _safe_relpath(source) if source else ""
        if rel_path and rel_path not in paths:
            paths.append(rel_path)
        if len(paths) >= _MAX_START_ARTICLES:
            break
    return paths, ""


def _trace_chain(
    claim: str,
    starts: list[str],
    model_name: str | None,
    abort_signal: Any = None,
):
    """Breadth-first walk through credited library sources; yields status events, returns steps."""

    steps: list[_Step] = []
    queue: list[tuple[str, int, int | None]] = [(path, 0, None) for path in starts]
    visited: set[str] = set()
    while queue and len(steps) < _MAX_TRACES:
        if abort_signal is not None and abort_signal.is_aborted():
            break
        rel_path, depth, parent = queue.pop(0)
        key = rel_path.replace("\\", "/").lower()
        if key in visited:
            continue
        visited.add(key)
        record = library_record(rel_path)
        step = _Step(rel_path=rel_path, depth=depth, record=record)
        yield {"type": "status", "msg": _t("status_read", label=_article_label(rel_path, record))}
        try:
            step.result = trace_origin(claim, rel_path, model_name=model_name)
        except OriginTraceError as exc:
            step.error = exc.message
        except Exception as exc:  # noqa: BLE001 - one unreadable article must not end the chain
            _logger.warning("Origin tracing failed on %s: %s", rel_path, exc)
            step.error = type(exc).__name__
        steps.append(step)
        index = len(steps) - 1
        if parent is not None:
            steps[parent].children.append(index)
        result = step.result or {}
        if result.get("verdict") != VERDICT_ATTRIBUTES_ELSEWHERE or depth >= _MAX_DEPTH:
            continue
        followed = 0
        for citation in result.get("citations") or []:
            if citation.get("attribution") != FOR_THIS_IDEA:
                continue
            for reference in citation.get("references") or []:
                library = reference.get("library") or {}
                next_path = str(library.get("rel_path") or "")
                if next_path and next_path.replace("\\", "/").lower() not in visited and followed < _MAX_BRANCHES:
                    queue.append((next_path, depth + 1, index))
                    followed += 1
    return steps, bool(queue)


def _source_payload(step: _Step, number: int) -> dict[str, Any]:
    from src.rag.query_engine import _source_snippet_for_display

    record = step.record or {}
    passage = (step.result or {}).get("passage") or {}
    return {
        "index": number,
        "ref_label": f"S{number}",
        "file": step.rel_path,
        "page": "",
        "location_label": str(passage.get("page") or ""),
        "snippet": _source_snippet_for_display(str(passage.get("text") or ""), limit=_SNIPPET_CHARS),
        "content_type": "pdf_page",
        "citation": str(record.get("apa") or ""),
        "title": str(record.get("title") or ""),
        "authors": str(record.get("authors_display") or ""),
        "author": str(record.get("author") or ""),
        "year": str(record.get("year") or ""),
        "journal": str(record.get("journal") or ""),
        "doi": str(record.get("doi") or ""),
    }


def _credited_references(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        reference
        for citation in result.get("citations") or []
        if citation.get("attribution") == FOR_THIS_IDEA
        for reference in citation.get("references") or []
    ]


def _reference_phrase(reference: dict[str, Any], labels_by_path: dict[str, str]) -> str:
    library = reference.get("library") or {}
    number = reference.get("number")
    suffix = f" ({_t('ref', number=number)})" if isinstance(number, int) else ""
    path = str(library.get("rel_path") or "").replace("\\", "/").lower()
    if path and path in labels_by_path:
        return f"{library.get('label') or _entry_text(reference)} [{labels_by_path[path]}]{suffix}"
    if library.get("label"):
        return f"{library['label']}{suffix}"
    return f"{_entry_text(reference)}{suffix}, {_t('outside')}"


def _step_phrase(step: _Step, labels_by_path: dict[str, str]) -> str:
    if step.error:
        return _t("unreadable", message=step.error)
    result = step.result or {}
    verdict = result.get("verdict")
    if verdict == VERDICT_ATTRIBUTES_ELSEWHERE:
        refs = "; ".join(_reference_phrase(ref, labels_by_path) for ref in _credited_references(result))
        return _t("attributes", refs=refs)
    if verdict == VERDICT_NO_ATTRIBUTION:
        return _t("no_attribution") + (_t("self_claim") if result.get("self_claim") else "")
    if verdict == VERDICT_UNCHECKED:
        refs = "; ".join(
            _reference_phrase(ref, labels_by_path)
            for citation in result.get("citations") or []
            for ref in citation.get("references") or []
        )
        return _t("unchecked", refs=refs)
    notes = result.get("notes") or []
    for note in ("passage_not_found", "reference_list_not_found", "markers_not_resolved"):
        if note in notes:
            return _t(note)
    return _t("markers_not_resolved")


def _earliest_reference(steps: list[_Step]) -> tuple[int, dict[str, Any]] | None:
    best: tuple[int, dict[str, Any]] | None = None
    for step in steps:
        for reference in _credited_references(step.result or {}):
            year = _reference_year(reference)
            if year and (best is None or int(year) < best[0]):
                best = (int(year), reference)
    return best


def _earliest(steps: list[_Step], labels_by_path: dict[str, str]) -> str:
    best = _earliest_reference(steps)
    return _reference_phrase(best[1], labels_by_path) if best else ""


def render_origin_answer(claim: str, steps: list[_Step], *, stopped_early: bool) -> tuple[str, list[dict[str, Any]]]:
    """Markdown answer and its ``[Sx]`` sources: one source per article read."""

    sources: list[dict[str, Any]] = []
    labels_by_path: dict[str, str] = {}
    for step in steps:
        if step.result and step.result.get("passage"):
            number = len(sources) + 1
            sources.append(_source_payload(step, number))
            step.source_label = f"S{number}"
            labels_by_path[step.rel_path.replace("\\", "/").lower()] = step.source_label

    claim_text = claim.replace("[", "(").replace("]", ")").replace("*", "")
    lines = [_t("heading", claim=claim_text), ""]
    earliest = _earliest(steps, labels_by_path)
    if earliest:
        lines += [_t("earliest", label=earliest), ""]
    lines += [_t("chain"), ""]

    def walk(index: int) -> None:
        step = steps[index]
        label = _article_label(step.rel_path, step.record)
        cite = f" [{step.source_label}]" if step.source_label else ""
        page = (step.result or {}).get("passage", {}) or {}
        where = f", {page.get('page')}" if page.get("page") else ""
        arrow = "↳ " * step.depth
        lines.append(f"- {arrow}**{label}**{cite}{where}{_t('colon')}{_step_phrase(step, labels_by_path)}")
        for child in step.children:
            walk(child)

    for index, step in enumerate(steps):
        if step.depth == 0:
            walk(index)
    lines.append("")
    lines.append(_t("caveat"))
    if any((step.result or {}).get("model_reliability") == "low" for step in steps):
        lines.append(_t("weak_model"))
    outside = any(
        not (reference.get("library") or {}).get("rel_path")
        for step in steps
        for reference in _credited_references(step.result or {})
    )
    if outside:
        lines.append(_t("outside_hint"))
    if stopped_early:
        lines.append(_t("limit", count=len(steps)))
    return "\n".join(lines).strip(), sources


def _step_data(index: int, step: _Step, parents: dict[int, int]) -> dict[str, Any]:
    result = step.result or {}
    return {
        "index": index,
        "parent": parents.get(index),
        "depth": step.depth,
        "rel_path": step.rel_path,
        "label": _article_label(step.rel_path, step.record),
        "record": step.record,
        "error": step.error,
        "verdict": result.get("verdict") or "",
        "notes": result.get("notes") or [],
        "passage": result.get("passage"),
        "self_claim": bool(result.get("self_claim")),
        "model_reliability": result.get("model_reliability") or "",
        "credited": [
            {**reference, "year": _reference_year(reference)} for reference in _credited_references(result)
        ],
    }


def trace_library_chain(
    claim: str,
    model_name: str | None,
    threshold: float,
    *,
    tags: Any = None,
    collections: Any = None,
) -> dict[str, Any]:
    """The walk of ``/origine`` as data, for the Discovery "Origines" view."""

    claim = re.sub(r"\s+", " ", str(claim or "")).strip()
    if not claim:
        raise OriginTraceError("invalid_request", "Idée manquante.")
    starts, reason = _starting_articles(claim, model_name, threshold, tags=tags, collections=collections)
    if reason:
        return {"ok": False, "error": "index_unavailable", "msg": reason}
    walker = _trace_chain(claim, starts, model_name)
    while True:
        try:
            next(walker)
        except StopIteration as stop:
            steps, stopped_early = stop.value
            break
    parents = {child: index for index, step in enumerate(steps) for child in step.children}
    earliest = _earliest_reference(steps)
    return {
        "ok": True,
        "claim": claim,
        "steps": [_step_data(index, step, parents) for index, step in enumerate(steps)],
        "earliest": {**earliest[1], "year": str(earliest[0])} if earliest else None,
        "stopped_early": stopped_early,
    }


def stream_chat_origin(
    claim: str,
    model_name: str | None,
    threshold: float,
    *,
    tags: Any = None,
    collections: Any = None,
    article_paths: list[str] | None = None,
    abort_signal: Any = None,
):
    """Chat tool stream: ``status`` events while reading, then the answer and its sources."""

    from src.rag.query_engine import _split_text_for_streaming

    def answer(text: str, sources: list[dict[str, Any]]):
        for piece in _split_text_for_streaming(text):
            yield {"type": "token", "text": piece}
        yield {"type": "sources", "data": sources, "answer_origin": "origin_trace"}

    claim = re.sub(r"\s+", " ", str(claim or "")).strip()
    if not claim:
        yield from answer(_t("empty"), [])
        return
    starts = [path for path in (article_paths or []) if path][:_MAX_START_ARTICLES]
    if not starts:
        yield {"type": "status", "msg": _t("status_search")}
        starts, reason = _starting_articles(
            claim, model_name, threshold, tags=tags, collections=collections
        )
        if reason:
            yield from answer(_t("no_index", reason=reason), [])
            return
    if not starts:
        yield from answer(_t("no_article"), [])
        return
    steps, stopped_early = yield from _trace_chain(claim, starts, model_name, abort_signal)
    text, sources = render_origin_answer(claim, steps, stopped_early=stopped_early)
    yield from answer(text, sources)
