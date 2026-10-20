"""Literature review pipeline.

Two-pass synthesis from a wide retrieval:

  1. **Group by paper** — chunks are clustered by ``article_id`` so each
     paper contributes one cohesive context block.
  2. **Structured synthesis** — a single LLM call (Pydantic-typed output)
     produces a chronologie / écoles de pensée / désaccords / synthèse.

The output is a ``LiteratureReview`` Pydantic model rendered as Markdown
for the chat UI. Citations refer to ``[Sn]`` labels matching the order in
the ``sources`` list returned alongside the review.

This module reuses ``query_engine._retrieve_documents`` for retrieval and
``llm_provider.invoke_structured_chat`` for the synthesis call. Wide
retrieval is configurable but capped at ``MAX_REVIEW_CHUNKS`` to keep
LLM token usage predictable.
"""

from __future__ import annotations

import logging
import math
import re
from collections import OrderedDict
from functools import lru_cache
from types import GenericAlias
from typing import Any

from pydantic import BaseModel, Field, create_model

_logger = logging.getLogger(__name__)

MAX_REVIEW_CHUNKS = 40
DEFAULT_REVIEW_CHUNKS = 24
MAX_PAPERS_IN_PROMPT = 16
PER_PAPER_CHAR_BUDGET = 1500

# Papers that do not fit one context window are synthesised in batches and
# merged. Each batch is a full LLM round-trip, so the cap bounds latency on a
# local model; beyond it the lowest-ranked papers are dropped.
MAX_REVIEW_BATCHES = 4
_CHARS_PER_TOKEN = 3.2
# Ollama's own default when no num_ctx reaches the model.
_FALLBACK_CONTEXT_TOKENS = 4096
_REMOTE_CONTEXT_TOKENS = 8192
_CONTEXT_INPUT_RATIO = 0.80
# Room for the JSON schema, the generation envelope, and the answer itself.
_OUTPUT_RESERVE_TOKENS = 1100
_MIN_INPUT_BUDGET_TOKENS = 600
_BLOCK_SEPARATOR_TOKENS = 8


# --- Output language --------------------------------------------------------
#
# Every Ollama structured call is wrapped by ``llm_provider`` in an English
# framing prompt, and the JSON schema travels with the call as well.  A French
# request therefore used to come back with English prose while ``to_markdown``
# still emitted French headings.  The language resolved here is threaded
# through the instructions, the schema descriptions and the Markdown renderer
# so the whole visible review shares one language.
_DEFAULT_REVIEW_LANGUAGE = "fr"

_FRENCH_DIACRITIC_RE = re.compile(r"[àâçéèêëîïôùûüÿœ]", re.IGNORECASE)

# Directives that name the output language, e.g. "réponds en anglais" or
# "..., in English.".  Deliberately narrow: a bare adjective ("littérature
# anglaise", "English literature") describes the topic, not the output.
_LANGUAGE_DIRECTIVE_VERBS = (
    r"(?:r[eé]pond(?:s|ez)?|r[eé]dig(?:e|ez)|[eé]cri(?:s|s|vez)|rends|rendez|fais|faites|"
    r"produis|produisez|answer|respond|reply|write|generate|produce|draft)"
)
_LANGUAGE_DIRECTIVE_SPLIT = r"(?:^|[.,;:!?]\s*|\bet\b\s+|\band\b\s+|\bpuis\b\s+|\bthen\b\s+)"
_LANGUAGE_NAMES = {
    "en": r"(?:in\s+english|en\s+anglais)",
    "fr": r"(?:in\s+french|en\s+fran[cç]ais)",
}
_EXPLICIT_LANGUAGE_PATTERNS = {
    code: re.compile(
        rf"(?:{_LANGUAGE_DIRECTIVE_VERBS}[^.\n]{{0,60}}?{name})"
        rf"|(?:{_LANGUAGE_DIRECTIVE_SPLIT}{name}\b)",
        re.IGNORECASE,
    )
    for code, name in _LANGUAGE_NAMES.items()
}

_OUTPUT_LANGUAGE_RULES = {
    "fr": (
        "Langue de sortie : rédige toute la revue visible en français, y compris les titres "
        "de sections, les thèmes, la chronologie et la synthèse, même si le schéma JSON, les "
        "consignes d'encadrement ou les sources sont dans une autre langue. Ne mélange jamais "
        "deux langues dans une même réponse."
    ),
    "en": (
        "Output language: write the whole visible review in English, including section titles, "
        "themes, timeline entries and synthesis, even when the JSON schema, the framing "
        "instructions or the sources are in another language. Never mix two languages in one "
        "answer."
    ),
}

_SECTION_LABELS = {
    "fr": {
        "overview": "Vue d'ensemble",
        "chronology": "Chronologie",
        "themes": "Écoles de pensée",
        "theme_fallback": "Thème",
        "disagreements": "Désaccords identifiés",
        "synthesis": "Synthèse",
    },
    "en": {
        "overview": "Overview",
        "chronology": "Timeline",
        "themes": "Schools of Thought",
        "theme_fallback": "Theme",
        "disagreements": "Identified Disagreements",
        "synthesis": "Synthesis",
    },
}

_REVIEW_STRINGS = {
    "fr": {
        "search": "Recherche élargie pour la revue de littérature…",
        "no_docs": (
            "Aucune source pertinente trouvée dans la bibliothèque pour cette requête. "
            "Élargissez les filtres ou reformulez."
        ),
        "retrieved": "{count} passages récupérés, synthèse en cours…",
        "batch": (
            "Contexte limité : synthèse par lots, lot {index}/{total} ({papers} articles)…"
        ),
        "merge": "Fusion des {count} synthèses partielles…",
        "merge_partial": "### Synthèse partielle {index}",
        "merge_question": "Question utilisateur:",
        "merge_index": "Index des sources:",
        "merge_parts": "Synthèses partielles à fusionner:",
        "synthesis_failed": "Synthèse impossible: {error}",
        "empty": "La synthèse est vide.",
    },
    "en": {
        "search": "Broadening the search for the literature review…",
        "no_docs": (
            "No relevant source found in the library for this request. "
            "Widen the filters or rephrase the question."
        ),
        "retrieved": "{count} passages retrieved, synthesising…",
        "batch": "Limited context: batched synthesis, batch {index}/{total} ({papers} papers)…",
        "merge": "Merging the {count} partial syntheses…",
        "merge_partial": "### Partial synthesis {index}",
        "merge_question": "User question:",
        "merge_index": "Source index:",
        "merge_parts": "Partial syntheses to merge:",
        "synthesis_failed": "Synthesis failed: {error}",
        "empty": "The synthesis is empty.",
    },
}


def normalize_review_language(value: Any) -> str:
    """Return ``"fr"``/``"en"`` for a known language code, else ``""``."""
    code = str(value or "").strip().lower().replace("_", "-")
    if code.startswith("fr"):
        return "fr"
    if code.startswith("en"):
        return "en"
    return ""


def requested_language_in_text(text: str) -> str:
    """Return the language explicitly asked for in the request, else ``""``."""
    value = str(text or "")
    for code in ("en", "fr"):
        if _EXPLICIT_LANGUAGE_PATTERNS[code].search(value):
            return code
    return ""


def _request_text_language(text: str) -> str:
    """Infer fr/en from the request wording; ``""`` when it is ambiguous."""
    value = str(text or "")
    try:
        from src.rag.reranker import _language_marker_counts

        french, english = _language_marker_counts(value)
    except Exception:
        _logger.debug("Recovered from an error in _request_text_language.", exc_info=True)
        return ""
    if _FRENCH_DIACRITIC_RE.search(value) and french >= 1:
        return "fr"
    if french >= 2 and french > english:
        return "fr"
    if english >= 2 and english > french:
        return "en"
    return ""


def _settings_review_language() -> str:
    try:
        from src.app_settings import get_app_settings
        from src.response_language import current_response_language

        return normalize_review_language(
            current_response_language() or get_app_settings().get("ui_language")
        )
    except Exception:
        _logger.debug("Recovered from an error in _settings_review_language.", exc_info=True)
        return ""


def resolve_review_language(user_query: str, language: Any = None) -> str:
    """Resolve the output language of one literature review.

    Precedence: a language explicitly asked for inside the request text, then
    the language the request itself is written in, then the caller-supplied
    language (route or UI preference), then the stored UI language, and
    finally French as the project default.
    """
    directive = requested_language_in_text(user_query)
    if directive:
        return directive
    detected = _request_text_language(user_query)
    if detected:
        return detected
    explicit = normalize_review_language(language)
    if explicit:
        return explicit
    return _settings_review_language() or _DEFAULT_REVIEW_LANGUAGE


def _review_language(user_query: str, language: Any = None) -> str:
    return normalize_review_language(resolve_review_language(user_query, language)) or (
        _DEFAULT_REVIEW_LANGUAGE
    )


class ReviewTheme(BaseModel):
    title: str = Field(..., description="Libellé court de cette école de pensée / de ce thème.")
    summary: str = Field(
        ...,
        description=(
            "Ce que ce groupe d'articles défend, en 2 à 4 phrases. Cite les sources sous "
            "la forme [Sn]."
        ),
    )
    sources: list[str] = Field(
        default_factory=list,
        description="Labels des sources (par ex. 'S1', 'S3') qui appuient ce thème.",
    )


class ReviewDisagreement(BaseModel):
    point: str = Field(..., description="L'affirmation contestée ou la question ouverte.")
    positions: str = Field(
        ...,
        description="Résumé des positions opposées, avec les citations [Sn].",
    )


class LiteratureReview(BaseModel):
    overview: str = Field(
        ...,
        description=(
            "Aperçu en 2 ou 3 phrases du domaine tel que représenté par ces sources."
        ),
    )
    chronology: list[str] = Field(
        default_factory=list,
        description=(
            "Chronologie à puces. Chaque entrée tient sur une ligne courte de la forme "
            "'ANNÉE — apport ou jalon [Sn]'. Tri par année croissante."
        ),
    )
    themes: list[ReviewTheme] = Field(
        default_factory=list,
        description="2 à 5 écoles de pensée / thèmes distincts présents dans le corpus.",
    )
    disagreements: list[ReviewDisagreement] = Field(
        default_factory=list,
        description="Contradictions ou débats ouverts identifiés entre les sources.",
    )
    synthesis: str = Field(
        ...,
        description=(
            "Synthèse finale de 3 à 5 phrases qui répond à la question de l'utilisateur, "
            "avec les citations [Sn]."
        ),
    )

    def to_markdown(self, language: str = _DEFAULT_REVIEW_LANGUAGE) -> str:
        labels = _SECTION_LABELS[
            normalize_review_language(language) or _DEFAULT_REVIEW_LANGUAGE
        ]
        out: list[str] = []
        if self.overview.strip():
            out.append(f"## {labels['overview']}\n\n" + self.overview.strip())
        if self.chronology:
            lines = [f"## {labels['chronology']}"]
            for entry in self.chronology:
                cleaned = str(entry or "").strip()
                if not cleaned:
                    continue
                if not cleaned.startswith("- "):
                    cleaned = f"- {cleaned}"
                lines.append(cleaned)
            if len(lines) > 1:
                out.append("\n".join(lines))
        if self.themes:
            lines = [f"## {labels['themes']}"]
            for theme in self.themes:
                title = theme.title.strip() or labels["theme_fallback"]
                summary = theme.summary.strip()
                if not summary:
                    continue
                lines.append(f"\n### {title}\n\n{summary}")
            if len(lines) > 1:
                out.append("\n".join(lines))
        if self.disagreements:
            lines = [f"## {labels['disagreements']}"]
            for disagreement in self.disagreements:
                point = disagreement.point.strip()
                positions = disagreement.positions.strip()
                if not point and not positions:
                    continue
                bullet = f"- **{point}** — {positions}" if point else f"- {positions}"
                lines.append(bullet)
            if len(lines) > 1:
                out.append("\n".join(lines))
        if self.synthesis.strip():
            out.append(f"## {labels['synthesis']}\n\n" + self.synthesis.strip())
        return "\n\n".join(out).strip()


_SCHEMA_DESCRIPTIONS = {
    "en": {
        "theme_title": "Short label for this school of thought / theme.",
        "theme_summary": (
            "What this group of papers argues, in 2-4 sentences. Cite sources as [Sn]."
        ),
        "theme_sources": "Source labels (e.g. 'S1', 'S3') backing this theme.",
        "disagreement_point": "The contested claim or open question.",
        "disagreement_positions": "Summary of the conflicting positions, with [Sn] citations.",
        "overview": "2-3 sentence overview of the field as represented by these sources.",
        "chronology": (
            "Bulleted timeline. Each entry is one short line in the form "
            "'YEAR — claim or milestone [Sn]'. Sort ascending by year."
        ),
        "themes": "2-5 distinct schools of thought / themes present in the corpus.",
        "disagreements": "Identified contradictions or open debates between sources.",
        "synthesis": (
            "Final 3-5 sentence synthesis answering the user's query, with [Sn] citations."
        ),
    },
}


def _list_of(model: type[BaseModel]) -> Any:
    """``list[model]`` for a class built at runtime.

    ``list[<variable>]`` is not a valid static type expression, but it is what
    pydantic needs at runtime, so the alias is built explicitly.
    """
    return GenericAlias(list, (model,))


@lru_cache(maxsize=4)
def _review_response_models(
    language: str,
) -> tuple[type[BaseModel], type[BaseModel], type[BaseModel]]:
    """Response models whose JSON-schema descriptions match the output language.

    French is the module default, so the static classes already carry French
    descriptions and are returned as-is. Other languages get subclasses with
    translated field descriptions, which keeps ``isinstance`` checks against
    :class:`LiteratureReview` valid for callers.
    """
    if language != "en":
        return ReviewTheme, ReviewDisagreement, LiteratureReview

    text = _SCHEMA_DESCRIPTIONS["en"]
    theme = create_model(
        "ReviewTheme",
        __base__=ReviewTheme,
        title=(str, Field(..., description=text["theme_title"])),
        summary=(str, Field(..., description=text["theme_summary"])),
        sources=(list[str], Field(default_factory=list, description=text["theme_sources"])),
    )
    disagreement = create_model(
        "ReviewDisagreement",
        __base__=ReviewDisagreement,
        point=(str, Field(..., description=text["disagreement_point"])),
        positions=(str, Field(..., description=text["disagreement_positions"])),
    )
    review = create_model(
        "LiteratureReview",
        __base__=LiteratureReview,
        overview=(str, Field(..., description=text["overview"])),
        chronology=(list[str], Field(default_factory=list, description=text["chronology"])),
        themes=(
            _list_of(theme),
            Field(default_factory=list, description=text["themes"]),
        ),
        disagreements=(
            _list_of(disagreement),
            Field(default_factory=list, description=text["disagreements"]),
        ),
        synthesis=(str, Field(..., description=text["synthesis"])),
    )
    return theme, disagreement, review


def _group_chunks_by_paper(docs: list[Any]) -> OrderedDict[str, list[Any]]:
    grouped: OrderedDict[str, list[Any]] = OrderedDict()
    for doc in docs:
        metadata = getattr(doc, "metadata", {}) or {}
        key = (
            str(metadata.get("article_id", "")).strip()
            or str(metadata.get("source", "")).strip()
            or str(id(doc))
        )
        grouped.setdefault(key, []).append(doc)
    return grouped


def _format_paper_block(label: str, docs: list[Any]) -> str:
    if not docs:
        return ""
    metadata = getattr(docs[0], "metadata", {}) or {}
    title = str(metadata.get("zotero_title", "") or "").strip() or "Sans titre"
    authors = str(metadata.get("zotero_authors", "") or metadata.get("zotero_first_author", "") or "").strip()
    year = str(metadata.get("zotero_year", "") or "").strip()
    header_parts = [f"[{label}]", title]
    if authors:
        header_parts.append(f"— {authors}")
    if year:
        header_parts.append(f"({year})")
    header = " ".join(header_parts)

    body_parts: list[str] = []
    used = 0
    for doc in docs:
        text = str(getattr(doc, "page_content", "") or "").strip()
        if not text:
            continue
        remaining = PER_PAPER_CHAR_BUDGET - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining].rstrip() + "…"
        body_parts.append(text)
        used += len(text)
    body = "\n".join(body_parts)
    return f"{header}\n{body}"


def _review_instructions(language: str) -> str:
    language = normalize_review_language(language) or _DEFAULT_REVIEW_LANGUAGE
    if language == "en":
        return (
            "You are a literature-review assistant. Using the sources below, produce a "
            "structured review that answers the user's question.\n\n"
            "Strict rules:\n"
            "1. ALWAYS cite your claims with the [S1], [S2], etc. labels of the supplied sources.\n"
            "2. Do NOT invent any fact that is absent from the sources. If a dimension is missing, say so.\n"
            "3. Identify genuine disagreements between sources when they exist (do not fabricate controversy).\n"
            "4. The timeline must be based on the real years given in the source headers.\n"
            "5. Themes / schools of thought must group sources that defend a similar position.\n"
            "6. Stay sober and factual. No gratuitous superlatives.\n"
            f"7. {_OUTPUT_LANGUAGE_RULES['en']}\n"
        )
    return (
        "Tu es un assistant de revue de littérature. À partir des sources ci-dessous, "
        "produis une revue structurée qui répond à la question de l'utilisateur.\n\n"
        "Règles strictes:\n"
        "1. Cite TOUJOURS tes affirmations avec les labels [S1], [S2], etc. correspondant aux sources fournies.\n"
        "2. N'invente AUCUN fait absent des sources. Si une dimension est absente, dis-le.\n"
        "3. Identifie de vrais désaccords entre sources s'ils existent (ne fabrique pas de controverses).\n"
        "4. La chronologie doit être basée sur les années réelles fournies dans les en-têtes de sources.\n"
        "5. Les thèmes / écoles de pensée doivent regrouper des sources qui défendent une position similaire.\n"
        "6. Reste sobre et factuel. Pas de superlatifs gratuits.\n"
        f"7. {_OUTPUT_LANGUAGE_RULES['fr']}\n"
    )


def _merge_instructions(language: str) -> str:
    language = normalize_review_language(language) or _DEFAULT_REVIEW_LANGUAGE
    if language == "en":
        return (
            "You are merging several partial syntheses of the same literature review. "
            "Each part was produced from a subset of the sources.\n\n"
            "Strict rules:\n"
            "1. Keep the [Sn] labels exactly as they are. Do not invent or renumber any.\n"
            "2. Do not add any fact that is absent from the partial syntheses.\n"
            "3. Merge themes that describe the same position instead of repeating them.\n"
            "4. The final timeline is the union of the partial timelines, sorted by ascending year.\n"
            "5. Keep a disagreement only when it really opposes two positions.\n"
            "6. Stay sober and factual.\n"
            f"7. {_OUTPUT_LANGUAGE_RULES['en']}\n"
        )
    return (
        "Tu fusionnes plusieurs synthèses partielles d'une même revue de littérature. "
        "Chaque partie a été produite sur un sous-ensemble des sources.\n\n"
        "Règles strictes:\n"
        "1. Conserve les labels [Sn] exactement tels quels. N'en invente aucun et n'en renumérote aucun.\n"
        "2. N'ajoute AUCUN fait absent des synthèses partielles.\n"
        "3. Fusionne les thèmes qui décrivent la même position au lieu de les répéter.\n"
        "4. La chronologie finale est l'union des chronologies partielles, triée par année croissante.\n"
        "5. Ne garde un désaccord que s'il oppose réellement deux positions.\n"
        "6. Reste sobre et factuel.\n"
        f"7. {_OUTPUT_LANGUAGE_RULES['fr']}\n"
    )


def _estimate_tokens(text: Any) -> int:
    return max(1, math.ceil(len(str(text or "")) / _CHARS_PER_TOKEN))


def _build_paper_blocks(docs: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Group chunks into per-paper blocks carrying globally stable [Sn] labels.

    Labels are assigned before any batching so a paper keeps the same number
    whichever batch it lands in, and the ``sources`` list stays aligned.
    """
    grouped = _group_chunks_by_paper(docs)
    capped_paper_keys = list(grouped.keys())[:MAX_PAPERS_IN_PROMPT]
    blocks: list[dict[str, Any]] = []
    labels: list[str] = []
    for index, key in enumerate(capped_paper_keys, start=1):
        label = f"S{index}"
        labels.append(label)
        paper_docs = grouped[key]
        text = _format_paper_block(label, paper_docs)
        blocks.append(
            {
                "label": label,
                "text": text,
                "docs": paper_docs,
                "tokens": _estimate_tokens(text),
            }
        )
    return blocks, labels


def _compose_prompt(
    user_query: str,
    blocks: list[dict[str, Any]],
    language: str = _DEFAULT_REVIEW_LANGUAGE,
) -> str:
    language = normalize_review_language(language) or _DEFAULT_REVIEW_LANGUAGE
    question_label = "Question utilisateur:" if language == "fr" else "User question:"
    sources_label = "Sources disponibles:" if language == "fr" else "Available sources:"
    sources_section = "\n\n---\n\n".join(
        str(block["text"]) for block in blocks if str(block["text"] or "").strip()
    )
    return (
        f"{_review_instructions(language)}\n"
        f"{question_label}\n{user_query.strip()}\n\n"
        f"{sources_label}\n\n{sources_section}"
    )


def build_review_prompt(
    user_query: str,
    docs: list[Any],
    *,
    language: Any = None,
) -> tuple[str, list[Any], list[str]]:
    """Return (prompt, capped_docs, source_labels) for the synthesis LLM call."""
    resolved = _review_language(user_query, language)
    blocks, labels = _build_paper_blocks(docs)
    capped_docs = [doc for block in blocks for doc in block["docs"]]
    return _compose_prompt(user_query, blocks, resolved), capped_docs, labels


def _resolve_review_context(model_name: str, required_tokens: int) -> int:
    """Context window the synthesis call can actually count on."""
    try:
        from src.llm_provider import is_ollama_provider, resolve_ollama_context_length

        if not is_ollama_provider():
            return _REMOTE_CONTEXT_TOKENS
        resolved = resolve_ollama_context_length(model_name, required_tokens=required_tokens)
    except Exception:
        _logger.debug("Recovered from an error in _resolve_review_context.", exc_info=True)
        return _FALLBACK_CONTEXT_TOKENS
    return resolved or _FALLBACK_CONTEXT_TOKENS


def _review_input_budget(context_tokens: int) -> int:
    # Budget against the window we were actually given: the API accepts values
    # below the 4096 fallback, and over-budgeting there is what overflows.
    resolved = int(context_tokens) if int(context_tokens) > 0 else _FALLBACK_CONTEXT_TOKENS
    usable = int(resolved * _CONTEXT_INPUT_RATIO)
    return max(_MIN_INPUT_BUDGET_TOKENS, usable - _OUTPUT_RESERVE_TOKENS)


def _truncate_block(block: dict[str, Any], budget: int) -> dict[str, Any]:
    """Shrink a single oversized paper block so it fits a batch on its own."""
    if block["tokens"] <= budget:
        return block
    max_chars = max(400, int(budget * _CHARS_PER_TOKEN))
    text = str(block["text"] or "")[:max_chars].rstrip() + "…"
    return {**block, "text": text, "tokens": _estimate_tokens(text)}


def _plan_review_batches(
    blocks: list[dict[str, Any]],
    budget: int,
    overhead_tokens: int,
) -> list[list[dict[str, Any]]]:
    """Pack papers into at most ``MAX_REVIEW_BATCHES`` context-sized groups."""
    per_batch_budget = max(_MIN_INPUT_BUDGET_TOKENS, budget - overhead_tokens)
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for block in blocks:
        fitted = _truncate_block(block, per_batch_budget)
        cost = fitted["tokens"] + _BLOCK_SEPARATOR_TOKENS
        if current and used + cost > per_batch_budget:
            batches.append(current)
            if len(batches) >= MAX_REVIEW_BATCHES:
                return batches
            current = []
            used = 0
        current.append(fitted)
        used += cost
    if current:
        batches.append(current)
    return batches[:MAX_REVIEW_BATCHES]


def _year_sort_key(entry: str) -> tuple[int, str]:
    match = re.search(r"(1[5-9]\d{2}|20\d{2})", str(entry or ""))
    return (int(match.group(1)) if match else 9999, str(entry or ""))


def _merge_reviews_locally(partials: list[LiteratureReview]) -> LiteratureReview:
    """Deterministic merge used when the reduce call is unavailable."""
    overview = next((p.overview.strip() for p in partials if p.overview.strip()), "")
    chronology: list[str] = []
    seen: set[str] = set()
    for partial in partials:
        for entry in partial.chronology:
            cleaned = str(entry or "").strip()
            key = cleaned.lower()
            if cleaned and key not in seen:
                seen.add(key)
                chronology.append(cleaned)
    chronology.sort(key=_year_sort_key)
    themes: list[ReviewTheme] = []
    disagreements: list[ReviewDisagreement] = []
    for partial in partials:
        themes.extend(partial.themes)
        disagreements.extend(partial.disagreements)
    synthesis = " ".join(p.synthesis.strip() for p in partials if p.synthesis.strip())
    return LiteratureReview(
        overview=overview,
        chronology=chronology,
        themes=themes[:6],
        disagreements=disagreements[:6],
        synthesis=synthesis,
    )


def _build_merge_prompt(
    user_query: str,
    partials: list[LiteratureReview],
    blocks: list[dict[str, Any]],
    language: str = _DEFAULT_REVIEW_LANGUAGE,
) -> str:
    language = normalize_review_language(language) or _DEFAULT_REVIEW_LANGUAGE
    strings = _REVIEW_STRINGS[language]
    index_lines = []
    for block in blocks:
        header = str(block["text"] or "").splitlines()[:1]
        if header:
            index_lines.append(header[0].strip())
    parts = []
    for number, partial in enumerate(partials, start=1):
        rendered = partial.to_markdown(language).strip()
        if rendered:
            parts.append(f"{strings['merge_partial'].format(index=number)}\n\n{rendered}")
    return (
        f"{_merge_instructions(language)}\n"
        f"{strings['merge_question']}\n{user_query.strip()}\n\n"
        f"{strings['merge_index']}\n" + "\n".join(index_lines) + "\n\n"
        f"{strings['merge_parts']}\n\n" + "\n\n---\n\n".join(parts)
    )


def _invoke_review(
    user_query: str,
    blocks: list[dict[str, Any]],
    model_name: str,
    overhead_tokens: int,
    language: str = _DEFAULT_REVIEW_LANGUAGE,
) -> LiteratureReview:
    from src.llm_provider import invoke_structured_chat

    language = normalize_review_language(language) or _DEFAULT_REVIEW_LANGUAGE
    _theme_model, _disagreement_model, review_model = _review_response_models(language)
    prompt = _compose_prompt(user_query, blocks, language)
    context_tokens = overhead_tokens + sum(
        int(block["tokens"]) + _BLOCK_SEPARATOR_TOKENS for block in blocks
    )
    review = None
    empty_error: RuntimeError | None = None
    # Small models occasionally return an empty review, either as a valid but
    # empty object or as the structured-output "all fields empty" error; one
    # more attempt usually yields the synthesis.
    for attempt in range(2):
        try:
            review = invoke_structured_chat(
                model_name,
                [{"role": "user", "content": prompt}],
                response_model=review_model,
                context_tokens_hint=context_tokens,
            )
        except RuntimeError as exc:
            if "structured fields were empty" not in str(exc):
                raise
            empty_error, review = exc, None
        else:
            if not isinstance(review, LiteratureReview):
                raise RuntimeError("LLM returned an unexpected type for LiteratureReview")
            if review.to_markdown(language).strip():
                break
        _logger.warning(
            "literature review: %s returned an empty synthesis (attempt %s)",
            model_name,
            attempt + 1,
        )
    if review is None:
        assert empty_error is not None
        raise empty_error
    assert isinstance(review, LiteratureReview)
    return review


def _generate_review_events(
    user_query: str,
    *,
    docs: list[Any],
    model_name: str,
    language: Any = None,
):
    """Yield progress strings; return ``(review, used_docs, labels)``.

    A single call is used whenever the whole corpus fits the context window.
    Otherwise the papers are split into context-sized batches, synthesised
    one batch at a time, and merged by a final reduce pass — so a small
    context costs more round-trips rather than silently losing papers.

    Progress strings and the review itself follow the language resolved from
    the request (see :func:`resolve_review_language`).
    """
    if not docs:
        raise ValueError("literature review requires at least one retrieved chunk")

    language = _review_language(user_query, language)
    strings = _REVIEW_STRINGS[language]
    blocks, labels = _build_paper_blocks(docs)
    overhead = (
        _estimate_tokens(_review_instructions(language)) + _estimate_tokens(user_query) + 60
    )
    required = overhead + sum(
        int(block["tokens"]) + _BLOCK_SEPARATOR_TOKENS for block in blocks
    )
    budget = _review_input_budget(_resolve_review_context(model_name, required))

    if required <= budget:
        review = _invoke_review(user_query, blocks, model_name, overhead, language)
        used_docs = [doc for block in blocks for doc in block["docs"]]
        return review, used_docs, labels

    batches = _plan_review_batches(blocks, budget, overhead)
    if not batches:
        raise RuntimeError("literature review could not fit any paper in the context window")

    used_blocks = [block for batch in batches for block in batch]
    dropped = len(blocks) - len(used_blocks)
    if dropped > 0:
        _logger.info(
            "literature review dropped %s paper(s) beyond %s batches",
            dropped,
            MAX_REVIEW_BATCHES,
        )

    partials: list[LiteratureReview] = []
    for number, batch in enumerate(batches, start=1):
        yield strings["batch"].format(
            index=number,
            total=len(batches),
            papers=len(batch),
        )
        partials.append(_invoke_review(user_query, batch, model_name, overhead, language))

    used_docs = [doc for block in used_blocks for doc in block["docs"]]
    used_labels = [str(block["label"]) for block in used_blocks]

    if len(partials) == 1:
        return partials[0], used_docs, used_labels

    yield strings["merge"].format(count=len(partials))
    merge_prompt = _build_merge_prompt(user_query, partials, used_blocks, language)
    if _estimate_tokens(merge_prompt) <= budget:
        try:
            from src.llm_provider import invoke_structured_chat

            _theme_model, _disagreement_model, review_model = _review_response_models(
                language
            )
            merged = invoke_structured_chat(
                model_name,
                [{"role": "user", "content": merge_prompt}],
                response_model=review_model,
                context_tokens_hint=_estimate_tokens(merge_prompt),
            )
            if isinstance(merged, LiteratureReview):
                return merged, used_docs, used_labels
        except Exception:
            _logger.exception("literature review merge pass failed, merging locally")

    return _merge_reviews_locally(partials), used_docs, used_labels


def generate_literature_review(
    user_query: str,
    *,
    docs: list[Any],
    model_name: str,
    language: Any = None,
) -> tuple[LiteratureReview, list[Any], list[str]]:
    """Run the synthesis, discarding progress updates.

    Returns ``(review, used_docs, source_labels)``. Raises on LLM failure.

    ``language`` is the caller/UI preference: the request text decides first
    (see :func:`resolve_review_language`).
    """
    events = _generate_review_events(
        user_query,
        docs=docs,
        model_name=model_name,
        language=language,
    )
    try:
        while True:
            next(events)
    except StopIteration as stop:
        return stop.value


def stream_literature_review(
    user_query: str,
    model_name: str,
    threshold: float,
    *,
    tags: Any = None,
    collections: Any = None,
    target_chunks: int = DEFAULT_REVIEW_CHUNKS,
    abort_signal: Any = None,
    language: Any = None,
):
    """Generator that yields status / token / sources events for a review.

    Mirrors the shape of ``stream_chat_api`` so the frontend can reuse the
    chat rendering pipeline.

    ``language`` is the caller/UI preference; the request text decides first
    (see :func:`resolve_review_language`), and that resolved language drives
    the progress messages, the synthesis prompt and the rendered headings.
    """
    review_language = _review_language(user_query, language)
    strings = _REVIEW_STRINGS[review_language]
    # Lazy imports to avoid circular dependencies at module load.
    from src.rag import state
    from src.rag.query_engine import (
        _RAG_INIT_LOCK,
        _build_anchor_event,
        _extract_inline_filters,
        _merge_filter_maps,
        _prepare_explicit_filters,
        _retrieve_documents,
        format_sources_api,
        init_rag,
    )

    def _check_abort() -> bool:
        return bool(abort_signal is not None and getattr(abort_signal, "is_aborted", lambda: False)())
    from src.rag.query_engine import (
        _split_text_for_streaming as split_text_for_streaming,
    )

    with _RAG_INIT_LOCK:
        with state._state_lock:
            needs_init = state.document_chain is None or state.vectorstore is None
        if needs_init:
            ok, msg = init_rag(model_name, threshold, force_reload=False)
            if not ok:
                yield {"type": "error", "msg": msg}
                return

    if _check_abort():
        return
    yield {"type": "status", "msg": strings["search"]}

    from src.rag.query_engine import _stream_reranker_warmup_status

    yield from _stream_reranker_warmup_status()

    explicit_filters = _prepare_explicit_filters(tags=tags, collections=collections)
    cleaned_message, inline_filters = _extract_inline_filters(user_query)
    filters = _merge_filter_maps(explicit_filters, inline_filters)

    cap = max(8, min(MAX_REVIEW_CHUNKS, int(target_chunks)))
    docs = _retrieve_documents(
        cleaned_message or user_query,
        threshold=threshold,
        max_chunks=cap,
        filters=filters,
    )
    from src.rag.query_engine import _build_reranker_metrics_event as _get_rerank_metrics

    _pending_rerank_metrics = _get_rerank_metrics()

    if not docs:
        yield {
            "type": "token",
            "text": strings["no_docs"],
        }
        yield {"type": "sources", "data": [], "answer_origin": "literature_review"}
        return

    if _check_abort():
        return
    yield {
        "type": "status",
        "msg": strings["retrieved"].format(count=len(docs)),
    }

    events = _generate_review_events(
        cleaned_message or user_query,
        docs=docs,
        model_name=model_name,
        language=review_language,
    )
    try:
        while True:
            try:
                progress = next(events)
            except StopIteration as stop:
                review, capped_docs, _labels = stop.value
                break
            if _check_abort():
                events.close()
                return
            yield {"type": "status", "msg": progress}
    except Exception as exc:
        _logger.exception("literature review synthesis failed")
        yield {"type": "error", "msg": strings["synthesis_failed"].format(error=exc)}
        return

    markdown = review.to_markdown(review_language)
    if not markdown.strip():
        yield {"type": "error", "msg": strings["empty"]}
        return

    with state._state_lock:
        state.last_sources = capped_docs

    for piece in split_text_for_streaming(markdown):
        if _check_abort():
            return
        yield {"type": "token", "text": piece}
    yield {
        "type": "sources",
        "data": format_sources_api(capped_docs),
        "answer_origin": "literature_review",
    }
    if _pending_rerank_metrics:
        yield _pending_rerank_metrics
    anchor_event = _build_anchor_event(markdown, capped_docs)
    if anchor_event:
        yield anchor_event


__all__ = [
    "DEFAULT_REVIEW_CHUNKS",
    "LiteratureReview",
    "MAX_REVIEW_BATCHES",
    "MAX_REVIEW_CHUNKS",
    "ReviewDisagreement",
    "ReviewTheme",
    "build_review_prompt",
    "generate_literature_review",
    "normalize_review_language",
    "requested_language_in_text",
    "resolve_review_language",
    "stream_literature_review",
]
