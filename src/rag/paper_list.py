"""Deterministic paper-list mode for exhaustive chat requests."""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Generator
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)

DEFAULT_PAPER_LIST_CHUNKS = 32
MAX_PAPER_LIST_CHUNKS = 40
# "Any others?" excludes the papers already listed: a wider window finds new ones.
FOLLOWUP_PAPER_LIST_CHUNKS = 64
PER_PAPER_SUMMARY_CHAR_BUDGET = 650
PAPER_SUMMARY_BASE_OUTPUT_TOKENS = 160
# One JSON item with a one-sentence justification is about 100 tokens.
PAPER_SUMMARY_TOKENS_PER_PAPER = 120
PAPER_SUMMARY_MAX_OUTPUT_TOKENS = 1200
PAPER_SUMMARY_MAX_PAPERS = 12
# Small local models answer for one or two papers out of twelve; four per
# call keeps each strictly validated answer complete.
PAPER_SUMMARY_BATCH_SIZE = 4
PAPER_LIST_STATUS_INTERVAL_SECONDS = 2.5

_MISSING_AUTHOR_RE = re.compile(
    r"\b(?:author|auteur)\s*:\s*$|"
    r"\b(?:author|auteur)\s*:\s*(?:\.{3,}|…|___+)\s*$|"
    r"\b(?:ecrit|ecrite|ecrits|ecrites|written|authored)\s+(?:par|by)\s*$|"
    r"\b(?:ecrit|ecrite|ecrits|ecrites|written|authored)\s+(?:par|by)\s*(?:\.{3,}|…|___+)\s*$",
    flags=re.IGNORECASE,
)
_MISSING_TOPIC_RE = re.compile(
    r"\b(?:qui\s+parlent\s+de|qui\s+parle\s+de|sur|about|on|au\s+sujet\s+de|related\s+to)\s*$|"
    r"\b(?:qui\s+parlent\s+de|qui\s+parle\s+de|sur|about|on|au\s+sujet\s+de|related\s+to)\s*(?:\.{3,}|…|___+)\s*$",
    flags=re.IGNORECASE,
)
_GENERIC_PAPER_LIST_TERMS = {
    "article",
    "articles",
    "about",
    "and",
    "aux",
    "cherche",
    "chercher",
    "copie",
    "copier",
    "copy",
    "copied",
    "dans",
    "de",
    "des",
    "du",
    "ecrit",
    "ecrite",
    "ecrits",
    "ecrites",
    "for",
    "find",
    "list",
    "liste",
    "les",
    "on",
    "paper",
    "papers",
    "par",
    "publication",
    "publications",
    "qui",
    "recherche",
    "search",
    "show",
    "sur",
    "the",
    "trouve",
    "trouver",
    "tous",
    "toutes",
    "all",
    "parle",
    "parlent",
    "traite",
    "traitent",
    "mentionne",
    "mentionnent",
    "relatif",
    "relatifs",
    "related",
    "sujet",
    "topic",
    "written",
    "published",
    "publie",
    "publiee",
    "publiees",
    "publies",
    "quel",
    "quelle",
    "quelles",
    "quels",
    "sont",
    "are",
    "were",
    "what",
    "which",
}

_BROAD_RELEVANCE_TERMS = {
    "analysis",
    "analyse",
    "effect",
    "effects",
    "effet",
    "effets",
    "energie",
    "energies",
    "energy",
    "etude",
    "etudes",
    "method",
    "methods",
    "methode",
    "methodes",
    "model",
    "models",
    "modele",
    "modeles",
    "result",
    "results",
    "resultat",
    "resultats",
    "study",
    "studies",
}

_DIRECT_TOPIC_ALIAS_GROUPS = (
    {
        "renewable",
        "renewables",
        "renouvelable",
        "renouvelables",
        "solar",
        "solaire",
        "photovoltaic",
        "photovoltaique",
        "wind",
        "eolien",
        "eolienne",
        "hydroelectric",
        "hydraulique",
        "geothermal",
        "geothermie",
        "biomass",
        "biomasse",
    },
    {"climate", "climat", "climatic", "climatique"},
    {"battery", "batteries", "batterie"},
    {"loss", "losses", "perte", "pertes"},
    {
        "superconductor",
        "superconductors",
        "superconducting",
        "supraconducteur",
        "supraconducteurs",
        "supraconductrice",
    },
    # Papers name the conductor, rarely the class: "LTS" must reach Nb3Sn coils.
    {"lts", "nbti", "nb-ti", "nb3sn", "nb3al"},
    {"hts", "rebco", "ybco", "reba", "bscco", "bi-2212", "bi2212", "bi-2223", "bi2223"},
)


class PaperListSummaryItem(BaseModel):
    label: str = Field(..., description="Source label, exactly like S1, S2, etc.")
    relevant: bool = Field(
        ...,
        description=(
            "True only when the title, metadata, or excerpt directly supports that the "
            "paper addresses the user's requested subject."
        ),
    )
    summary: str = Field(
        ...,
        description=(
            "One short evidence-based sentence explaining why the paper is or is not "
            "directly relevant to the user query."
        ),
    )


class PaperListSummaryResponse(BaseModel):
    items: list[PaperListSummaryItem] = Field(
        default_factory=list,
        description="Exactly one item for every provided source label, in source order.",
    )


def _normalize_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_request_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower()
    text = re.sub(r"['’`]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _paper_list_language(language: str = "fr") -> str:
    return "en" if str(language or "").strip().lower() == "en" else "fr"


def incomplete_paper_list_request_message(user_query: str, *, language: str = "fr") -> str:
    """Return a user-facing validation message before launching retrieval."""
    language = _paper_list_language(language)
    normalized = _normalize_request_text(user_query)
    if not normalized:
        if language == "en":
            return "Enter a query before starting the paper search."
        return "Precisez une requete avant de lancer la recherche d'articles."
    if _MISSING_AUTHOR_RE.search(normalized):
        if language == "en":
            return "The author's name is missing."
        return "Il manque le nom de l'auteur."
    if _MISSING_TOPIC_RE.search(normalized):
        if language == "en":
            return "The topic for the list is missing. Example: `List all papers about AC losses`."
        return (
            "Il manque le sujet de la liste. Exemple: "
            "`Liste tous les articles qui parlent de pertes AC`."
        )
    return ""


def _paper_list_query_tokens(value: str) -> set[str]:
    normalized = _normalize_request_text(value)
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", normalized)
        if token not in _GENERIC_PAPER_LIST_TERMS
    }


def _retrieval_query_for_filters(
    *,
    user_query: str,
    cleaned_message: str,
    filters: dict[str, list[str]],
) -> str:
    """Keep author names as lexical clues when the cleaned query is only generic."""
    cleaned = str(cleaned_message or "").strip()
    if filters.get("authors") and not _paper_list_query_tokens(cleaned):
        return str(user_query or "").strip() or cleaned
    return cleaned or str(user_query or "").strip()


def _is_author_metadata_only_query(
    cleaned_message: str,
    filters: dict[str, list[str]],
) -> bool:
    if not filters.get("authors") or _paper_list_query_tokens(cleaned_message):
        return False
    return not any(filters.get(key) for key in ("tags", "collections", "years", "dois", "sources"))


def _library_record_to_doc(record: dict[str, Any], index: int) -> Any:
    authors = [str(value).strip() for value in record.get("authors", []) if str(value).strip()]
    metadata = {
        "source": str(record.get("source") or "").strip(),
        "page": "?",
        "content_type": "pdf_page",
        "article_id": str(record.get("article_id") or "").strip(),
        "ref_label": f"S{index}",
        "zotero_title": str(record.get("title") or "").strip(),
        "zotero_authors": ", ".join(authors),
        "zotero_first_author": authors[0] if authors else "",
        "zotero_year": str(record.get("year") or "").strip(),
        "zotero_journal": str(record.get("journal") or "").strip(),
        "zotero_doi": str(record.get("doi") or "").strip(),
        "zotero_tags": " | ".join(
            str(value).strip() for value in record.get("tags", []) if str(value).strip()
        ),
        "zotero_collections": " | ".join(
            str(value).strip() for value in record.get("collections", []) if str(value).strip()
        ),
        "zotero_citation": str(record.get("citation") or "").strip(),
    }
    return SimpleNamespace(page_content="", metadata=metadata)


def _token_fuzzy_matches(left: str, right: str) -> bool:
    if left == right:
        return True
    if min(len(left), len(right)) < 5:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.86


def _topic_aliases(token: str) -> set[str]:
    aliases = {token}
    for group in _DIRECT_TOPIC_ALIAS_GROUPS:
        if any(_token_fuzzy_matches(token, member) for member in group):
            aliases.update(group)
    return aliases


def _paper_direct_evidence_text(doc: Any) -> str:
    metadata = getattr(doc, "metadata", {}) or {}
    values = [
        metadata.get("zotero_title", ""),
        metadata.get("zotero_abstract", ""),
        metadata.get("zotero_tags", ""),
        metadata.get("zotero_collections", ""),
        metadata.get("zotero_authors", ""),
        metadata.get("zotero_year", ""),
        metadata.get("zotero_doi", ""),
        getattr(doc, "page_content", ""),
    ]
    return _normalize_request_text(" ".join(str(value or "") for value in values))


def _has_direct_query_evidence(user_query: str, doc: Any) -> bool:
    """Require an explicit topic anchor before trusting semantic similarity or an LLM."""
    query_tokens = _paper_list_query_tokens(user_query)
    if not query_tokens:
        return True
    focus_tokens = query_tokens - _BROAD_RELEVANCE_TERMS
    required_tokens = focus_tokens or query_tokens
    evidence_tokens = set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}", _paper_direct_evidence_text(doc)))
    return any(
        _token_fuzzy_matches(alias, evidence_token)
        for query_token in required_tokens
        for alias in _topic_aliases(query_token)
        for evidence_token in evidence_tokens
    )


def _paper_key(metadata: dict[str, Any]) -> str:
    article_id = str(metadata.get("article_id", "") or "").strip()
    if article_id:
        return f"article:{article_id}"
    doi = _normalize_key(metadata.get("zotero_doi", ""))
    if doi:
        return f"doi:{doi}"
    title = _normalize_key(metadata.get("zotero_title", ""))
    if title:
        return f"title:{title}"
    source = str(metadata.get("source", "") or "").strip()
    if source:
        return f"source:{source}"
    return f"unknown:{id(metadata)}"


def _identity_keys(article_id: Any, doi: Any, title: Any) -> set[str]:
    keys = set()
    if str(article_id or "").strip():
        keys.add(f"article:{str(article_id).strip()}")
    if _normalize_key(doi):
        keys.add(f"doi:{_normalize_key(doi)}")
    if _normalize_key(title):
        keys.add(f"title:{_normalize_key(title)}")
    return keys


def _exclude_listed_papers(docs: list[Any], listed_sources: Any) -> tuple[list[Any], int]:
    """Drop the papers an earlier answer already listed; return how many were dropped."""
    listed: set[str] = set()
    for source in listed_sources or []:
        if isinstance(source, dict):
            listed |= _identity_keys(
                source.get("article_id"), source.get("doi"), source.get("title")
            )
    if not listed:
        return docs, 0
    kept = []
    for doc in docs:
        metadata = getattr(doc, "metadata", {}) or {}
        keys = _identity_keys(
            metadata.get("article_id"), metadata.get("zotero_doi"), metadata.get("zotero_title")
        )
        if not keys & listed:
            kept.append(doc)
    return kept, len(docs) - len(kept)


def _already_listed_note(count: int, language: str) -> str:
    if not count:
        return ""
    return _localized(
        language,
        en=f"\n\n{count} paper(s) already listed above were left out.",
        fr=f"\n\n{count} article(s) deja liste(s) plus haut ont ete ecarte(s).",
    )


def dedupe_docs_by_paper(docs: list[Any]) -> list[Any]:
    grouped: OrderedDict[str, Any] = OrderedDict()
    for doc in docs:
        metadata = getattr(doc, "metadata", {}) or {}
        grouped.setdefault(_paper_key(metadata), doc)
    return list(grouped.values())


def _stream_timed_operation[T](
    operation: Callable[[], T],
    *,
    stage: str,
    initial_message: str,
    waiting_message: str,
    abort_signal: Any = None,
    language: str = "fr",
) -> Generator[dict[str, Any], None, tuple[T, float]]:
    """Run a blocking operation while emitting elapsed-time status events."""
    result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result_queue.put(("result", operation()))
        except BaseException as exc:
            result_queue.put(("error", exc))

    started_at = time.perf_counter()
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    yield {
        "type": "status",
        "stage": stage,
        "elapsed_seconds": 0.0,
        "msg": initial_message,
    }

    while True:
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        try:
            outcome, payload = result_queue.get(timeout=PAPER_LIST_STATUS_INTERVAL_SECONDS)
        except queue.Empty:
            elapsed = time.perf_counter() - started_at
            elapsed_label = "elapsed" if _paper_list_language(language) == "en" else "ecoulees"
            yield {
                "type": "status",
                "stage": stage,
                "elapsed_seconds": round(elapsed, 1),
                "msg": f"{waiting_message} ({int(elapsed)}s {elapsed_label})...",
            }
            continue

        elapsed = time.perf_counter() - started_at
        if outcome == "error":
            raise payload
        return payload, elapsed


def _rag_index_chunk_count(vectorstore: Any) -> int | None:
    collection = getattr(vectorstore, "_collection", None)
    if collection is None or not hasattr(collection, "count"):
        return None
    try:
        return max(0, int(collection.count()))
    except Exception:
        _logger.debug("Recovered from an error in _rag_index_chunk_count.", exc_info=True)
        return None


def _paper_title(metadata: dict[str, Any], *, language: str = "fr") -> str:
    title = str(metadata.get("zotero_title", "") or "").strip()
    if title:
        return title
    source = str(metadata.get("source", "") or "").strip()
    if source:
        return source
    return "Untitled" if _paper_list_language(language) == "en" else "Sans titre"


def _paper_authors(metadata: dict[str, Any]) -> str:
    return str(
        metadata.get("zotero_authors", "") or metadata.get("zotero_first_author", "") or ""
    ).strip()


def _source_label(doc: Any, fallback_index: int) -> str:
    metadata = getattr(doc, "metadata", {}) or {}
    label = str(metadata.get("ref_label", "") or "").strip().upper()
    if re.fullmatch(r"S\d+", label):
        return label
    return f"S{fallback_index}"


def _normalize_source_label(value: str) -> str:
    match = re.fullmatch(r"\s*S\s*(\d+)\s*", str(value or ""), flags=re.IGNORECASE)
    return f"S{int(match.group(1))}" if match else ""


def _ensure_summary_label(label: str, summary: str) -> str:
    text = re.sub(r"\s+", " ", str(summary or "")).strip()
    if not text:
        return ""
    if re.match(rf"^\s*{re.escape(label)}\b", text, flags=re.IGNORECASE):
        return text
    return f"{label} {text}"


def build_paper_list_markdown(
    user_query: str,
    docs: list[Any],
    *,
    summaries: dict[str, str] | None = None,
    language: str = "fr",
) -> str:
    """Render one deterministic markdown item per deduplicated paper."""
    from src.rag.query_engine import _source_snippet_for_display

    language = _paper_list_language(language)
    papers = dedupe_docs_by_paper(docs)
    if not papers:
        if language == "en":
            return (
                "No relevant papers were found in the RAG index for this query. "
                "Broaden the filters or rephrase the topic."
            )
        return (
            "Aucun article pertinent retrouve dans l'index RAG pour cette requete. "
            "Elargissez les filtres ou reformulez le sujet."
        )

    if language == "en":
        noun = "paper" if len(papers) == 1 else "papers"
        lines = [
            f"I found {len(papers)} {noun} in the expanded RAG index for: {user_query.strip()}",
            "",
        ]
    else:
        lines = [
            f"J'ai retrouve {len(papers)} article{'s' if len(papers) > 1 else ''} dans l'index RAG elargi pour: {user_query.strip()}",
            "",
        ]
    for index, doc in enumerate(papers, start=1):
        metadata = getattr(doc, "metadata", {}) or {}
        label = _source_label(doc, index)
        title = _paper_title(metadata, language=language)
        authors = _paper_authors(metadata)
        year = str(metadata.get("zotero_year", "") or "").strip()
        snippet = _source_snippet_for_display(
            str(getattr(doc, "page_content", "") or ""), limit=240
        )

        descriptor_parts: list[str] = []
        if authors:
            descriptor_parts.append(authors)
        if year:
            descriptor_parts.append(year)
        descriptor = f" ({', '.join(descriptor_parts)})" if descriptor_parts else ""

        lines.append(f"{index}. **{title}**{descriptor} [{label}]")
        summary = _ensure_summary_label(label, (summaries or {}).get(label, ""))
        if summary:
            lines.append(f"   - {summary}")
        if snippet and not summary:
            evidence_label = "Evidence" if language == "en" else "Preuve"
            lines.append(f"   - {evidence_label}: {snippet}")
        lines.append("")

    return "\n".join(lines).strip()


def _format_paper_for_summary(label: str, doc: Any, *, language: str = "fr") -> str:
    from src.rag.query_engine import _safe_relpath, _source_snippet_for_display

    language = _paper_list_language(language)
    metadata = getattr(doc, "metadata", {}) or {}
    title = _paper_title(metadata, language=language)
    authors = _paper_authors(metadata)
    year = str(metadata.get("zotero_year", "") or "").strip()
    doi = str(metadata.get("zotero_doi", "") or "").strip()
    unknown = "unknown" if language == "en" else "inconnu"
    source = _safe_relpath(str(metadata.get("source", "") or unknown))
    page = str(metadata.get("page", "?") or "?").strip()
    snippet = _source_snippet_for_display(
        str(getattr(doc, "page_content", "") or ""),
        limit=PER_PAPER_SUMMARY_CHAR_BUDGET,
    )
    if language == "en":
        meta_parts = [f"Title: {title}", f"File: {source}", f"Page: {page}"]
    else:
        meta_parts = [f"Titre: {title}", f"Fichier: {source}", f"Page: {page}"]
    if authors:
        meta_parts.append(f"{'Authors' if language == 'en' else 'Auteurs'}: {authors}")
    if year:
        meta_parts.append(f"{'Year' if language == 'en' else 'Annee'}: {year}")
    if doi:
        meta_parts.append(f"DOI: {doi}")
    if snippet:
        meta_parts.append(f"{'Excerpt' if language == 'en' else 'Extrait'}: {snippet}")
    return f"[{label}]\n" + "\n".join(meta_parts)


def build_paper_summary_prompt(
    user_query: str,
    docs: list[Any],
    *,
    language: str = "fr",
) -> tuple[str, list[str]]:
    language = _paper_list_language(language)
    labels = [_source_label(doc, index) for index, doc in enumerate(docs, start=1)]
    source_blocks = [
        _format_paper_for_summary(label, doc, language=language)
        for label, doc in zip(labels, docs, strict=False)
    ]
    if language == "en":
        prompt = (
            "The following candidate papers were found by the RAG system. Assess their "
            "relevance before presenting them as results.\n\n"
            "Strict rules:\n"
            "1. Do not omit any source.\n"
            "2. Do not add any source.\n"
            "3. Use exactly the provided labels: "
            f"{', '.join(labels)}.\n"
            "4. Return exactly one entry per label.\n"
            "5. Set relevant=true only when the title, metadata, or excerpt directly shows "
            "that the paper addresses the requested topic.\n"
            "6. A neighboring field, a generic term, or a merely possible application is "
            "not enough. For example, a paper about energy in general or superconductors "
            "is not a paper about renewable energy without explicit evidence.\n"
            "7. If evidence is absent, insufficient, or contradicts the connection, set "
            "relevant=false.\n"
            "8. Write the summary sentence in English and briefly justify the decision "
            "using only the provided information.\n"
            "9. Do not invent facts absent from the titles, metadata, or excerpts.\n\n"
            f"User query:\n{user_query.strip()}\n\n"
            "Sources:\n\n" + "\n\n---\n\n".join(source_blocks)
        )
    else:
        prompt = (
            "Voici des articles candidats trouves par le RAG. Evalue leur pertinence avant "
            "de les presenter comme des resultats.\n\n"
            "Regles strictes:\n"
            "1. N'omets aucune source.\n"
            "2. N'ajoute aucune source.\n"
            "3. Respecte exactement les labels fournis: "
            f"{', '.join(labels)}.\n"
            "4. Retourne exactement une entree par label.\n"
            "5. Fixe relevant=true uniquement si le titre, les metadonnees ou l'extrait montrent "
            "directement que l'article traite le sujet demande.\n"
            "6. Un domaine voisin, un mot generique ou une application seulement possible ne "
            "suffisent pas. Exemple: un article sur l'energie en general ou les supraconducteurs "
            "n'est pas un article sur les energies renouvelables sans preuve explicite.\n"
            "7. Si la preuve est absente, insuffisante ou contredit le lien, fixe relevant=false.\n"
            "8. La phrase summary doit justifier brievement la decision avec les seuls elements "
            "fournis.\n"
            "9. N'invente aucun fait absent des titres, metadonnees ou extraits.\n\n"
            f"Requete utilisateur:\n{user_query.strip()}\n\n"
            "Sources:\n\n" + "\n\n---\n\n".join(source_blocks)
        )
    return prompt, labels


def validate_paper_summaries(
    response: PaperListSummaryResponse,
    expected_labels: list[str],
) -> dict[str, str] | None:
    expected = [_normalize_source_label(label) for label in expected_labels]
    expected = [label for label in expected if label]
    if len(expected) != len(expected_labels):
        return None

    items = list(response.items or [])
    if len(items) != len(expected):
        return None

    seen: set[str] = set()
    summaries: dict[str, str] = {}
    for item in items:
        label = _normalize_source_label(item.label)
        if not label or label not in expected or label in seen:
            return None
        seen.add(label)
        text = re.sub(r"\s+", " ", str(item.summary or "")).strip()
        if not text:
            return None
        if item.relevant:
            summaries[label] = text

    if set(seen) != set(expected):
        return None
    return summaries


def _select_relevant_papers(
    docs: list[Any], summaries: dict[str, str]
) -> tuple[list[Any], dict[str, str]]:
    """Keep assessed-relevant papers and relabel them without citation gaps."""
    from src.rag.query_engine import _clone_document_with_metadata

    selected_docs: list[Any] = []
    selected_summaries: dict[str, str] = {}
    for doc in docs:
        old_label = _source_label(doc, len(selected_docs) + 1)
        summary = summaries.get(old_label)
        if not summary:
            continue
        new_label = f"S{len(selected_docs) + 1}"
        selected_docs.append(_clone_document_with_metadata(doc, {"ref_label": new_label}))
        summary = re.sub(
            rf"^\s*{re.escape(old_label)}\b\s*[:.\-–—]?\s*",
            "",
            summary,
            flags=re.IGNORECASE,
        ).strip()
        selected_summaries[new_label] = summary
    return selected_docs, selected_summaries


class PaperSummaries(dict[str, str]):
    """Summaries of the papers judged relevant, by label.

    ``unverified`` lists the labels of papers whose batch the model never
    answered completely: they are not relevant, only unassessed.
    """

    def __init__(self, summaries: dict[str, str] | None = None) -> None:
        super().__init__(summaries or {})
        self.unverified: list[str] = []


def generate_paper_summaries(
    user_query: str,
    docs: list[Any],
    *,
    model_name: str,
    abort_signal: Any = None,
    language: str = "fr",
) -> PaperSummaries | None:
    """Assess relevance in small batches; ``None`` when no batch could be verified.

    Each batch is validated strictly (one entry per label) and retried once; a
    batch that still fails leaves its papers unverified, never relevant.
    """
    if not docs:
        return PaperSummaries()
    summaries = PaperSummaries()
    verified_any = False
    for start in range(0, len(docs), PAPER_SUMMARY_BATCH_SIZE):
        batch = docs[start : start + PAPER_SUMMARY_BATCH_SIZE]
        result = None
        for _attempt in range(2):
            result = _summarize_paper_batch(
                user_query,
                batch,
                model_name=model_name,
                abort_signal=abort_signal,
                language=language,
            )
            if result is not None:
                break
        if result is None:
            summaries.unverified.extend(
                _source_label(doc, start + offset) for offset, doc in enumerate(batch, start=1)
            )
            continue
        verified_any = True
        summaries.update(result)
    if summaries.unverified:
        _logger.info(
            "paper list: %s could not assess %s of %s paper(s)",
            model_name,
            len(summaries.unverified),
            len(docs),
        )
    return summaries if verified_any else None


def _batch_summary_schema(labels: list[str]) -> dict[str, Any]:
    """JSON schema forcing exactly one item per label, labels limited to the batch.

    Ollama compiles the schema into a grammar: a small model can no longer
    answer with an empty or partial list.
    """
    schema = PaperListSummaryResponse.model_json_schema()
    items = schema["properties"]["items"]
    items["minItems"] = len(labels)
    items["maxItems"] = len(labels)
    schema["$defs"]["PaperListSummaryItem"]["properties"]["label"]["enum"] = list(labels)
    return schema


def _summarize_paper_batch(
    user_query: str,
    docs: list[Any],
    *,
    model_name: str,
    abort_signal: Any = None,
    language: str = "fr",
) -> dict[str, str] | None:
    # Lazy import to keep the deterministic fallback available even if provider setup fails.
    from src.llm_provider import (
        invoke_structured_chat,
        is_ollama_provider,
        stream_ollama_chat,
    )

    prompt, expected_labels = build_paper_summary_prompt(user_query, docs, language=language)
    response: Any
    try:
        if abort_signal is not None:
            abort_signal.throw_if_aborted()
        if is_ollama_provider():
            max_output_tokens = min(
                PAPER_SUMMARY_MAX_OUTPUT_TOKENS,
                PAPER_SUMMARY_BASE_OUTPUT_TOKENS + (len(docs) * PAPER_SUMMARY_TOKENS_PER_PAPER),
            )
            raw_response = "".join(
                str(chunk.get("content") or "")
                for chunk in stream_ollama_chat(
                    model_name,
                    [{"role": "user", "content": prompt}],
                    think=False,
                    temperature=0.0,
                    top_p=1.0,
                    abort_signal=abort_signal,
                    options_override={"num_predict": max_output_tokens},
                    format_schema=_batch_summary_schema(expected_labels),
                )
            ).strip()
            response = PaperListSummaryResponse.model_validate(json.loads(raw_response))
        else:
            response = invoke_structured_chat(
                model_name,
                [{"role": "user", "content": prompt}],
                response_model=PaperListSummaryResponse,
                temperature=0.0,
                top_p=1.0,
            )
    except InterruptedError:
        raise
    except Exception:
        _logger.warning("Paper relevance batch failed.", exc_info=True)
        return None
    if not isinstance(response, PaperListSummaryResponse):
        return None
    summaries = validate_paper_summaries(response, expected_labels)
    if summaries is None:
        _logger.warning(
            "Paper relevance batch rejected for %s (expected %s): %s",
            model_name,
            ", ".join(expected_labels),
            response.model_dump_json()[:400],
        )
    return summaries


def _localized(language: str, *, en: str, fr: str) -> str:
    return en if language == "en" else fr


def _unverified_note(summaries: Any, language: str) -> str:
    count = len(getattr(summaries, "unverified", None) or [])
    if not count:
        return ""
    return _localized(
        language,
        en=f"\n\nNote: the model could not assess {count} candidate(s); they are not listed.",
        fr=f"\n\nNote : le modele n'a pas pu evaluer {count} candidat(s), qui ne sont pas listes.",
    )


@dataclass
class _PaperListRun:
    """Counts and timings of one paper-list request, reported as pipeline metrics."""

    retrieved_chunks: int = 0
    candidate_papers: int = 0
    direct_evidence_papers: int = 0
    assessed_papers: int = 0
    retrieval_seconds: float = 0.0
    rerank_seconds: float = 0.0
    summary_seconds: float = 0.0
    # Retrieval and reranker events, forwarded once the answer is streamed.
    pending_metrics: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_seconds(self) -> float:
        return self.retrieval_seconds + self.rerank_seconds + self.summary_seconds

    def paper_list_metric(
        self,
        *,
        unique_papers: int,
        relevance_verified: bool,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "type": "pipeline_metrics",
            "stage": "paper_list",
            "data": {
                "duration_ms": round(self.total_seconds * 1000.0, 1),
                "retrieval_ms": round(self.retrieval_seconds * 1000.0, 1),
                "reranker_ms": round(self.rerank_seconds * 1000.0, 1),
                "summaries_ms": round(self.summary_seconds * 1000.0, 1),
                "retrieved_chunks": self.retrieved_chunks,
                "candidate_papers": self.candidate_papers,
                "direct_evidence_papers": self.direct_evidence_papers,
                "assessed_papers": self.assessed_papers,
                "unique_papers": unique_papers,
                "relevance_verified": relevance_verified,
                **extra,
            },
        }

    def summaries_metric(
        self,
        *,
        model_name: str,
        summaries: dict[str, str] | None,
        relevance_verified: bool,
        relevant_papers: int,
    ) -> dict[str, Any]:
        return {
            "type": "pipeline_metrics",
            "stage": "paper_summaries",
            "data": {
                "duration_ms": round(self.summary_seconds * 1000.0, 1),
                "model": model_name,
                "papers": self.assessed_papers,
                "summaries_generated": len(summaries or {}),
                "relevance_verified": relevance_verified,
                "relevant_papers": relevant_papers,
            },
        }


def _paper_list_answer_events(
    markdown: str,
    sources: list[dict[str, Any]],
    run: _PaperListRun,
    *,
    unique_papers: int,
    relevance_verified: bool,
    summaries_metric: dict[str, Any] | None = None,
    **paper_list_extra: Any,
):
    """Stream the answer text, its sources, then the pipeline metrics."""
    from src.rag.query_engine import _split_text_for_streaming

    for piece in _split_text_for_streaming(markdown):
        yield {"type": "token", "text": piece}
    yield {"type": "sources", "data": sources, "answer_origin": "paper_list"}
    yield from run.pending_metrics
    if summaries_metric is not None:
        yield summaries_metric
    yield run.paper_list_metric(
        unique_papers=unique_papers,
        relevance_verified=relevance_verified,
        **paper_list_extra,
    )


def _stream_author_metadata_list(
    user_query: str,
    authors: Any,
    *,
    abort_signal: Any,
    language: str,
    exclude_sources: Any = None,
):
    """List an author's papers straight from the library metadata.

    Returns False (as the generator's value) when the metadata is unavailable,
    so the caller falls back to the RAG index.
    """
    from src.library import find_library_articles_by_authors
    from src.rag import state
    from src.rag.query_engine import format_sources_api

    (records, metadata_available), lookup_seconds = yield from _stream_timed_operation(
        lambda: find_library_articles_by_authors(authors, indexed_only=True),
        stage="paper_list_metadata_retrieval",
        initial_message=_localized(
            language,
            en="Searching for the author in the library metadata...",
            fr="Recherche de l'auteur dans les metadonnees de la bibliotheque...",
        ),
        waiting_message=_localized(
            language,
            en="Searching the library metadata",
            fr="Recherche dans les metadonnees de la bibliotheque",
        ),
        abort_signal=abort_signal,
        language=language,
    )
    if not metadata_available:
        return False
    metadata_docs = [
        _library_record_to_doc(record, index) for index, record in enumerate(records, start=1)
    ]
    metadata_docs, already_listed = _exclude_listed_papers(metadata_docs, exclude_sources)
    for index, doc in enumerate(metadata_docs, start=1):
        doc.metadata["ref_label"] = f"S{index}"
    with state._state_lock:
        state.last_sources = metadata_docs
    run = _PaperListRun(
        candidate_papers=len(metadata_docs),
        direct_evidence_papers=len(metadata_docs),
        assessed_papers=len(metadata_docs),
        retrieval_seconds=lookup_seconds,
    )
    if already_listed and not metadata_docs:
        # The metadata lookup is exhaustive: nothing is left to find elsewhere.
        markdown = _localized(
            language,
            en=(
                "No other paper by this author is in the indexed library: the "
                f"{already_listed} listed above are all of them."
            ),
            fr=(
                "Aucun autre article de cet auteur dans la bibliotheque indexee : les "
                f"{already_listed} listes plus haut sont les seuls."
            ),
        )
    else:
        markdown = build_paper_list_markdown(user_query, metadata_docs, language=language)
        markdown += _already_listed_note(already_listed, language)
    yield from _paper_list_answer_events(
        markdown,
        format_sources_api(metadata_docs),
        run,
        unique_papers=len(metadata_docs),
        relevance_verified=True,
        metadata_only=True,
    )
    return True


def _index_size_label(vectorstore: Any, language: str) -> str:
    index_chunk_count = _rag_index_chunk_count(vectorstore)
    if index_chunk_count is None:
        return _localized(language, en="the available passages", fr="les passages")
    if language == "en":
        return f"{index_chunk_count:,} passages"
    return f"{index_chunk_count:,} passages".replace(",", " ")


def _stream_paper_candidates(
    run: _PaperListRun,
    retrieval_query: str,
    *,
    threshold: float,
    max_chunks: int,
    filters: Any,
    abort_signal: Any,
    language: str,
):
    """Retrieve chunks, keep one per paper with direct evidence, then rerank them.

    Returns ``(retrieved_papers, paper_candidates)`` as the generator's value.
    """
    from src.rag import state
    from src.rag.query_engine import (
        _build_reranker_metrics_event,
        _consume_pipeline_metrics,
        _get_reranker_status,
        _is_reranker_enabled,
        _rerank_documents,
        _retrieve_documents,
        _stream_reranker_warmup_status,
    )

    with state._state_lock:
        vectorstore = state.vectorstore
    index_size_label = _index_size_label(vectorstore, language)

    def retrieve() -> tuple[list[Any], list[dict[str, Any]]]:
        docs = _retrieve_documents(
            retrieval_query,
            threshold=threshold,
            max_chunks=max_chunks,
            filters=filters,
            use_reranker=False,
        )
        return docs, _consume_pipeline_metrics()

    (docs, retrieval_metrics), run.retrieval_seconds = yield from _stream_timed_operation(
        retrieve,
        stage="paper_list_retrieval",
        initial_message=_localized(
            language,
            en=(
                f"Vector search followed by lexical analysis of {index_size_label} "
                "in the RAG index..."
            ),
            fr=(
                f"Recherche vectorielle puis analyse lexicale de {index_size_label} "
                "dans l'index RAG..."
            ),
        ),
        waiting_message=_localized(
            language,
            en=f"Analyzing the RAG index across {index_size_label}",
            fr=f"Analyse de l'index RAG en cours sur {index_size_label}",
        ),
        abort_signal=abort_signal,
        language=language,
    )
    run.pending_metrics.extend(retrieval_metrics)
    run.retrieved_chunks = len(docs)
    if abort_signal is not None:
        abort_signal.throw_if_aborted()
    retrieved_papers = dedupe_docs_by_paper(docs)
    paper_candidates = [
        doc for doc in retrieved_papers if _has_direct_query_evidence(retrieval_query, doc)
    ]
    run.candidate_papers = len(retrieved_papers)

    if len(paper_candidates) > 1 and _is_reranker_enabled():
        yield from _stream_reranker_warmup_status()
        reranker_name = str(_get_reranker_status().get("model_name") or "reranker").strip()
        to_rerank = paper_candidates

        def rerank_papers() -> tuple[list[Any], dict[str, Any] | None]:
            reranked = _rerank_documents(retrieval_query, to_rerank, top_k=len(to_rerank))
            return reranked, _build_reranker_metrics_event()

        (paper_candidates, rerank_metric), run.rerank_seconds = yield from _stream_timed_operation(
            rerank_papers,
            stage="paper_list_reranker",
            initial_message=_localized(
                language,
                en=f"Reranking {len(to_rerank)} unique papers with {reranker_name}...",
                fr=f"Reranking de {len(to_rerank)} articles uniques avec {reranker_name}...",
            ),
            waiting_message=_localized(
                language,
                en=f"Reranking {len(to_rerank)} papers with {reranker_name}",
                fr=f"Reranking de {len(to_rerank)} articles avec {reranker_name}",
            ),
            abort_signal=abort_signal,
            language=language,
        )
        if rerank_metric:
            run.pending_metrics.append(rerank_metric)
    run.direct_evidence_papers = len(paper_candidates)
    return retrieved_papers, paper_candidates


# --- Whole library and exhaustive topic lists --------------------------------
#
# A list used to come from the 32-40 passages of one retrieval, one per paper,
# and only the first 12 were checked by the LLM: "list all my papers" found
# none (no topic word to match) and a topic with 63 papers showed 12.

# Words of a list request that name no topic: "Quels articles ai-je dans ma
# bibliothèque ?" asks for the library itself.
_LIBRARY_REQUEST_TERMS = frozenset(
    "mes mon ma my moi bibliotheque library zotero collection index indexe indexes indexee "
    "indexees indexed have possede possedes disponibles available combien many how number "
    "nombre montre montrer affiche afficher display voir see entiere entier complete complet "
    "whole toute ensemble ici here documents document pdf pdfs liste lister listes give".split()
)
_YEAR_TOKEN_RE = re.compile(r"^(?:19|20)\d{2}$")
# Notes, wiki pages and figure readings are not papers of the library.
_NOT_A_PAPER_TYPES = frozenset({"zotero_note", "wiki_page", "figure_ocr"})
_TOPIC_MARKER_RE = re.compile(
    r"\b(?:qui\s+)?(?:parlent|parle|traitent|traite|portent|porte|mentionnent|mentionne|"
    r"abordent|aborde|concernent|concerne)\s+(?:de|du|des|sur|d)\b|"
    r"\bau\s+sujet\s+(?:de|du|des|d)\b|\bconcernant\b|\bportant\s+sur\b|"
    r"\btraitant\s+(?:de|du|des|d)\b|\brelated\s+to\b|\bregarding\b|\babout\b|\bsur\b|\bon\b",
    re.IGNORECASE,
)
_TOPIC_VECTOR_CHUNKS = 300
_TOPIC_LEXICAL_CHUNKS = 800
_TOPIC_RERANK_TEXT_CHARS = 700
# Reranker scores (Qwen3-Reranker logits) of a paper's title and best passage,
# measured on 2026-10-14 for flux pumps, bulk superconductors, quench, AC
# losses and no-insulation coils, plus two topics absent from the library
# (best scores -1.75 and -8). Relevant papers stay within ~3 points of the
# best one; a floor keeps an absent topic from listing its nearest misses.
_RELEVANT_GAP, _RELEVANT_FLOOR = 3.0, 2.0
_RELATED_GAP, _RELATED_FLOOR = 5.0, 0.0


def _request_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]{3,}", _normalize_request_text(text))


def library_request_topic(text: str) -> tuple[set[str], list[str]]:
    """``(topic words, years)`` of a list request; no topic word means the whole library."""
    topic = {
        word
        for word in _request_words(text)
        if word not in _GENERIC_PAPER_LIST_TERMS and word not in _LIBRARY_REQUEST_TERMS
    }
    years = sorted(word for word in topic if _YEAR_TOKEN_RE.match(word))
    return topic - set(years), years


_LEADING_ARTICLE_RE = re.compile(r"^(?:les|le|la|l['’]|des|du|the|a|an)\s*", re.IGNORECASE)


def paper_list_topic(text: str) -> str:
    """The subject of "Liste tous les articles sur les pompes de flux": "pompes de flux"."""
    value = str(text or "").strip()
    match = _TOPIC_MARKER_RE.search(value)
    if match:
        tail = re.sub(r"^[\s'’:,-]+|[\s?.!]+$", "", value[match.end() :])
        tail = _LEADING_ARTICLE_RE.sub("", tail)
        if _paper_list_query_tokens(tail):
            return tail
    kept = [
        word
        for word in re.findall(r"[\wÀ-ÿ'’-]+", value)
        if _normalize_request_text(word) not in _GENERIC_PAPER_LIST_TERMS
        and _normalize_request_text(word) not in _LIBRARY_REQUEST_TERMS
    ]
    return " ".join(kept) or value


def _catalogue_key(metadata: dict[str, Any]) -> str:
    """Two copies of a PDF are one paper: same title and year."""
    title = _normalize_key(metadata.get("zotero_title"))
    if title:
        return f"title:{title}:{_normalize_key(metadata.get('zotero_year'))}"
    doi = _normalize_key(metadata.get("zotero_doi"))
    if doi:
        return f"doi:{doi}"
    return f"path:{metadata.get('rel_path') or metadata.get('source') or id(metadata)}"


def _is_listed_paper(metadata: dict[str, Any], filters: Any, excluded_keys: Any) -> bool:
    from src.rag.exclusions import is_metadata_excluded
    from src.rag.query_engine import _doc_matches_filters

    if str(metadata.get("content_type") or "") in _NOT_A_PAPER_TYPES:
        return False
    if filters and not _doc_matches_filters(metadata, filters):
        return False
    return not is_metadata_excluded(metadata, excluded_keys)


def library_papers(filters: Any = None) -> list[Any]:
    """Every indexed paper once, newest first, from the index metadata."""
    from src.rag import state
    from src.rag.exclusions import excluded_path_keys
    from src.rag.lexical_index import file_metadata

    with state._state_lock:
        index_path = str(state.runtime_db_path or "")
    excluded_keys = excluded_path_keys()
    papers: OrderedDict[str, Any] = OrderedDict()
    for metadata in file_metadata(index_path) if index_path else []:
        if _is_listed_paper(metadata, filters, excluded_keys):
            papers.setdefault(
                _catalogue_key(metadata), SimpleNamespace(page_content="", metadata=metadata)
            )

    def newest_first(doc: Any) -> tuple[int, str]:
        year = str(doc.metadata.get("zotero_year") or "").strip()
        return (-int(year) if year.isdigit() else 0, _paper_title(doc.metadata).casefold())

    return sorted(papers.values(), key=newest_first)


def _short_authors(metadata: dict[str, Any]) -> str:
    first = str(metadata.get("zotero_first_author") or "").strip()
    authors = _paper_authors(metadata)
    if first and authors and authors.strip() != first:
        return f"{first} et al."
    return first or authors


def _compact_paper_lines(docs: list[Any], *, start: int, language: str) -> list[str]:
    lines: list[str] = []
    for index, doc in enumerate(docs, start=start):
        metadata = getattr(doc, "metadata", {}) or {}
        details = ", ".join(
            part
            for part in (_short_authors(metadata), str(metadata.get("zotero_year") or "").strip())
            if part
        )
        details = f" ({details})" if details else ""
        lines.append(
            f"{index}. **{_paper_title(metadata, language=language)}**{details} "
            f"[{_source_label(doc, index)}]"
        )
    return lines


def _label_papers(docs: list[Any]) -> list[Any]:
    from src.rag.query_engine import _clone_document_with_metadata

    labelled: list[Any] = []
    for index, doc in enumerate(docs, start=1):
        metadata = {**(getattr(doc, "metadata", {}) or {}), "ref_label": f"S{index}"}
        if isinstance(doc, SimpleNamespace):
            labelled.append(SimpleNamespace(page_content=doc.page_content, metadata=metadata))
        else:
            labelled.append(_clone_document_with_metadata(doc, {"ref_label": f"S{index}"}))
    return labelled


def _filters_phrase(filters: dict[str, list[str]]) -> str:
    from src.rag.query_engine import _filter_summary

    summary = _filter_summary(filters or {})
    if not summary:
        return ""
    return f" ({summary})"


def _stream_library_catalogue(
    filters: dict[str, list[str]],
    *,
    exclude_sources: Any,
    language: str,
):
    """List the whole indexed library, without retrieval or LLM."""
    from src.rag import state
    from src.rag.query_engine import format_sources_api

    run = _PaperListRun()
    started = time.perf_counter()
    papers = library_papers(filters)
    run.retrieval_seconds = time.perf_counter() - started
    run.candidate_papers = len(papers)
    papers, already_listed = _exclude_listed_papers(papers, exclude_sources)
    papers = _label_papers(papers)
    scope = _filters_phrase(filters)
    if not papers:
        markdown = _localized(
            language,
            en=f"No indexed paper matches this request{scope}.",
            fr=f"Aucun article indexé ne correspond à cette demande{scope}.",
        )
    else:
        count = len(papers)
        lines = [
            _localized(
                language,
                en=f"Your indexed library holds {count} paper{'s' if count > 1 else ''}{scope}, newest first:",
                fr=f"Votre bibliothèque indexée contient {count} article{'s' if count > 1 else ''}{scope}, du plus récent au plus ancien :",
            ),
            "",
        ]
        year_heading = count > 15
        current_year = None
        for index, doc in enumerate(papers, start=1):
            year = str(doc.metadata.get("zotero_year") or "").strip() or _localized(
                language, en="Undated", fr="Sans date"
            )
            if year_heading and year != current_year:
                lines.extend(["", f"### {year}"] if current_year is not None else [f"### {year}"])
                current_year = year
            lines.extend(_compact_paper_lines([doc], start=index, language=language))
        markdown = "\n".join(lines)
    markdown += _already_listed_note(already_listed, language)
    with state._state_lock:
        state.last_sources = papers
    yield from _paper_list_answer_events(
        markdown,
        format_sources_api(papers),
        run,
        unique_papers=len(papers),
        relevance_verified=True,
        catalogue=True,
    )


def _topic_candidates(queries: list[str], filters: Any) -> list[Any]:
    """Papers whose passages come up for the topic: wide vector and lexical searches."""
    from src.rag import query_engine as qe
    from src.rag import state
    from src.rag.exclusions import excluded_path_keys, excluded_rel_paths
    from src.rag.lexical_index import search

    with state._state_lock:
        index_path = str(state.runtime_db_path or "")
    excluded_keys = excluded_path_keys()
    excluded_paths = excluded_rel_paths()
    papers: OrderedDict[str, Any] = OrderedDict()

    def add(doc: Any) -> None:
        metadata = doc.metadata or {}
        if _is_listed_paper(metadata, filters, excluded_keys):
            # The first passage found for a paper is its best one.
            papers.setdefault(_catalogue_key(metadata), qe._sanitize_document(doc))

    for query in queries:
        for doc, _score in qe._collect_scored_docs(
            query, k=_TOPIC_VECTOR_CHUNKS, excluded_paths=excluded_paths
        ):
            add(doc)
    if index_path:
        for query in queries:
            for doc in search(
                index_path,
                query,
                limit=_TOPIC_LEXICAL_CHUNKS,
                stopwords=qe._LEXICAL_SEARCH_STOPWORDS,
            ):
                add(doc)
    return list(papers.values())


_TOPIC_TRANSLATION_PROMPT = (
    "A researcher asks for the papers of their library on the topic below, written in "
    "French. Titles of papers of that library that may be related:\n{titles}\n\n"
    "Give the English wording of the topic, with the technical terms these titles use "
    "when they name the same thing. Answer with the wording only, on one line.\n\n"
    "Topic: {topic}"
)
_TOPIC_TRANSLATION_TITLES = 15


def _valid_topic_translation(value: str) -> str:
    text = str(value or "").strip().splitlines()[0] if str(value or "").strip() else ""
    text = re.sub(r"^(?:translation|english)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = text.strip(" \"'`«»“”.")
    return text if text and len(text.split()) <= 12 else ""


def english_topic(topic: str, model_name: str, titles: list[str] | tuple[str, ...] = ()) -> str:
    """English wording of a French topic, as the library's paper titles phrase it.

    Word for word, the offline translator wrote "flow pumps" for "pompes de
    flux" and "mass superconductors" for "supraconducteurs massifs", and the
    reranker then missed half the papers; the chat model alone did no better
    ("Flow pumps", "Isolation-free coils"). Shown the titles the French query
    finds, it uses their terms. The model is loaded already (it answers the
    chat), so this costs a few tenths of a second; the offline translator is
    the fallback.
    """
    from src.llm_provider import (
        create_chat_llm,
        invoke_ollama_generate,
        invoke_text,
        is_lmstudio_provider,
        is_remote_provider,
    )
    from src.rag.reranker import translate_french_to_english

    listed = "\n".join(f"- {title}" for title in list(titles)[:_TOPIC_TRANSLATION_TITLES])
    prompt = _TOPIC_TRANSLATION_PROMPT.format(topic=topic, titles=listed or "-")
    translated = ""
    if model_name:
        try:
            if is_remote_provider() or is_lmstudio_provider():
                llm = create_chat_llm(
                    model_name, temperature=0.0, max_output_tokens=32, max_retries=0
                )
                translated = _valid_topic_translation(invoke_text(llm, prompt))
            else:
                _thinking, raw = invoke_ollama_generate(
                    model_name,
                    prompt,
                    think=False,
                    temperature=0.0,
                    options_override={"num_predict": 32},
                    request_timeout_seconds=20,
                )
                translated = _valid_topic_translation(raw)
        except Exception:
            _logger.warning("Topic translation by the chat model failed", exc_info=True)
    if not translated:
        translated = translate_french_to_english(topic)
    return "" if translated.casefold() == topic.casefold() else translated


def _rank_topic_papers(query: str, papers: list[Any]) -> list[tuple[Any, float]] | None:
    """Reranker scores of each paper's title and best passage; None without a reranker."""
    from src.rag.reranker import consume_last_metrics, rerank_texts

    if not papers:
        return []
    texts = [
        f"{_paper_title(doc.metadata or {})}\n\n{str(doc.page_content or '')[:_TOPIC_RERANK_TEXT_CHARS]}"
        for doc in papers
    ]
    ranked = rerank_texts(query, texts, top_k=len(texts))
    metrics = consume_last_metrics() or {}
    if not metrics.get("applied"):
        return None
    return [(papers[index], score) for index, score in ranked]


def _stream_topic_paper_list(
    topic: str,
    filters: dict[str, list[str]],
    *,
    model_name: str,
    exclude_sources: Any,
    abort_signal: Any,
    language: str,
):
    """Every paper of the library on a topic, in two tiers. False without a reranker."""
    from src.rag import state
    from src.rag.query_engine import (
        _build_reranker_metrics_event,
        _has_reranker_predict_error,
        _is_reranker_enabled,
        format_sources_api,
    )

    # The tiers rest on reranker scores: without one, the LLM-checked list.
    if not _is_reranker_enabled() or _has_reranker_predict_error():
        return False
    run = _PaperListRun()

    def find_candidates() -> tuple[list[Any], str]:
        found = _topic_candidates([topic], filters)
        if language != "fr":
            return found, ""
        english = english_topic(
            topic, model_name, [_paper_title(doc.metadata or {}) for doc in found]
        )
        if not english:
            return found, ""
        merged: OrderedDict[str, Any] = OrderedDict(
            (_catalogue_key(doc.metadata or {}), doc) for doc in found
        )
        for doc in _topic_candidates([english], filters):
            merged.setdefault(_catalogue_key(doc.metadata or {}), doc)
        return list(merged.values()), english

    (papers, english), run.retrieval_seconds = yield from _stream_timed_operation(
        find_candidates,
        stage="paper_list_retrieval",
        initial_message=_localized(
            language,
            en=f"Looking for every paper of the library about « {topic} »...",
            fr=f"Recherche de tous les articles de la bibliothèque sur « {topic} »...",
        ),
        waiting_message=_localized(
            language, en="Searching the library", fr="Recherche dans la bibliothèque"
        ),
        abort_signal=abort_signal,
        language=language,
    )
    run.candidate_papers = len(papers)
    ranked, run.rerank_seconds = yield from _stream_timed_operation(
        lambda: _rank_topic_papers(english or topic, papers),
        stage="paper_list_reranker",
        initial_message=_localized(
            language,
            en=f"Judging the relevance of {len(papers)} papers...",
            fr=f"Évaluation de la pertinence de {len(papers)} articles...",
        ),
        waiting_message=_localized(
            language, en="Judging relevance", fr="Évaluation de la pertinence"
        ),
        abort_signal=abort_signal,
        language=language,
    )
    if ranked is None:
        return False
    rerank_metric = _build_reranker_metrics_event()
    if rerank_metric:
        run.pending_metrics.append(rerank_metric)
    best = ranked[0][1] if ranked else float("-inf")
    relevant_cut = max(best - _RELEVANT_GAP, _RELEVANT_FLOOR)
    related_cut = max(best - _RELATED_GAP, _RELATED_FLOOR)
    relevant = [doc for doc, score in ranked if score >= relevant_cut]
    related = [doc for doc, score in ranked if related_cut <= score < relevant_cut]
    relevant, listed_before = _exclude_listed_papers(relevant, exclude_sources)
    related, listed_related = _exclude_listed_papers(related, exclude_sources)
    already_listed = listed_before + listed_related
    run.direct_evidence_papers = run.assessed_papers = len(relevant) + len(related)

    labelled = _label_papers([*relevant, *related])
    relevant_docs, related_docs = labelled[: len(relevant)], labelled[len(relevant) :]
    scope = _filters_phrase(filters)
    if not labelled and already_listed:
        markdown = _localized(
            language,
            en=f"No other paper on « {topic} »: the {already_listed} listed above are all of them.",
            fr=f"Aucun autre article sur « {topic} » : les {already_listed} listés plus haut sont tous ceux que j'ai trouvés.",
        )
        already_listed = 0
    elif not labelled:
        markdown = _localized(
            language,
            en=f"No paper of your indexed library seems to deal with « {topic} »{scope}.",
            fr=f"Aucun article de votre bibliothèque indexée ne semble traiter de « {topic} »{scope}.",
        )
    else:
        lines: list[str] = []
        if relevant_docs:
            count = len(relevant_docs)
            lines.extend(
                [
                    _localized(
                        language,
                        en=f"{count} paper{'s' if count > 1 else ''} of your library deal{'' if count > 1 else 's'} with « {topic} »{scope}:",
                        fr=f"{count} article{'s' if count > 1 else ''} de votre bibliothèque traite{'nt' if count > 1 else ''} de « {topic} »{scope} :",
                    ),
                    "",
                    *_compact_paper_lines(relevant_docs, start=1, language=language),
                ]
            )
        if related_docs:
            lines.extend(
                [
                    "",
                    _localized(
                        language,
                        en=f"**Possibly related ({len(related_docs)})**, where the topic is less central:",
                        fr=f"**Peut-être liés ({len(related_docs)})**, où le sujet est moins central :",
                    ),
                    "",
                    *_compact_paper_lines(
                        related_docs, start=len(relevant_docs) + 1, language=language
                    ),
                ]
            )
        markdown = "\n".join(lines).strip()
    markdown += _already_listed_note(already_listed, language)
    with state._state_lock:
        state.last_sources = labelled
    yield from _paper_list_answer_events(
        markdown,
        format_sources_api(labelled),
        run,
        unique_papers=len(labelled),
        relevance_verified=True,
        relevant_papers=len(relevant_docs),
        related_papers=len(related_docs),
        best_score=round(best, 2) if ranked else None,
    )
    return True


def stream_paper_list(
    user_query: str,
    model_name: str,
    threshold: float,
    *,
    tags: Any = None,
    collections: Any = None,
    target_chunks: int = DEFAULT_PAPER_LIST_CHUNKS,
    abort_signal: Any = None,
    language: str = "fr",
    exclude_sources: Any = None,
):
    """Generator yielding status / token / sources events for paper-list mode.

    ``exclude_sources`` are the sources of earlier lists in the conversation:
    "d'autres ?" asks for papers not shown yet.
    """
    language = _paper_list_language(language)
    if abort_signal is not None:
        abort_signal.throw_if_aborted()
    incomplete_message = incomplete_paper_list_request_message(user_query, language=language)
    if incomplete_message:
        yield {"type": "token", "text": incomplete_message}
        yield {"type": "sources", "data": [], "answer_origin": "paper_list"}
        return

    from src.rag import state
    from src.rag.query_engine import (
        _RAG_INIT_LOCK,
        _build_anchor_event,
        _clone_document_with_metadata,
        _extract_inline_filters,
        _merge_filter_maps,
        _prepare_explicit_filters,
        format_sources_api,
        init_rag,
    )

    explicit_filters = _prepare_explicit_filters(tags=tags, collections=collections)
    cleaned_message, inline_filters = _extract_inline_filters(user_query)
    filters = _merge_filter_maps(explicit_filters, inline_filters)
    retrieval_query = _retrieval_query_for_filters(
        user_query=user_query,
        cleaned_message=cleaned_message,
        filters=filters,
    )

    if _is_author_metadata_only_query(cleaned_message, filters):
        answered = yield from _stream_author_metadata_list(
            user_query,
            filters["authors"],
            abort_signal=abort_signal,
            language=language,
            exclude_sources=exclude_sources,
        )
        if answered:
            return

    with _RAG_INIT_LOCK:
        with state._state_lock:
            needs_init = state.document_chain is None or state.vectorstore is None
        if needs_init:
            ok, msg = init_rag(model_name, threshold, force_reload=False)
            if not ok:
                yield {"type": "error", "msg": msg}
                return

    topic_words, years = library_request_topic(cleaned_message or user_query)
    if not topic_words:
        catalogue_filters = dict(filters)
        if years:
            catalogue_filters["years"] = [*catalogue_filters.get("years", []), *years]
        yield from _stream_library_catalogue(
            catalogue_filters, exclude_sources=exclude_sources, language=language
        )
        return
    answered = yield from _stream_topic_paper_list(
        paper_list_topic(cleaned_message or user_query),
        filters,
        model_name=model_name,
        exclude_sources=exclude_sources,
        abort_signal=abort_signal,
        language=language,
    )
    if answered:
        return

    # Without a reranker: the passages of one retrieval, checked by the LLM.
    run = _PaperListRun()
    retrieved_papers, paper_candidates = yield from _stream_paper_candidates(
        run,
        retrieval_query,
        threshold=threshold,
        max_chunks=max(
            8,
            min(
                FOLLOWUP_PAPER_LIST_CHUNKS if exclude_sources else MAX_PAPER_LIST_CHUNKS,
                int(target_chunks),
            ),
        ),
        filters=filters,
        abort_signal=abort_signal,
        language=language,
    )
    paper_candidates, already_listed = _exclude_listed_papers(paper_candidates, exclude_sources)
    retrieved_papers, _ = _exclude_listed_papers(retrieved_papers, exclude_sources)
    paper_docs = [
        _clone_document_with_metadata(doc, {"ref_label": f"S{index}"})
        for index, doc in enumerate(paper_candidates, start=1)
    ]

    if not paper_docs:
        if retrieved_papers:
            markdown = _localized(
                language,
                en=(
                    "No directly relevant paper was confirmed in the RAG index for this "
                    "query. Closely related candidates without direct evidence of the topic "
                    "were excluded."
                ),
                fr=(
                    "Aucun article directement pertinent n'a ete confirme dans l'index RAG "
                    "pour cette requete. Les candidats proches mais sans preuve directe du "
                    "sujet ont ete exclus."
                ),
            )
        else:
            markdown = build_paper_list_markdown(
                cleaned_message or user_query, [], language=language
            )
        markdown += _already_listed_note(already_listed, language)
        with state._state_lock:
            state.last_sources = []
        yield from _paper_list_answer_events(
            markdown, [], run, unique_papers=0, relevance_verified=True
        )
        return

    assessed_docs = paper_docs[:PAPER_SUMMARY_MAX_PAPERS]
    run.assessed_papers = len(assessed_docs)
    summaries, run.summary_seconds = yield from _stream_timed_operation(
        lambda: generate_paper_summaries(
            cleaned_message or user_query,
            assessed_docs,
            model_name=model_name,
            abort_signal=abort_signal,
            language=language,
        ),
        stage="paper_list_summaries",
        initial_message=_localized(
            language,
            en=(
                f"{len(paper_docs)} candidate paper(s) found. Checking the relevance of "
                f"the top {len(assessed_docs)} candidates with {model_name}..."
            ),
            fr=(
                f"{len(paper_docs)} article(s) candidats retrouves. "
                f"Verification de la pertinence des {len(assessed_docs)} meilleurs "
                f"candidats avec {model_name}..."
            ),
        ),
        waiting_message=_localized(
            language,
            en=f"Checking relevance with {model_name}",
            fr=f"Verification de la pertinence avec {model_name}",
        ),
        abort_signal=abort_signal,
        language=language,
    )
    if abort_signal is not None:
        abort_signal.throw_if_aborted()

    relevance_verified = summaries is not None
    if summaries is None:
        relevant_docs: list[Any] = []
        relevant_summaries: dict[str, str] = {}
        empty_message = _localized(
            language,
            en=(
                "Candidate papers were found, but their relevance could not be verified. "
                "No result is presented as relevant. Try again or rephrase the topic."
            ),
            fr=(
                "Des articles candidats ont ete retrouves, mais leur pertinence n'a pas pu "
                "etre verifiee. Aucun resultat n'est presente comme pertinent. Reessayez ou "
                "reformulez le sujet."
            ),
        )
    else:
        relevant_docs, relevant_summaries = _select_relevant_papers(assessed_docs, summaries)
        empty_message = _localized(
            language,
            en=(
                "No directly relevant paper was confirmed in the RAG index for this query. "
                "Closely related but off-topic candidates were excluded."
            ),
            fr=(
                "Aucun article directement pertinent n'a ete confirme dans l'index RAG pour "
                "cette requete. Les candidats proches mais hors sujet ont ete exclus."
            ),
        )

    empty_message += _unverified_note(summaries, language)
    empty_message += _already_listed_note(already_listed, language)
    total_seconds = run.total_seconds
    summaries_metric = run.summaries_metric(
        model_name=model_name,
        summaries=summaries if not relevant_docs else relevant_summaries,
        relevance_verified=relevance_verified,
        relevant_papers=len(relevant_docs),
    )
    if not relevant_docs:
        with state._state_lock:
            state.last_sources = []
        yield {
            "type": "status",
            "stage": "paper_list_complete",
            "elapsed_seconds": round(total_seconds, 1),
            "msg": _localized(
                language,
                en=f"Verification complete: no relevant paper confirmed in {total_seconds:.1f}s.",
                fr=(
                    "Verification terminee: aucun article pertinent confirme en "
                    f"{total_seconds:.1f}s."
                ),
            ),
        }
        yield from _paper_list_answer_events(
            empty_message,
            [],
            run,
            unique_papers=0,
            relevance_verified=relevance_verified,
            summaries_metric=summaries_metric,
        )
        return

    yield {
        "type": "status",
        "stage": "paper_list_complete",
        "elapsed_seconds": round(total_seconds, 1),
        "msg": _localized(
            language,
            en=(
                f"List ready: {len(relevant_docs)} paper(s) in {total_seconds:.1f}s "
                f"(index {run.retrieval_seconds:.1f}s, reranking {run.rerank_seconds:.1f}s, "
                f"summaries {run.summary_seconds:.1f}s)."
            ),
            fr=(
                f"Liste prete: {len(relevant_docs)} article(s) en {total_seconds:.1f}s "
                f"(index {run.retrieval_seconds:.1f}s, reranking {run.rerank_seconds:.1f}s, "
                f"resumes {run.summary_seconds:.1f}s)."
            ),
        ),
    }
    markdown = build_paper_list_markdown(
        cleaned_message or user_query,
        relevant_docs,
        summaries=relevant_summaries,
        language=language,
    )
    markdown += _unverified_note(summaries, language)
    markdown += _already_listed_note(already_listed, language)
    if len(paper_candidates) > len(assessed_docs):
        markdown += _localized(
            language,
            en=(
                "\n\nNote: only the top "
                f"{len(assessed_docs)} candidates could be verified. "
                "This cautious list is therefore not exhaustive."
            ),
            fr=(
                "\n\nNote: seuls les "
                f"{len(assessed_docs)} meilleurs candidats ont pu etre verifies. "
                "Cette liste prudente n'est donc pas exhaustive."
            ),
        )
    with state._state_lock:
        state.last_sources = relevant_docs

    yield from _paper_list_answer_events(
        markdown,
        format_sources_api(relevant_docs),
        run,
        unique_papers=len(relevant_docs),
        relevance_verified=relevance_verified,
        summaries_metric=summaries_metric,
    )
    anchor_event = _build_anchor_event(markdown, relevant_docs)
    if anchor_event:
        yield anchor_event


__all__ = [
    "DEFAULT_PAPER_LIST_CHUNKS",
    "FOLLOWUP_PAPER_LIST_CHUNKS",
    "MAX_PAPER_LIST_CHUNKS",
    "PaperListSummaryItem",
    "PaperListSummaryResponse",
    "build_paper_list_markdown",
    "build_paper_summary_prompt",
    "dedupe_docs_by_paper",
    "generate_paper_summaries",
    "incomplete_paper_list_request_message",
    "stream_paper_list",
    "validate_paper_summaries",
]
