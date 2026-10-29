"""Native research actions exposed through the main chat.

These modes reuse the existing RAG retrieval contract and stream the same
``status`` / ``token`` / ``sources`` events as the regular chat pipeline.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.app_settings import get_app_settings
from src.chat_export import collect_export_bibliography_sources, select_export_source
from src.rag.paper_list import _normalize_key, _source_label

_logger = logging.getLogger(__name__)


def _native_lang() -> str:
    from src.response_language import current_response_language

    if requested := current_response_language():
        return requested
    try:
        lang = str(get_app_settings().get("ui_language") or "").strip().lower()
    except Exception:
        _logger.debug("Recovered from an error in _native_lang.", exc_info=True)
        lang = ""
    return lang if lang in ("fr", "en") else "fr"


def _nt(key: str, **kwargs: object) -> str:
    _L10N_NATIVE: dict[str, dict[str, str]] = {
        "sans_titre": {"fr": "Sans titre", "en": "Untitled"},
        "fallback_compare": {
            "fr": "## Comparaison des papiers\n\nLe modele n'a pas produit de comparaison structuree fiable; voici la comparaison minimale basee sur les sources retrouvees.\n\n{table}",
            "en": "## Paper comparison\n\nThe model did not produce a reliable structured comparison; here is the minimal comparison based on the sources found.\n\n{table}",
        },
        "fallback_evidence": {
            "fr": "## Matrice de preuves\n\nLe modele n'a pas produit de matrice structuree fiable; voici les preuves brutes retrouvees.\n\n{table}",
            "en": "## Evidence matrix\n\nThe model did not produce a reliable structured matrix; here are the raw evidence found.\n\n{table}",
        },
        "fallback_fact_check": {
            "fr": "## Verification locale\n\nJe n'ai pas pu obtenir une verification structuree fiable. Les sources retrouvees peuvent aider a verifier le passage, mais je ne peux pas conclure automatiquement.\n\nSources a consulter: {labels}\n\nPassage a verifier:\n\n> {query}",
            "en": "## Local verification\n\nI could not obtain a reliable structured verification. The sources found may help verify the passage, but I cannot conclude automatically.\n\nSources to consult: {labels}\n\nPassage to verify:\n\n> {query}",
        },
        "compare_headers": {"fr": "Source | Papier | Methode | Limites | Pertinence", "en": "Source | Paper | Method | Limits | Relevance"},
        "evidence_headers": {"fr": "Source | Claim | Preuve | Conditions | Limites", "en": "Source | Claim | Evidence | Conditions | Limits"},
        "fact_check_headers": {"fr": "Affirmation | Statut | Justification | Sources", "en": "Claim | Status | Justification | Sources"},
        "compare_title": {"fr": "## Comparaison des papiers", "en": "## Paper comparison"},
        "evidence_title": {"fr": "## Matrice de preuves", "en": "## Evidence matrix"},
        "fact_check_title": {"fr": "## Verification locale", "en": "## Local verification"},
        "synthesis_title": {"fr": "## Synthese", "en": "## Synthesis"},
        "cautious_rewrite_title": {"fr": "## Reformulation prudente", "en": "## Cautious rewrite"},
        "non_determine": {"fr": "Non determine", "en": "Undetermined"},
        "pertinent_for": {"fr": "Pertinent pour: {query}", "en": "Relevant for: {query}"},
        "voir_extrait": {"fr": "Voir extrait source", "en": "See source excerpt"},
        "a_verifier": {"fr": "A verifier dans le PDF", "en": "To verify in the PDF"},
        "element_pertinent": {"fr": "Element pertinent pour: {query}", "en": "Relevant element for: {query}"},
        "biblio_title": {"fr": "## Bibliographie", "en": "## Bibliography"},
        "no_sources_biblio": {
            "fr": "Je n'ai pas trouve de sources citees dans cette conversation. Generez d'abord une reponse RAG sourcee.",
            "en": "I could not find cited sources in this conversation. First generate a sourced RAG response.",
        },
        "no_sources_notebook": {
            "fr": "Je n'ai pas trouve de reponse ou de sources recentes a transformer en carnet. Generez d'abord une reponse sourcee.",
            "en": "I could not find a recent answer or sources to transform into a notebook. First generate a sourced response.",
        },
        "notebook_created": {
            "fr": "Carnet cree : **{title}**.",
            "en": "Notebook created: **{title}**.",
        },
        "sources_added": {"fr": "- Sources ajoutees : {count}", "en": "- Sources added: {count}"},
        "notes_added_yes": {"fr": "- Notes ajoutees : oui", "en": "- Notes added: yes"},
        "notes_added_no": {"fr": "- Notes ajoutees : non", "en": "- Notes added: no"},
        "open_notebooks_tab": {
            "fr": "Ouvrez l'onglet Carnets pour le consulter.",
            "en": "Open the Notebooks tab to view it.",
        },
        "carnet_depui_chat": {"fr": "Carnet depuis le chat", "en": "Notebook from chat"},
        "source_du_chat": {"fr": "Source du chat", "en": "Chat source"},
        "no_source_found": {
            "fr": "Aucune source pertinente retrouvee dans l'index RAG pour cette demande.",
            "en": "No relevant source found in the RAG index for this request.",
        },
        "searching_papers": {"fr": "Recherche des papiers a comparer...", "en": "Searching for papers to compare..."},
        "searching_evidence": {"fr": "Recherche des preuves dans l'index RAG...", "en": "Searching for evidence in RAG index..."},
        "searching_fact_check": {"fr": "Recherche de sources pour verifier le passage...", "en": "Searching for sources to verify the passage..."},
        "sources_kept": {"fr": "{count} source(s) retenue(s), analyse en cours...", "en": "{count} source(s) kept, analyzing..."},
    }
    lang = _native_lang()
    entry = _L10N_NATIVE.get(key, {})
    template = entry.get(lang, entry.get("fr", f"<{key}>"))
    if kwargs:
        return template.format(**kwargs)
    return template

MAX_NATIVE_CHAT_CHUNKS = 40
DEFAULT_NATIVE_CHAT_CHUNKS = 28
PER_SOURCE_PROMPT_CHARS = 1200

NativeRetrievalMode = Literal["paper_compare", "evidence_matrix", "fact_check"]


class PaperComparisonRow(BaseModel):
    source: str = Field(..., description="Source label, exactly like S1.")
    paper: str = Field(..., description="Paper title or short identifier.")
    method: str = Field("", description="Main method, model, dataset, or protocol.")
    assumptions: str = Field("", description="Important assumptions or setup constraints.")
    results: str = Field("", description="Main result relevant to the user query.")
    limits: str = Field("", description="Limits, caveats, or missing information.")
    relevance: str = Field("", description="Why this paper matters for the query.")


class PaperComparisonResponse(BaseModel):
    summary: str = Field("", description="Short cross-paper comparison summary with citations.")
    rows: list[PaperComparisonRow] = Field(default_factory=list)
    takeaway: str = Field("", description="Practical synthesis of the comparison.")


class EvidenceMatrixRow(BaseModel):
    source: str = Field(..., description="Source label, exactly like S1.")
    claim: str = Field(..., description="Atomic claim relevant to the user query.")
    evidence: str = Field("", description="What the source says that supports the claim.")
    conditions: str = Field("", description="Experimental/modeling conditions if present.")
    limits: str = Field("", description="Known caveat or uncertainty.")


class EvidenceMatrixResponse(BaseModel):
    topic: str = Field("", description="The topic covered by the matrix.")
    rows: list[EvidenceMatrixRow] = Field(default_factory=list)
    synthesis: str = Field("", description="Short synthesis of the strongest evidence.")


class FactCheckItem(BaseModel):
    claim: str = Field(..., description="Claim extracted from the user text.")
    status: Literal["supported", "partial", "unsupported", "not_found"] = "not_found"
    evidence: str = Field("", description="Explanation grounded in the sources.")
    sources: list[str] = Field(default_factory=list, description="Source labels such as S1.")


class FactCheckResponse(BaseModel):
    verdict: str = Field("", description="Overall verdict in one short paragraph.")
    items: list[FactCheckItem] = Field(default_factory=list)
    safer_rewrite: str = Field("", description="Optional cautious rewrite of the user text.")


def _paper_key(metadata: dict[str, Any]) -> str:
    article_id = str(metadata.get("article_id", "") or "").strip()
    if article_id:
        return f"article:{article_id}"
    doi = _normalize_key(metadata.get("zotero_doi", "") or metadata.get("doi", ""))
    if doi:
        return f"doi:{doi}"
    title = _normalize_key(metadata.get("zotero_title", "") or metadata.get("title", ""))
    if title:
        return f"title:{title}"
    source = str(metadata.get("source", "") or metadata.get("file", "") or "").strip()
    if source:
        return f"source:{source}"
    return f"unknown:{id(metadata)}"


def _dedupe_by_paper(docs: list[Any]) -> list[Any]:
    grouped: OrderedDict[str, Any] = OrderedDict()
    for doc in docs:
        metadata = getattr(doc, "metadata", {}) or {}
        grouped.setdefault(_paper_key(metadata), doc)
    return list(grouped.values())


def _title(metadata: dict[str, Any]) -> str:
    return str(
        metadata.get("zotero_title")
        or metadata.get("title")
        or metadata.get("source")
        or _nt("sans_titre")
    ).strip()


def _authors(metadata: dict[str, Any]) -> str:
    return str(
        metadata.get("zotero_authors")
        or metadata.get("authors")
        or metadata.get("zotero_first_author")
        or metadata.get("author")
        or ""
    ).strip()


def _year(metadata: dict[str, Any]) -> str:
    return str(metadata.get("zotero_year") or metadata.get("year") or "").strip()


def _format_source_block(label: str, doc: Any) -> str:
    from src.rag.query_engine import _safe_relpath, _source_snippet_for_display

    metadata = getattr(doc, "metadata", {}) or {}
    title = _title(metadata)
    authors = _authors(metadata)
    year = _year(metadata)
    source = _safe_relpath(str(metadata.get("source", "") or "inconnu"))
    page = str(metadata.get("page", "?") or "?").strip()
    snippet = _source_snippet_for_display(
        str(getattr(doc, "page_content", "") or ""),
        limit=PER_SOURCE_PROMPT_CHARS,
    )
    parts = [f"[{label}]", f"Titre: {title}", f"Fichier: {source}", f"Page: {page}"]
    if authors:
        parts.append(f"Auteurs: {authors}")
    if year:
        parts.append(f"Annee: {year}")
    if snippet:
        parts.append(f"Extrait: {snippet}")
    return "\n".join(parts)


def _pipe(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).replace("|", "/")


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return ""
    out = ["| " + " | ".join(headers) + " |"]
    out.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        out.append("| " + " | ".join(_pipe(cell) for cell in row) + " |")
    return "\n".join(out)


def _fallback_compare_markdown(user_query: str, docs: list[Any]) -> str:
    rows: list[list[str]] = []
    for index, doc in enumerate(docs, start=1):
        metadata = getattr(doc, "metadata", {}) or {}
        label = _source_label(doc, index)
        descriptor = ", ".join(part for part in (_authors(metadata), _year(metadata)) if part)
        paper = _title(metadata) + (f" ({descriptor})" if descriptor else "")
        rows.append([f"[{label}]", paper, _nt("voir_extrait"), _nt("non_determine"), _nt("pertinent_for", query=user_query)])
    table = _markdown_table(["Source", "Papier", "Methode", "Limites", "Pertinence"], rows)
    return _nt("fallback_compare", table=table).strip()


def _fallback_evidence_markdown(user_query: str, docs: list[Any]) -> str:
    from src.rag.query_engine import _source_snippet_for_display

    rows: list[list[str]] = []
    for index, doc in enumerate(docs, start=1):
        label = _source_label(doc, index)
        snippet = _source_snippet_for_display(str(getattr(doc, "page_content", "") or ""), limit=260)
        rows.append([f"[{label}]", _nt("element_pertinent", query=user_query), snippet, _nt("non_determine"), _nt("a_verifier")])
    table = _markdown_table(["Source", "Claim", "Preuve", "Conditions", "Limites"], rows)
    return _nt("fallback_evidence", table=table).strip()


def _fallback_fact_check_markdown(user_query: str, docs: list[Any]) -> str:
    labels = ", ".join(f"[{_source_label(doc, index)}]" for index, doc in enumerate(docs, start=1)) or "aucune"
    return _nt("fallback_fact_check", labels=labels, query=user_query.strip()).strip()


def _compare_to_markdown(response: PaperComparisonResponse) -> str:
    rows = [
        [
            f"[{row.source.strip('[]')}]",
            row.paper,
            row.method,
            row.assumptions,
            row.results,
            row.limits,
            row.relevance,
        ]
        for row in response.rows
        if row.source.strip()
    ]
    parts: list[str] = [_nt("compare_title")]
    if response.summary.strip():
        parts.append(response.summary.strip())
    table = _markdown_table(
        ["Source", "Papier", "Methode", "Hypotheses", "Resultats", "Limites", "Pertinence"],
        rows,
    )
    if table:
        parts.append(table)
    if response.takeaway.strip():
        parts.append(f"{_nt('synthesis_title')}\n\n" + response.takeaway.strip())
    return "\n\n".join(parts).strip()


def _evidence_to_markdown(response: EvidenceMatrixResponse) -> str:
    rows = [
        [f"[{row.source.strip('[]')}]", row.claim, row.evidence, row.conditions, row.limits]
        for row in response.rows
        if row.source.strip() and row.claim.strip()
    ]
    title = response.topic.strip() or _nt("evidence_title").lstrip("## ")
    parts: list[str] = [f"## {title}"]
    table = _markdown_table(["Source", "Claim", "Preuve", "Conditions", "Limites"], rows)
    if table:
        parts.append(table)
    if response.synthesis.strip():
        parts.append(f"{_nt('synthesis_title')}\n\n" + response.synthesis.strip())
    return "\n\n".join(parts).strip()


def _fact_check_to_markdown(response: FactCheckResponse) -> str:
    rows = [
        [
            item.claim,
            item.status,
            item.evidence,
            ", ".join(f"[{src.strip('[]')}]" for src in item.sources if str(src).strip()),
        ]
        for item in response.items
        if item.claim.strip()
    ]
    parts: list[str] = [_nt("fact_check_title")]
    if response.verdict.strip():
        parts.append(response.verdict.strip())
    table = _markdown_table(["Affirmation", "Statut", "Justification", "Sources"], rows)
    if table:
        parts.append(table)
    if response.safer_rewrite.strip():
        parts.append(f"{_nt('cautious_rewrite_title')}\n\n" + response.safer_rewrite.strip())
    return "\n\n".join(parts).strip()


def _build_native_prompt(mode: NativeRetrievalMode, user_query: str, docs: list[Any]) -> str:
    from src.prompts import get_response_language_instruction

    labels = [_source_label(doc, index) for index, doc in enumerate(docs, start=1)]
    source_blocks = [
        _format_source_block(label, doc) for label, doc in zip(labels, docs, strict=False)
    ]
    sources = "\n\n---\n\n".join(source_blocks)
    lang_instruction = get_response_language_instruction(_native_lang())
    common = (
        "Tu travailles uniquement a partir des sources fournies. "
        "N'invente aucun fait absent des extraits. Cite les sources avec les labels exacts "
        f"{', '.join(labels)}. Si une information manque, dis-le clairement.\n\n"
        f"{lang_instruction}\n\n"
        f"Demande utilisateur:\n{user_query.strip()}\n\n"
        f"Sources:\n\n{sources}"
    )
    if mode == "paper_compare":
        return (
            "Compare les papiers sur la methode, les hypotheses, les resultats, les limites "
            "et la pertinence pour la demande.\n\n"
            + common
        )
    if mode == "evidence_matrix":
        return (
            "Construis une matrice de preuves. Chaque ligne doit etre un claim atomique, "
            "avec preuve, conditions et limites quand elles sont presentes.\n\n"
            + common
        )
    return (
        "Verifie le passage ou les affirmations de l'utilisateur contre les sources. "
        "Classe chaque affirmation en supported, partial, unsupported ou not_found.\n\n"
        + common
    )


def _invoke_native_llm(
    mode: NativeRetrievalMode,
    user_query: str,
    docs: list[Any],
    *,
    model_name: str,
) -> str | None:
    from src.llm_provider import invoke_structured_chat

    model: type[BaseModel]
    if mode == "paper_compare":
        model = PaperComparisonResponse
    elif mode == "evidence_matrix":
        model = EvidenceMatrixResponse
    else:
        model = FactCheckResponse

    prompt = _build_native_prompt(mode, user_query, docs)
    try:
        response = invoke_structured_chat(
            model_name,
            [{"role": "user", "content": prompt}],
            response_model=model,
            temperature=0.0,
            top_p=1.0,
        )
    except Exception:
        _logger.debug("Recovered from an error in _invoke_native_llm.", exc_info=True)
        return None

    if mode == "paper_compare" and isinstance(response, PaperComparisonResponse):
        return _compare_to_markdown(response)
    if mode == "evidence_matrix" and isinstance(response, EvidenceMatrixResponse):
        return _evidence_to_markdown(response)
    if mode == "fact_check" and isinstance(response, FactCheckResponse):
        return _fact_check_to_markdown(response)
    return None


def _fallback_markdown(mode: NativeRetrievalMode, user_query: str, docs: list[Any]) -> str:
    if mode == "paper_compare":
        return _fallback_compare_markdown(user_query, docs)
    if mode == "evidence_matrix":
        return _fallback_evidence_markdown(user_query, docs)
    return _fallback_fact_check_markdown(user_query, docs)


def ensure_rag_ready(model_name: str, threshold: float) -> None:
    """Initialize the RAG index once; raise ``RuntimeError`` when it cannot load."""
    from src.rag import state
    from src.rag.query_engine import _RAG_INIT_LOCK, init_rag

    with _RAG_INIT_LOCK:
        with state._state_lock:
            needs_init = state.document_chain is None or state.vectorstore is None
        if needs_init:
            ok, msg = init_rag(model_name, threshold, force_reload=False, build_missing=False)
            if not ok:
                raise RuntimeError(msg)


def _prepare_retrieval(
    user_query: str,
    model_name: str,
    threshold: float,
    *,
    tags: Any,
    collections: Any,
    target_chunks: int,
    dedupe_papers: bool,
) -> tuple[str, list[Any], dict | None]:
    from src.rag.query_engine import (
        _build_reranker_metrics_event,
        _clone_document_with_metadata,
        _extract_inline_filters,
        _merge_filter_maps,
        _prepare_explicit_filters,
        _retrieve_documents,
    )

    ensure_rag_ready(model_name, threshold)

    explicit_filters = _prepare_explicit_filters(tags=tags, collections=collections)
    cleaned_message, inline_filters = _extract_inline_filters(user_query)
    filters = _merge_filter_maps(explicit_filters, inline_filters)
    cap = max(8, min(MAX_NATIVE_CHAT_CHUNKS, int(target_chunks)))
    docs = _retrieve_documents(
        cleaned_message or user_query,
        threshold=threshold,
        max_chunks=cap,
        filters=filters,
    )
    if dedupe_papers:
        docs = _dedupe_by_paper(docs)
    labelled_docs = [
        _clone_document_with_metadata(doc, {"ref_label": f"S{index}"})
        for index, doc in enumerate(docs, start=1)
    ]
    return cleaned_message or user_query, labelled_docs, _build_reranker_metrics_event()


def stream_native_retrieval_action(
    mode: NativeRetrievalMode,
    user_query: str,
    model_name: str,
    threshold: float,
    *,
    tags: Any = None,
    collections: Any = None,
    target_chunks: int = DEFAULT_NATIVE_CHAT_CHUNKS,
):
    from src.rag import state
    from src.rag.query_engine import (
        _build_anchor_event,
        _split_text_for_streaming,
        _stream_reranker_warmup_status,
        format_sources_api,
    )

    status_key = {
        "paper_compare": "searching_papers",
        "evidence_matrix": "searching_evidence",
        "fact_check": "searching_fact_check",
    }[mode]
    yield {"type": "status", "msg": _nt(status_key)}
    yield from _stream_reranker_warmup_status()

    try:
        cleaned_query, docs, pending_metrics = _prepare_retrieval(
            user_query,
            model_name,
            threshold,
            tags=tags,
            collections=collections,
            target_chunks=target_chunks,
            dedupe_papers=mode == "paper_compare",
        )
    except Exception as exc:
        _logger.debug("Recovered from an error in stream_native_retrieval_action.", exc_info=True)
        yield {"type": "error", "msg": str(exc)}
        return

    if not docs:
        text = _nt("no_source_found")
        for piece in _split_text_for_streaming(text):
            yield {"type": "token", "text": piece}
        yield {"type": "sources", "data": [], "answer_origin": mode}
        return

    yield {"type": "status", "msg": _nt("sources_kept", count=len(docs))}
    markdown = _invoke_native_llm(mode, cleaned_query, docs, model_name=model_name)
    if not markdown:
        markdown = _fallback_markdown(mode, cleaned_query, docs)

    with state._state_lock:
        state.last_sources = docs

    for piece in _split_text_for_streaming(markdown):
        yield {"type": "token", "text": piece}
    yield {"type": "sources", "data": format_sources_api(docs), "answer_origin": mode}
    if pending_metrics:
        yield pending_metrics
    anchor_event = _build_anchor_event(markdown, docs)
    if anchor_event:
        yield anchor_event


def _source_value(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = re.sub(r"\s+", " ", str(source.get(key) or "").strip())
        if value:
            return value
    return ""


def _bibliography_entry(source: dict[str, Any]) -> str:
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
    if parts:
        return " ".join(parts).strip()
    file_value = _source_value(source, "file", "source")
    page = _source_value(source, "page")
    return ", ".join(part for part in (file_value, f"p. {page}" if page else "") if part)


def stream_chat_bibliography(
    *,
    previous_messages: list[dict[str, Any]],
    scope: Literal["previous", "chat"] = "chat",
):
    from src.rag.query_engine import _split_text_for_streaming

    sources = collect_export_bibliography_sources(previous_messages, scope=scope)
    if not sources:
        text = _nt("no_sources_biblio")
        for piece in _split_text_for_streaming(text):
            yield {"type": "token", "text": piece}
        yield {"type": "sources", "data": [], "answer_origin": "bibliography"}
        return

    lines = [_nt("biblio_title"), ""]
    for index, source in enumerate(sources, start=1):
        entry = _bibliography_entry(source)
        if entry:
            lines.append(f"{index}. {entry}")
    markdown = "\n".join(lines).strip()
    for piece in _split_text_for_streaming(markdown):
        yield {"type": "token", "text": piece}
    yield {"type": "sources", "data": sources, "answer_origin": "bibliography"}


def _notebook_title_from_query(user_query: str) -> str:
    default_title = _nt("carnet_depui_chat")
    text = re.sub(r"\s+", " ", str(user_query or "").strip())
    if not text:
        return default_title
    text = re.sub(
        r"^(?:cree|creer|create|mets|mettre|ajoute|ajouter|make|build)\s+",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip(" .:;")
    if len(text) > 72:
        text = text[:72].rstrip() + "..."
    return text or default_title


def stream_notebook_from_chat(
    *,
    user_query: str,
    previous_messages: list[dict[str, Any]],
):
    from src.notebooks import add_source, create_notebook, update_notes
    from src.rag.query_engine import _split_text_for_streaming

    source_text, _default_title = select_export_source(previous_messages, scope="previous")
    sources = collect_export_bibliography_sources(previous_messages, scope="previous")
    if not source_text and not sources:
        text = _nt("no_sources_notebook")
        for piece in _split_text_for_streaming(text):
            yield {"type": "token", "text": piece}
        yield {"type": "sources", "data": [], "answer_origin": "notebook_create"}
        return

    nb = create_notebook(_notebook_title_from_query(user_query))
    nb_id = str(nb.get("id") or "")
    added = 0
    for source in sources:
        title = _source_value(source, "title", "zotero_title", "file", "source") or _nt("source_du_chat")
        content_parts = [
            _bibliography_entry(source),
            _source_value(source, "snippet"),
        ]
        content = "\n\n".join(part for part in content_parts if part).strip()
        if not content:
            continue
        added_source = add_source(
            nb_id,
            title=title,
            content=content,
            source_path=_source_value(source, "file", "source"),
        )
        if added_source is not None:
            added += 1

    if source_text:
        update_notes(nb_id, source_text)

    nb_title = nb.get('title') or _nt('carnet_depui_chat')
    text = (
        f"{_nt('notebook_created', title=nb_title)}\n\n"
        f"{_nt('sources_added', count=added)}\n"
        f"{_nt('notes_added_yes' if source_text else 'notes_added_no')}\n\n"
        f"{_nt('open_notebooks_tab')}"
    )
    for piece in _split_text_for_streaming(text):
        yield {"type": "token", "text": piece}
    yield {
        "type": "sources",
        "data": sources,
        "answer_origin": "notebook_create",
    }


__all__ = [
    "DEFAULT_NATIVE_CHAT_CHUNKS",
    "EvidenceMatrixResponse",
    "FactCheckResponse",
    "MAX_NATIVE_CHAT_CHUNKS",
    "PaperComparisonResponse",
    "stream_chat_bibliography",
    "stream_native_retrieval_action",
    "stream_notebook_from_chat",
]
