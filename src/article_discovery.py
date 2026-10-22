"""
Article discovery:
- Generate local search query with the configured LLM
- Query Semantic Scholar
- Fallback to Unpaywall for OA PDF links
- Download OA PDF locally (and best-effort local Zotero connector import)
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import html
import ipaddress
import json
import logging
import math
import os
import random
import re
import socket
import time
import unicodedata
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

import httpx

import src.config
from src.app_settings import get_app_settings
from src.library import get_item_citations
from src.llm_provider import (
    SOFT_ABORT_REASON,
    StreamAbortSignal,
    create_chat_llm,
    invoke_text,
    is_remote_provider,
    resolve_ollama_context_length,
    stream_ollama_chat,
)
from src.model_profiles import (
    get_discovery_generation_settings,
    get_discovery_prompt_note,
)
from src.notebooks import get_notebook

logger = logging.getLogger(__name__)

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"[^A-Za-z0-9]+")
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_DOI_VERSION_SUFFIX_RE = re.compile(r"\.v\d+$", re.IGNORECASE)
_ARXIV_VERSION_SUFFIX_RE = re.compile(r"v\d+$", re.IGNORECASE)
_STOPWORDS = {
    # Bare adjectives match almost any abstract ("high" field, current...).
    "high",
    "low",
    "new",
    "all",
    "avec",
    "comment",
    "dans",
    "de",
    "des",
    "du",
    "en",
    "every",
    "everything",
    "how",
    "je",
    "la",
    "le",
    "leur",
    "leurs",
    "moi",
    "pour",
    "quel",
    "quelle",
    "quelles",
    "quels",
    "ses",
    "that",
    "this",
    "those",
    "these",
    "tout",
    "toute",
    "toutes",
    "tous",
    "type",
    "types",
    "from",
    "comme",
    "mais",
    "donc",
    "ainsi",
    "etre",
    "avoir",
    "their",
    "there",
    "which",
    "while",
    "where",
    "when",
    "les",
    "une",
    "the",
    "and",
    "or",
    "sur",
    "vos",
    "your",
    "elle",
    "lui",
    "nous",
    "vous",
    "they",
    "them",
    "also",
    "into",
    "over",
    "under",
    "article",
    "study",
    "paper",
    "cherche",
    "cherches",
    "chercher",
    "recherche",
    "differentes",
    "differentes",
    "different",
    "diff",
    "particulier",
    "particuliere",
    "particularly",
    "particular",
    "based",
    "using",
    "used",
    "result",
    "results",
}
_GENERIC_DISCOVERY_TERMS = {
    "academic",
    "analyse",
    "analysis",
    "approach",
    "article",
    "articles",
    "data",
    "effect",
    "effects",
    "impact",
    "impacts",
    "method",
    "methods",
    "paper",
    "papers",
    "recherche",
    "research",
    "result",
    "results",
    "review",
    "study",
    "studies",
    "system",
    "systems",
    "type",
    "types",
}

_S2_BASE = "https://api.semanticscholar.org"
_UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
_OPENALEX_BASE = "https://api.openalex.org"
_CROSSREF_BASE = "https://api.crossref.org"
_ARXIV_BASE = "https://export.arxiv.org/api/query"
_HAL_BASE = "https://api.hal.science/search/"
_DEFAULT_UNPAYWALL_EMAIL = "local@bibliobrain.local"

_API_INTERVAL_SECONDS = {
    "semanticscholar": 0.75,
    "unpaywall": 0.35,
    "openalex": 0.45,
    "crossref": 0.6,
    "arxiv": 3.0,
    "hal": 0.45,
}
# One lock per upstream API so calls to different services don't serialize each
# other. _get_api_lock lazily creates the lock to avoid issues with module
# import order vs. event-loop creation.
_api_locks: dict[str, asyncio.Lock] = {}
_request_locks: dict[str, asyncio.Lock] = {}


def _get_api_lock(api_name: str) -> asyncio.Lock:
    lock = _api_locks.get(api_name)
    if lock is None:
        lock = asyncio.Lock()
        _api_locks[api_name] = lock
    return lock


_api_last_call: dict[str, float] = {"semanticscholar": 0.0, "unpaywall": 0.0}
_api_last_call["openalex"] = 0.0
_api_last_call["crossref"] = 0.0
_api_last_call["arxiv"] = 0.0
_api_last_call["hal"] = 0.0
_discovery_deadline_var: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "discovery_deadline", default=None
)
_discovery_request_metrics_var: contextvars.ContextVar[dict[str, int] | None] = (
    contextvars.ContextVar("discovery_request_metrics", default=None)
)
# Mutated in place by the pipeline so the hard-timeout path can still say
# which sources answered and how many candidates were filtered out.
_discovery_run_report_var: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("discovery_run_report", default=None)
)
# Progressive search: receives progress and preliminary-result events.
_discovery_event_sink_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "discovery_event_sink", default=None
)


def _emit_discovery_event(event: dict[str, Any]) -> None:
    sink = _discovery_event_sink_var.get()
    if sink is None:
        return
    try:
        sink(event)
    except Exception:
        logger.debug("Discovery event sink failed", exc_info=True)


def _emit_progress(stage: str, message: str) -> None:
    _emit_discovery_event({"type": "progress", "stage": stage, "msg": message})
_semantic_circuit: dict[str, Any] = {
    "consecutive_rate_limits": 0,
    "open_until": 0.0,
    "last_error": "",
}
_DISCOVERY_LLM_TIMEOUT_SECONDS = 22.0
# Local models need roughly 10 s per validation batch; 55 s left no room for
# the LLM steps once the providers had answered.
_DISCOVERY_TOTAL_BUDGET_SECONDS = 75.0
_DISCOVERY_VALIDATION_BATCH_SIZE = 6
_DISCOVERY_MIN_CONTEXT_TOKENS = 6000
_DISCOVERY_OUTPUT_TOKENS_RESERVE = 1500
_DISCOVERY_VALIDATION_ABSTRACT_CHARS = 900
_DISCOVERY_VALIDATION_BATCH_MIN_SECONDS = 10.0
# Kept for the result summaries and the response once validation stops.
_DISCOVERY_POST_VALIDATION_RESERVE_SECONDS = 8.0
# The pipeline keeps itself inside the budget; this only catches a stuck step.
_DISCOVERY_HARD_TIMEOUT_MARGIN_SECONDS = 10.0
_DISCOVERY_HARD_TIMEOUT_SECONDS = 58.0
_DISCOVERY_OPTIONAL_NETWORK_RESERVE_SECONDS = 9.0
_MISSING_ABSTRACT_PROMPT = "[Abstract unavailable in source metadata]"
_MISSING_ABSTRACT_PREVIEW = "Abstract indisponible dans la source externe."
_DISCOVERY_QUERY_PLANNER_MAX_FACETS = 3
_DISCOVERY_QUERY_PLANNER_MAX_SEARCH_QUERIES = 4
_DISCOVERY_CANDIDATE_MULTIPLIER = 4
_DISCOVERY_CANDIDATE_MIN = 12
_DISCOVERY_CANDIDATE_MAX = 48
_DISCOVERY_HEURISTIC_CANDIDATE_MULTIPLIER = 2
_DISCOVERY_LLM_VALIDATION_MULTIPLIER = 3
_DISCOVERY_LLM_VALIDATION_MIN = 12
_DISCOVERY_LLM_VALIDATION_MAX = 24
_DISCOVERY_ABSTRACT_ENRICHMENT_MAX_PAPERS = 12
_DISCOVERY_SEMANTIC_QUERY_ATTEMPTS = 3
_DISCOVERY_SEMANTIC_CACHE_SEARCH_TTL = 6 * 60 * 60
_DISCOVERY_SEMANTIC_CACHE_PAPER_TTL = 7 * 24 * 60 * 60
_DISCOVERY_SEMANTIC_CACHE_RECOMMENDATION_TTL = 12 * 60 * 60
# Dense (embedding-based) semantic ranking. Candidates kept after the dense
# pre-rank for abstract enrichment + cross-encoder + LLM judge.
_DISCOVERY_DENSE_PREFILTER_MAX = 60
_DISCOVERY_DENSE_TEXT_CHARS = 1600
# Citation snowballing: expand recall by pulling neighbours of the best
# on-topic seeds via the Semantic Scholar recommendations graph.
_DISCOVERY_SNOWBALL_SEEDS = 5
_DISCOVERY_SNOWBALL_LIMIT = 12
# Domain anchor list size. Kept >4 so short acronyms (MI, NI) can lead without
# evicting broad anchors (magnets, superconductor) from the strict-fallback
# anchoring used by OpenAlex/HAL/Crossref (`require_query_anchor=True`).
_DISCOVERY_DOMAIN_ANCHOR_LIMIT = 6
_HAL_DISCOVERY_DOC_TYPES = ("ART", "COMM", "PREPRINT", "REPORT", "THESE")
_DISCOVERY_PROVIDER_LABELS = {
    "semanticscholar": "Semantic Scholar",
    "openalex": "OpenAlex",
    "arxiv": "arXiv",
    "hal": "HAL",
    "crossref": "Crossref",
}
_OPENALEX_MISSING_API_KEY_WARNING = (
    "Clé API OpenAlex non configurée: depuis février 2026, OpenAlex exige une clé "
    "gratuite pour un usage fiable. Renseignez-la dans Réglages > Avancé > Clés et accès API "
    "(OPENALEX_API_KEY reste compatible). "
    "Si Semantic Scholar est indisponible, cette clé peut grandement améliorer les résultats de fallback."
)
_SHORT_TECHNICAL_TOKENS = {"ac", "dc", "hts", "2d", "3d", "ta", "av"}
_KNOWN_ACRONYMS = {"rebco", "ybco", "gdbco", "bscco", "scif", "nmr", "mri", "lts", "corc", "smes"}
_METHOD_QUERY_TERMS = {
    "algorithm",
    "algorithms",
    "analysis",
    "analytical",
    "benchmark",
    "comparison",
    "comparative",
    "design",
    "estimation",
    "finite",
    "formulation",
    "formulations",
    "framework",
    "frameworks",
    "identification",
    "implementation",
    "mathematical",
    "measurement",
    "method",
    "methods",
    "model",
    "models",
    "modeling",
    "modelling",
    "numerical",
    "optimization",
    "optimisation",
    "review",
    "simulation",
    "simulations",
    "solver",
    "solvers",
    "workflow",
}
_TOOL_QUERY_TERMS = {
    "abaqus",
    "ansys",
    "cadence",
    "comsol",
    "cst",
    "elmer",
    "excel",
    "femm",
    "fluent",
    "fortran",
    "hfss",
    "julia",
    "labview",
    "matlab",
    "numpy",
    "openfoam",
    "python",
    "simulink",
}
_NEGATIVE_DOCUMENT_TERMS = {
    "book review",
    "call for papers",
    "conference calendar",
    "corrigendum",
    "editorial",
    "erratum",
    "foreword",
    "guest editorial",
    "introduction",
    "news",
    "preface",
    "retraction",
}
_DIVERSITY_STOPWORDS = _STOPWORDS | _GENERIC_DISCOVERY_TERMS | {
    "application",
    "applications",
    "approaches",
    "case",
    "cases",
    "high",
    "low",
    "new",
    "novel",
    "overview",
    "performance",
    "properties",
    "property",
    "temperature",
    "using",
}
_WEAK_TOPIC_TERMS = {
    "ac",
    "current",
    "currents",
    "field",
    "fields",
    "loss",
    "losses",
    "temperature",
    "type",
    "types",
}
_DOMAIN_ANCHOR_ALIAS_MAP = {
    "coil": ["coil", "coils"],
    "coils": ["coil", "coils"],
    "hts": [
        "hts",
        "high temperature superconductor",
        "high temperature superconductors",
        "high temperature superconducting",
        "high-temperature superconductor",
        "high-temperature superconductors",
        "high-temperature superconducting",
    ],
    "magnet": ["magnet", "magnets"],
    "magnets": ["magnet", "magnets"],
    "superconductor": ["superconductor", "superconductors", "superconducting"],
    "superconductors": ["superconductor", "superconductors", "superconducting"],
    "superconducting": ["superconductor", "superconductors", "superconducting"],
    "tape": ["tape", "tapes"],
    "tapes": ["tape", "tapes"],
    "rebco": [
        "rebco", "ybco", "gdbco", "eubco", "re123", "reba2cu3o", "rare-earth barium copper oxide",
        "rare earth barium copper oxide", "coated conductor", "coated conductors",
    ],
    "no-insulation": ["no-insulation", "no insulation", "non-insulated", "non insulated", "ni coil", "ni coils"],
    "screening currents": [
        "screening current", "screening currents", "screening-current", "shielding current",
        "shielding currents", "scif",
    ],
}
# Several spellings of one concept, used by every keyword match.
_KEYWORD_ALIAS_MAP = {
    key: aliases
    for key, aliases in _DOMAIN_ANCHOR_ALIAS_MAP.items()
    if key in {"rebco", "no-insulation", "screening currents", "hts"}
}
_KEYWORD_ALIAS_MAP["screening current"] = _DOMAIN_ANCHOR_ALIAS_MAP["screening currents"]
_KEYWORD_ALIAS_MAP["no insulation"] = _DOMAIN_ANCHOR_ALIAS_MAP["no-insulation"]
_KEYWORD_ALIAS_MAP["ybco"] = _DOMAIN_ANCHOR_ALIAS_MAP["rebco"]
_FR_EN_QUERY_MAP = {
    "aimant": "magnet",
    "aimants": "magnets",
    "bobine": "coil",
    "bobines": "coils",
    "champ": "field",
    "champs": "fields",
    "comsol": "COMSOL",
    "courant": "current",
    "courants": "currents",
    "elements": "elements",
    "fini": "finite",
    "finis": "finite",
    "formulation": "formulation",
    "formulations": "formulations",
    "hts": "HTS",
    "loss": "loss",
    "losses": "losses",
    "magnet": "magnet",
    "magnets": "magnets",
    "modele": "model",
    "modeles": "models",
    "modeling": "modeling",
    "modelisation": "modeling",
    "modelisations": "modeling",
    "modeliser": "modeling",
    "modelises": "modeled",
    "modelling": "modeling",
    "perte": "loss",
    "pertes": "losses",
    "simulation": "simulation",
    "simulations": "simulation",
    "supraconducteur": "superconductor",
    "supraconducteurs": "superconductors",
    "supraconductrice": "superconducting",
    "supraconductrices": "superconducting",
}


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        clean = (value or "").strip()
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out


def _join_query_terms(values: list[str], *, limit: int = 8) -> str:
    terms = _dedupe_keep_order([str(value or "").strip() for value in values])
    return " ".join(terms[:limit]).strip()


def _llm_timeout() -> float:
    try:
        value = float(
            os.getenv("DISCOVERY_LLM_TIMEOUT_SECONDS", str(_DISCOVERY_LLM_TIMEOUT_SECONDS)).strip()
        )
        return max(4.0, min(60.0, value))
    except (TypeError, ValueError):
        return _DISCOVERY_LLM_TIMEOUT_SECONDS


def _discovery_total_budget() -> float:
    try:
        value = float(
            os.getenv(
                "DISCOVERY_TOTAL_BUDGET_SECONDS",
                str(_DISCOVERY_TOTAL_BUDGET_SECONDS),
            ).strip()
        )
    except (TypeError, ValueError):
        value = _DISCOVERY_TOTAL_BUDGET_SECONDS
    return max(20.0, min(_DISCOVERY_HARD_TIMEOUT_SECONDS, value))


def _remaining_discovery_seconds(*, reserve: float = 0.0) -> float:
    deadline = _discovery_deadline_var.get()
    if deadline is None:
        return 3600.0
    return max(0.0, deadline - time.monotonic() - max(0.0, reserve))


def _has_discovery_budget(seconds: float, *, reserve: float = 0.0) -> bool:
    return _remaining_discovery_seconds(reserve=reserve) >= max(0.0, seconds)


def _bounded_task_timeout(task: str) -> float:
    configured = _llm_timeout()
    # Query planning must leave enough room for external retrieval and ranking;
    # validation gets most of the remaining optional budget; result summaries
    # are deliberately cheap because the abstract preview is already usable.
    caps = {
        "query": (15.0, 30.0),
        "planner": (15.0, 30.0),
        "validate": (20.0, 7.0),
        "summary": (12.0, 1.5),
    }
    cap, reserve = caps.get(str(task or "").strip().lower(), (12.0, 5.0))
    available = _remaining_discovery_seconds(reserve=reserve)
    if available < 1.0:
        raise TimeoutError("Budget de temps discovery insuffisant pour le LLM")
    return max(0.75, min(configured, cap, available))


def _request_metrics() -> dict[str, int]:
    metrics = _discovery_request_metrics_var.get()
    if metrics is None:
        metrics = {"network_calls": 0, "cache_hits": 0, "retries": 0, "batch_calls": 0}
        _discovery_request_metrics_var.set(metrics)
    return metrics


def _semantic_circuit_status() -> dict[str, Any]:
    now = time.monotonic()
    open_until = float(_semantic_circuit.get("open_until") or 0.0)
    return {
        "open": open_until > now,
        "retry_after_seconds": round(max(0.0, open_until - now), 3),
        "consecutive_rate_limits": int(
            _semantic_circuit.get("consecutive_rate_limits") or 0
        ),
        "last_error": str(_semantic_circuit.get("last_error") or ""),
    }


def _raise_if_semantic_circuit_open() -> None:
    status = _semantic_circuit_status()
    if status["open"]:
        raise RuntimeError(
            "Semantic Scholar circuit open (rate limit); "
            f"retry after {status['retry_after_seconds']:.1f}s"
        )


def _note_semantic_rate_limit(error: str = "Rate limit") -> None:
    failures = int(_semantic_circuit.get("consecutive_rate_limits") or 0) + 1
    _semantic_circuit["consecutive_rate_limits"] = failures
    _semantic_circuit["last_error"] = str(error or "Rate limit")
    if failures >= 2:
        cooldown = min(180.0, 60.0 * (2 ** min(2, failures - 2)))
        _semantic_circuit["open_until"] = max(
            float(_semantic_circuit.get("open_until") or 0.0),
            time.monotonic() + cooldown,
        )


def _note_semantic_success() -> None:
    _semantic_circuit["consecutive_rate_limits"] = 0
    _semantic_circuit["open_until"] = 0.0
    _semantic_circuit["last_error"] = ""


def _request_cache_key(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
) -> str:
    canonical = json.dumps(
        {
            "method": str(method or "GET").upper(),
            "url": str(url or ""),
            "params": params or {},
            "json": json_body,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()


def _semantic_cache_ttl(url: str) -> float:
    value = str(url or "").lower()
    if "/recommendations/" in value:
        return float(_DISCOVERY_SEMANTIC_CACHE_RECOMMENDATION_TTL)
    if "/paper/search" in value or "/paper/search/bulk" in value:
        return float(_DISCOVERY_SEMANTIC_CACHE_SEARCH_TTL)
    return float(_DISCOVERY_SEMANTIC_CACHE_PAPER_TTL)


def _get_request_lock(cache_key: str) -> asyncio.Lock:
    lock = _request_locks.get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        _request_locks[cache_key] = lock
    return lock


async def _budgeted_sleep(delay: float) -> None:
    safe_delay = max(0.0, float(delay))
    if safe_delay <= 0:
        return
    if not _has_discovery_budget(safe_delay, reserve=0.5):
        raise TimeoutError("Budget de temps discovery epuise pendant le backoff")
    await asyncio.sleep(safe_delay)


def _extract_dois(text: str) -> list[str]:
    found = [match.strip().rstrip(".,;:") for match in _DOI_RE.findall(text or "")]
    return _dedupe_keep_order(found)


def _canonicalize_doi(value: str) -> str:
    doi = str(value or "").strip().lower()
    doi = doi.replace("https://doi.org/", "").replace("http://doi.org/", "")
    doi = doi.replace("doi:", "").strip().rstrip(".,;:")
    if not doi:
        return ""
    return _DOI_VERSION_SUFFIX_RE.sub("", doi)


def _grounded_dois(candidates: list[Any], source_text: str) -> list[str]:
    """Keep only the DOIs that really occur in the user's text.

    Local models asked for "dois" invent plausible ones
    (10.1016/j.cryogenics.2023.103456); a made-up DOI either fails or pulls in
    an unrelated real paper as a snowball seed.
    """
    present = {_canonicalize_doi(value) for value in _extract_dois(source_text or "")}
    present.discard("")
    grounded: list[str] = []
    for value in candidates or []:
        canonical = _canonicalize_doi(str(value or ""))
        if canonical and canonical in present:
            grounded.append(str(value).strip())
    return grounded


def _canonicalize_arxiv_id(value: str) -> str:
    raw = str(value or "").strip()
    raw = raw.replace("https://arxiv.org/abs/", "").replace("http://arxiv.org/abs/", "")
    raw = raw.replace("https://arxiv.org/pdf/", "").replace("http://arxiv.org/pdf/", "")
    raw = raw.removesuffix(".pdf").strip().rstrip("/")
    return _ARXIV_VERSION_SUFFIX_RE.sub("", raw).strip()


def _normalize_search_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


def _tokenize_search_text(text: str) -> list[str]:
    normalized = _normalize_search_text(text).lower()
    return re.findall(r"[A-Za-z0-9]{2,}", normalized)


def _keyword_fallback(text: str, *, limit: int = 8) -> list[str]:
    tokens = _tokenize_search_text(text)
    protected = _acronym_short_tokens(text)
    freq: dict[str, int] = {}
    order: dict[str, int] = {}
    for idx, token in enumerate(tokens):
        if len(token) < 4 and token not in _SHORT_TECHNICAL_TOKENS and token not in protected:
            continue
        if token in _STOPWORDS:
            continue
        if token.isdigit():
            continue
        freq[token] = freq.get(token, 0) + 1
        order.setdefault(token, idx)
    ranked = sorted(freq.items(), key=lambda kv: (-kv[1], order.get(kv[0], 0), kv[0]))
    return [token for token, _ in ranked[: max(1, limit)]]


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _extract_acronyms(text: str) -> list[str]:
    matches = re.findall(r"\b[A-Z]{2,}(?:-[A-Z])?\b", text or "")
    return _dedupe_keep_order(matches)


def _acronym_short_tokens(text: str) -> set[str]:
    """Lowercased short acronyms (e.g. MI, NI) detected in the source text.

    These are the most discriminating tokens of a technical query yet are
    short enough to be dropped by the length filters. Detecting them per-call
    (rather than hard-coding a domain map) keeps the fix general.
    """
    return {acr.lower() for acr in _extract_acronyms(text) if 2 <= len(acr) <= 5}


def _translate_query_token(token: str) -> str:
    return _FR_EN_QUERY_MAP.get(token.lower(), token)


def _canonical_query_term(term: str) -> str:
    raw = _normalize_search_text(term).strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if " " in raw:
        parts = [_canonical_query_term(part) for part in raw.split()]
        return " ".join(part for part in parts if part).strip()
    translated = _translate_query_token(lowered)
    if lowered in _TOOL_QUERY_TERMS:
        return translated.upper() if len(translated) <= 6 else translated
    # Short words were all uppercased as acronyms, which turned "high" into a
    # "HIGH" keyword; keep that for real acronyms only.
    if translated.isalpha() and (
        raw.isupper() or len(translated) <= 3 or translated in _KNOWN_ACRONYMS
    ):
        return translated.upper()
    return translated


def _classify_query_term(term: str) -> str:
    normalized = _normalize_search_text(term).lower()
    if not normalized:
        return "topic"
    tokens = normalized.split()
    if any(token in _TOOL_QUERY_TERMS for token in tokens):
        return "tool"
    if any(token in _METHOD_QUERY_TERMS for token in tokens):
        return "method"
    return "topic"


def _build_domain_anchor_terms(
    text: str, keywords: list[str], *, limit: int = _DISCOVERY_DOMAIN_ANCHOR_LIMIT
) -> list[str]:
    # Acronyms (HTS, MI, NI...) are the most discriminating terms, so they lead
    # the anchor list rather than losing the priority race to generic tokens.
    acronyms = _extract_acronyms(text) + _extract_acronyms(" ".join(keywords))
    ordered_terms = acronyms + keywords + _ordered_keyword_sequence(text, limit=12)
    protected = _acronym_short_tokens(text) | {kw.lower() for kw in _extract_acronyms(" ".join(keywords))}
    anchors: list[str] = []
    for raw_term in ordered_terms:
        canonical = _canonical_query_term(str(raw_term or ""))
        lowered = canonical.lower()
        if not canonical or lowered in _STOPWORDS or lowered in _GENERIC_DISCOVERY_TERMS:
            continue
        if lowered in _METHOD_QUERY_TERMS or lowered in _TOOL_QUERY_TERMS or lowered in _WEAK_TOPIC_TERMS:
            continue
        if len(lowered) < 4 and lowered not in _SHORT_TECHNICAL_TOKENS and lowered not in protected:
            continue
        anchors.append(canonical)
    return _dedupe_keep_order(anchors)[:limit]


def _build_query_facets(text: str, keywords: list[str]) -> dict[str, list[str]]:
    ordered_terms = keywords + _ordered_keyword_sequence(text, limit=10) + _extract_acronyms(text)
    facets: dict[str, list[str]] = {"topic": [], "method": [], "tool": []}
    for raw_term in ordered_terms:
        canonical = _canonical_query_term(str(raw_term or ""))
        lowered = canonical.lower()
        if not canonical or lowered in _STOPWORDS or lowered in _GENERIC_DISCOVERY_TERMS:
            continue
        label = _classify_query_term(canonical)
        if canonical not in facets[label]:
            facets[label].append(canonical)

    if not facets["topic"]:
        for keyword in keywords:
            canonical = _canonical_query_term(keyword)
            if canonical and canonical not in facets["topic"]:
                facets["topic"].append(canonical)

    for label in ("topic", "method", "tool"):
        facets[label] = facets[label][:4]
    return facets


def _query_facets_to_rows(facets: dict[str, list[str]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for label in ("topic", "method", "tool"):
        keywords = _dedupe_keep_order([str(value or "").strip() for value in facets.get(label, [])])
        if not keywords:
            continue
        rows.append({"label": label, "keywords": keywords})
    return rows[:_DISCOVERY_QUERY_PLANNER_MAX_FACETS]


def _build_facet_queries(facets: dict[str, list[str]]) -> list[str]:
    topic = facets.get("topic") or []
    method = facets.get("method") or []
    tool = facets.get("tool") or []
    queries: list[str] = []

    if topic or method or tool:
        queries.append(_join_query_terms(topic[:4] + method[:2] + tool[:1]))
    if method and topic:
        queries.append(_join_query_terms(method[:2] + topic[:4]))
    if tool and topic:
        queries.append(_join_query_terms(tool[:2] + topic[:4]))
    if len(topic) >= 2:
        queries.append(_join_query_terms(topic[:5]))

    return _dedupe_keep_order([query for query in queries if query])[
        :_DISCOVERY_QUERY_PLANNER_MAX_SEARCH_QUERIES
    ]


def _query_negative_terms(facets: dict[str, list[str]]) -> list[str]:
    negatives = list(_NEGATIVE_DOCUMENT_TERMS)
    if facets.get("method"):
        negatives.extend(["case report", "clinical trial"])
    return _dedupe_keep_order(negatives)


def _ordered_keyword_sequence(text: str, *, limit: int = 10) -> list[str]:
    ordered: list[str] = []
    protected = _acronym_short_tokens(text)
    for token in _tokenize_search_text(text):
        if len(token) < 4 and token not in _SHORT_TECHNICAL_TOKENS and token not in protected:
            continue
        if token in _STOPWORDS or token.isdigit():
            continue
        if token not in ordered:
            ordered.append(token)
        if len(ordered) >= limit:
            break
    return ordered


def _heuristic_query_variants(text: str, keywords: list[str]) -> list[str]:
    normalized = _normalize_search_text(text).lower()
    ordered_keywords = _ordered_keyword_sequence(text, limit=10)
    translated = [_translate_query_token(token) for token in ordered_keywords]
    acronyms = _extract_acronyms(text)
    variants: list[str] = []

    if translated:
        variants.append(_join_query_terms(translated))

    if acronyms:
        prioritized = [
            token for token in translated if token.upper() not in {a.upper() for a in acronyms}
        ]
        variants.append(_join_query_terms(acronyms + prioritized[:6]))

    has_modeling = _contains_any(
        normalized,
        ["modeliser", "modelisation", "modelling", "modeling", "simulation", "formulation"],
    )
    has_loss = _contains_any(normalized, ["perte", "loss"])
    has_ac = re.search(r"\bac\b", normalized) is not None
    has_comsol = "comsol" in normalized

    technical_terms: list[str] = []
    if acronyms:
        technical_terms.extend(acronyms[:2])
    if has_ac:
        technical_terms.append("AC")
    if has_loss:
        technical_terms.append("loss")
    if has_modeling:
        technical_terms.append("modeling")
    if _contains_any(normalized, ["formulation", "formulations"]):
        technical_terms.append("formulation")
    if has_comsol and "COMSOL" not in technical_terms:
        technical_terms.append("COMSOL")
    technical_terms = _dedupe_keep_order(technical_terms)

    domain_tail = [
        token
        for token in translated
        if token.lower() not in {"ac", "loss", "losses", "modeling", "formulation", "comsol"}
    ]
    if technical_terms:
        variants.append(_join_query_terms(technical_terms + domain_tail))

    facet_queries = _build_facet_queries(_build_query_facets(text, keywords))
    variants = facet_queries + variants

    if not variants and keywords:
        variants.append(_join_query_terms(keywords[:6]))
    return _dedupe_keep_order([variant.strip() for variant in variants if variant.strip()])[
        :_DISCOVERY_QUERY_PLANNER_MAX_SEARCH_QUERIES
    ]


def _native_search_queries(text: str, *, limit: int = 2) -> list[str]:
    """Original-language queries (no EN translation) for native repositories.

    The main ``search_queries`` are reformulated to English for the global
    catalogs; HAL is a French repository where the original wording improves
    recall, so we keep an untranslated variant alongside.
    """
    keywords = _keyword_fallback(text, limit=8)
    queries: list[str] = []
    if keywords:
        queries.append(" ".join(keywords[:6]))
    if len(keywords) > 4:
        queries.append(" ".join(keywords[:4]))
    return _dedupe_keep_order([query.strip() for query in queries if query.strip()])[:limit]


def _build_search_queries(
    keywords: list[str],
    primary_query: str = "",
    *,
    source_text: str = "",
) -> list[str]:
    candidates: list[str] = []
    compact_keywords = _dedupe_keep_order([str(item or "").strip() for item in keywords])
    facets = _build_query_facets(source_text or primary_query, compact_keywords)
    heuristic_variants = _heuristic_query_variants(source_text or primary_query, compact_keywords)
    facet_queries = _build_facet_queries(facets)
    if primary_query:
        candidates.append(primary_query)
    if not primary_query:
        candidates.extend(heuristic_variants)
    elif heuristic_variants:
        candidates.extend(heuristic_variants)
    candidates.extend(facet_queries)
    if compact_keywords and not primary_query:
        candidates.append(" ".join(compact_keywords[:6]))
    return _dedupe_keep_order([query for query in candidates if query])[
        :_DISCOVERY_QUERY_PLANNER_MAX_SEARCH_QUERIES
    ]


def _safe_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _discovery_candidate_limit(
    max_results: int,
    *,
    llm_available: bool,
    settings: dict[str, Any] | None = None,
) -> int:
    result_limit = _safe_int(max_results, 10, 1, 30)
    settings_payload = settings if isinstance(settings, dict) else {}
    configured_multiplier = _safe_int(
        settings_payload.get("discovery_candidate_multiplier"),
        _DISCOVERY_CANDIDATE_MULTIPLIER,
        1,
        8,
    )
    multiplier = (
        configured_multiplier
        if llm_available
        else _DISCOVERY_HEURISTIC_CANDIDATE_MULTIPLIER
    )
    minimum = _DISCOVERY_CANDIDATE_MIN if llm_available else max(8, result_limit)
    maximum = _DISCOVERY_CANDIDATE_MAX if llm_available else 30
    return _safe_int(result_limit * multiplier, result_limit, minimum, maximum)


def _discovery_validation_limit(
    max_results: int,
    *,
    settings: dict[str, Any] | None = None,
) -> int:
    result_limit = _safe_int(max_results, 10, 1, 30)
    settings_payload = settings if isinstance(settings, dict) else {}
    configured_multiplier = _safe_int(
        settings_payload.get("discovery_llm_validation_multiplier"),
        _DISCOVERY_LLM_VALIDATION_MULTIPLIER,
        1,
        8,
    )
    return _safe_int(
        result_limit * configured_multiplier,
        result_limit,
        _DISCOVERY_LLM_VALIDATION_MIN,
        _DISCOVERY_LLM_VALIDATION_MAX,
    )


def _clean_json_blob(text: str) -> str:
    return _JSON_FENCE_RE.sub("", (text or "").strip()).strip()


def _parse_json_obj(text: str) -> dict[str, Any] | None:
    blob = _clean_json_blob(text)
    if not blob:
        return None
    try:
        value = json.loads(blob)
        return value if isinstance(value, dict) else None
    except (TypeError, json.JSONDecodeError, ValueError):
        value = None

    start = blob.find("{")
    end = blob.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(blob[start : end + 1])
            return value if isinstance(value, dict) else None
        except (TypeError, json.JSONDecodeError, ValueError):
            return None
    return None


def _list_from_payload(payload: dict[str, Any] | None, *keys: str) -> list[Any]:
    if not isinstance(payload, dict):
        return []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def _string_from_row(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _bool_from_row(row: dict[str, Any], *keys: str) -> bool | None:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "oui"}:
            return True
        if text in {"0", "false", "no", "non"}:
            return False
    return None


def _extract_query_payload_fields(parsed: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(parsed, dict):
        return {}
    return {
        "keywords": [
            str(value).strip()
            for value in (
                parsed.get("keywords")
                or parsed.get("query_keywords")
                or parsed.get("terms")
                or parsed.get("search_terms")
                or []
            )
            if str(value).strip()
        ],
        "dois": [
            str(value).strip()
            for value in (
                parsed.get("dois") or parsed.get("doi_list") or parsed.get("seed_dois") or []
            )
            if str(value).strip()
        ],
        "search_query": _string_from_row(
            parsed, "search_query", "query", "main_query", "primary_query"
        ),
        "search_queries": [
            str(value).strip()
            for value in (
                parsed.get("search_queries")
                or parsed.get("queries")
                or parsed.get("query_variants")
                or []
            )
            if str(value).strip()
        ],
        "topic_summary": _string_from_row(
            parsed, "topic_summary", "research_summary", "corpus_summary", "summary"
        ),
    }


def _extract_query_planner_facets(parsed: dict[str, Any] | None) -> list[dict[str, Any]]:
    rows = [
        row
        for row in _list_from_payload(parsed, "facets", "query_facets", "aspects", "sub_queries")
        if isinstance(row, dict)
    ]
    facets: list[dict[str, Any]] = []
    for row in rows[:_DISCOVERY_QUERY_PLANNER_MAX_FACETS]:
        query = _string_from_row(row, "query", "search_query", "search", "sub_query")
        label = _string_from_row(row, "label", "aspect", "topic", "name")
        keywords = _dedupe_keep_order(
            [
                str(value).strip()
                for value in (row.get("keywords") or row.get("terms") or [])
                if str(value).strip()
            ]
        )[:6]
        if not query and not keywords:
            continue
        facets.append(
            {
                "label": label,
                "query": query,
                "keywords": keywords,
            }
        )
    return facets


def _extract_query_plan_fields(parsed: dict[str, Any] | None) -> dict[str, Any]:
    payload = _extract_query_payload_fields(parsed)
    payload["intent"] = _string_from_row(parsed or {}, "intent", "task_type", "question_type")
    payload["answer_hypothesis"] = _string_from_row(
        parsed or {}, "answer_hypothesis", "hypothesis", "working_answer", "draft_answer"
    )
    payload["ideal_abstract"] = _string_from_row(
        parsed or {}, "ideal_abstract", "ideal_paper", "hypothetical_abstract", "target_abstract"
    )
    payload["facets"] = _extract_query_planner_facets(parsed)
    return payload


def _build_heuristic_query_payload(text: str, *, generator: str = "heuristic") -> dict[str, Any]:
    dois = _extract_dois(text)
    keywords = _keyword_fallback(text, limit=8)
    facets = _build_query_facets(text, keywords[:8])
    domain_anchor_terms = _build_domain_anchor_terms(text, keywords[:8])
    heuristic_queries = _build_search_queries(keywords[:8], source_text=text)
    search_query = heuristic_queries[0] if heuristic_queries else " ".join(keywords[:6])
    return {
        "keywords": keywords[:8],
        "dois": dois[:8],
        "search_query": search_query,
        "search_queries": heuristic_queries or _build_search_queries(
            keywords[:8],
            search_query,
            source_text=text,
        ),
        "native_search_queries": _native_search_queries(text),
        "query_facets": _query_facets_to_rows(facets),
        "facet_keywords": facets,
        "domain_anchor_terms": domain_anchor_terms,
        "negative_terms": _query_negative_terms(facets),
        "topic_summary": _first_sentence(text)[:420],
        "generator": generator,
    }


def _build_query_payload_from_plan(input_text: str, plan: dict[str, Any] | None) -> dict[str, Any]:
    text = (input_text or "").strip()
    fallback_keywords = _keyword_fallback(text, limit=8)
    fallback_dois = _extract_dois(text)
    parsed = plan if isinstance(plan, dict) else {}
    keywords = _dedupe_keep_order(
        [str(value).strip() for value in (parsed.get("keywords") or []) if str(value).strip()]
        + fallback_keywords
    )[:8]
    dois = _dedupe_keep_order(
        _grounded_dois(list(parsed.get("dois") or []), text) + fallback_dois
    )[:8]
    primary_query = str(parsed.get("search_query") or "").strip()
    planner_queries = [
        str(value).strip() for value in (parsed.get("search_queries") or []) if str(value).strip()
    ]
    facet_queries = [
        str(facet.get("query") or "").strip()
        for facet in (parsed.get("facets") or [])
        if isinstance(facet, dict) and str(facet.get("query") or "").strip()
    ]
    if not primary_query:
        primary_query = planner_queries[0] if planner_queries else " ".join(keywords[:6])
    heuristic_facets = _build_query_facets(text, keywords)
    domain_anchor_terms = _build_domain_anchor_terms(text, keywords)
    search_queries = _dedupe_keep_order(
        [primary_query]
        + planner_queries
        + facet_queries
        + _build_facet_queries(heuristic_facets)
    )[
        :_DISCOVERY_QUERY_PLANNER_MAX_SEARCH_QUERIES
    ]
    merged_facets = [
        facet
        for facet in (parsed.get("facets") or [])
        if isinstance(facet, dict) and (facet.get("query") or facet.get("keywords"))
    ][:_DISCOVERY_QUERY_PLANNER_MAX_FACETS]
    if not merged_facets:
        merged_facets = _query_facets_to_rows(heuristic_facets)
    return {
        "keywords": keywords,
        "dois": dois,
        "search_query": primary_query,
        "search_queries": search_queries,
        "native_search_queries": _native_search_queries(text),
        "query_facets": merged_facets,
        "facet_keywords": heuristic_facets,
        "domain_anchor_terms": domain_anchor_terms,
        "negative_terms": _query_negative_terms(heuristic_facets),
        "topic_summary": str(parsed.get("topic_summary") or "").strip(),
        "generator": "llm_query_planner",
        "query_plan": {
            "intent": str(parsed.get("intent") or "").strip(),
            "answer_hypothesis": str(parsed.get("answer_hypothesis") or "").strip(),
            "ideal_abstract": str(parsed.get("ideal_abstract") or "").strip(),
            "facets": merged_facets,
        },
    }


def _extract_summary_rows(parsed: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        row
        for row in _list_from_payload(parsed, "summaries", "results", "items")
        if isinstance(row, dict)
    ]


def _extract_validation_rows(parsed: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        row
        for row in _list_from_payload(parsed, "evaluations", "results", "items", "articles")
        if isinstance(row, dict)
    ]


def _extract_rationale_rows(parsed: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [
        row
        for row in _list_from_payload(parsed, "rationales", "results", "items")
        if isinstance(row, dict)
    ]


def _validation_payload_is_low_quality(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < 2:
        return False
    scores: list[int] = []
    rationale_count = 0
    for row in rows:
        score = _safe_int(
            row.get("relevance_score", row.get("score", row.get("rating"))),
            -1,
            -1,
            100,
        )
        if score >= 0:
            scores.append(score)
        if _string_from_row(row, "rationale", "why", "reason", "justification", "explanation"):
            rationale_count += 1
    if len(scores) >= 3 and len(set(scores)) == 1 and scores[0] == 0:
        return True
    if rows and rationale_count <= max(0, len(rows) // 4):
        return True
    return False


def _llm_dimension_score(row: dict[str, Any], *keys: str) -> int:
    return _safe_int(
        _string_from_row(row, *keys) or row.get(keys[0] if keys else "score"),
        -1,
        -1,
        100,
    )


def _llm_validation_weights(query_payload: dict[str, Any]) -> dict[str, float]:
    facets = _query_facet_keywords(query_payload)
    weights = {"topic": 0.5, "method": 0.2, "tool": 0.1, "directness": 0.2}
    if not facets.get("method"):
        weights["topic"] += weights["method"] * 0.7
        weights["directness"] += weights["method"] * 0.3
        weights["method"] = 0.0
    if not facets.get("tool"):
        weights["topic"] += weights["tool"] * 0.5
        weights["directness"] += weights["tool"] * 0.5
        weights["tool"] = 0.0
    return weights


def _weighted_llm_relevance(row: dict[str, Any], query_payload: dict[str, Any]) -> tuple[int, dict[str, int]]:
    dimensions = {
        "topic": _llm_dimension_score(row, "topic_score", "topic_match_score", "subject_score"),
        "method": _llm_dimension_score(
            row,
            "method_score",
            "formulation_score",
            "method_match_score",
        ),
        "tool": _llm_dimension_score(
            row,
            "tool_score",
            "software_score",
            "application_score",
        ),
        "directness": _llm_dimension_score(
            row,
            "directness_score",
            "relevance_score",
            "score",
            "rating",
        ),
    }
    weights = _llm_validation_weights(query_payload)
    total = 0.0
    weight_sum = 0.0
    for label, weight in weights.items():
        score = dimensions.get(label, -1)
        if weight <= 0 or score < 0:
            continue
        total += score * weight
        weight_sum += weight
    if weight_sum <= 0:
        fallback = _safe_int(row.get("relevance_score", row.get("score", row.get("rating"))), 0, 0, 100)
        return fallback, dimensions
    return int(round(total / weight_sum)), dimensions


def _debug_text(value: Any, *, limit: int = 12000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n...[tronque]"


def _record_llm_debug(
    events: list[dict[str, Any]] | None,
    *,
    stage: str,
    prompt: str,
    raw_output: str = "",
    parsed: dict[str, Any] | None = None,
    error: str = "",
) -> None:
    if events is None:
        return
    events.append(
        {
            "stage": stage,
            "prompt": _debug_text(prompt),
            "raw_output": _debug_text(raw_output),
            "parsed": parsed or None,
            "error": _debug_text(error, limit=4000),
        }
    )


def _throw_if_aborted(abort_signal: StreamAbortSignal | None) -> None:
    if abort_signal is not None:
        abort_signal.throw_if_aborted()


async def _invoke_discovery_llm_text(
    *,
    prompt: str,
    model_name: str | None,
    task: str = "query",
    abort_signal: StreamAbortSignal | None = None,
) -> str:
    _throw_if_aborted(abort_signal)
    if not model_name:
        return ""

    generation = get_discovery_generation_settings(model_name)
    timeout_seconds = _bounded_task_timeout(task)

    if is_remote_provider():
        llm = create_chat_llm(
            model_name,
            temperature=generation["temperature"],
            repeat_penalty=generation["repeat_penalty"],
            top_p=generation["top_p"],
        )
        text = await asyncio.wait_for(
            asyncio.to_thread(invoke_text, llm, prompt),
            timeout=timeout_seconds,
        )
        _throw_if_aborted(abort_signal)
        return text or ""

    # One window for every discovery step: a validation batch (~4k prompt
    # tokens plus the answer) overflows Ollama's 4096 default, which drops the
    # instructions at the start of the prompt; a constant size also avoids
    # reloading the model between steps.
    context_length = resolve_ollama_context_length(
        model_name,
        required_tokens=max(
            _DISCOVERY_MIN_CONTEXT_TOKENS,
            math.ceil(len(prompt) / 3.2) + _DISCOVERY_OUTPUT_TOKENS_RESERVE,
        ),
    )
    options_override = {"num_ctx": context_length} if context_length else None
    # A timed-out wait_for leaves the worker thread streaming: Ollama kept
    # generating the abandoned answer and the next step queued behind it and
    # timed out too. Each call gets its own signal, fired on timeout and on
    # the user's stop.
    call_signal = StreamAbortSignal()
    if abort_signal is not None:
        abort_signal.register_close_callback(lambda: call_signal.abort("discovery aborted"))

    def _run_local() -> str:
        chunks: list[str] = []
        for chunk in stream_ollama_chat(
            model_name,
            [{"role": "user", "content": prompt}],
            think=False,
            temperature=generation["temperature"],
            repeat_penalty=generation["repeat_penalty"],
            top_p=generation["top_p"],
            options_override=options_override,
            abort_signal=call_signal,
            request_timeout_seconds=timeout_seconds + 1.0,
        ):
            piece = str(chunk.get("content") or "")
            if piece:
                chunks.append(piece)
        return "".join(chunks)

    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(_run_local),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        call_signal.abort(SOFT_ABORT_REASON)
        raise TimeoutError(f"{timeout_seconds:.0f} s") from exc
    _throw_if_aborted(abort_signal)
    return text or ""


# Publishers prepend a section label to the abstract ("Abstract The ...").
# Either punctuation follows the label, or the real text starts with a
# capital: "Summary statistics show" keeps its first word.
_ABSTRACT_LABEL_RE = re.compile(
    r"^(?i:abstract|summary|r\u00e9sum\u00e9|resume)"
    r"(?:\s*[:.\u2014\u2013-]\s*|\s+(?=[A-Z\u00c0-\u00dd0-9]))"
)


def _strip_abstract_label(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    return _ABSTRACT_LABEL_RE.sub("", cleaned, count=1)


def _first_sentence(text: str, *, max_chars: int = 190) -> str:
    cleaned = _strip_abstract_label(text)
    if not cleaned:
        return ""
    m = re.search(r"(.{40,}?[.!?])(\s|$)", cleaned)
    sentence = m.group(1).strip() if m else cleaned[:max_chars]
    if len(sentence) > max_chars:
        sentence = sentence[: max_chars - 1].rstrip() + "…"
    return sentence


async def _throttle(api_name: str) -> None:
    interval = _API_INTERVAL_SECONDS.get(api_name, 0.35)
    if api_name == "semanticscholar" and _semantic_scholar_api_key():
        # The introductory keyed quota is 1 request/s across all endpoints.
        # Leave margin for clock/network jitter instead of riding the boundary.
        interval = max(interval, 1.18)
    async with _get_api_lock(api_name):
        now = time.monotonic()
        last = _api_last_call.get(api_name, 0.0)
        wait_for = interval - (now - last)
        if wait_for > 0:
            await _budgeted_sleep(wait_for)
        _api_last_call[api_name] = time.monotonic()


async def _request_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    method: str,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    api_name: str = "semanticscholar",
    retries: int = 2,
) -> Any:
    safe_method = str(method or "GET").strip().upper()
    cache_key = _request_cache_key(
        safe_method,
        url,
        params=params,
        json_body=json_body,
    )
    use_cache = api_name == "semanticscholar"

    async with _get_request_lock(cache_key):
        if use_cache:
            try:
                from src.discovery_cache_db import get_cached_response

                cached = get_cached_response(cache_key)
            except Exception:
                logger.debug("Recovered from an error in _request_json_with_retry.", exc_info=True)
                cached = None
            if cached is not None:
                _request_metrics()["cache_hits"] += 1
                return cached

        max_retries = _safe_int(retries, 2, 0, 4)
        last_error = "Erreur inconnue"
        headers = _api_request_headers(api_name)
        for attempt in range(max_retries + 1):
            if api_name == "semanticscholar":
                _raise_if_semantic_circuit_open()
            await _throttle(api_name)
            remaining = _remaining_discovery_seconds(reserve=0.5)
            if remaining < 0.5:
                raise TimeoutError("Budget de temps discovery epuise avant l'appel externe")
            try:
                request_timeout = max(0.5, min(20.0, remaining))
                if safe_method == "POST":
                    request_coro = client.post(
                        url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
                    _request_metrics()["batch_calls"] += 1
                else:
                    request_coro = client.get(url, params=params, headers=headers)
                _request_metrics()["network_calls"] += 1
                response = await asyncio.wait_for(request_coro, timeout=request_timeout)
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    fallback = (1.8 * (2**attempt)) + random.uniform(0.0, 0.35)
                    delay = _retry_after_seconds(retry_after, fallback=fallback)
                    last_error = "Rate limit"
                    if api_name == "semanticscholar":
                        _note_semantic_rate_limit(last_error)
                    if attempt < max_retries:
                        _request_metrics()["retries"] += 1
                        await _budgeted_sleep(delay)
                        continue
                    break
                response.raise_for_status()
                payload = response.json()
                if api_name == "semanticscholar":
                    _note_semantic_success()
                if use_cache:
                    try:
                        from src.discovery_cache_db import set_cached_response

                        set_cached_response(
                            cache_key,
                            api_name,
                            payload,
                            ttl_seconds=_semantic_cache_ttl(url),
                        )
                    except Exception:
                        logger.debug("Unable to cache the discovery response for %s", url, exc_info=True)
                return payload
            except (TimeoutError, httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    _request_metrics()["retries"] += 1
                    delay = (0.8 * (2**attempt)) + random.uniform(0.0, 0.25)
                    await _budgeted_sleep(delay)
                    continue
        raise RuntimeError(last_error)


async def _get_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    api_name: str = "semanticscholar",
    retries: int = 2,
) -> dict[str, Any]:
    payload = await _request_json_with_retry(
        client,
        url,
        method="GET",
        params=params,
        api_name=api_name,
        retries=retries,
    )
    return payload if isinstance(payload, dict) else {}


async def _post_json_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any | None = None,
    api_name: str = "semanticscholar",
    retries: int = 2,
) -> Any:
    return await _request_json_with_retry(
        client,
        url,
        method="POST",
        params=params,
        json_body=json_body,
        api_name=api_name,
        retries=retries,
    )


async def _get_text_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    api_name: str = "arxiv",
    retries: int = 2,
) -> str:
    last_error: str = "Erreur inconnue"
    for attempt in range(retries + 1):
        await _throttle(api_name)
        try:
            remaining = _remaining_discovery_seconds(reserve=0.5)
            if remaining < 0.5:
                raise TimeoutError("Budget de temps discovery epuise avant l'appel externe")
            response = await asyncio.wait_for(
                client.get(url, params=params),
                timeout=max(0.5, min(20.0, remaining)),
            )
            _request_metrics()["network_calls"] += 1
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                delay = _retry_after_seconds(
                    retry_after,
                    fallback=(1.8 * (2**attempt)) + random.uniform(0.0, 0.35),
                )
                await _budgeted_sleep(delay)
                last_error = "Rate limit"
                continue
            response.raise_for_status()
            return response.text
        except httpx.HTTPError as exc:
            last_error = str(exc)
            if attempt < retries:
                _request_metrics()["retries"] += 1
                await _budgeted_sleep((0.8 * (2**attempt)) + random.uniform(0.0, 0.25))
                continue
    raise RuntimeError(last_error)


def _retry_after_seconds(value: str | None, *, fallback: float) -> float:
    raw = str(value or "").strip()
    if not raw:
        return fallback
    try:
        parsed = float(raw)
        return max(0.5, min(30.0, parsed))
    except (TypeError, ValueError):
        parsed = None
    try:
        dt = parsedate_to_datetime(raw)
        delta = dt.timestamp() - time.time()
        return max(0.5, min(30.0, delta))
    except (TypeError, ValueError, IndexError):
        return fallback


def _openalex_api_key() -> str:
    return os.getenv("OPENALEX_API_KEY", "").strip()


def _semantic_scholar_api_key() -> str:
    return (
        os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
        or os.getenv("S2_API_KEY", "")
    ).strip()


def _api_request_headers(api_name: str) -> dict[str, str] | None:
    if api_name == "semanticscholar":
        key = _semantic_scholar_api_key()
        if key:
            return {"x-api-key": key}
    if api_name == "openalex":
        # Header keeps the key out of URLs (proxy logs, raise_for_status messages).
        key = _openalex_api_key()
        if key:
            return {"Authorization": f"Bearer {key}"}
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    message = str(exc or "").strip().lower()
    return ("rate limit" in message) or ("429" in message) or ("too many requests" in message)


def _semantic_retry_delay_seconds(attempt_index: int, exc: Exception) -> float:
    if _is_rate_limit_error(exc):
        return min(8.0, 1.5 + (attempt_index * 1.5))
    return min(4.0, 0.8 + (attempt_index * 0.8))


def _clean_metadata_text(value: Any) -> str:
    text = html.unescape(str(value or "")).strip()
    if not text:
        return ""
    text = re.sub(r"</(?:p|div|br|jats:p|jats:sec)>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"(?<=\w)\s*-\s*(?=\w)", "-", text)


def _dict_payload(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_payload(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _normalize_paper(raw: dict[str, Any]) -> dict[str, Any]:
    external_ids = _dict_payload(raw.get("externalIds"))
    doi = _canonicalize_doi(str(raw.get("doi") or external_ids.get("DOI") or "").strip())
    publisher_url = f"https://doi.org/{doi}" if doi else ""
    open_access_pdf = raw.get("openAccessPdf")
    if not isinstance(open_access_pdf, dict):
        open_access_pdf = {}
    authors = []
    for author in _list_payload(raw.get("authors")):
        if not isinstance(author, dict):
            continue
        name = str(author.get("name") or "").strip()
        if name:
            authors.append(name)
    return {
        "paper_id": str(raw.get("paperId") or "").strip(),
        "title": _clean_metadata_text(raw.get("title")),
        "abstract": str(raw.get("abstract") or "").strip(),
        "year": str(raw.get("year") or "").strip(),
        "doi": doi,
        "authors": authors,
        "venue": str(raw.get("venue") or "").strip(),
        "publisher": "",
        "semantic_scholar_url": str(raw.get("url") or "").strip(),
        "publisher_url": publisher_url,
        "external_url": str(raw.get("url") or "").strip(),
        "external_source_label": "Semantic Scholar",
        "provider_sources": ["Semantic Scholar"],
        "provider_support_count": 1,
        "open_access_pdf": str(open_access_pdf.get("url") or "").strip(),
        "openAccessPdf": (
            {"url": str(open_access_pdf.get("url") or "").strip()}
            if open_access_pdf.get("url")
            else None
        ),
        "open_access_source": "semantic_scholar" if open_access_pdf.get("url") else "",
        "citation_count": _safe_int(raw.get("citationCount"), 0, 0, 10000000),
        "influential_citation_count": _safe_int(
            raw.get("influentialCitationCount"), 0, 0, 10000000
        ),
        "retrieval_rrf_score": float(raw.get("_discovery_rrf_score") or 0.0),
        "retrieval_query_hits": list(raw.get("_discovery_query_hits") or []),
    }


def _abstract_from_inverted_index(index: Any) -> str:
    if not isinstance(index, dict) or not index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, raw_positions in index.items():
        if not isinstance(word, str) or not isinstance(raw_positions, list):
            continue
        for pos in raw_positions:
            try:
                positions.append((int(pos), word))
            except (TypeError, ValueError):
                continue
    if not positions:
        return ""
    positions.sort(key=lambda item: item[0])
    return " ".join(word for _, word in positions)


def _normalize_openalex_work(raw: dict[str, Any]) -> dict[str, Any]:
    doi = _canonicalize_doi(str(raw.get("doi") or "").strip())
    authorships = _list_payload(raw.get("authorships"))
    authors: list[str] = []
    for authorship in authorships:
        if not isinstance(authorship, dict):
            continue
        author = _dict_payload(authorship.get("author"))
        name = str(author.get("display_name") or "").strip()
        if name:
            authors.append(name)

    best_oa = _dict_payload(raw.get("best_oa_location"))
    primary_location = _dict_payload(raw.get("primary_location"))
    source = None
    if isinstance(primary_location.get("source"), dict):
        source = primary_location.get("source")
    elif isinstance(best_oa.get("source"), dict):
        source = best_oa.get("source")
    source = source if isinstance(source, dict) else {}

    publisher_url = str(
        primary_location.get("landing_page_url") or best_oa.get("landing_page_url") or ""
    ).strip()
    pdf_url = str(best_oa.get("pdf_url") or "").strip()
    external_url = str(raw.get("id") or "").strip()
    abstract = _abstract_from_inverted_index(raw.get("abstract_inverted_index"))

    return {
        "paper_id": str(raw.get("id") or "").strip(),
        "title": _clean_metadata_text(raw.get("display_name")),
        "abstract": abstract,
        "year": str(raw.get("publication_year") or "").strip(),
        "doi": doi,
        "authors": authors,
        "venue": str(source.get("display_name") or "").strip(),
        "publisher": str(source.get("host_organization_name") or "").strip(),
        "semantic_scholar_url": "",
        "publisher_url": publisher_url,
        "external_url": external_url,
        "external_source_label": "OpenAlex",
        "provider_sources": ["OpenAlex"],
        "provider_support_count": 1,
        "open_access_pdf": pdf_url,
        "openAccessPdf": {"url": pdf_url} if pdf_url else None,
        "open_access_source": "openalex" if pdf_url else "",
        "citation_count": _safe_int(raw.get("cited_by_count"), 0, 0, 10000000),
        "influential_citation_count": 0,
    }


def _crossref_year(raw: dict[str, Any]) -> str:
    for key in ("published-print", "published-online", "published", "issued", "created"):
        value = raw.get(key)
        if not isinstance(value, dict):
            continue
        parts = value.get("date-parts")
        if not isinstance(parts, list) or not parts:
            continue
        first = parts[0]
        if not isinstance(first, list) or not first:
            continue
        try:
            return str(int(first[0]))
        except (TypeError, ValueError):
            continue
    return ""


def _clean_crossref_abstract(value: Any) -> str:
    return _clean_metadata_text(value)


def _first_payload_text(value: Any) -> str:
    if isinstance(value, list):
        for item in value:
            text = str(item or "").strip()
            if text:
                return text
        return ""
    return str(value or "").strip()


def _payload_texts(value: Any) -> list[str]:
    if isinstance(value, list):
        return _dedupe_keep_order([str(item or "").strip() for item in value])
    text = str(value or "").strip()
    return [text] if text else []


def _normalize_crossref_work(raw: dict[str, Any]) -> dict[str, Any]:
    doi = _canonicalize_doi(str(raw.get("DOI") or raw.get("doi") or "").strip())
    authors: list[str] = []
    for author in _list_payload(raw.get("author")):
        if not isinstance(author, dict):
            continue
        given = str(author.get("given") or "").strip()
        family = str(author.get("family") or "").strip()
        name = " ".join(part for part in [given, family] if part).strip()
        if name:
            authors.append(name)

    links = _list_payload(raw.get("link"))
    pdf_url = ""
    for link in links:
        if not isinstance(link, dict):
            continue
        content_type = str(link.get("content-type") or "").lower()
        url = str(link.get("URL") or "").strip()
        if url and ("pdf" in content_type or url.lower().endswith(".pdf")):
            pdf_url = url
            break

    publisher_url = f"https://doi.org/{doi}" if doi else str(raw.get("URL") or "").strip()
    return {
        "paper_id": doi or str(raw.get("URL") or "").strip(),
        "title": _clean_metadata_text(_first_payload_text(raw.get("title"))),
        "abstract": _clean_crossref_abstract(raw.get("abstract")),
        "year": _crossref_year(raw),
        "doi": doi,
        "authors": authors,
        "venue": _first_payload_text(raw.get("container-title")),
        "publisher": _first_payload_text(raw.get("publisher")),
        "semantic_scholar_url": "",
        "publisher_url": publisher_url,
        "external_url": str(raw.get("URL") or publisher_url).strip(),
        "external_source_label": "Crossref",
        "provider_sources": ["Crossref"],
        "provider_support_count": 1,
        "open_access_pdf": pdf_url,
        "openAccessPdf": {"url": pdf_url} if pdf_url else None,
        "open_access_source": "crossref" if pdf_url else "",
        "citation_count": _safe_int(raw.get("is-referenced-by-count"), 0, 0, 10000000),
        "influential_citation_count": 0,
    }


def _normalize_arxiv_work(raw: dict[str, Any]) -> dict[str, Any]:
    doi = _canonicalize_doi(str(raw.get("doi") or "").strip())
    arxiv_id = _canonicalize_arxiv_id(str(raw.get("id") or raw.get("arxiv_id") or "").strip())
    abstract_url = str(raw.get("abstract_url") or raw.get("id") or "").strip()
    if not abstract_url and arxiv_id:
        abstract_url = f"https://arxiv.org/abs/{arxiv_id}"
    pdf_url = str(raw.get("pdf_url") or "").strip()
    if not pdf_url and arxiv_id:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}"

    year = ""
    for key in ("published", "updated"):
        raw_date = str(raw.get(key) or "").strip()
        if len(raw_date) >= 4 and raw_date[:4].isdigit():
            year = raw_date[:4]
            break

    categories = _payload_texts(raw.get("categories"))
    primary_category = str(raw.get("primary_category") or "").strip()
    category_label = primary_category or (categories[0] if categories else "")
    venue = f"arXiv:{category_label}" if category_label else "arXiv"

    return {
        "paper_id": f"arxiv:{arxiv_id}" if arxiv_id else abstract_url,
        "title": re.sub(r"\s+", " ", str(raw.get("title") or "")).strip(),
        "abstract": re.sub(r"\s+", " ", str(raw.get("summary") or raw.get("abstract") or "")).strip(),
        "year": year,
        "doi": doi,
        "authors": _payload_texts(raw.get("authors"))[:8],
        "venue": venue,
        "publisher": "arXiv",
        "semantic_scholar_url": "",
        "publisher_url": f"https://doi.org/{doi}" if doi else abstract_url,
        "external_url": abstract_url,
        "external_source_label": "arXiv",
        "provider_sources": ["arXiv"],
        "provider_support_count": 1,
        "open_access_pdf": pdf_url,
        "openAccessPdf": {"url": pdf_url} if pdf_url else None,
        "open_access_source": "arxiv" if pdf_url else "",
        "citation_count": 0,
        "influential_citation_count": 0,
    }


def _normalize_hal_work(raw: dict[str, Any]) -> dict[str, Any]:
    doi = _canonicalize_doi(
        _first_payload_text(raw.get("doiId_s")) or _first_payload_text(raw.get("doi_s"))
    )
    title = _first_payload_text(raw.get("title_s"))
    if not title:
        title = _first_payload_text(raw.get("en_title_s")) or _first_payload_text(raw.get("fr_title_s"))

    abstract = _first_payload_text(raw.get("abstract_s"))
    if not abstract:
        abstract = _first_payload_text(raw.get("en_abstract_s")) or _first_payload_text(
            raw.get("fr_abstract_s")
        )
    abstract = re.sub(r"\s+", " ", html.unescape(abstract)).strip()

    year = ""
    for key in ("publicationDateY_i", "producedDateY_i", "submittedDateY_i"):
        value = raw.get(key)
        try:
            parsed_year = int(value or 0)
        except (TypeError, ValueError):
            continue
        if parsed_year > 0:
            year = str(parsed_year)
            break

    hal_id = str(raw.get("halId_s") or raw.get("docid") or "").strip()
    external_url = str(raw.get("uri_s") or "").strip()
    if not external_url and hal_id:
        external_url = f"https://hal.science/{hal_id}"
    venue = (
        _first_payload_text(raw.get("journalTitle_s"))
        or _first_payload_text(raw.get("conferenceTitle_s"))
        or _first_payload_text(raw.get("docType_s"))
        or "HAL"
    )
    pdf_url = _first_payload_text(raw.get("fileMain_s"))

    return {
        "paper_id": f"hal:{hal_id}" if hal_id else external_url,
        "title": re.sub(r"\s+", " ", html.unescape(title)).strip(),
        "abstract": abstract,
        "year": year,
        "doi": doi,
        "authors": _payload_texts(raw.get("authFullName_s"))[:8],
        "venue": venue,
        "publisher": _first_payload_text(raw.get("journalPublisher_s")),
        "semantic_scholar_url": "",
        "publisher_url": f"https://doi.org/{doi}" if doi else external_url,
        "external_url": external_url,
        "external_source_label": "HAL",
        "provider_sources": ["HAL"],
        "provider_support_count": 1,
        "open_access_pdf": pdf_url,
        "openAccessPdf": {"url": pdf_url} if pdf_url else None,
        "open_access_source": "hal" if pdf_url else "",
        "citation_count": 0,
        "influential_citation_count": 0,
    }


def _paper_year_int(paper: dict[str, Any]) -> int:
    try:
        return int(str(paper.get("year") or "").strip())
    except (TypeError, ValueError):
        return 0


def _paper_has_meaningful_abstract(paper: dict[str, Any]) -> bool:
    abstract = str(paper.get("abstract") or "").strip()
    if not abstract:
        return False
    if abstract == _MISSING_ABSTRACT_PROMPT:
        return False
    return len(abstract) >= 40


def _paper_identity_key(paper: dict[str, Any]) -> str:
    doi = _canonicalize_doi(str(paper.get("doi") or "").strip())
    if doi:
        return f"doi:{doi}"
    paper_id = str(paper.get("paper_id") or "").strip().lower()
    if paper_id:
        return f"id:{paper_id}"
    title = _normalize_search_text(str(paper.get("title") or "")).lower()
    return f"title:{title}" if title else ""


def _merge_paper_records(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    provider_sources = _dedupe_keep_order(
        [
            str(value or "").strip()
            for value in list(existing.get("provider_sources") or [])
            + list(incoming.get("provider_sources") or [])
            if str(value or "").strip()
        ]
    )
    existing["provider_sources"] = provider_sources
    existing["provider_support_count"] = len(provider_sources)

    if not existing.get("abstract") and incoming.get("abstract"):
        existing["abstract"] = incoming.get("abstract")
    if not existing.get("venue") and incoming.get("venue"):
        existing["venue"] = incoming.get("venue")
    if not existing.get("publisher") and incoming.get("publisher"):
        existing["publisher"] = incoming.get("publisher")
    if not existing.get("publisher_url") and incoming.get("publisher_url"):
        existing["publisher_url"] = incoming.get("publisher_url")
    if not existing.get("external_url") and incoming.get("external_url"):
        existing["external_url"] = incoming.get("external_url")
    if not existing.get("semantic_scholar_url") and incoming.get("semantic_scholar_url"):
        existing["semantic_scholar_url"] = incoming.get("semantic_scholar_url")
    if not existing.get("open_access_pdf") and incoming.get("open_access_pdf"):
        existing["open_access_pdf"] = incoming.get("open_access_pdf")
        existing["openAccessPdf"] = incoming.get("openAccessPdf")
        existing["open_access_source"] = incoming.get("open_access_source") or existing.get(
            "open_access_source"
        )
    if not existing.get("doi") and incoming.get("doi"):
        existing["doi"] = incoming.get("doi")
    if not existing.get("year") and incoming.get("year"):
        existing["year"] = incoming.get("year")
    if not existing.get("authors") and incoming.get("authors"):
        existing["authors"] = list(incoming.get("authors") or [])
    else:
        merged_authors = _dedupe_keep_order(
            [str(value or "").strip() for value in list(existing.get("authors") or []) + list(incoming.get("authors") or [])]
        )
        existing["authors"] = merged_authors[:8]
    existing["citation_count"] = max(
        _safe_int(existing.get("citation_count"), 0, 0, 10000000),
        _safe_int(incoming.get("citation_count"), 0, 0, 10000000),
    )
    existing["influential_citation_count"] = max(
        _safe_int(existing.get("influential_citation_count"), 0, 0, 10000000),
        _safe_int(incoming.get("influential_citation_count"), 0, 0, 10000000),
    )
    existing["retrieval_rrf_score"] = float(
        existing.get("retrieval_rrf_score") or 0.0
    ) + float(incoming.get("retrieval_rrf_score") or 0.0)
    existing["retrieval_query_hits"] = sorted(
        {
            _safe_int(value, 0, 0, 100)
            for value in list(existing.get("retrieval_query_hits") or [])
            + list(incoming.get("retrieval_query_hits") or [])
        }
    )
    return existing


def _merge_paper_lists(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], int]:
    """Merge ``incoming`` papers into ``existing`` (dedupe by identity key).

    Duplicates are merged into the existing record (provider support, abstract,
    OA links...) via ``_merge_paper_records``; genuinely new papers are
    appended. Returns the list and the number of newly added papers.
    """
    index: dict[str, dict[str, Any]] = {}
    for paper in existing:
        key = _paper_identity_key(paper)
        if key:
            index[key] = paper
    added = 0
    for paper in incoming:
        key = _paper_identity_key(paper)
        if key and key in index:
            _merge_paper_records(index[key], paper)
            continue
        existing.append(paper)
        if key:
            index[key] = paper
        added += 1
    return existing, added


def _normalize_provider_row(provider: str, raw: dict[str, Any]) -> dict[str, Any]:
    if provider == "openalex":
        return _normalize_openalex_work(raw)
    if provider == "crossref":
        return _normalize_crossref_work(raw)
    if provider == "arxiv":
        return _normalize_arxiv_work(raw)
    if provider == "hal":
        return _normalize_hal_work(raw)
    return _normalize_paper(raw)


def _provider_label(provider: str) -> str:
    return _DISCOVERY_PROVIDER_LABELS.get(provider, provider)


def _init_provider_stats() -> dict[str, dict[str, Any]]:
    return {
        provider: {
            "label": label,
            "raw_count": 0,
            "normalized_count": 0,
            "query_count": 0,
            "phase": "",
            "errors": [],
        }
        for provider, label in _DISCOVERY_PROVIDER_LABELS.items()
    }


def _new_result_funnel() -> dict[str, int]:
    return {"found": 0, "dropped_anchor": 0, "dropped_strict": 0, "rejected_llm": 0, "shown": 0}


def _source_status(stat: dict[str, Any]) -> tuple[str, str]:
    """(status, French detail) for one provider, from its fetch stats."""
    raw = _safe_int(stat.get("raw_count"), 0, 0, 1000000)
    queries = _safe_int(stat.get("query_count"), 0, 0, 1000000)
    errors = [str(error) for error in stat.get("errors") or [] if str(error).strip()]
    if raw:
        return "ok", f"{raw} résultat(s)"
    if errors:
        last = errors[-1]
        lowered = last.casefold()
        if "rate limit" in lowered or "circuit" in lowered or "429" in lowered:
            retry = re.search(r"retry after ([0-9.]+)s", lowered)
            wait = f", réessayez dans {max(1, round(float(retry.group(1))))} s" if retry else ""
            return "rate_limited", f"limite de requêtes atteinte{wait}"
        if "timeout" in lowered or "timed out" in lowered or "délai" in lowered:
            return "unavailable", "pas de réponse dans le délai"
        return "unavailable", f"indisponible ({_first_sentence(last, max_chars=90)})"
    if queries:
        return "empty", "aucun résultat pour cette requête"
    return "not_queried", "non interrogée (pas nécessaire)"


def _build_source_report(provider_stats: dict[str, dict[str, Any]] | None) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for provider, stat in (provider_stats or {}).items():
        status, detail = _source_status(stat)
        report.append(
            {
                "provider": provider,
                "label": str(stat.get("label") or _provider_label(provider)),
                "status": status,
                "detail": detail,
                "count": _safe_int(stat.get("raw_count"), 0, 0, 1000000),
            }
        )
    return report


def _empty_result_explanation(
    source_report: list[dict[str, Any]],
    funnel: dict[str, int],
    *,
    timed_out: bool = False,
) -> str:
    """Why a search shows no article, in the user's terms."""
    answered = [item for item in source_report if item["status"] == "ok"]
    failed = [item for item in source_report if item["status"] in {"rate_limited", "unavailable"}]
    if timed_out and not answered:
        return (
            "La recherche a dépassé sa limite de temps avant qu'une source externe "
            "ne réponde. Réessayez dans une minute."
        )
    if not answered and failed:
        names = ", ".join(f"{item['label']} ({item['detail']})" for item in failed)
        return f"Aucune source externe n'a fourni de résultat : {names}."
    if not answered:
        return "Les sources interrogées n'ont trouvé aucun article pour cette requête."
    parts = []
    if funnel.get("dropped_anchor"):
        parts.append(f"{funnel['dropped_anchor']} sans lien assez net avec les mots-clés")
    if funnel.get("dropped_strict"):
        parts.append(f"{funnel['dropped_strict']} sans abstract exploitable")
    if funnel.get("rejected_llm"):
        parts.append(f"{funnel['rejected_llm']} jugés hors sujet par le modèle")
    found = funnel.get("found") or sum(item["count"] for item in answered)
    detail = f" : {', '.join(parts)}" if parts else ""
    prefix = "La recherche a dépassé sa limite de temps. " if timed_out else ""
    return f"{prefix}{found} article(s) trouvés mais écartés{detail}. Reformulez ou élargissez la requête."


def _record_provider_fetch(
    provider_stats: dict[str, dict[str, Any]],
    provider: str,
    *,
    rows: list[dict[str, Any]] | None = None,
    phase: str = "",
    query_count: int = 0,
    error: str = "",
) -> None:
    stat = provider_stats.setdefault(
        provider,
        {
            "label": _provider_label(provider),
            "raw_count": 0,
            "normalized_count": 0,
            "query_count": 0,
            "phase": "",
            "errors": [],
        },
    )
    if rows is not None:
        stat["raw_count"] = _safe_int(stat.get("raw_count"), 0, 0, 1000000) + len(rows)
    if query_count:
        stat["query_count"] = _safe_int(stat.get("query_count"), 0, 0, 1000000) + query_count
    if phase and not stat.get("phase"):
        stat["phase"] = phase
    if error:
        errors = list(stat.get("errors") or [])
        errors.append(str(error))
        stat["errors"] = errors[:5]


def _normalized_papers_from_provider_rows(
    provider_rows: list[tuple[str, dict[str, Any]]],
    provider_stats: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    seen_papers: dict[str, dict[str, Any]] = {}
    for source_provider, raw in provider_rows:
        normalized = _normalize_provider_row(source_provider, raw)
        if not normalized.get("title"):
            continue
        if provider_stats is not None:
            stat = provider_stats.setdefault(
                source_provider,
                {
                    "label": _provider_label(source_provider),
                    "raw_count": 0,
                    "normalized_count": 0,
                    "query_count": 0,
                    "phase": "",
                    "errors": [],
                },
            )
            stat["normalized_count"] = (
                _safe_int(stat.get("normalized_count"), 0, 0, 1000000) + 1
            )
        uniq = _paper_identity_key(normalized)
        if not uniq:
            continue
        existing = seen_papers.get(uniq)
        if existing is None:
            seen_papers[uniq] = normalized
        else:
            _merge_paper_records(existing, normalized)
    return list(seen_papers.values())


def _provider_query_budget(provider: str, search_queries: list[str], *, llm_available: bool) -> list[str]:
    queries = [str(value or "").strip() for value in search_queries if str(value or "").strip()]
    if provider == "semanticscholar":
        # Facet variants improve recall, but the caller stops adaptively once
        # enough strong candidates have been found.
        limit = 3 if llm_available else 2
        return queries[:limit]
    if provider == "arxiv":
        limit = 2 if llm_available else 1
        return queries[:limit]
    if provider == "hal":
        limit = 2 if llm_available else 1
        return queries[:limit]
    if provider == "crossref":
        return queries[:2]
    if provider == "openalex":
        limit = 2 if llm_available else 1
        return queries[:limit]
    return queries[:_DISCOVERY_QUERY_PLANNER_MAX_SEARCH_QUERIES]


def _annotate_ranked_provider_rows(
    rows: list[dict[str, Any]],
    *,
    query_index: int,
) -> list[dict[str, Any]]:
    annotated: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["_discovery_rrf_score"] = round(1.0 / (60.0 + rank), 8)
        item["_discovery_query_hits"] = [query_index]
        annotated.append(item)
    return annotated


def _candidate_pool_is_sufficient(
    papers: list[dict[str, Any]],
    *,
    result_limit: int,
) -> bool:
    if not papers:
        return False
    required = max(result_limit + 3, min(result_limit * 2, 18))
    strong = [
        paper
        for paper in papers
        if paper.get("has_query_anchor")
        and _paper_has_meaningful_abstract(paper)
        and _safe_int(paper.get("query_coverage"), 0, 0, 3) >= 1
        and _safe_int(paper.get("negative_penalty"), 0, 0, 1000) < 20
    ]
    return len(strong) >= required


def _plural_variants(word: str) -> list[str]:
    if len(word) <= 3 or not word.isalpha():
        return [word]
    if word.endswith("sses"):
        return [word, word[:-2]]  # losses -> loss
    if word.endswith("ss"):
        return [word, word + "es"]  # loss -> losses
    if word.endswith("ies"):
        return [word, word[:-3] + "y"]
    if word.endswith("s"):
        return [word, word[:-1]]
    return [word, word + "s"]


@functools.lru_cache(maxsize=4096)
def _keyword_variants(term: str) -> tuple[str, ...]:
    """Spellings of one keyword: aliases, hyphen/space, singular/plural.

    Exact matching missed "Magnetization Loss" for "magnetization losses",
    "non-insulated" for "no-insulation" and YBCO papers for REBCO queries,
    and the anchor filter then discarded relevant results.
    """
    base = term.strip().lower()
    if not base:
        return ()
    variants: list[str] = list(_KEYWORD_ALIAS_MAP.get(base, [])) + [base]
    for spelling in (base, base.replace("-", " "), base.replace(" ", "-")):
        words = spelling.split(" ")
        for last in _plural_variants(words[-1]):
            variants.append(" ".join(words[:-1] + [last]))
    return tuple(_dedupe_keep_order(variants))


def _text_has_keyword(text: str, keyword: str) -> bool:
    value = str(text or "").lower()
    if not value or not str(keyword or "").strip():
        return False
    for term in _keyword_variants(str(keyword)):
        if " " in term or "-" in term:
            if term in value:
                return True
        elif len(term) <= 3:
            if re.search(rf"\b{re.escape(term)}\b", value) is not None:
                return True
        elif term in value:
            return True
    return False


def _matched_keywords(text: str, keywords: list[str]) -> list[str]:
    return [keyword for keyword in keywords if _text_has_keyword(text, keyword)]


def _query_facet_keywords(query_payload: dict[str, Any]) -> dict[str, list[str]]:
    raw = query_payload.get("facet_keywords")
    if isinstance(raw, dict):
        facets: dict[str, list[str]] = {}
        for label in ("topic", "method", "tool"):
            values = raw.get(label)
            facets[label] = _dedupe_keep_order(
                [str(value or "").strip() for value in (values or []) if str(value or "").strip()]
            )[:4]
        if any(facets.values()):
            return facets
    return _build_query_facets("", [str(value or "").strip() for value in query_payload.get("keywords") or []])


def _query_active_facet_labels(query_payload: dict[str, Any]) -> list[str]:
    facets = _query_facet_keywords(query_payload)
    return [label for label in ("topic", "method", "tool") if facets.get(label)]


def _query_needs_catalog_complement(query_payload: dict[str, Any]) -> bool:
    facets = _query_facet_keywords(query_payload)
    return bool(facets.get("topic")) and bool(facets.get("method") or facets.get("tool"))


def _query_domain_anchor_terms(query_payload: dict[str, Any]) -> list[str]:
    raw = query_payload.get("domain_anchor_terms")
    if isinstance(raw, list):
        anchors = _dedupe_keep_order(
            [_canonical_query_term(str(value or "")) for value in raw if str(value or "").strip()]
        )
        anchors = [value for value in anchors if value]
        if anchors:
            return anchors[:_DISCOVERY_DOMAIN_ANCHOR_LIMIT]
    fallback_keywords = [str(value or "").strip() for value in query_payload.get("keywords") or []]
    return _build_domain_anchor_terms("", fallback_keywords)


def _query_term_aliases(term: str) -> list[str]:
    canonical = _canonical_query_term(term).lower()
    if not canonical:
        return []
    aliases = list(_DOMAIN_ANCHOR_ALIAS_MAP.get(canonical, []))
    if not aliases:
        aliases.append(canonical)
        if canonical.endswith("s") and len(canonical) > 4:
            aliases.append(canonical[:-1])
        elif canonical.isalpha() and len(canonical) > 3 and canonical not in _SHORT_TECHNICAL_TOKENS:
            aliases.append(canonical + "s")
    return _dedupe_keep_order(aliases)


def _matched_domain_anchor_terms(text: str, domain_terms: list[str]) -> list[str]:
    matches: list[str] = []
    for term in domain_terms:
        if any(_text_has_keyword(text, alias) for alias in _query_term_aliases(term)):
            matches.append(term)
    return _dedupe_keep_order(matches)


def _negative_relevance_penalty(
    paper: dict[str, Any],
    query_payload: dict[str, Any],
    *,
    domain_matches: list[str],
    topic_matches: list[str],
    method_matches: list[str],
    tool_matches: list[str],
) -> tuple[float, list[str]]:
    title = str(paper.get("title") or "").lower()
    abstract = str(paper.get("abstract") or "").lower()
    combined = f"{title} {abstract}".strip()
    active_facets = _query_active_facet_labels(query_payload)
    domain_terms = _query_domain_anchor_terms(query_payload)
    penalty = 0.0
    reasons: list[str] = []

    if any(term in combined for term in _NEGATIVE_DOCUMENT_TERMS):
        penalty += 28.0
        reasons.append("type documentaire peu utile")

    coverage = sum(bool(matches) for matches in (topic_matches, method_matches, tool_matches))
    if domain_terms and not domain_matches:
        penalty += 30.0
        reasons.append("domaine technique absent")
    if active_facets and "topic" in active_facets and not topic_matches:
        penalty += 18.0
        reasons.append("sujet central absent")
    if "method" in active_facets and not method_matches and (tool_matches or len(topic_matches) <= 1):
        penalty += 8.0
        reasons.append("methode/formulation absente")
    if "tool" in active_facets and not tool_matches:
        penalty += 4.0
        reasons.append("outil/contexte non retrouve")
    if len(active_facets) >= 2 and coverage <= 1:
        penalty += 10.0
        reasons.append("couverture facettes faible")
    if any(term in combined for term in ("overview", "survey", "editorial", "preface")) and coverage <= 1:
        penalty += 6.0
        reasons.append("trop generique")

    return penalty, _dedupe_keep_order(reasons)[:3]


def _score_discovery_paper(paper: dict[str, Any], query_payload: dict[str, Any]) -> dict[str, Any]:
    keywords = [
        str(keyword or "").strip().lower()
        for keyword in (query_payload.get("keywords") or [])
        if str(keyword or "").strip()
        and str(keyword or "").strip().lower() not in _GENERIC_DISCOVERY_TERMS
    ]
    facet_keywords = _query_facet_keywords(query_payload)
    domain_anchor_terms = _query_domain_anchor_terms(query_payload)
    title = str(paper.get("title") or "").lower()
    abstract = str(paper.get("abstract") or "").lower()
    combined = f"{title} {abstract}".strip()
    matched: list[str] = []
    score = 0.0

    topic_title_matches = _matched_keywords(title, facet_keywords.get("topic") or [])
    topic_abstract_matches = _matched_keywords(abstract, facet_keywords.get("topic") or [])
    method_title_matches = _matched_keywords(title, facet_keywords.get("method") or [])
    method_abstract_matches = _matched_keywords(abstract, facet_keywords.get("method") or [])
    tool_title_matches = _matched_keywords(title, facet_keywords.get("tool") or [])
    tool_abstract_matches = _matched_keywords(abstract, facet_keywords.get("tool") or [])
    domain_matches = _matched_domain_anchor_terms(combined, domain_anchor_terms)

    matched.extend(topic_title_matches + topic_abstract_matches)
    matched.extend(method_title_matches + method_abstract_matches)
    matched.extend(tool_title_matches + tool_abstract_matches)
    matched.extend(domain_matches)
    score += 17.0 * len(_dedupe_keep_order(topic_title_matches))
    score += 11.0 * len(
        [item for item in _dedupe_keep_order(topic_abstract_matches) if item not in topic_title_matches]
    )
    score += 11.0 * len(_dedupe_keep_order(method_title_matches))
    score += 8.0 * len(
        [item for item in _dedupe_keep_order(method_abstract_matches) if item not in method_title_matches]
    )
    score += 8.0 * len(_dedupe_keep_order(tool_title_matches))
    score += 6.0 * len(
        [item for item in _dedupe_keep_order(tool_abstract_matches) if item not in tool_title_matches]
    )

    generic_matches: list[str] = []
    for keyword in keywords:
        if keyword in matched:
            continue
        if _text_has_keyword(title, keyword):
            generic_matches.append(keyword)
            score += 10.0
        elif _text_has_keyword(abstract, keyword):
            generic_matches.append(keyword)
            score += 6.0
    matched.extend(generic_matches)

    active_facets = _query_active_facet_labels(query_payload)
    coverage = sum(
        bool(matches)
        for matches in (
            _dedupe_keep_order(topic_title_matches + topic_abstract_matches),
            _dedupe_keep_order(method_title_matches + method_abstract_matches),
            _dedupe_keep_order(tool_title_matches + tool_abstract_matches),
        )
    )
    abstract_coverage = sum(
        bool(matches)
        for matches in (
            _dedupe_keep_order(topic_abstract_matches),
            _dedupe_keep_order(method_abstract_matches),
            _dedupe_keep_order(tool_abstract_matches),
        )
    )
    if coverage >= 2:
        score += 10.0
    if coverage >= 3:
        score += 6.0
    if abstract_coverage >= 2:
        score += 8.0
    if abstract_coverage >= 3:
        score += 4.0

    citation_count = _safe_int(paper.get("citation_count"), 0, 0, 10000000)
    influential_count = _safe_int(paper.get("influential_citation_count"), 0, 0, 10000000)
    has_anchor = bool(domain_matches) if domain_anchor_terms else bool(topic_title_matches or topic_abstract_matches)
    if not has_anchor and not facet_keywords.get("topic"):
        has_anchor = bool(matched)
    if has_anchor:
        score += min(8.0, citation_count / 90.0)
        score += min(4.0, influential_count / 20.0)
        provider_support = _safe_int(paper.get("provider_support_count"), 1, 1, 5)
        if provider_support > 1:
            score += min(6.0, (provider_support - 1) * 3.0)

    year = _paper_year_int(paper)
    current_year = time.gmtime().tm_year
    if has_anchor:
        if year >= current_year - 2:
            score += 4.0
        elif year >= current_year - 5:
            score += 2.0
        elif year >= current_year - 10:
            score += 1.0

    if has_anchor and paper.get("open_access_pdf"):
        score += 2.0

    negative_penalty, negative_reasons = _negative_relevance_penalty(
        paper,
        query_payload,
        domain_matches=domain_matches,
        topic_matches=_dedupe_keep_order(topic_title_matches + topic_abstract_matches),
        method_matches=_dedupe_keep_order(method_title_matches + method_abstract_matches),
        tool_matches=_dedupe_keep_order(tool_title_matches + tool_abstract_matches),
    )
    score -= negative_penalty

    reasons: list[str] = []
    if matched:
        reasons.append("mots-cles: " + ", ".join(_dedupe_keep_order(matched)[:4]))
    else:
        reasons.append("ancrage lexical faible")
    if active_facets:
        reasons.append(f"facettes couvertes: {coverage}/{len(active_facets)}")
    if domain_anchor_terms:
        reasons.append(f"ancrage domaine: {len(domain_matches)}/{len(domain_anchor_terms)}")
    reasons.extend(negative_reasons[:2])
    if citation_count:
        reasons.append(f"{citation_count} citation(s)")
    if _safe_int(paper.get("provider_support_count"), 1, 1, 10) > 1:
        reasons.append(f"confirme par {_safe_int(paper.get('provider_support_count'), 1, 1, 10)} sources")
    if paper.get("open_access_pdf"):
        reasons.append("PDF OA")
    if year:
        reasons.append(str(year))

    paper["matched_keywords"] = _dedupe_keep_order(matched)[:6]
    paper["matched_topic_keywords"] = _dedupe_keep_order(topic_title_matches + topic_abstract_matches)[:4]
    paper["matched_method_keywords"] = _dedupe_keep_order(method_title_matches + method_abstract_matches)[:4]
    paper["matched_tool_keywords"] = _dedupe_keep_order(tool_title_matches + tool_abstract_matches)[:4]
    paper["matched_domain_keywords"] = domain_matches[:4]
    paper["domain_anchor_required"] = bool(domain_anchor_terms)
    paper["domain_anchor_terms"] = domain_anchor_terms[:4]
    paper["domain_anchor_coverage"] = len(domain_matches)
    paper["has_query_anchor"] = has_anchor
    paper["query_coverage"] = coverage
    paper["abstract_coverage"] = abstract_coverage
    paper["negative_penalty"] = int(round(negative_penalty))
    paper["discovery_score"] = int(round(score))
    paper["score_reasons"] = reasons[:4]
    paper["citation_count"] = citation_count
    paper["influential_citation_count"] = influential_count
    return paper


def _paper_abstract_text(paper: dict[str, Any]) -> str:
    return _strip_abstract_label(str(paper.get("abstract") or ""))


def _paper_abstract_for_prompt(
    paper: dict[str, Any], *, limit: int, placeholder: str = _MISSING_ABSTRACT_PROMPT
) -> str:
    abstract = _paper_abstract_text(paper)
    if not abstract:
        return placeholder
    return abstract[:limit]


def _present_paper(paper: dict[str, Any], *, notebook_related: bool = False) -> dict[str, Any]:
    paper["abstract"] = _paper_abstract_text(paper)
    paper["abstract_available"] = _paper_has_meaningful_abstract(paper)
    paper["abstract_preview"] = _paper_abstract_preview(paper, max_chars=260)
    paper["authors_line"] = ", ".join((paper.get("authors") or [])[:5]) or "Auteur inconnu"
    paper["download_ready"] = bool(paper.get("open_access_pdf"))
    if not paper.get("short_summary"):
        paper["short_summary"] = _first_sentence(paper.get("abstract", ""))
    if notebook_related:
        paper["notebook_related"] = True
    return paper


def _paper_abstract_preview(paper: dict[str, Any], *, max_chars: int = 260) -> str:
    preview = _first_sentence(_paper_abstract_text(paper), max_chars=max_chars)
    return preview or _MISSING_ABSTRACT_PREVIEW


async def _enrich_missing_abstracts(
    papers: list[dict[str, Any]],
    client: httpx.AsyncClient,
    *,
    mailto: str = "",
    max_papers: int = _DISCOVERY_ABSTRACT_ENRICHMENT_MAX_PAPERS,
) -> int:
    enriched = 0
    targets: list[tuple[dict[str, Any], str]] = []
    for paper in papers:
        if len(targets) >= max_papers:
            break
        if _paper_has_meaningful_abstract(paper):
            continue
        doi = _canonicalize_doi(str(paper.get("doi") or "").strip())
        if not doi:
            continue
        targets.append((paper, doi))

    semantic_by_doi: dict[str, dict[str, Any]] = {}
    if len(targets) >= 2 and _has_discovery_budget(2.0, reserve=4.0):
        try:
            rows = await _semantic_papers_batch(
                client,
                [f"DOI:{doi}" for _paper, doi in targets],
                fields="title,abstract,authors,year,externalIds,openAccessPdf,paperId,venue,url,citationCount,influentialCitationCount",
            )
            for row in rows:
                candidate = _normalize_paper(row)
                candidate_doi = _canonicalize_doi(str(candidate.get("doi") or ""))
                if candidate_doi and _paper_has_meaningful_abstract(candidate):
                    semantic_by_doi[candidate_doi] = candidate
        except Exception:
            logger.debug("Recovered from an error in _enrich_missing_abstracts.", exc_info=True)
            semantic_by_doi = {}

    for paper, doi in targets:
        normalized: dict[str, Any] | None = semantic_by_doi.get(doi)
        # Preserve the cheap single-item path (and its compatibility with
        # existing providers); multi-item enrichment always uses one batch.
        if normalized is None and len(targets) == 1:
            try:
                semantic_payload = await _semantic_paper_by_doi(client, doi)
                if semantic_payload:
                    candidate = _normalize_paper(semantic_payload)
                    if _paper_has_meaningful_abstract(candidate):
                        normalized = candidate
            except Exception:
                logger.debug("Recovered from an error in _enrich_missing_abstracts.", exc_info=True)
                normalized = None
        if normalized is None:
            if not _has_discovery_budget(0.8, reserve=3.0):
                break
            try:
                openalex_payload = await _openalex_work_by_doi(client, doi)
                if openalex_payload:
                    candidate = _normalize_openalex_work(openalex_payload)
                    if _paper_has_meaningful_abstract(candidate):
                        normalized = candidate
            except Exception:
                logger.debug("Recovered from an error in _enrich_missing_abstracts.", exc_info=True)
                normalized = None
        if normalized is None:
            if not _has_discovery_budget(0.8, reserve=2.0):
                break
            try:
                crossref_payload = await _crossref_work_by_doi(client, doi, mailto=mailto)
                if crossref_payload:
                    candidate = _normalize_crossref_work(crossref_payload)
                    if _paper_has_meaningful_abstract(candidate):
                        normalized = candidate
            except Exception:
                logger.debug("Recovered from an error in _enrich_missing_abstracts.", exc_info=True)
                normalized = None
        if normalized is None:
            continue
        before = _paper_has_meaningful_abstract(paper)
        _merge_paper_records(paper, normalized)
        after = _paper_has_meaningful_abstract(paper)
        if not before and after:
            enriched += 1
    return enriched


def _rank_discovery_papers(
    papers: list[dict[str, Any]],
    query_payload: dict[str, Any],
    *,
    require_query_anchor: bool = False,
    minimum_domain_anchor_coverage: int = 1,
) -> tuple[list[dict[str, Any]], int]:
    for paper in papers:
        _score_discovery_paper(paper, query_payload)

    minimum_anchor_coverage = max(1, int(minimum_domain_anchor_coverage))
    anchored_papers = [
        paper
        for paper in papers
        if paper.get("has_query_anchor")
        and _safe_int(paper.get("domain_anchor_coverage"), 0, 0, 100)
        >= minimum_anchor_coverage
    ]
    dropped_count = 0
    if require_query_anchor:
        dropped_count = len(papers) - len(anchored_papers)
        papers = anchored_papers
    elif anchored_papers:
        dropped_count = len(papers) - len(anchored_papers)
        papers = anchored_papers

    papers.sort(
        key=lambda paper: (
            _safe_int(paper.get("discovery_score"), 0, 0, 1000000),
            _safe_int(paper.get("query_coverage"), 0, 0, 10),
            _safe_int(paper.get("abstract_coverage"), 0, 0, 10),
            _paper_year_int(paper),
            _safe_int(paper.get("citation_count"), 0, 0, 10000000),
        ),
        reverse=True,
    )
    return papers, dropped_count


@functools.lru_cache(maxsize=8192)
def _cached_paper_signature_tokens(
    title: str,
    venue: str,
    matched_keywords: tuple[str, ...],
) -> frozenset[str]:
    text = " ".join([title, venue, " ".join(matched_keywords)])
    return frozenset(
        token
        for token in _tokenize_search_text(text)
        if len(token) >= 3 and token not in _DIVERSITY_STOPWORDS
    )


def _paper_signature_tokens(paper: dict[str, Any]) -> frozenset[str]:
    return _cached_paper_signature_tokens(
        str(paper.get("title") or ""),
        str(paper.get("venue") or ""),
        tuple(str(item or "") for item in paper.get("matched_keywords") or []),
    )


def _paper_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_doi = str(left.get("doi") or "").strip().lower()
    right_doi = str(right.get("doi") or "").strip().lower()
    if left_doi and right_doi and left_doi == right_doi:
        return 1.0
    left_tokens = _paper_signature_tokens(left)
    right_tokens = _paper_signature_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    if union <= 0:
        return 0.0
    similarity = intersection / union
    if (
        str(left.get("venue") or "").strip().lower()
        and str(left.get("venue") or "").strip().lower()
        == str(right.get("venue") or "").strip().lower()
    ):
        similarity += 0.08
    return min(1.0, similarity)


def _paper_selection_score(paper: dict[str, Any]) -> float:
    precomputed = paper.get("semantic_rank_score")
    if isinstance(precomputed, (int, float)) and not isinstance(precomputed, bool):
        return float(precomputed)
    base_score = float(
        _safe_int(
            paper.get("validated_score", paper.get("discovery_score")),
            0,
            0,
            1000000,
        )
    )
    if paper.get("llm_relevance_score") is not None:
        base_score += 1000.0
    return base_score


def _assign_semantic_rank_scores(papers: list[dict[str, Any]]) -> None:
    """Compute a normalized composite rank score dominated by semantic signals.

    Tiers preserve the LLM judge as a gate (accepted-judged > unjudged >
    rejected). Within a tier, papers are ordered by a weighted blend of the
    LLM relevance, cross-encoder, dense (embedding) and (weak) lexical signals,
    each normalized to 0..1. ``citation_count`` is deliberately excluded: it
    biases toward popular off-topic papers. The result is stored on each paper
    as ``semantic_rank_score`` and consumed by ``_paper_selection_score``.
    """
    if not papers:
        return

    ce_values = [
        float(paper["reranker_score"])
        for paper in papers
        if isinstance(paper.get("reranker_score"), (int, float))
        and not isinstance(paper.get("reranker_score"), bool)
    ]
    ce_lo = min(ce_values) if ce_values else 0.0
    ce_hi = max(ce_values) if ce_values else 0.0
    lex_values = [
        float(_safe_int(paper.get("discovery_score"), 0, -1000000, 1000000)) for paper in papers
    ]
    lex_lo = min(lex_values) if lex_values else 0.0
    lex_hi = max(lex_values) if lex_values else 0.0
    rrf_values = [float(paper.get("retrieval_rrf_score") or 0.0) for paper in papers]
    rrf_lo = min(rrf_values) if rrf_values else 0.0
    rrf_hi = max(rrf_values) if rrf_values else 0.0

    def _norm(value: float, lo: float, hi: float) -> float:
        if hi <= lo:
            return 0.0
        return max(0.0, min(1.0, (value - lo) / (hi - lo)))

    for paper in papers:
        llm_score = paper.get("llm_relevance_score")
        rejected = bool(paper.get("llm_rejected"))
        judged = llm_score is not None and not rejected
        if rejected:
            tier = 0
        elif judged:
            tier = 2
        else:
            tier = 1

        signals: list[tuple[float, float]] = []
        if judged:
            signals.append(
                (0.45, max(0.0, min(1.0, float(_safe_int(llm_score, 0, 0, 100)) / 100.0)))
            )
            ce_weight, dense_weight, rrf_weight, lex_weight = 0.30, 0.13, 0.09, 0.03
        else:
            ce_weight, dense_weight, rrf_weight, lex_weight = 0.50, 0.25, 0.15, 0.10

        ce_raw = paper.get("reranker_score")
        if isinstance(ce_raw, (int, float)) and not isinstance(ce_raw, bool):
            signals.append((ce_weight, _norm(float(ce_raw), ce_lo, ce_hi)))
        dense_raw = paper.get("dense_score")
        if isinstance(dense_raw, (int, float)) and not isinstance(dense_raw, bool):
            signals.append((dense_weight, max(0.0, min(1.0, float(dense_raw)))))
        rrf_raw = float(paper.get("retrieval_rrf_score") or 0.0)
        if rrf_raw > 0.0:
            signals.append((rrf_weight, _norm(rrf_raw, rrf_lo, rrf_hi)))
        signals.append(
            (
                lex_weight,
                _norm(
                    float(_safe_int(paper.get("discovery_score"), 0, -1000000, 1000000)),
                    lex_lo,
                    lex_hi,
                ),
            )
        )

        weight_sum = sum(weight for weight, _ in signals)
        composite = (
            sum(weight * value for weight, value in signals) / weight_sum
            if weight_sum > 0
            else 0.0
        )
        paper["semantic_composite"] = round(composite, 4)
        paper["semantic_rank_score"] = round(tier * 1000.0 + composite * 100.0, 4)


def _select_diverse_top_papers(
    papers: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    remaining = list(papers)
    selected: list[dict[str, Any]] = []
    capped_limit = _safe_int(limit, 10, 1, 30)
    while remaining and len(selected) < capped_limit:
        best_idx = 0
        best_score: float | None = None
        best_penalty = 0.0
        for idx, paper in enumerate(remaining):
            base_score = _paper_selection_score(paper)
            max_similarity = max((_paper_similarity(paper, chosen) for chosen in selected), default=0.0)
            penalty = max_similarity * 18.0
            adjusted_score = base_score - penalty
            if best_score is None or adjusted_score > best_score:
                best_idx = idx
                best_score = adjusted_score
                best_penalty = penalty
        chosen = remaining.pop(best_idx)
        chosen["diversity_penalty"] = int(round(best_penalty))
        chosen["final_rank_score"] = int(round(best_score or 0.0))
        selected.append(chosen)
    return selected


def _strong_snowball_seed_dois(
    papers: list[dict[str, Any]],
    *,
    limit: int = _DISCOVERY_SNOWBALL_SEEDS,
) -> list[str]:
    strong = [
        paper
        for paper in papers
        if paper.get("has_query_anchor")
        and _paper_has_meaningful_abstract(paper)
        and _safe_int(paper.get("negative_penalty"), 0, 0, 1000) < 20
        and (
            _safe_int(paper.get("query_coverage"), 0, 0, 3) >= 2
            or _safe_int(paper.get("domain_anchor_coverage"), 0, 0, 10) >= 1
        )
    ]
    return _dedupe_keep_order(
        [str(paper.get("doi") or "").strip() for paper in strong if paper.get("doi")]
    )[: max(1, limit)]


def _provider_query_terms(query: str, *, limit: int = 8) -> list[str]:
    terms: list[str] = []
    protected = _acronym_short_tokens(query)
    for token in _tokenize_search_text(query):
        if len(token) < 3 and token not in _SHORT_TECHNICAL_TOKENS and token not in protected:
            continue
        if token in _STOPWORDS or token in _GENERIC_DISCOVERY_TERMS or token.isdigit():
            continue
        canonical = _canonical_query_term(token)
        if canonical:
            terms.append(canonical)
    return _dedupe_keep_order(terms)[:limit]


def _arxiv_query(query: str) -> str:
    terms = _provider_query_terms(query, limit=8)
    if not terms:
        normalized = _normalize_search_text(query).strip()
        return f"all:{normalized}" if normalized else "all:scientific"
    return " AND ".join(f"all:{term}" for term in terms)


def _hal_escape_solr_term(term: str) -> str:
    return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", term)


def _hal_query(query: str) -> str:
    terms = [_hal_escape_solr_term(term) for term in _provider_query_terms(query, limit=8)]
    if not terms:
        normalized = _normalize_search_text(query).strip()
        return f"text:{_hal_escape_solr_term(normalized)}" if normalized else "text:scientific"
    return "text:(" + " ".join(terms) + ")"


def _hal_author_query(name: str) -> str:
    # A phrase, not AND: on a collaboration paper two different authors would
    # otherwise supply the first name and the surname.
    tokens = [token for token in _author_name_tokens(name) if len(token) > 1]
    if not tokens:
        return "authFullName_t:scientific"
    phrases = [" ".join(tokens)]
    if len(tokens) == 2:
        phrases.append(" ".join(reversed(tokens)))
    return "authFullName_t:(" + " OR ".join(f'"{phrase}"' for phrase in phrases) + ")"


def _arxiv_entry_text(entry: ET.Element, name: str) -> str:
    child = entry.find(f"atom:{name}", {"atom": "http://www.w3.org/2005/Atom"})
    return str(child.text or "").strip() if child is not None else ""


def _parse_arxiv_atom(text: str) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    ns = {
        "atom": "http://www.w3.org/2005/Atom",
        "arxiv": "http://arxiv.org/schemas/atom",
    }
    rows: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        links = []
        for link in entry.findall("atom:link", ns):
            if not isinstance(link.attrib, dict):
                continue
            links.append(dict(link.attrib))
        pdf_url = ""
        abstract_url = ""
        for link_payload in links:
            href = str(link_payload.get("href") or "").strip()
            if not href:
                continue
            if str(link_payload.get("title") or "").lower() == "pdf" or "application/pdf" in str(
                link_payload.get("type") or ""
            ).lower():
                pdf_url = href
            elif str(link_payload.get("rel") or "").lower() == "alternate":
                abstract_url = href

        authors = []
        for author in entry.findall("atom:author", ns):
            name = author.find("atom:name", ns)
            text_value = str(name.text or "").strip() if name is not None else ""
            if text_value:
                authors.append(text_value)

        categories = [
            str(category.attrib.get("term") or "").strip()
            for category in entry.findall("atom:category", ns)
            if str(category.attrib.get("term") or "").strip()
        ]
        primary = entry.find("arxiv:primary_category", ns)
        primary_category = (
            str(primary.attrib.get("term") or "").strip() if primary is not None else ""
        )
        doi = entry.find("arxiv:doi", ns)
        journal_ref = entry.find("arxiv:journal_ref", ns)
        rows.append(
            {
                "id": _arxiv_entry_text(entry, "id"),
                "title": _arxiv_entry_text(entry, "title"),
                "summary": _arxiv_entry_text(entry, "summary"),
                "published": _arxiv_entry_text(entry, "published"),
                "updated": _arxiv_entry_text(entry, "updated"),
                "doi": str(doi.text or "").strip() if doi is not None else "",
                "journal_ref": str(journal_ref.text or "").strip()
                if journal_ref is not None
                else "",
                "authors": authors,
                "categories": categories,
                "primary_category": primary_category,
                "abstract_url": abstract_url,
                "pdf_url": pdf_url,
            }
        )
    return rows


async def _arxiv_search(
    client: httpx.AsyncClient,
    query: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    payload = await _get_text_with_retry(
        client,
        _ARXIV_BASE,
        params={
            "search_query": _arxiv_query(query),
            "start": 0,
            "max_results": _safe_int(limit, 10, 1, 30),
            "sortBy": "relevance",
            "sortOrder": "descending",
        },
        api_name="arxiv",
    )
    return _parse_arxiv_atom(payload)


async def _hal_search(
    client: httpx.AsyncClient,
    query: str,
    *,
    limit: int = 10,
    author: str = "",
) -> list[dict[str, Any]]:
    params = {
        "q": _hal_author_query(author) if author else _hal_query(query),
        "fq": "docType_s:(" + " OR ".join(_HAL_DISCOVERY_DOC_TYPES) + ")",
        "rows": _safe_int(limit, 10, 1, 30),
        "sort": "producedDate_tdate desc" if author else "score desc",
        "wt": "json",
        "fl": ",".join(
            [
                "docid",
                "halId_s",
                "uri_s",
                "title_s",
                "en_title_s",
                "fr_title_s",
                "abstract_s",
                "en_abstract_s",
                "fr_abstract_s",
                "producedDateY_i",
                "publicationDateY_i",
                "submittedDateY_i",
                "authFullName_s",
                "journalTitle_s",
                "journalPublisher_s",
                "conferenceTitle_s",
                "doiId_s",
                "fileMain_s",
                "docType_s",
            ]
        ),
    }
    payload = await _get_json_with_retry(
        client,
        _HAL_BASE,
        params=params,
        api_name="hal",
    )
    response = _dict_payload(payload.get("response"))
    docs = response.get("docs")
    if not isinstance(docs, list):
        return []
    return [row for row in docs if isinstance(row, dict)]


async def _semantic_search(
    client: httpx.AsyncClient,
    query: str,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    params = {
        "query": query,
        "limit": _safe_int(limit, 10, 1, 30),
        "fields": "title,abstract,authors,year,externalIds,openAccessPdf,paperId,venue,url,citationCount,influentialCitationCount",
    }
    payload = await _get_json_with_retry(
        client,
        f"{_S2_BASE}/graph/v1/paper/search",
        params=params,
        api_name="semanticscholar",
        # Retries are owned by _semantic_search_with_retries. Keeping a single
        # layer prevents a 3x3 retry explosion for one logical query.
        retries=0,
    )
    rows = payload.get("data")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


async def _semantic_search_with_retries(
    client: httpx.AsyncClient,
    query: str,
    *,
    limit: int = 10,
    attempts: int = _DISCOVERY_SEMANTIC_QUERY_ATTEMPTS,
) -> list[dict[str, Any]]:
    max_attempts = _safe_int(attempts, _DISCOVERY_SEMANTIC_QUERY_ATTEMPTS, 1, 8)
    last_exc: Exception | None = None
    for attempt_index in range(max_attempts):
        try:
            return await _semantic_search(client, query, limit=limit)
        except Exception as exc:
            last_exc = exc
            if attempt_index + 1 >= max_attempts:
                raise
            _request_metrics()["retries"] += 1
            await _budgeted_sleep(_semantic_retry_delay_seconds(attempt_index, exc))
    if last_exc is not None:
        raise last_exc
    return []


async def _semantic_paper_by_doi(
    client: httpx.AsyncClient,
    doi: str,
) -> dict[str, Any] | None:
    safe_doi = _canonicalize_doi(doi)
    if not safe_doi:
        return None
    payload = await _get_json_with_retry(
        client,
        f"{_S2_BASE}/graph/v1/paper/{quote(f'DOI:{safe_doi}', safe='')}",
        params={
            "fields": "title,abstract,authors,year,externalIds,openAccessPdf,paperId,venue,url,citationCount,influentialCitationCount"
        },
        api_name="semanticscholar",
    )
    return payload if isinstance(payload, dict) else None


async def _openalex_search(
    client: httpx.AsyncClient,
    query: str,
    *,
    limit: int = 10,
    strict_title_abstract: bool = False,
) -> list[dict[str, Any]]:
    params = {
        "per-page": _safe_int(limit, 10, 1, 30),
        "select": ",".join(
            [
                "id",
                "display_name",
                "doi",
                "publication_year",
                "authorships",
                "primary_location",
                "best_oa_location",
                "abstract_inverted_index",
                "cited_by_count",
            ]
        ),
    }
    if strict_title_abstract:
        params["filter"] = f"title_and_abstract.search:{query}"
    else:
        params["search"] = query
    payload = await _get_json_with_retry(
        client,
        f"{_OPENALEX_BASE}/works",
        params=params,
        api_name="openalex",
    )
    rows = payload.get("results")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


async def _openalex_work_by_doi(
    client: httpx.AsyncClient,
    doi: str,
) -> dict[str, Any] | None:
    safe_doi = _canonicalize_doi(doi)
    if not safe_doi:
        return None
    params = {
        "filter": f"doi:{safe_doi}",
        "per-page": 1,
        "select": ",".join(
            [
                "id",
                "display_name",
                "doi",
                "publication_year",
                "authorships",
                "primary_location",
                "best_oa_location",
                "abstract_inverted_index",
                "cited_by_count",
            ]
        ),
    }
    payload = await _get_json_with_retry(
        client,
        f"{_OPENALEX_BASE}/works",
        params=params,
        api_name="openalex",
    )
    rows = payload.get("results")
    if not isinstance(rows, list) or not rows:
        return None
    first = rows[0]
    return first if isinstance(first, dict) else None


async def _crossref_search(
    client: httpx.AsyncClient,
    query: str,
    *,
    limit: int = 10,
    mailto: str = "",
    author: str = "",
) -> list[dict[str, Any]]:
    params = {
        ("query.author" if author else "query.bibliographic"): author or query,
        "rows": _safe_int(limit, 10, 1, 30),
        "sort": "score",
        "order": "desc",
        "select": ",".join(
            [
                "DOI",
                "URL",
                "title",
                "abstract",
                "author",
                "container-title",
                "publisher",
                "published-print",
                "published-online",
                "issued",
                "created",
                "is-referenced-by-count",
                "link",
            ]
        ),
    }
    if mailto:
        params["mailto"] = mailto
    payload = await _get_json_with_retry(
        client,
        f"{_CROSSREF_BASE}/works",
        params=params,
        api_name="crossref",
    )
    message = _dict_payload(payload.get("message"))
    rows = message.get("items")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


async def _crossref_work_by_doi(
    client: httpx.AsyncClient,
    doi: str,
    *,
    mailto: str = "",
) -> dict[str, Any] | None:
    safe_doi = _canonicalize_doi(doi)
    if not safe_doi:
        return None
    params = {"mailto": mailto} if mailto else None
    payload = await _get_json_with_retry(
        client,
        f"{_CROSSREF_BASE}/works/{quote(safe_doi, safe='')}",
        params=params,
        api_name="crossref",
    )
    message = payload.get("message") if isinstance(payload.get("message"), dict) else None
    return message if isinstance(message, dict) else None


async def _semantic_papers_batch(
    client: httpx.AsyncClient,
    identifiers: list[str],
    *,
    fields: str,
) -> list[dict[str, Any]]:
    safe_ids = _dedupe_keep_order(
        [str(value or "").strip() for value in identifiers if str(value or "").strip()]
    )[:500]
    if not safe_ids:
        return []
    payload = await _post_json_with_retry(
        client,
        f"{_S2_BASE}/graph/v1/paper/batch",
        params={"fields": fields},
        json_body={"ids": safe_ids},
        api_name="semanticscholar",
    )
    rows = payload if isinstance(payload, list) else []
    return [row for row in rows if isinstance(row, dict)]


async def _semantic_recommendations_for_dois(
    client: httpx.AsyncClient,
    seed_dois: list[str],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    canonical_dois = _dedupe_keep_order(
        [_canonicalize_doi(value) for value in seed_dois if _canonicalize_doi(value)]
    )[:8]
    if not canonical_dois:
        return []
    resolved = await _semantic_papers_batch(
        client,
        [f"DOI:{doi}" for doi in canonical_dois],
        fields="paperId",
    )
    paper_ids = _dedupe_keep_order(
        [str(row.get("paperId") or "").strip() for row in resolved]
    )
    if not paper_ids:
        return []
    payload = await _post_json_with_retry(
        client,
        f"{_S2_BASE}/recommendations/v1/papers",
        params={
            "limit": _safe_int(limit, 12, 1, 100),
            "fields": "title,abstract,authors,year,externalIds,openAccessPdf,paperId,venue,url,citationCount,influentialCitationCount",
        },
        json_body={"positivePaperIds": paper_ids[:8]},
        api_name="semanticscholar",
    )
    recs = payload.get("recommendedPapers") if isinstance(payload, dict) else None
    if not isinstance(recs, list) and isinstance(payload, dict):
        recs = payload.get("data")
    return [row for row in (recs or []) if isinstance(row, dict)]


async def _unpaywall_pdf_url(
    client: httpx.AsyncClient,
    doi: str,
    email: str,
) -> str:
    payload = await _get_json_with_retry(
        client,
        f"{_UNPAYWALL_BASE}/{quote(doi, safe='')}",
        params={"email": email},
        api_name="unpaywall",
    )
    best = payload.get("best_oa_location")
    if isinstance(best, dict):
        best_pdf = str(best.get("url_for_pdf") or "").strip()
        if best_pdf:
            return best_pdf
    locations = payload.get("oa_locations")
    if isinstance(locations, list):
        for entry in locations:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url_for_pdf") or "").strip()
            if url:
                return url
    return ""


def _short_title_slug(title: str, doi: str) -> str:
    source = title or doi or "paper"
    source = _NON_WORD_RE.sub("_", source).strip("_").lower()
    if not source:
        source = "paper"
    return source[:90]


async def _generate_query_payload_with_llm(
    input_text: str,
    model_name: str | None,
    debug_events: list[dict[str, Any]] | None = None,
    abort_signal: StreamAbortSignal | None = None,
) -> dict[str, Any]:
    text = (input_text or "").strip()
    heuristic_payload = _build_heuristic_query_payload(text, generator="heuristic")
    dois = heuristic_payload["dois"]
    keywords = heuristic_payload["keywords"]

    if not text or not model_name:
        return heuristic_payload

    prompt = (
        "Tu es un assistant de recherche academique.\n"
        "A partir du texte utilisateur, construis une recherche bibliographique precise.\n"
        "OBLIGATOIRE: formule search_query et search_queries en anglais academique "
        "(les bases scientifiques sont indexees en anglais).\n"
        "Developpe les acronymes specialises en gardant le sigle, ex: 'NI (no-insulation)', "
        "'MI (metal insulation)'. Conserve les sigles courts (MI, NI, HTS) dans les requetes.\n"
        "Evite les termes generiques seuls; privilegie concepts, methodes, populations, objets et effets.\n"
        "Retourne uniquement un JSON strict avec:\n"
        "{\n"
        '  "keywords": ["..."],\n'
        '  "dois": ["uniquement les DOI écrits dans le texte utilisateur, sinon []"],\n'
        '  "search_query": "requete principale concise",\n'
        '  "search_queries": ["requete precise", "reformulation proche"],\n'
        '  "topic_summary": "synthese scientifique du sujet, 2 phrases max"\n'
        "}\n"
        "Contraintes: keywords max 10, dois max 8, search_queries max 4, chaque requete <= 12 mots.\n"
        "Chaque requete doit garder les concepts centraux; n'ajoute pas de variante large/generique.\n"
        "Pas de commentaire hors JSON.\n"
        f"{get_discovery_prompt_note(model_name, 'query')}\n\n"
        f"Texte utilisateur:\n{text}\n"
    )

    try:
        raw = await _invoke_discovery_llm_text(
            prompt=prompt,
            model_name=model_name,
            task="query",
            abort_signal=abort_signal,
        )
        parsed = _parse_json_obj(raw or "")
        extracted = _extract_query_payload_fields(parsed)
        _record_llm_debug(
            debug_events,
            stage="generation_requete",
            prompt=prompt,
            raw_output=raw or "",
            parsed=parsed if parsed else extracted,
        )
        if extracted:
            llm_keywords = extracted.get("keywords") or []
            llm_dois = _grounded_dois(list(extracted.get("dois") or []), text)
            query = str(extracted.get("search_query") or "").strip()
            search_queries = extracted.get("search_queries") or []
            keywords = _dedupe_keep_order(llm_keywords + keywords)[:8]
            dois = _dedupe_keep_order(llm_dois + dois)[:8]
            if not query:
                query = search_queries[0] if search_queries else " ".join(keywords[:6])
            heuristic_payload = _build_heuristic_query_payload(text, generator="llm_local_heuristic_seed")
            merged_search_queries = _dedupe_keep_order(
                [query]
                + search_queries
                + (heuristic_payload.get("search_queries") or [])
            )[:_DISCOVERY_QUERY_PLANNER_MAX_SEARCH_QUERIES]
            return {
                "keywords": keywords,
                "dois": dois,
                "search_query": query,
                "search_queries": merged_search_queries
                if merged_search_queries
                else _build_search_queries(keywords, query, source_text=text),
                "native_search_queries": heuristic_payload.get("native_search_queries")
                or _native_search_queries(text),
                "query_facets": heuristic_payload.get("query_facets") or [],
                "facet_keywords": heuristic_payload.get("facet_keywords") or {},
                "negative_terms": heuristic_payload.get("negative_terms") or [],
                "topic_summary": str(extracted.get("topic_summary") or "").strip(),
                "generator": "llm_local",
            }
    except Exception as exc:
        logger.debug("Recovered from an error in _generate_query_payload_with_llm.", exc_info=True)
        _record_llm_debug(
            debug_events,
            stage="generation_requete",
            prompt=prompt,
            error=_llm_error_text(exc),
        )

    return heuristic_payload


async def _generate_query_payload_with_planner(
    input_text: str,
    model_name: str | None,
    debug_events: list[dict[str, Any]] | None = None,
    abort_signal: StreamAbortSignal | None = None,
) -> dict[str, Any]:
    text = (input_text or "").strip()
    if not text or not model_name:
        return await _generate_query_payload_with_llm(
            input_text,
            model_name,
            debug_events,
            abort_signal,
        )

    prompt = (
        "Tu es un query planner pour une recherche bibliographique scientifique.\n"
        "Analyse la demande utilisateur et construis un plan de recherche multi-facettes.\n"
        "Le but n'est pas de repondre completement, mais d'identifier les angles documentaires utiles.\n"
        "OBLIGATOIRE: formule search_query, search_queries et ideal_abstract en anglais scientifique "
        "(la litterature est indexee en anglais). Les keywords peuvent rester techniques/anglais.\n"
        "Developpe les acronymes specialises en gardant le sigle, ex: 'NI (no-insulation)', "
        "'MI (metal insulation)'. Conserve les sigles courts (MI, NI, HTS) dans les requetes.\n"
        "Retourne uniquement un JSON strict avec:\n"
        "{\n"
        '  "intent": "type de besoin documentaire",\n'
        '  "answer_hypothesis": "hypothese de travail tres courte",\n'
        '  "topic_summary": "synthese scientifique du corpus ou de la demande, 2 phrases max",\n'
        '  "ideal_abstract": "abstract de l\'article ideal, 2 a 3 phrases en anglais scientifique",\n'
        '  "keywords": ["..."],\n'
        '  "dois": ["uniquement les DOI écrits dans le texte fourni, sinon []"],\n'
        '  "search_query": "requete principale concise",\n'
        '  "search_queries": ["requete proche 1", "requete proche 2"],\n'
        '  "facets": [\n'
        '    {"label": "angle", "query": "requete facette", "keywords": ["..."]}\n'
        "  ]\n"
        "}\n"
        "Contraintes:\n"
        "- intent tres court\n"
        "- answer_hypothesis max 1 phrase courte\n"
        "- topic_summary max 2 phrases; synthese les themes communs sans enumerer les sources\n"
        "- ideal_abstract: decris l'article qui repondrait parfaitement, 2 a 3 phrases, factuel, en anglais scientifique\n"
        "- keywords max 10\n"
        "- dois max 8\n"
        f"- search_queries max {_DISCOVERY_QUERY_PLANNER_MAX_SEARCH_QUERIES}\n"
        f"- facets max {_DISCOVERY_QUERY_PLANNER_MAX_FACETS}\n"
        "- chaque query <= 12 mots\n"
        "- n'invente pas de details factuels trop speculatifs\n"
        "- privilegie concepts, methodes, populations, applications, materiaux, effets et comparaisons\n"
        "- aucune sortie hors JSON\n"
        f"{get_discovery_prompt_note(model_name, 'query')}\n\n"
        f"Texte utilisateur:\n{text}\n"
    )
    try:
        raw = await _invoke_discovery_llm_text(
            prompt=prompt,
            model_name=model_name,
            task="query",
            abort_signal=abort_signal,
        )
        parsed = _parse_json_obj(raw or "")
        extracted = _extract_query_plan_fields(parsed)
        _record_llm_debug(
            debug_events,
            stage="query_planner",
            prompt=prompt,
            raw_output=raw or "",
            parsed=parsed if parsed else extracted,
        )
        planner_payload = _build_query_payload_from_plan(text, extracted)
        if planner_payload.get("search_query") or planner_payload.get("search_queries"):
            return planner_payload
    except Exception as exc:
        logger.debug(
            "Recovered from an error in _generate_query_payload_with_planner.", exc_info=True
        )
        _record_llm_debug(
            debug_events,
            stage="query_planner",
            prompt=prompt,
            error=str(exc),
        )

    fallback = await _generate_query_payload_with_llm(
        input_text,
        model_name,
        debug_events,
        abort_signal,
    )
    fallback["generator"] = "query_planner_fallback"
    return fallback


_NOTEBOOK_DISCOVERY_MAX_SOURCES = 12
_NOTEBOOK_DISCOVERY_SOURCE_CHARS = 1200
_NOTEBOOK_DISCOVERY_NOTES_CHARS = 3200
_NOTEBOOK_DISCOVERY_CONTEXT_CHARS = 16000


def _build_notebook_discovery_profile(nb_id: str | None) -> dict[str, Any]:
    """Build a bounded scientific profile from a notebook for discovery.

    The full notebook remains local. Only this compact research context is sent
    through the existing query-planning call when external discovery is run.
    """

    nb = get_notebook((nb_id or "").strip())
    if not nb:
        return {}

    title = _clean_metadata_text(nb.get("title")) or "Carnet sans titre"
    notes = _normalize_search_text(str(nb.get("notes") or "")).strip()
    notes = notes[:_NOTEBOOK_DISCOVERY_NOTES_CHARS]
    dois: list[str] = []
    existing_keys: list[str] = []
    source_rows: list[dict[str, str]] = []

    for source in nb.get("sources", []):
        if not isinstance(source, dict):
            continue
        source_title = _clean_metadata_text(source.get("title"))
        rel_path = str(source.get("source_path") or "").strip()
        source_dois: list[str] = []
        if rel_path:
            citation = get_item_citations(rel_path)
            citation_doi = _canonicalize_doi(str(citation.get("doi") or ""))
            if citation_doi:
                source_dois.append(citation_doi)
        content = _normalize_search_text(str(source.get("content") or "")).strip()
        source_dois.extend(_extract_dois(content))
        source_dois = _dedupe_keep_order(
            [_canonicalize_doi(value) for value in source_dois if _canonicalize_doi(value)]
        )
        dois.extend(source_dois)
        if source_title:
            existing_keys.append(
                f"title:{_normalize_search_text(source_title).lower()}"
            )
        existing_keys.extend(f"doi:{doi}" for doi in source_dois)
        if len(source_rows) < _NOTEBOOK_DISCOVERY_MAX_SOURCES:
            source_rows.append(
                {
                    "title": source_title or "Source sans titre",
                    "excerpt": content[:_NOTEBOOK_DISCOVERY_SOURCE_CHARS],
                    "doi": source_dois[0] if source_dois else "",
                }
            )

    dois = _dedupe_keep_order(dois)
    existing_keys = _dedupe_keep_order(existing_keys)
    sections = [f"Carnet scientifique : {title}"]
    if notes:
        sections.append(f"Notes et axes de travail :\n{notes}")
    if source_rows:
        serialized_sources = []
        for index, row in enumerate(source_rows, start=1):
            doi_suffix = f" — DOI {row['doi']}" if row["doi"] else ""
            source_text = f"[{index}] {row['title']}{doi_suffix}"
            if row["excerpt"]:
                source_text += f"\n{row['excerpt']}"
            serialized_sources.append(source_text)
        sections.append("Articles déjà présents :\n" + "\n\n".join(serialized_sources))
    sections.append(
        "Objectif : identifier de nouveaux articles scientifiquement proches, "
        "complémentaires ou utiles pour prolonger ce carnet. Ne pas proposer les "
        "articles déjà présents."
    )
    context_text = "\n\n".join(sections)[:_NOTEBOOK_DISCOVERY_CONTEXT_CHARS]

    summary_parts = [row["title"] for row in source_rows[:4] if row["title"]]
    if notes:
        summary = _first_sentence(notes)[:360]
    elif summary_parts:
        summary = "Thèmes représentés par : " + "; ".join(summary_parts)
    else:
        summary = f"Recherche autour du carnet « {title} »."

    return {
        "id": str(nb.get("id") or nb_id or ""),
        "title": title,
        "summary": summary,
        "source_count": len([row for row in nb.get("sources", []) if isinstance(row, dict)]),
        "profiled_source_count": len(source_rows),
        "seed_dois": dois[:12],
        "existing_keys": existing_keys,
        "context_text": context_text,
    }


async def _attach_local_short_summaries(
    papers: list[dict[str, Any]],
    model_name: str | None,
    query_payload: dict[str, Any] | None = None,
    debug_events: list[dict[str, Any]] | None = None,
    abort_signal: StreamAbortSignal | None = None,
) -> None:
    for paper in papers:
        paper["short_summary"] = _first_sentence(paper.get("abstract", ""))

    if not model_name or not papers:
        return

    top = papers[:6]
    serialized = json.dumps(
        [
            {
                "idx": idx,
                "title": item.get("title", ""),
                "abstract": _paper_abstract_for_prompt(item, limit=900),
            }
            for idx, item in enumerate(top)
        ],
        ensure_ascii=False,
    )
    prompt = (
        "Tu es un assistant de lecture scientifique.\n"
        "Pour chaque article, ecris une phrase utile pour choisir quoi lire.\n"
        "La phrase doit dire la contribution ou l'interet probable par rapport a la recherche.\n"
        "Retourne uniquement un JSON strict:\n"
        '{ "summaries": [ {"idx": 0, "summary": "..."}, ... ] }\n'
        f"{get_discovery_prompt_note(model_name, 'summary')}\n"
        f"Recherche: {json.dumps(query_payload or {}, ensure_ascii=False)}\n"
        f"Articles: {serialized}\n"
    )
    try:
        raw = await _invoke_discovery_llm_text(
            prompt=prompt,
            model_name=model_name,
            task="summary",
            abort_signal=abort_signal,
        )
        parsed = _parse_json_obj(raw or "")
        rows = _extract_summary_rows(parsed)
        _record_llm_debug(
            debug_events,
            stage="resume_resultats",
            prompt=prompt,
            raw_output=raw or "",
            parsed=parsed if parsed else {"summaries": rows},
        )
        if not rows:
            return
        for row in rows:
            idx = row.get("idx")
            summary = _string_from_row(row, "summary", "why", "rationale", "description")
            if not isinstance(idx, int):
                try:
                    idx = int(idx or 0)
                except (TypeError, ValueError):
                    continue
            if idx < 0 or idx >= len(top) or not summary:
                continue
            top[idx]["short_summary"] = summary[:260]
    except Exception as exc:
        logger.debug("Recovered from an error in _attach_local_short_summaries.", exc_info=True)
        _record_llm_debug(
            debug_events,
            stage="resume_resultats",
            prompt=prompt,
            error=_llm_error_text(exc),
        )
        return


def _llm_error_text(exc: BaseException) -> str:
    """Readable error for the debug panel; str(TimeoutError()) is empty."""
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        detail = str(exc).strip()
        return f"TimeoutError: délai LLM dépassé{f' ({detail})' if detail else ''}"
    detail = str(exc).strip()
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def _validation_prompt(
    batch: list[dict[str, Any]],
    model_name: str,
    query_payload: dict[str, Any],
) -> tuple[str, str]:
    serialized = json.dumps(
        [
            {
                "idx": idx,
                "title": item.get("title", ""),
                "year": item.get("year", ""),
                "venue": item.get("venue", ""),
                "abstract": _paper_abstract_for_prompt(item, limit=_DISCOVERY_VALIDATION_ABSTRACT_CHARS),
                "heuristic_score": item.get("discovery_score", 0),
                "heuristic_reasons": item.get("score_reasons", []),
            }
            for idx, item in enumerate(batch)
        ],
        ensure_ascii=False,
    )
    prompt = (
        "Tu es un pair reviewer chargé de filtrer une veille bibliographique.\n"
        "Lis la recherche puis chaque titre/abstract. Évalue la pertinence scientifique réelle.\n"
        "Donne une note explicite par critère, puis une appréciation globale.\n"
        "Pénalise les résultats qui ne traitent qu'un mot-clé isolé sans lien avec l'intention.\n"
        "Mets reject=true si le titre/abstract ne permet pas de confirmer le lien direct avec la recherche.\n"
        "Retourne uniquement un JSON strict:\n"
        '{ "evaluations": [ {"idx": 0, "topic_score": 0, "method_score": 0, "tool_score": 0, "directness_score": 0, "reject": true, "rationale": "..."} ] }\n'
        "0 = hors sujet, 50 = utile mais indirect, 100 = central pour la question.\n"
        "topic_score = adéquation au sujet central; method_score = adéquation à la méthode/formulation; tool_score = adéquation à l'outil ou contexte applicatif; directness_score = capacité à aider directement la question.\n"
        "Rationale: une seule phrase factuelle de 25 mots maximum.\n"
        "Explique pourquoi l'article est pertinent pour la recherche, en nommant le lien exact avec la question.\n"
        "Si possible, mentionne aussi ce que l'article semble apporter (méthode, population, résultat, comparaison, revue, etc.).\n"
        "Ne formule pas une justification vague du type 'semble pertinent'; dis en quoi il correspond.\n"
        f"{get_discovery_prompt_note(model_name, 'validate')}\n"
        f"Recherche: {json.dumps(query_payload or {}, ensure_ascii=False)}\n"
        f"Articles: {serialized}\n"
    )
    return prompt, serialized


async def _validate_discovery_batch(
    batch: list[dict[str, Any]],
    model_name: str,
    query_payload: dict[str, Any],
    *,
    debug_events: list[dict[str, Any]] | None,
    abort_signal: StreamAbortSignal | None,
) -> list[dict[str, Any]]:
    prompt, serialized = _validation_prompt(batch, model_name, query_payload)
    raw = await _invoke_discovery_llm_text(
        prompt=prompt,
        model_name=model_name,
        task="validate",
        abort_signal=abort_signal,
    )
    parsed = _parse_json_obj(raw or "")
    rows = _extract_validation_rows(parsed)
    used_prompt = prompt
    used_stage = "validation_abstracts"
    if rows and _validation_payload_is_low_quality(rows) and _has_discovery_budget(
        8.0, reserve=_DISCOVERY_POST_VALIDATION_RESERVE_SECONDS
    ):
        retry_prompt = (
            "La sortie précédente n'était pas assez exploitable.\n"
            "Régénère exactement le schéma JSON demandé.\n"
            "Contraintes additionnelles:\n"
            "- n'utilise pas le même score pour tous les articles sans justification forte\n"
            "- chaque rationale doit être non vide et spécifique\n"
            "- si un article est partiellement pertinent, préfère un score intermédiaire à 0\n\n"
            '{ "evaluations": [ {"idx": 0, "topic_score": 0, "method_score": 0, "tool_score": 0, "directness_score": 0, "reject": true, "rationale": "..."} ] }\n\n'
            f"{get_discovery_prompt_note(model_name, 'validate')}\n\n"
            f"Recherche: {json.dumps(query_payload or {}, ensure_ascii=False)}\n"
            f"Articles: {serialized}\n\n"
            f"Sortie précédente à corriger:\n{raw}"
        )
        retry_raw = await _invoke_discovery_llm_text(
            prompt=retry_prompt,
            model_name=model_name,
            task="validate",
            abort_signal=abort_signal,
        )
        retry_parsed = _parse_json_obj(retry_raw or "")
        retry_rows = _extract_validation_rows(retry_parsed)
        if retry_rows:
            raw, parsed, rows = retry_raw, retry_parsed, retry_rows
            used_prompt = retry_prompt
            used_stage = "validation_abstracts_retry"
    _record_llm_debug(
        debug_events,
        stage=used_stage,
        prompt=used_prompt,
        raw_output=raw or "",
        parsed=parsed if parsed else {"evaluations": rows},
        error="" if rows else "Réponse sans évaluation exploitable",
    )
    return rows


def _apply_validation_rows(
    batch: list[dict[str, Any]],
    rows: list[dict[str, Any]],
    query_payload: dict[str, Any],
) -> int:
    applied: set[int] = set()
    for row in rows:
        try:
            idx = int(row.get("idx", row.get("rank", row.get("id"))) or 0)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(batch):
            continue
        relevance, dimensions = _weighted_llm_relevance(row, query_payload)
        rejected_flag = _bool_from_row(row, "reject", "rejected")
        is_relevant = _bool_from_row(row, "is_relevant", "relevant")
        rejected = bool(rejected_flag) if rejected_flag is not None else False
        if is_relevant is False:
            rejected = True
        rejected = rejected or relevance < 35
        rationale = _string_from_row(
            row, "rationale", "why", "reason", "justification", "explanation"
        )
        paper = batch[idx]
        heuristic = _safe_int(paper.get("discovery_score"), 0, 0, 1000000)
        paper["llm_relevance_score"] = relevance
        paper["llm_criteria_scores"] = dimensions
        paper["llm_rationale"] = rationale[:420]
        paper["llm_rejected"] = rejected
        paper["validated_score"] = (
            0 if rejected else int(round((heuristic * 0.2) + (relevance * 0.8)))
        )
        reasons = list(paper.get("score_reasons") or [])
        dimension_summary = ", ".join(
            f"{label}:{score}"
            for label, score in dimensions.items()
            if isinstance(score, int) and score >= 0
        )
        reasons.insert(
            0,
            f"LLM: {relevance}/100"
            + (f" ({dimension_summary})" if dimension_summary else "")
            + (" rejeté" if rejected else ""),
        )
        paper["score_reasons"] = _dedupe_keep_order(
            [str(item) for item in reasons if str(item).strip()]
        )[:5]
        applied.add(idx)
    return len(applied)


async def _repair_missing_rationales(
    top: list[dict[str, Any]],
    model_name: str,
    query_payload: dict[str, Any],
    *,
    debug_events: list[dict[str, Any]] | None,
    abort_signal: StreamAbortSignal | None,
) -> None:
    missing_rationale_payload = [
        {
            "idx": idx,
            "title": item.get("title", ""),
            "year": item.get("year", ""),
            "venue": item.get("venue", ""),
            "abstract": _paper_abstract_for_prompt(item, limit=900),
            "relevance_score": item.get("llm_relevance_score"),
        }
        for idx, item in enumerate(top)
        if item.get("llm_relevance_score") is not None
        and not item.get("llm_rejected")
        and _safe_int(item.get("llm_relevance_score"), 0, 0, 100) >= 35
        and not str(item.get("llm_rationale") or "").strip()
    ]
    if not missing_rationale_payload:
        return
    rationale_prompt = (
        "Tu complètes des justifications de pertinence pour une veille bibliographique.\n"
        "Pour chaque article, écris une justification brève et concrète.\n"
        "La justification doit expliquer en quoi l'article est pertinent pour la recherche.\n"
        "Retourne uniquement un JSON strict:\n"
        '{ "rationales": [ {"idx": 0, "rationale": "..."} ] }\n'
        "Chaque rationale doit contenir 1 à 2 phrases courtes et ne jamais être vide.\n"
        f"{get_discovery_prompt_note(model_name, 'validate')}\n"
        f"Recherche: {json.dumps(query_payload or {}, ensure_ascii=False)}\n"
        f"Articles: {json.dumps(missing_rationale_payload, ensure_ascii=False)}\n"
    )
    try:
        rationale_raw = await _invoke_discovery_llm_text(
            prompt=rationale_prompt,
            model_name=model_name,
            task="validate",
            abort_signal=abort_signal,
        )
    except InterruptedError:
        raise
    except Exception as exc:
        logger.debug("Recovered from an error in _repair_missing_rationales.", exc_info=True)
        _record_llm_debug(
            debug_events,
            stage="validation_rationales_repair",
            prompt=rationale_prompt,
            error=_llm_error_text(exc),
        )
        return
    rationale_parsed = _parse_json_obj(rationale_raw or "")
    rationale_rows = _extract_rationale_rows(rationale_parsed)
    _record_llm_debug(
        debug_events,
        stage="validation_rationales_repair",
        prompt=rationale_prompt,
        raw_output=rationale_raw or "",
        parsed=rationale_parsed if rationale_parsed else {"rationales": rationale_rows},
    )
    for row in rationale_rows:
        try:
            idx = int(row.get("idx", row.get("rank", row.get("id"))) or 0)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(top):
            continue
        rationale = _string_from_row(
            row, "rationale", "why", "reason", "justification", "explanation"
        )
        if rationale:
            top[idx]["llm_rationale"] = rationale[:420]


async def _attach_llm_relevance_validation(
    papers: list[dict[str, Any]],
    model_name: str | None,
    query_payload: dict[str, Any],
    *,
    validation_limit: int | None = None,
    debug_events: list[dict[str, Any]] | None = None,
    abort_signal: StreamAbortSignal | None = None,
) -> dict[str, Any]:
    """Score abstracts with the LLM, by small batches, within the time budget.

    One prompt for 24 abstracts needs 20-25 s of generation on a local 9B
    model and always hit the per-call cap, so nothing was ever validated.
    Batches of a few articles fit the cap; the best-ranked ones go first so a
    budget cut still validates what the user sees at the top.

    Returns ``{"status", "evaluated", "requested", "error"}`` where status is
    complete, partial, failed or skipped.
    """
    for paper in papers:
        paper["llm_relevance_score"] = None
        paper["llm_criteria_scores"] = {}
        paper["llm_rationale"] = ""
        paper["llm_rejected"] = False
        paper["validated_score"] = _safe_int(paper.get("discovery_score"), 0, 0, 1000000)

    outcome: dict[str, Any] = {"status": "skipped", "evaluated": 0, "requested": 0, "error": ""}
    if not model_name or not papers:
        return outcome

    cap = _safe_int(validation_limit, 10, 1, _DISCOVERY_LLM_VALIDATION_MAX)
    top = papers[:cap]
    outcome["requested"] = len(top)
    errors: list[str] = []
    for offset in range(0, len(top), _DISCOVERY_VALIDATION_BATCH_SIZE):
        if offset and not _has_discovery_budget(
            _DISCOVERY_VALIDATION_BATCH_MIN_SECONDS,
            reserve=_DISCOVERY_POST_VALIDATION_RESERVE_SECONDS,
        ):
            break
        batch = top[offset : offset + _DISCOVERY_VALIDATION_BATCH_SIZE]
        _emit_progress(
            "validation",
            f"Le modèle lit les abstracts : {offset}/{len(top)} article(s) vérifiés…",
        )
        try:
            rows = await _validate_discovery_batch(
                batch,
                model_name,
                query_payload,
                debug_events=debug_events,
                abort_signal=abort_signal,
            )
        except InterruptedError:
            raise
        except Exception as exc:
            logger.debug(
                "Recovered from an error in _attach_llm_relevance_validation.", exc_info=True
            )
            error = _llm_error_text(exc)
            errors.append(error)
            prompt, _serialized = _validation_prompt(batch, model_name, query_payload)
            _record_llm_debug(
                debug_events,
                stage="validation_abstracts",
                prompt=prompt,
                error=error,
            )
            # A timeout on one batch means the next would time out too.
            break
        outcome["evaluated"] += _apply_validation_rows(batch, rows, query_payload)

    if outcome["evaluated"] and _has_discovery_budget(
        8.0, reserve=_DISCOVERY_POST_VALIDATION_RESERVE_SECONDS
    ):
        await _repair_missing_rationales(
            top,
            model_name,
            query_payload,
            debug_events=debug_events,
            abort_signal=abort_signal,
        )

    if outcome["evaluated"] >= outcome["requested"]:
        outcome["status"] = "complete"
    elif outcome["evaluated"]:
        outcome["status"] = "partial"
    else:
        outcome["status"] = "failed"
    outcome["error"] = " | ".join(errors)[:300]
    return outcome


def _query_ideal_abstract(query_payload: dict[str, Any]) -> str:
    plan = query_payload.get("query_plan")
    if isinstance(plan, dict):
        ideal = str(plan.get("ideal_abstract") or "").strip()
        if ideal:
            return ideal
    return str(query_payload.get("ideal_abstract") or "").strip()


def _query_answer_hypothesis(query_payload: dict[str, Any]) -> str:
    plan = query_payload.get("query_plan")
    if isinstance(plan, dict):
        hypothesis = str(plan.get("answer_hypothesis") or "").strip()
        if hypothesis:
            return hypothesis
    return str(query_payload.get("answer_hypothesis") or "").strip()


def _dense_query_text(input_text: str, query_payload: dict[str, Any]) -> str:
    """English-primary semantic query text for dense retrieval (HyDE-aware).

    Scientific abstracts are overwhelmingly English; leading with the English
    HyDE ideal abstract + reformulated queries keeps the query vector aligned
    with the corpus. Facet terms are canonicalized (FR->EN where known) and the
    raw user text is appended last (a multilingual embedder still benefits from
    the exact phrasing).
    """
    facets = _query_facet_keywords(query_payload)
    facet_terms: list[str] = []
    for label in ("topic", "method", "tool"):
        for term in facets.get(label) or []:
            facet_terms.append(_canonical_query_term(term) or term)
    search_queries = _dedupe_keep_order(
        [str(value or "").strip() for value in (query_payload.get("search_queries") or [])]
    )
    parts = [
        _query_ideal_abstract(query_payload),
        _query_answer_hypothesis(query_payload),
        str(query_payload.get("search_query") or "").strip(),
        " ".join(search_queries),
        " ".join(_dedupe_keep_order(facet_terms)),
        str(input_text or "").strip(),
    ]
    return "\n".join(part for part in parts if part).strip()


def _dense_paper_text(paper: dict[str, Any]) -> str:
    parts = [
        str(paper.get("title") or "").strip(),
        _paper_abstract_text(paper),
        str(paper.get("venue") or "").strip(),
    ]
    text = "\n".join(part for part in parts if part)
    return text[:_DISCOVERY_DENSE_TEXT_CHARS]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right:
        return 0.0
    size = min(len(left), len(right))
    dot = 0.0
    norm_left = 0.0
    norm_right = 0.0
    for idx in range(size):
        a = float(left[idx])
        b = float(right[idx])
        dot += a * b
        norm_left += a * a
        norm_right += b * b
    if norm_left <= 0.0 or norm_right <= 0.0:
        return 0.0
    return dot / ((norm_left**0.5) * (norm_right**0.5))


async def _dense_rank_discovery_papers(
    papers: list[dict[str, Any]],
    *,
    input_text: str,
    query_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rank candidates by embedding cosine similarity to the (HyDE) query.

    Soft-fail: if no embedding model is configured or embedding raises, the
    papers are returned untouched with ``applied=False`` so the caller keeps
    the legacy lexical ordering.
    """
    meta: dict[str, Any] = {"applied": False, "candidates": len(papers)}
    if len(papers) <= 1:
        return papers, meta

    try:
        from src.llm_provider import (
            create_embeddings,
            default_embedding_prompts,
            required_embedding_model_name,
            with_embedding_prompts,
        )
    except Exception as exc:  # pragma: no cover - import guard
        logger.debug("Recovered from an error in _dense_rank_discovery_papers.", exc_info=True)
        meta["error"] = str(exc)
        return papers, meta

    try:
        model_name = (required_embedding_model_name() or "").strip()
    except Exception as exc:
        logger.debug("Recovered from an error in _dense_rank_discovery_papers.", exc_info=True)
        meta["error"] = str(exc)
        return papers, meta
    if not model_name:
        meta["error"] = "no embedding model configured"
        return papers, meta

    query_text = _dense_query_text(input_text, query_payload)
    if not query_text:
        meta["error"] = "empty dense query"
        return papers, meta

    texts = [_dense_paper_text(paper) for paper in papers]

    def _run() -> tuple[list[float], list[list[float]]]:
        embeddings = with_embedding_prompts(
            create_embeddings(model_name), default_embedding_prompts(model_name)
        )
        query_vec = embeddings.embed_query(query_text)
        doc_vecs = embeddings.embed_documents(texts)
        return list(query_vec or []), [list(vec or []) for vec in (doc_vecs or [])]

    try:
        available = _remaining_discovery_seconds(reserve=11.0)
        if available < 1.0:
            meta["skipped"] = "time_budget"
            return papers, meta
        query_vec, doc_vecs = await asyncio.wait_for(
            asyncio.to_thread(_run),
            timeout=min(10.0, available),
        )
    except Exception as exc:
        logger.debug("Recovered from an error in _dense_rank_discovery_papers.", exc_info=True)
        meta["error"] = str(exc)
        return papers, meta

    if not query_vec or len(doc_vecs) != len(papers):
        meta["error"] = "embedding shape mismatch"
        return papers, meta

    for paper, vec in zip(papers, doc_vecs):
        similarity = _cosine(query_vec, vec)
        paper["dense_score"] = round(max(0.0, min(1.0, similarity)), 6)

    ordered = sorted(
        papers,
        key=lambda paper: float(paper.get("dense_score") or 0.0),
        reverse=True,
    )
    for rank, paper in enumerate(ordered, start=1):
        paper["dense_rank"] = rank

    meta.update({"applied": True, "model": model_name, "scored": len(ordered)})
    return ordered, meta


def _discovery_reranker_query(input_text: str, query_payload: dict[str, Any]) -> str:
    facets = _query_facet_keywords(query_payload)
    facet_bits = []
    for label in ("topic", "method", "tool"):
        values = facets.get(label) or []
        if values:
            facet_bits.append(f"{label}: {', '.join(values)}")
    parts = [
        str(input_text or "").strip(),
        str(query_payload.get("search_query") or "").strip(),
        " | ".join(str(value or "").strip() for value in query_payload.get("search_queries") or []),
        " ; ".join(facet_bits),
    ]
    return "\n".join(part for part in parts if part)


def _discovery_paper_text_for_reranker(paper: dict[str, Any]) -> str:
    parts = [
        str(paper.get("title") or "").strip(),
        str(paper.get("abstract") or "").strip(),
        str(paper.get("venue") or "").strip(),
        " ".join(str(value or "") for value in paper.get("matched_keywords") or []),
        " ".join(str(value or "") for value in paper.get("provider_sources") or []),
    ]
    return "\n\n".join(part for part in parts if part)


async def _rerank_discovery_papers(
    papers: list[dict[str, Any]],
    *,
    input_text: str,
    query_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if len(papers) <= 1:
        return papers, {"applied": False, "candidates": len(papers)}
    try:
        from src.rag.reranker import is_reranker_enabled, rerank_texts
    except Exception as exc:
        logger.debug("Recovered from an error in _rerank_discovery_papers.", exc_info=True)
        return papers, {"applied": False, "candidates": len(papers), "error": str(exc)}

    try:
        if not is_reranker_enabled():
            return papers, {"applied": False, "candidates": len(papers), "disabled": True}
    except Exception as exc:
        logger.debug("Recovered from an error in _rerank_discovery_papers.", exc_info=True)
        return papers, {"applied": False, "candidates": len(papers), "error": str(exc)}

    query = _discovery_reranker_query(input_text, query_payload)
    texts = [_discovery_paper_text_for_reranker(paper) for paper in papers]

    def _run() -> list[tuple[int, float]]:
        return rerank_texts(query, texts, top_k=len(texts))

    try:
        available = _remaining_discovery_seconds(reserve=7.0)
        if available < 1.0:
            return papers, {
                "applied": False,
                "candidates": len(papers),
                "skipped": "time_budget",
            }
        ranked = await asyncio.wait_for(
            asyncio.to_thread(_run),
            timeout=min(12.0, available),
        )
    except Exception as exc:
        # The first call also loads the cross-encoder and can exceed the cap;
        # the load keeps going in its thread, so the next search benefits.
        logger.debug("Recovered from an error in _rerank_discovery_papers.", exc_info=True)
        return papers, {"applied": False, "candidates": len(papers), "error": _llm_error_text(exc)}

    if not ranked:
        return papers, {"applied": False, "candidates": len(papers)}

    by_idx = {idx: paper for idx, paper in enumerate(papers)}
    ordered: list[dict[str, Any]] = []
    seen: set[int] = set()
    for rank, (idx, score) in enumerate(ranked, start=1):
        paper = by_idx.get(idx)
        if paper is None:
            continue
        seen.add(idx)
        paper["reranker_score"] = round(float(score), 6)
        paper["reranker_rank"] = rank
        paper["reranker_rank_score"] = max(0, len(papers) - rank + 1)
        ordered.append(paper)
    for idx, paper in enumerate(papers):
        if idx not in seen:
            ordered.append(paper)

    applied = any(float(score) != 0.0 for _, score in ranked) or [
        idx for idx, _score in ranked
    ] != list(range(len(ranked)))
    return ordered, {
        "applied": bool(applied),
        "candidates": len(texts),
        "top_k": len(ranked),
    }


@dataclass
class _ProviderCollection:
    """Candidate rows gathered from the external providers for one discovery run."""

    client: httpx.AsyncClient
    query_payload: dict[str, Any]
    abort_signal: StreamAbortSignal | None
    llm_available: bool
    result_limit: int
    candidate_limit: int
    provider_stats: dict[str, dict[str, Any]]
    warnings: list[str]
    search_queries: list[str] = field(default_factory=list)
    rows: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    providers_used: set[str] = field(default_factory=set)
    primary_provider: str = ""
    fallback_provider: str = ""
    provider_used: str = ""
    rate_limited: bool = False

    def set_primary(self, provider: str) -> None:
        self.primary_provider = provider
        self.provider_used = provider


def _normalize_discovery_mode(mode: str) -> str:
    normalized_mode = (mode or "search").strip().lower()
    if normalized_mode == "recommendations":
        # Compatibility with searches saved before the notebook-centric rename.
        return "notebook"
    return normalized_mode if normalized_mode in {"search", "notebook"} else "search"


def _discovery_input_text(input_text: str, notebook_profile: dict[str, Any]) -> str:
    """The user's request, framed by the notebook's content in notebook mode."""
    effective_input_text = (input_text or "").strip()
    if not notebook_profile:
        return effective_input_text
    notebook_context = str(notebook_profile.get("context_text") or "").strip()
    if not effective_input_text:
        return notebook_context
    return (
        f"Angle de recherche demandé par l'utilisateur :\n{effective_input_text}\n\n"
        f"{notebook_context}"
    )


def _discovery_search_queries(query_payload: dict[str, Any], warnings: list[str]) -> list[str]:
    query = str(query_payload.get("search_query") or "").strip()
    if not query:
        query = " ".join(query_payload.get("keywords") or [])
    if not query:
        query = "scientific review"
        warnings.append("Requête vide, requête par défaut utilisée.")
    search_queries = [
        str(value or "").strip()
        for value in (query_payload.get("search_queries") or [])
        if str(value or "").strip()
    ]
    return _dedupe_keep_order([query] + search_queries)[:_DISCOVERY_QUERY_PLANNER_MAX_SEARCH_QUERIES]


async def _collect_notebook_recommendations(
    run: _ProviderCollection,
    seed_dois: list[str],
    *,
    max_results: int,
) -> None:
    recommendation_dois = _dedupe_keep_order((run.query_payload.get("dois") or []) + seed_dois)
    if not recommendation_dois:
        run.warnings.append(
            "Aucun DOI exploitable dans le carnet; la recherche reste basée sur son contenu."
        )
        return
    try:
        _throw_if_aborted(run.abort_signal)
        papers_raw = await _semantic_recommendations_for_dois(
            run.client,
            recommendation_dois,
            limit=max_results,
        )
        if papers_raw:
            run.providers_used.add("Semantic Scholar")
            run.rows.extend(
                ("semanticscholar", row) for row in papers_raw if isinstance(row, dict)
            )
            _record_provider_fetch(
                run.provider_stats,
                "semanticscholar",
                rows=[row for row in papers_raw if isinstance(row, dict)],
                phase="notebook_recommendations",
                query_count=len(recommendation_dois[:8]),
            )
            run.set_primary("semanticscholar")
    except Exception as exc:
        logger.debug("Semantic Scholar recommendations failed.", exc_info=True)
        if _is_rate_limit_error(exc):
            run.rate_limited = True
            run.warnings.append(
                "Semantic Scholar limite temporairement les requêtes (rate limit). "
                "Réessayez dans 1 a 2 minutes."
            )
        else:
            run.warnings.append(f"Recommandations DOI du carnet indisponibles: {exc}")
        papers_raw = []
    if not papers_raw and not run.rate_limited:
        run.warnings.append(
            "Aucune recommandation DOI; la recherche thématique du carnet reste active."
        )


async def _collect_semantic_scholar_rows(
    run: _ProviderCollection,
) -> list[tuple[str, dict[str, Any]]]:
    """Primary search; stops querying once the candidate pool is good enough."""
    semantic_rows: list[tuple[str, dict[str, Any]]] = []
    try:
        semantic_queries = _provider_query_budget(
            "semanticscholar",
            run.search_queries,
            llm_available=run.llm_available,
        )
        for query_index, search_query in enumerate(semantic_queries):
            _throw_if_aborted(run.abort_signal)
            rows = await _semantic_search_with_retries(
                run.client,
                search_query,
                limit=run.candidate_limit,
            )
            _record_provider_fetch(
                run.provider_stats,
                "semanticscholar",
                rows=rows,
                phase="primary",
                query_count=1,
            )
            annotated_rows = _annotate_ranked_provider_rows(rows, query_index=query_index)
            semantic_rows.extend(("semanticscholar", row) for row in annotated_rows)
            if rows:
                run.providers_used.add("Semantic Scholar")
            provisional, _provisional_dropped = _rank_discovery_papers(
                _normalized_papers_from_provider_rows(semantic_rows),
                run.query_payload,
            )
            if _candidate_pool_is_sufficient(provisional, result_limit=run.result_limit):
                break
    except Exception as exc:
        if not _is_rate_limit_error(exc):
            raise
        run.rate_limited = True
        _record_provider_fetch(
            run.provider_stats,
            "semanticscholar",
            phase="primary",
            error=str(exc),
        )
        if semantic_rows:
            run.warnings.append(
                "Semantic Scholar a limité une requête secondaire; résultats partiels conservés."
            )
        else:
            run.warnings.append(
                "Semantic Scholar limite temporairement les requêtes (rate limit). "
                "Attendez un peu avant de relancer la recherche."
            )

    run.rows.extend(semantic_rows)
    if semantic_rows:
        run.set_primary("semanticscholar")
    return semantic_rows


async def _collect_openalex_rows(
    run: _ProviderCollection,
    *,
    catalog_complement: bool,
    api_key_configured: bool,
) -> None:
    """OpenAlex as the fallback of Semantic Scholar, or as a methodology complement."""
    openalex_rows: list[tuple[str, dict[str, Any]]] = []
    phase = "catalog_complement" if catalog_complement else "fallback_primary"
    if not api_key_configured:
        run.warnings.append(_OPENALEX_MISSING_API_KEY_WARNING)
        _record_provider_fetch(
            run.provider_stats,
            "openalex",
            phase=phase,
            error="OPENALEX_API_KEY missing",
        )
    try:
        openalex_queries = _provider_query_budget(
            "openalex",
            run.search_queries,
            llm_available=run.llm_available,
        )
        for search_query in openalex_queries:
            _throw_if_aborted(run.abort_signal)
            rows = await _openalex_search(run.client, search_query, limit=run.candidate_limit)
            _record_provider_fetch(
                run.provider_stats,
                "openalex",
                rows=rows,
                phase=phase,
                query_count=1,
            )
            openalex_rows.extend(("openalex", row) for row in rows if isinstance(row, dict))
            if rows:
                run.providers_used.add("OpenAlex")
    except Exception as exc:
        logger.debug("OpenAlex search failed.", exc_info=True)
        _record_provider_fetch(run.provider_stats, "openalex", phase=phase, error=str(exc))
        if not run.rows:
            run.warnings.append(f"Fallback OpenAlex indisponible: {exc}")
        else:
            run.warnings.append(f"Complement OpenAlex indisponible: {exc}")

    if openalex_rows:
        if not run.primary_provider:
            run.set_primary("openalex")
            if not api_key_configured:
                run.warnings.append(
                    "Fallback OpenAlex utilisé pour la recherche d'articles. "
                    "Configurer la clé OpenAlex dans les réglages peut grandement améliorer les résultats "
                    "quand Semantic Scholar est indisponible."
                )
            else:
                run.warnings.append("Fallback OpenAlex utilisé pour la recherche d'articles.")
        else:
            run.fallback_provider = "openalex"
            if catalog_complement:
                run.warnings.append(
                    "OpenAlex ajouté comme complément méthodologique à Semantic Scholar."
                )
        run.rows.extend(openalex_rows)
    elif run.rate_limited and not run.rows:
        run.warnings.append("OpenAlex n'a retourné aucun résultat en fallback.")


async def _fetch_arxiv_fallback(
    run: _ProviderCollection,
) -> tuple[list[dict[str, Any]], Exception | None]:
    arxiv_rows: list[dict[str, Any]] = []
    try:
        arxiv_queries = _provider_query_budget(
            "arxiv",
            run.search_queries,
            llm_available=run.llm_available,
        )
        for search_query in arxiv_queries:
            _throw_if_aborted(run.abort_signal)
            rows = await _arxiv_search(
                run.client,
                search_query,
                limit=max(4, min(16, run.candidate_limit // 2)),
            )
            arxiv_rows.extend(rows)
            _record_provider_fetch(
                run.provider_stats,
                "arxiv",
                rows=rows,
                phase="fallback_specialized",
                query_count=1,
            )
    except Exception as exc:
        logger.debug("arXiv fallback search failed.", exc_info=True)
        return arxiv_rows, exc
    return arxiv_rows, None


async def _fetch_hal_fallback(
    run: _ProviderCollection,
) -> tuple[list[dict[str, Any]], Exception | None]:
    hal_rows: list[dict[str, Any]] = []
    try:
        # HAL is a French repository: English reformulations hurt recall, so
        # prefer the native query first.
        native_queries = [
            str(value or "").strip()
            for value in (run.query_payload.get("native_search_queries") or [])
            if str(value or "").strip()
        ]
        hal_queries = _provider_query_budget(
            "hal",
            _dedupe_keep_order(native_queries + run.search_queries),
            llm_available=run.llm_available,
        )
        for search_query in hal_queries:
            _throw_if_aborted(run.abort_signal)
            rows = await _hal_search(
                run.client,
                search_query,
                limit=max(4, min(14, run.candidate_limit // 2)),
            )
            hal_rows.extend(rows)
            _record_provider_fetch(
                run.provider_stats,
                "hal",
                rows=rows,
                phase="fallback_specialized",
                query_count=1,
            )
    except Exception as exc:
        logger.debug("HAL fallback search failed.", exc_info=True)
        return hal_rows, exc
    return hal_rows, None


def _adopt_specialized_rows(
    run: _ProviderCollection,
    provider: str,
    label: str,
    rows: list[dict[str, Any]],
    error: Exception | None,
) -> None:
    if error is not None:
        _record_provider_fetch(
            run.provider_stats,
            provider,
            phase="fallback_specialized",
            error=str(error),
        )
        qualifier = "Fallback" if not run.rows else "Complement"
        run.warnings.append(f"{qualifier} {label} indisponible: {error}")
        return
    if not rows:
        return
    run.providers_used.add(label)
    if not run.primary_provider:
        run.set_primary(provider)
        run.warnings.append(f"Fallback {label} utilisé pour la recherche d'articles.")
    elif not run.fallback_provider:
        run.fallback_provider = provider
    run.rows.extend((provider, row) for row in rows if isinstance(row, dict))


async def _collect_crossref_last_resort(run: _ProviderCollection, *, mailto: str) -> None:
    try:
        crossref_queries = _provider_query_budget(
            "crossref",
            run.search_queries,
            llm_available=run.llm_available,
        )
        for search_query in crossref_queries:
            _throw_if_aborted(run.abort_signal)
            rows = await _crossref_search(
                run.client,
                search_query,
                limit=max(4, min(16, run.candidate_limit // 2)),
                mailto=mailto,
            )
            _record_provider_fetch(
                run.provider_stats,
                "crossref",
                rows=rows,
                phase="last_resort",
                query_count=1,
            )
            run.rows.extend(("crossref", row) for row in rows if isinstance(row, dict))
            if rows:
                run.providers_used.add("Crossref")
        if run.rows:
            run.set_primary("crossref")
            run.warnings.append(
                "Fallback Crossref utilisé en dernier recours; "
                "les résultats seront filtrés strictement."
            )
    except Exception as exc:
        logger.debug("Crossref last-resort search failed.", exc_info=True)
        _record_provider_fetch(run.provider_stats, "crossref", phase="last_resort", error=str(exc))
        run.warnings.append(f"Fallback Crossref indisponible: {exc}")


async def _collect_provider_rows(
    run: _ProviderCollection,
    *,
    notebook_mode: bool,
    seed_dois: list[str],
    max_results: int,
    openalex_api_key_configured: bool,
    crossref_mailto: str,
) -> None:
    """Query the providers in quality order, stopping once the results suffice."""
    if notebook_mode:
        await _collect_notebook_recommendations(run, seed_dois, max_results=max_results)

    run.search_queries = _discovery_search_queries(run.query_payload, run.warnings)
    semantic_rows = await _collect_semantic_scholar_rows(run)
    semantic_ranked, _semantic_dropped = _rank_discovery_papers(
        _normalized_papers_from_provider_rows(semantic_rows),
        run.query_payload,
    )
    semantic_sufficient = len(semantic_ranked) >= run.result_limit
    needs_catalog_complement = (
        semantic_sufficient
        and openalex_api_key_configured
        and _query_needs_catalog_complement(run.query_payload)
    )
    if semantic_sufficient and not needs_catalog_complement:
        run.warnings.append(
            "Semantic Scholar a fourni assez de résultats ancrés; fallbacks externes non mélangés."
        )
        return

    await _collect_openalex_rows(
        run,
        catalog_complement=needs_catalog_complement,
        api_key_configured=openalex_api_key_configured,
    )
    ranked_after_openalex, _openalex_dropped = _rank_discovery_papers(
        _normalized_papers_from_provider_rows(run.rows),
        run.query_payload,
        require_query_anchor=run.primary_provider == "openalex",
    )
    if not semantic_sufficient and len(ranked_after_openalex) < run.result_limit:
        # These independent repositories are queried together only after the
        # Semantic Scholar/OpenAlex quality short-circuit.
        (arxiv_rows, arxiv_error), (hal_rows, hal_error) = await asyncio.gather(
            _fetch_arxiv_fallback(run),
            _fetch_hal_fallback(run),
        )
        _adopt_specialized_rows(run, "arxiv", "arXiv", arxiv_rows, arxiv_error)
        _adopt_specialized_rows(run, "hal", "HAL", hal_rows, hal_error)

    if not run.rows:
        await _collect_crossref_last_resort(run, mailto=crossref_mailto)


async def _snowball_discovery_papers(
    run: _ProviderCollection,
    papers: list[dict[str, Any]],
    *,
    mailto: str,
    enrich_cap: int,
) -> tuple[list[dict[str, Any]], int]:
    """Add papers recommended from the strongest results' citation graph."""
    snowball_seed_dois = _strong_snowball_seed_dois(papers)
    if not snowball_seed_dois:
        return papers, 0
    try:
        snowball_raw = await _semantic_recommendations_for_dois(
            run.client,
            snowball_seed_dois,
            limit=_DISCOVERY_SNOWBALL_LIMIT,
        )
    except Exception as exc:
        logger.debug("Citation snowballing failed.", exc_info=True)
        snowball_raw = []
        if _is_rate_limit_error(exc):
            run.rate_limited = True
    if not snowball_raw:
        return papers, 0
    snowball_rows = [row for row in snowball_raw if isinstance(row, dict)]
    snowball_papers = _normalized_papers_from_provider_rows(
        [("semanticscholar", row) for row in snowball_rows]
    )
    papers, snowball_added = _merge_paper_lists(papers, snowball_papers)
    if not snowball_added:
        return papers, 0
    run.providers_used.add("Semantic Scholar")
    _record_provider_fetch(
        run.provider_stats,
        "semanticscholar",
        rows=snowball_rows,
        phase="snowball",
        query_count=len(snowball_seed_dois),
    )
    await _enrich_missing_abstracts(papers, run.client, mailto=mailto, max_papers=enrich_cap)
    papers, _snowball_dropped = _rank_discovery_papers(papers, run.query_payload)
    run.warnings.append(
        f"{snowball_added} article(s) ajoutés via graphe de citations (snowballing)."
    )
    return papers, snowball_added


async def _attach_unpaywall_links(
    run: _ProviderCollection,
    papers: list[dict[str, Any]],
    *,
    unpaywall_email: str | None,
) -> None:
    env_email = os.getenv("UNPAYWALL_EMAIL", "").strip()
    email = (unpaywall_email or "").strip() or env_email or _DEFAULT_UNPAYWALL_EMAIL
    if email == _DEFAULT_UNPAYWALL_EMAIL:
        run.warnings.append(
            "Email Unpaywall non configuré : ajoutez-en un dans Réglages pour trouver plus de PDF en accès libre."
        )
    result_limit = run.result_limit
    for paper in papers[: max(result_limit, min(len(papers), result_limit * 2))]:
        _throw_if_aborted(run.abort_signal)
        if not _has_discovery_budget(0.8, reserve=3.0):
            run.warnings.append(
                "Enrichissement Unpaywall arrêté pour respecter la limite de temps."
            )
            break
        if paper.get("open_access_pdf"):
            continue
        doi = str(paper.get("doi") or "").strip()
        if not doi:
            continue
        try:
            url = await _unpaywall_pdf_url(run.client, doi, email)
        except (httpx.HTTPError, RuntimeError, ValueError):
            url = ""
        if url:
            paper["open_access_pdf"] = url
            paper["openAccessPdf"] = {"url": url}
            paper["open_access_source"] = "unpaywall"


def _replace_rate_limit_warnings(warnings: list[str]) -> None:
    # This advice already says Semantic Scholar was rate limited.
    warnings[:] = [
        warning
        for warning in warnings
        if not warning.startswith("Semantic Scholar limite temporairement")
    ]
    warnings.append(
        "Semantic Scholar (nettement plus précis qu'OpenAlex) a été limité. "
        "Configurez une clé Semantic Scholar dans Réglages > Avancé > Clés et accès API pour un budget dédié "
        "et éviter le repli sur OpenAlex."
    )


def _paper_selection_sort_key(paper: dict[str, Any]) -> tuple[Any, int, int]:
    return (
        _paper_selection_score(paper),
        _safe_int(paper.get("discovery_score"), 0, 0, 1000000),
        _paper_year_int(paper),
    )


def _emit_preliminary_results(
    run: _ProviderCollection,
    papers: list[dict[str, Any]],
    *,
    funnel: dict[str, Any],
    notebook_related: bool,
    model_name: str | None,
) -> None:
    """Show the pre-validation ranking while the LLM checks the candidates."""
    preview = [dict(paper) for paper in papers]
    _assign_semantic_rank_scores(preview)
    preview.sort(key=_paper_selection_sort_key, reverse=True)
    preliminary = [
        _present_paper(paper, notebook_related=notebook_related)
        for paper in _select_diverse_top_papers(preview, limit=run.result_limit)
    ]
    _emit_discovery_event(
        {
            "type": "preliminary",
            "results": preliminary,
            "warnings": _dedupe_keep_order(run.warnings),
            "meta": {
                "result_count": len(preliminary),
                "source_report": _build_source_report(run.provider_stats),
                "result_funnel": dict(funnel),
                "providers_used": sorted(run.providers_used),
                "provider_used": run.provider_used,
                "llm_validation": {"status": "running" if model_name else "skipped"},
            },
        }
    )


async def _validate_discovery_papers_with_llm(
    papers: list[dict[str, Any]],
    *,
    model_name: str | None,
    query_payload: dict[str, Any],
    validation_limit: int,
    warnings: list[str],
    funnel: dict[str, Any],
    debug_events: list[dict[str, Any]] | None,
    abort_signal: StreamAbortSignal | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Let the model score the best candidates and drop the ones it rejects."""
    validation_window = papers[: min(len(papers), validation_limit)]
    llm_validation: dict[str, Any] = {"status": "skipped", "evaluated": 0, "requested": 0, "error": ""}
    if not model_name or _has_discovery_budget(1.0, reserve=4.0):
        outcome = await _attach_llm_relevance_validation(
            validation_window,
            model_name,
            query_payload,
            validation_limit=validation_limit,
            debug_events=debug_events,
            abort_signal=abort_signal,
        )
        if isinstance(outcome, dict):
            llm_validation.update(outcome)
    elif model_name:
        llm_validation["status"] = "failed"
        llm_validation["error"] = "budget de temps épuisé"
    if not model_name:
        return papers, llm_validation
    if llm_validation["status"] == "failed":
        reason = str(llm_validation.get("error") or "réponse inexploitable")
        warnings.append(
            f"Validation LLM non effectuée ({reason}) : classement par mots-clés et similarité uniquement."
        )
    evaluated = [paper for paper in papers if paper.get("llm_relevance_score") is not None]
    if evaluated:
        accepted_evaluated = [
            paper
            for paper in evaluated
            if not paper.get("llm_rejected")
            and _safe_int(paper.get("llm_relevance_score"), 0, 0, 100) >= 35
        ]
        unvalidated = [paper for paper in papers if paper.get("llm_relevance_score") is None]
        rejected_count = len(evaluated) - len(accepted_evaluated)
        funnel["rejected_llm"] += rejected_count
        papers = accepted_evaluated + unvalidated
        if rejected_count:
            warnings.append(f"{rejected_count} résultat(s) rejetés par validation LLM.")
    return papers, llm_validation


# "articles de l'auteur X", "papers by X", "author: X": the request names a
# person. Keyword search over titles and abstracts cannot answer it, so these
# requests go to the providers' author indexes instead.
_AUTHOR_REQUEST_PATTERNS = (
    re.compile(
        r"(?:^|\b(?:de|d|par|by|of|from)\s+(?:l\s*['’]\s*|la\s+|le\s+|the\s+)?|['’]\s*)"
        r"(?:auteure?|autrice|author|chercheur|chercheuse|researcher)\b\s*[:=]?\s*(?P<rest>.+)$",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:auteure?|autrice|author)\s*[:=]\s*(?P<rest>.+)$", re.IGNORECASE),
    re.compile(
        r"\b(?:[ée]crite?s?|r[ée]dig[ée]e?s?|publi[ée]e?s?|sign[ée]e?s?|written|authored|published)"
        r"\s+(?:par|by)\s+(?P<rest>.+)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:articles?|papers?|papiers?|publications?|travaux|works?)\s+(?:par|by)\s+(?P<rest>.+)$",
        re.IGNORECASE,
    ),
)
_AUTHOR_TOPIC_SPLIT_RE = re.compile(
    r"\s+(?:sur|about|on|concernant|regarding|portant\s+sur"
    r"|[àa]\s+propos\s+(?:de|du|des|d['’])|au\s+sujet\s+(?:de|du|des|d['’]))\s+",
    re.IGNORECASE,
)
_AUTHOR_NAME_RE = re.compile(r"[^\W\d_]+(?:[-'’.][^\W\d_]*)*")
# After "author", these start a clause ("authors who measure…"), not a name.
_AUTHOR_NAME_REJECTED_FIRST_TOKENS = {
    "of", "who", "that", "which", "is", "are", "and", "or", "in", "on", "about",
    "qui", "que", "dont", "est", "sont", "et", "ou", "sur", "du", "des", "dans",
}  # fmt: skip
# Words of the request itself ("trouve les publications récentes de…"), not a topic.
_AUTHOR_REQUEST_FILLER_TERMS = {
    "publication", "publications", "travaux", "work", "works", "papier", "papiers",
    "trouve", "trouver", "find", "show", "liste", "lister", "list", "montre", "montrer",
    "donne", "donner", "give", "veux", "want", "voir", "ecrit", "ecrits", "written",
    "publie", "publies", "published", "recent", "recents", "recentes", "derniers",
    "dernieres", "latest",
}  # fmt: skip
_AUTHOR_REQUEST_MAX_CHARS = 200
_AUTHOR_NAME_MAX_TOKENS = 4
_AUTHOR_SEARCH_MAX_AUTHORS = 5
_AUTHOR_SEARCH_TOPIC_FETCH_LIMIT = 100
_OPENALEX_WORK_SELECT = (
    "id,display_name,doi,publication_year,authorships,primary_location,"
    "best_oa_location,abstract_inverted_index,cited_by_count"
)
_SEMANTIC_AUTHOR_PAPER_FIELDS = (
    "title,abstract,authors,year,externalIds,openAccessPdf,paperId,venue,url,"
    "citationCount,influentialCitationCount"
)


def _author_name_tokens(name: str) -> list[str]:
    return re.findall(r"[a-z]+", _normalize_search_text(name).lower())


def _author_name_matches(candidate: str, wanted_tokens: list[str]) -> bool:
    """Whether ``candidate`` can be the wanted person, in any order, initials allowed.

    "F. Sirois" and "Sirois, Frédéric" both match "frederic sirois"; a shared
    first name or a shared initial alone does not.
    """
    candidate_tokens = _author_name_tokens(candidate)
    if not wanted_tokens or not candidate_tokens:
        return False
    full_match = False
    for wanted in wanted_tokens:
        if len(wanted) > 1 and wanted in candidate_tokens:
            full_match = True
            continue
        if not any(
            (len(token) == 1 and wanted.startswith(token))
            or (len(wanted) == 1 and token.startswith(wanted))
            for token in candidate_tokens
        ):
            return False
    return full_match


def _extract_author_request(text: str) -> dict[str, Any] | None:
    """The author a short request asks for, with the topic words around the name."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean or len(clean) > _AUTHOR_REQUEST_MAX_CHARS or "\n" in (text or "").strip():
        return None
    for pattern in _AUTHOR_REQUEST_PATTERNS:
        match = pattern.search(clean)
        if match is None:
            continue
        rest = match.group("rest").strip()
        split = _AUTHOR_TOPIC_SPLIT_RE.search(rest)
        topic = rest[split.end() :] if split else ""
        name_part = rest[: split.start()] if split else rest
        name_part = re.split(r"[,;:()?!\"«»]", name_part.strip(" \"'«»“”"), maxsplit=1)[0]
        name_part = name_part.strip(" .'’-")
        tokens = name_part.split()
        if not tokens or len(tokens) > _AUTHOR_NAME_MAX_TOKENS:
            continue
        if tokens[0].lower() in _AUTHOR_NAME_REJECTED_FIRST_TOKENS:
            continue
        if not all(_AUTHOR_NAME_RE.fullmatch(token) for token in tokens):
            continue
        if not any(len(token) > 1 for token in _author_name_tokens(name_part)):
            continue
        topic_text = f"{clean[: match.start()]} {topic}"
        topic_keywords = _dedupe_keep_order(
            [
                _canonical_query_term(token)
                for token in _keyword_fallback(topic_text, limit=8)
                if token not in _GENERIC_DISCOVERY_TERMS
                and token not in _AUTHOR_REQUEST_FILLER_TERMS
            ]
        )
        return {"name": name_part, "topic_keywords": [term for term in topic_keywords if term]}
    return None


async def _openalex_author_ids(client: httpx.AsyncClient, name: str) -> list[str]:
    payload = await _get_json_with_retry(
        client,
        f"{_OPENALEX_BASE}/authors",
        params={
            "search": name,
            "per-page": _AUTHOR_SEARCH_MAX_AUTHORS,
            "select": "id,display_name,display_name_alternatives",
        },
        api_name="openalex",
    )
    wanted = _author_name_tokens(name)
    author_ids: list[str] = []
    for row in _list_payload(payload.get("results")):
        if not isinstance(row, dict):
            continue
        names = [row.get("display_name"), *_list_payload(row.get("display_name_alternatives"))]
        if not any(_author_name_matches(str(value or ""), wanted) for value in names):
            continue
        author_id = str(row.get("id") or "").rstrip("/").rsplit("/", 1)[-1].strip()
        if author_id:
            author_ids.append(author_id)
    return author_ids


async def _openalex_author_works(
    client: httpx.AsyncClient,
    name: str,
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    author_ids = await _openalex_author_ids(client, name)
    if not author_ids:
        return []
    payload = await _get_json_with_retry(
        client,
        f"{_OPENALEX_BASE}/works",
        params={
            "filter": "authorships.author.id:" + "|".join(author_ids),
            "sort": "publication_date:desc",
            "per-page": _safe_int(limit, 30, 1, _AUTHOR_SEARCH_TOPIC_FETCH_LIMIT),
            "select": _OPENALEX_WORK_SELECT,
        },
        api_name="openalex",
    )
    return [row for row in _list_payload(payload.get("results")) if isinstance(row, dict)]


async def _semantic_author_papers(client: httpx.AsyncClient, name: str) -> list[dict[str, Any]]:
    payload = await _get_json_with_retry(
        client,
        f"{_S2_BASE}/graph/v1/author/search",
        params={
            "query": name,
            "limit": _AUTHOR_SEARCH_MAX_AUTHORS,
            "fields": "name,"
            + ",".join(
                f"papers.{paper_field}" for paper_field in _SEMANTIC_AUTHOR_PAPER_FIELDS.split(",")
            ),
        },
        api_name="semanticscholar",
        retries=0,
    )
    wanted = _author_name_tokens(name)
    papers: list[dict[str, Any]] = []
    for author in _list_payload(payload.get("data")):
        if not isinstance(author, dict):
            continue
        if not _author_name_matches(str(author.get("name") or ""), wanted):
            continue
        papers.extend(row for row in _list_payload(author.get("papers")) if isinstance(row, dict))
    return papers


async def _crossref_author_works(
    client: httpx.AsyncClient,
    name: str,
    *,
    limit: int = 30,
    mailto: str = "",
) -> list[dict[str, Any]]:
    # Crossref matches the name loosely against every author field.
    wanted = _author_name_tokens(name)
    rows = await _crossref_search(client, "", limit=limit, mailto=mailto, author=name)
    return [
        row
        for row in rows
        if any(
            _author_name_matches(f"{author.get('given') or ''} {author.get('family') or ''}", wanted)
            for author in _list_payload(row.get("author"))
            if isinstance(author, dict)
        )
    ]


async def _collect_author_rows(
    run: _ProviderCollection,
    provider: str,
    fetch: Any,
) -> None:
    label = _provider_label(provider)
    try:
        rows = await fetch()
    except Exception as exc:
        logger.debug("%s author search failed.", label, exc_info=True)
        _record_provider_fetch(run.provider_stats, provider, phase="author", error=str(exc))
        if provider == "semanticscholar" and _is_rate_limit_error(exc):
            run.rate_limited = True
        else:
            run.warnings.append(f"{label} indisponible pour la recherche par auteur: {exc}")
        return
    _record_provider_fetch(run.provider_stats, provider, rows=rows, phase="author", query_count=1)
    if not rows:
        return
    run.providers_used.add(label)
    if not run.primary_provider:
        run.set_primary(provider)
    run.rows.extend((provider, row) for row in rows)


def _merge_same_title_papers(papers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold the copies of one work that the sources index without a shared DOI."""
    by_title: dict[str, dict[str, Any]] = {}
    merged: list[dict[str, Any]] = []
    for paper in papers:
        key = " ".join(_tokenize_search_text(str(paper.get("title") or "")))
        existing = by_title.get(key) if key else None
        if existing is not None:
            _merge_paper_records(existing, paper)
            continue
        if key:
            by_title[key] = paper
        merged.append(paper)
    return merged


async def _discover_author_articles(
    author_request: dict[str, Any],
    *,
    max_results: int,
    include_unpaywall: bool,
    unpaywall_email: str | None,
    abort_signal: StreamAbortSignal | None = None,
) -> dict[str, Any]:
    """List a person's publications, newest first.

    Nothing here is topical: no keyword anchoring, no dense ranking and no LLM
    validation, which would all judge the papers against the author's name.
    """
    name = str(author_request.get("name") or "").strip()
    topic_keywords = list(author_request.get("topic_keywords") or [])
    query_payload: dict[str, Any] = {
        "keywords": [name, *topic_keywords],
        "dois": [],
        "search_query": name,
        "search_queries": [name],
        "topic_summary": f"Publications de {name}",
        "generator": "author_search",
        "author": name,
    }
    provider_stats = _init_provider_stats()
    run_report = _discovery_run_report_var.get()
    funnel = run_report["funnel"] if run_report is not None else _new_result_funnel()
    if run_report is not None:
        run_report["provider_stats"] = provider_stats
    result_limit = _safe_int(max_results, 10, 1, 30)
    fetch_limit = _AUTHOR_SEARCH_TOPIC_FETCH_LIMIT if topic_keywords else 30
    crossref_email = (unpaywall_email or os.getenv("UNPAYWALL_EMAIL", "") or "").strip()
    timeout = httpx.Timeout(connect=12.0, read=20.0, write=20.0, pool=20.0)
    headers = {"User-Agent": "BIBLIOBRAIN/1.0 (local)"}
    _emit_progress("search", f"Recherche des publications de {name}…")
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        _throw_if_aborted(abort_signal)
        run = _ProviderCollection(
            client=client,
            query_payload=query_payload,
            abort_signal=abort_signal,
            llm_available=False,
            result_limit=result_limit,
            candidate_limit=fetch_limit,
            provider_stats=provider_stats,
            warnings=[],
            search_queries=[name],
        )
        if not _openalex_api_key():
            run.warnings.append(_OPENALEX_MISSING_API_KEY_WARNING)
        await asyncio.gather(
            _collect_author_rows(
                run, "openalex", lambda: _openalex_author_works(client, name, limit=fetch_limit)
            ),
            _collect_author_rows(
                run, "hal", lambda: _hal_search(client, "", limit=fetch_limit, author=name)
            ),
            _collect_author_rows(
                run, "semanticscholar", lambda: _semantic_author_papers(client, name)
            ),
        )
        _throw_if_aborted(abort_signal)
        if not run.rows:
            await _collect_author_rows(
                run,
                "crossref",
                lambda: _crossref_author_works(
                    client, name, limit=fetch_limit, mailto=crossref_email
                ),
            )

        papers = _merge_same_title_papers(
            _normalized_papers_from_provider_rows(run.rows, provider_stats)
        )
        funnel["found"] = len(papers)
        papers.sort(
            key=lambda paper: (
                len(
                    _matched_keywords(
                        f"{paper.get('title') or ''} {paper.get('abstract') or ''}",
                        topic_keywords,
                    )
                ),
                _paper_year_int(paper),
                _safe_int(paper.get("citation_count"), 0, 0, 10000000),
            ),
            reverse=True,
        )
        papers = papers[:result_limit]
        if include_unpaywall and papers:
            await _attach_unpaywall_links(run, papers, unpaywall_email=unpaywall_email)

    warnings = run.warnings
    if papers:
        order = (
            "celles qui correspondent au sujet d'abord, puis les plus récentes"
            if topic_keywords
            else "les plus récentes d'abord"
        )
        warnings.insert(0, f"Recherche par auteur : publications de {name}, {order}.")
    for paper in papers:
        _present_paper(paper)

    funnel["shown"] = len(papers)
    source_report = _build_source_report(provider_stats)
    response: dict[str, Any] = {
        "ok": True,
        "mode": "search",
        "query": query_payload,
        "results": papers,
        "warnings": _dedupe_keep_order(warnings),
        "meta": {
            "result_count": len(papers),
            "include_unpaywall": bool(include_unpaywall),
            "external_calls_local_only": True,
            "rate_limited": run.rate_limited,
            "query_planner_enabled": False,
            "query_generator": "author_search",
            "author_search": {"name": name, "topic_keywords": topic_keywords},
            "provider_used": run.provider_used,
            "primary_provider": run.primary_provider,
            "fallback_provider": "",
            "providers_used": sorted(run.providers_used),
            "provider_stats": provider_stats,
            "openalex_api_key_configured": bool(_openalex_api_key()),
            "semantic_scholar_api_key_configured": bool(_semantic_scholar_api_key()),
            "llm_validation_enabled": False,
            "llm_validation": {"status": "skipped", "evaluated": 0, "requested": 0, "error": ""},
            "source_report": source_report,
            "result_funnel": funnel,
            "notebook_profile": None,
        },
    }
    if not papers:
        some_source_answered = any(item["status"] == "empty" for item in source_report)
        response["meta"]["empty_reason"] = (
            f"Aucune publication trouvée pour l'auteur « {name} ». "
            "Vérifiez l'orthographe ou essayez le nom complet (prénom et nom)."
            if some_source_answered
            else _empty_result_explanation(source_report, funnel)
        )
    return response


async def _discover_articles_impl(
    *,
    mode: str,
    input_text: str,
    model_name: str | None,
    notebook_id: str | None,
    max_results: int,
    include_unpaywall: bool,
    unpaywall_email: str | None,
    query_planner_enabled: bool = False,
    debug: bool = False,
    abort_signal: StreamAbortSignal | None = None,
) -> dict[str, Any]:
    normalized_mode = _normalize_discovery_mode(mode)
    author_request = _extract_author_request(input_text) if normalized_mode == "search" else None
    if author_request is not None:
        return await _discover_author_articles(
            author_request,
            max_results=max_results,
            include_unpaywall=include_unpaywall,
            unpaywall_email=unpaywall_email,
            abort_signal=abort_signal,
        )
    debug_events: list[dict[str, Any]] | None = [] if debug else None
    notebook_profile = (
        _build_notebook_discovery_profile(notebook_id)
        if normalized_mode == "notebook" and notebook_id
        else {}
    )
    effective_input_text = _discovery_input_text(input_text, notebook_profile)
    _emit_progress("query", "Génération de la requête bibliographique…")
    generate_query_payload = (
        _generate_query_payload_with_planner
        if query_planner_enabled
        else _generate_query_payload_with_llm
    )
    query_payload = await generate_query_payload(
        effective_input_text,
        model_name,
        debug_events,
        abort_signal,
    )
    seed_dois = list(notebook_profile.get("seed_dois") or [])
    if seed_dois and normalized_mode == "notebook":
        query_payload["seed_dois"] = seed_dois[:12]
    if notebook_profile:
        generated_summary = str(query_payload.get("topic_summary") or "").strip()
        if generated_summary:
            notebook_profile["summary"] = generated_summary[:600]
        query_payload["notebook_profile_summary"] = str(notebook_profile.get("summary") or "")

    provider_stats = _init_provider_stats()
    run_report = _discovery_run_report_var.get()
    funnel = run_report["funnel"] if run_report is not None else _new_result_funnel()
    if run_report is not None:
        run_report["provider_stats"] = provider_stats
    app_settings = get_app_settings()
    llm_available = bool((model_name or "").strip())
    dense_enabled = bool(app_settings.get("discovery_dense_enabled", True))
    dense_meta: dict[str, Any] = {"applied": False, "candidates": 0}
    snowball_added = 0
    openalex_api_key_configured = bool(_openalex_api_key())
    semantic_scholar_api_key_configured = bool(_semantic_scholar_api_key())
    result_limit = _safe_int(max_results, 10, 1, 30)
    candidate_limit = _discovery_candidate_limit(
        result_limit,
        llm_available=llm_available,
        settings=app_settings,
    )
    validation_limit = (
        _discovery_validation_limit(result_limit, settings=app_settings)
        if llm_available
        else result_limit
    )
    crossref_email = (unpaywall_email or os.getenv("UNPAYWALL_EMAIL", "") or "").strip()
    timeout = httpx.Timeout(connect=12.0, read=20.0, write=20.0, pool=20.0)
    headers = {"User-Agent": "BIBLIOBRAIN/1.0 (local)"}
    _emit_progress("search", "Interrogation de Semantic Scholar, OpenAlex et des compléments externes…")
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        _throw_if_aborted(abort_signal)
        run = _ProviderCollection(
            client=client,
            query_payload=query_payload,
            abort_signal=abort_signal,
            llm_available=llm_available,
            result_limit=result_limit,
            candidate_limit=candidate_limit,
            provider_stats=provider_stats,
            warnings=[],
        )
        await _collect_provider_rows(
            run,
            notebook_mode=normalized_mode == "notebook",
            seed_dois=seed_dois,
            max_results=max_results,
            openalex_api_key_configured=openalex_api_key_configured,
            crossref_mailto=crossref_email,
        )
        warnings = run.warnings

        papers = _normalized_papers_from_provider_rows(run.rows, provider_stats)
        funnel["found"] = len(papers)
        enrich_cap = (
            _DISCOVERY_DENSE_PREFILTER_MAX
            if dense_enabled
            else _DISCOVERY_ABSTRACT_ENRICHMENT_MAX_PAPERS
        )
        enriched_abstracts = await _enrich_missing_abstracts(
            papers,
            client,
            mailto=crossref_email,
            max_papers=enrich_cap,
        )
        if enriched_abstracts:
            warnings.append(f"{enriched_abstracts} abstract(s) complétés via DOI enrichi.")

        papers, dropped = _rank_discovery_papers(
            papers,
            query_payload,
            require_query_anchor=run.provider_used in {"hal", "openalex", "crossref"},
            minimum_domain_anchor_coverage=(
                2
                if run.provider_used == "crossref"
                and len(_query_domain_anchor_terms(query_payload)) >= 3
                else 1
            ),
        )
        funnel["dropped_anchor"] += dropped
        if dropped:
            warnings.append(f"{dropped} résultat(s) retirés: ancrage lexical insuffisant.")

        # Semantic pre-ranking happens before citation expansion so a merely
        # lexical false positive cannot drag the snowball graph off-topic.
        if dense_enabled:
            papers, dense_meta = await _dense_rank_discovery_papers(
                papers,
                input_text=effective_input_text,
                query_payload=query_payload,
            )
            if dense_meta.get("applied") and len(papers) > _DISCOVERY_DENSE_PREFILTER_MAX:
                papers = papers[:_DISCOVERY_DENSE_PREFILTER_MAX]

        if (
            normalized_mode == "search"
            and not run.rate_limited
            and papers
            and _has_discovery_budget(3.0, reserve=_DISCOVERY_OPTIONAL_NETWORK_RESERVE_SECONDS)
        ):
            papers, snowball_added = await _snowball_discovery_papers(
                run,
                papers,
                mailto=crossref_email,
                enrich_cap=enrich_cap,
            )

        for provider, label in (("hal", "HAL"), ("openalex", "OpenAlex")):
            if run.provider_used == provider and not papers and run.rows:
                warnings.append(
                    f"Fallback {label} trop peu fiable pour cette requête: "
                    "aucun résultat sans ancrage lexical n'a été conservé."
                )

        if include_unpaywall:
            await _attach_unpaywall_links(run, papers, unpaywall_email=unpaywall_email)

    provider_used = run.provider_used or run.primary_provider or run.fallback_provider
    if run.rate_limited and not semantic_scholar_api_key_configured:
        _replace_rate_limit_warnings(warnings)

    excluded_notebook_count = 0
    if notebook_profile:
        existing_keys = set(notebook_profile.get("existing_keys") or [])
        before_exclusion = len(papers)
        papers = [paper for paper in papers if _paper_identity_key(paper) not in existing_keys]
        excluded_notebook_count = before_exclusion - len(papers)
        if excluded_notebook_count:
            warnings.append(
                f"{excluded_notebook_count} article(s) déjà présents dans le carnet ont été exclus."
            )

    if dense_meta.get("applied"):
        # Dense ordering is authoritative; refresh lexical scores for the
        # composite tie-breaker without overriding the semantic order.
        for paper in papers:
            _score_discovery_paper(paper, query_payload)
    else:
        papers, _dropped = _rank_discovery_papers(papers, query_payload)
    if not llm_available:
        before_strict = len(papers)
        papers = [
            paper
            for paper in papers
            if paper.get("has_query_anchor") and _paper_has_meaningful_abstract(paper)
        ]
        strict_dropped = before_strict - len(papers)
        funnel["dropped_strict"] += strict_dropped
        if strict_dropped:
            warnings.append(
                f"{strict_dropped} résultat(s) retirés: mode sans LLM exige un ancrage et un abstract."
            )

    _emit_progress("ranking", f"Tri et reclassement de {len(papers)} article(s)…")
    papers, reranker_meta = await _rerank_discovery_papers(
        papers,
        input_text=effective_input_text,
        query_payload=query_payload,
    )
    if reranker_meta.get("applied") and not llm_available:
        for paper in papers:
            rank_score = _safe_int(paper.get("reranker_rank_score"), 0, 0, 1000000)
            heuristic = _safe_int(paper.get("discovery_score"), 0, 0, 1000000)
            paper["validated_score"] = rank_score * 1000 + heuristic

    if _discovery_event_sink_var.get() is not None:
        run.provider_used = provider_used
        _emit_preliminary_results(
            run,
            papers,
            funnel=funnel,
            notebook_related=bool(notebook_profile),
            model_name=model_name,
        )

    papers, llm_validation = await _validate_discovery_papers_with_llm(
        papers,
        model_name=model_name,
        query_payload=query_payload,
        validation_limit=validation_limit,
        warnings=warnings,
        funnel=funnel,
        debug_events=debug_events,
        abort_signal=abort_signal,
    )
    _assign_semantic_rank_scores(papers)
    papers.sort(key=_paper_selection_sort_key, reverse=True)
    papers = _select_diverse_top_papers(papers, limit=result_limit)
    if model_name and llm_validation["status"] == "partial":
        # Only what the user sees matters: the best-ranked batches go first,
        # so a partial run often still validated every displayed result.
        unchecked = sum(1 for paper in papers if paper.get("llm_relevance_score") is None)
        if unchecked:
            warnings.append(
                f"Validation LLM partielle : {unchecked} des {len(papers)} résultats affichés "
                "n'ont pas pu être vérifiés et sont classés par mots-clés et similarité."
            )
    if not model_name or _has_discovery_budget(0.8, reserve=1.0):
        _emit_progress("summary", "Rédaction des résumés…")
        await _attach_local_short_summaries(
            papers,
            model_name,
            query_payload,
            debug_events,
            abort_signal,
        )
    else:
        for paper in papers:
            paper["short_summary"] = _first_sentence(paper.get("abstract", ""))

    for paper in papers:
        _present_paper(paper, notebook_related=bool(notebook_profile))

    funnel["shown"] = len(papers)
    source_report = _build_source_report(provider_stats)
    response: dict[str, Any] = {
        "ok": True,
        "mode": normalized_mode,
        "query": query_payload,
        "results": papers,
        "warnings": _dedupe_keep_order(warnings),
        "meta": {
            "result_count": len(papers),
            "include_unpaywall": bool(include_unpaywall),
            "external_calls_local_only": True,
            "rate_limited": run.rate_limited,
            "query_planner_enabled": bool(query_planner_enabled),
            "query_generator": str(query_payload.get("generator") or ""),
            "provider_used": provider_used,
            "primary_provider": run.primary_provider or provider_used,
            "fallback_provider": run.fallback_provider,
            "providers_used": sorted(run.providers_used),
            "provider_stats": provider_stats,
            "candidate_limit": candidate_limit,
            "validation_limit": validation_limit,
            "openalex_api_key_configured": openalex_api_key_configured,
            "semantic_scholar_api_key_configured": semantic_scholar_api_key_configured,
            "reranker_applied": bool(reranker_meta.get("applied")),
            "reranker_candidates": _safe_int(reranker_meta.get("candidates"), 0, 0, 1000000),
            "reranker": reranker_meta,
            "dense_enabled": dense_enabled,
            "dense_applied": bool(dense_meta.get("applied")),
            "dense": dense_meta,
            "snowball_added": snowball_added,
            "abstracts_enriched": enriched_abstracts,
            "llm_validation_enabled": llm_available,
            "llm_validation": llm_validation,
            "source_report": source_report,
            "result_funnel": funnel,
            "llm_reranking_enabled": llm_available,
            "notebook_profile": (
                {
                    "id": notebook_profile.get("id"),
                    "title": notebook_profile.get("title"),
                    "summary": notebook_profile.get("summary"),
                    "source_count": notebook_profile.get("source_count", 0),
                    "profiled_source_count": notebook_profile.get("profiled_source_count", 0),
                    "seed_doi_count": len(seed_dois),
                    "excluded_result_count": excluded_notebook_count,
                }
                if notebook_profile
                else None
            ),
        },
    }
    if not papers:
        response["meta"]["empty_reason"] = _empty_result_explanation(source_report, funnel)
    if debug_events is not None:
        response["debug"] = {
            "llm_events": debug_events,
            "event_count": len(debug_events),
        }
    return response


async def discover_articles(
    *,
    mode: str,
    input_text: str,
    model_name: str | None,
    notebook_id: str | None,
    max_results: int,
    include_unpaywall: bool,
    unpaywall_email: str | None,
    query_planner_enabled: bool = False,
    debug: bool = False,
    abort_signal: StreamAbortSignal | None = None,
    on_event: Any = None,
    budget_seconds: float | None = None,
) -> dict[str, Any]:
    """Run discovery under a strict end-to-end latency budget.

    ``on_event`` receives progress events and a preliminary result list; the
    progressive endpoint passes a larger ``budget_seconds`` because results
    are already on screen while the LLM validates them.

    Optional enrichment stages read the shared deadline and stop early.  The
    hard timeout is a final safety net so an unhealthy provider or local model
    cannot make an interactive search exceed one minute.
    """

    started = time.monotonic()
    budget = float(budget_seconds) if budget_seconds else _discovery_total_budget()
    deadline_token = _discovery_deadline_var.set(started + budget)
    sink_token = _discovery_event_sink_var.set(on_event)
    metrics: dict[str, int] = {
        "network_calls": 0,
        "cache_hits": 0,
        "retries": 0,
        "batch_calls": 0,
    }
    metrics_token = _discovery_request_metrics_var.set(metrics)
    run_report: dict[str, Any] = {"provider_stats": None, "funnel": _new_result_funnel()}
    report_token = _discovery_run_report_var.set(run_report)
    try:
        response = await asyncio.wait_for(
            _discover_articles_impl(
                mode=mode,
                input_text=input_text,
                model_name=model_name,
                notebook_id=notebook_id,
                max_results=max_results,
                include_unpaywall=include_unpaywall,
                unpaywall_email=unpaywall_email,
                query_planner_enabled=query_planner_enabled,
                debug=debug,
                abort_signal=abort_signal,
            ),
            timeout=budget + _DISCOVERY_HARD_TIMEOUT_MARGIN_SECONDS,
        )
        meta = response.setdefault("meta", {})
        meta["elapsed_ms"] = round((time.monotonic() - started) * 1000.0, 3)
        meta["time_budget_seconds"] = budget
        meta["timed_out"] = False
        meta["external_request_metrics"] = dict(metrics)
        meta["semantic_scholar_circuit"] = _semantic_circuit_status()
        return response
    except TimeoutError:
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
        source_report = _build_source_report(run_report.get("provider_stats"))
        funnel = run_report["funnel"]
        return {
            "ok": False,
            "mode": str(mode or "search"),
            "query": _build_heuristic_query_payload(input_text),
            "results": [],
            "warnings": [
                "La recherche a atteint sa limite de temps ; relancez-la pour utiliser le cache déjà rempli."
            ],
            "msg": _empty_result_explanation(source_report, funnel, timed_out=True),
            "meta": {
                "result_count": 0,
                "elapsed_ms": elapsed_ms,
                "time_budget_seconds": budget,
                "timed_out": True,
                "source_report": source_report,
                "result_funnel": funnel,
                "external_request_metrics": dict(metrics),
                "semantic_scholar_circuit": _semantic_circuit_status(),
            },
        }
    finally:
        _discovery_event_sink_var.reset(sink_token)
        _discovery_run_report_var.reset(report_token)
        _discovery_request_metrics_var.reset(metrics_token)
        _discovery_deadline_var.reset(deadline_token)


_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024
_DOWNLOAD_MAX_REDIRECTS = 5


def _validate_public_download_url(url: str) -> str:
    """Check that ``url`` only reaches public addresses; return one of them.

    The download then connects to that address, so a second DNS answer
    (DNS rebinding) cannot send it to a local or private host.
    """
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL PDF invalide: seuls HTTP et HTTPS sont autorises.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL PDF invalide: les identifiants integres sont interdits.")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = {
        str(info[4][0])
        for info in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    }
    if not addresses:
        raise ValueError("URL PDF invalide: hote introuvable.")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("URL PDF refusee: les adresses locales ou privees sont interdites.")
    # IPv4 first: many home connections still have no IPv6 route.
    return sorted(addresses, key=lambda address: (":" in address, address))[0]


def _pinned_download_request(url: str, address: str) -> tuple[str, dict[str, str], dict[str, str]]:
    """The URL, headers and extensions to fetch ``url`` from ``address`` only.

    The host name stays in the Host header and in TLS (SNI and certificate
    check), so virtual hosts and HTTPS keep working.
    """
    parsed = urlsplit(url)
    hostname = parsed.hostname or ""
    host_header = parsed.netloc.rsplit("@", 1)[-1]
    ip_host = f"[{address}]" if ":" in address else address
    netloc = f"{ip_host}:{parsed.port}" if parsed.port else ip_host
    pinned_url = urlunsplit((parsed.scheme, netloc, parsed.path or "/", parsed.query, ""))
    extensions = {"sni_hostname": hostname} if parsed.scheme == "https" else {}
    return pinned_url, {"Host": host_header}, extensions


async def _download_binary(url: str) -> bytes:
    timeout = httpx.Timeout(connect=12.0, read=45.0, write=20.0, pool=20.0)
    headers = {"User-Agent": "BIBLIOBRAIN/1.0 (local)"}
    current_url = str(url or "").strip()
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=False) as client:
        for _redirect_count in range(_DOWNLOAD_MAX_REDIRECTS + 1):
            address = await asyncio.to_thread(_validate_public_download_url, current_url)
            pinned_url, pinned_headers, extensions = _pinned_download_request(current_url, address)
            async with client.stream(
                "GET", pinned_url, headers=pinned_headers, extensions=extensions
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location", "").strip()
                    if not location:
                        raise ValueError("Redirection PDF sans destination.")
                    current_url = urljoin(current_url, location)
                    continue

                response.raise_for_status()
                content_length = response.headers.get("content-length", "").strip()
                if content_length.isdigit() and int(content_length) > _DOWNLOAD_MAX_BYTES:
                    raise ValueError("Le PDF depasse la taille maximale autorisee.")

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > _DOWNLOAD_MAX_BYTES:
                        raise ValueError("Le PDF depasse la taille maximale autorisee.")
                    chunks.append(chunk)
                return b"".join(chunks)
        raise ValueError("Trop de redirections lors du telechargement PDF.")


async def _attempt_local_zotero_connector_add(
    *,
    title: str,
    doi: str,
    pdf_abs: Path,
    semantic_url: str = "",
    citation_count: int = 0,
    influential_citation_count: int = 0,
) -> tuple[bool, str]:
    endpoint = (
        os.getenv("ZOTERO_CONNECTOR_URL", "http://127.0.0.1:23119/connector/saveItems") or ""
    ).strip()
    if not endpoint:
        return False, "Zotero connector URL non configuree."

    extra_lines = []
    if citation_count > 0:
        extra_lines.append(f"citation_count: {citation_count}")
    if influential_citation_count > 0:
        extra_lines.append(f"influential_citation_count: {influential_citation_count}")

    item_payload = {
        "itemType": "journalArticle",
        "title": title or "Untitled",
        "DOI": doi or "",
        "url": semantic_url or "",
        "attachments": [
            {
                "title": pdf_abs.name,
                "path": pdf_abs.resolve().as_uri(),
                "mimeType": "application/pdf",
            }
        ],
    }
    if extra_lines:
        item_payload["extra"] = "\n".join(extra_lines)

    payload = {
        "items": [
            item_payload
        ]
    }
    try:
        timeout = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=5.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.post(endpoint, json=payload)
            if response.status_code < 300:
                return True, "Ajout via Zotero connector local."
            return False, f"Connector Zotero indisponible (HTTP {response.status_code})."
    except Exception as exc:
        logger.debug(
            "Recovered from an error in _attempt_local_zotero_connector_add.", exc_info=True
        )
        return False, f"Connector Zotero non joignable: {exc}"


async def download_and_store_paper_pdf(
    *,
    title: str,
    doi: str,
    pdf_url: str,
    semantic_url: str = "",
    citation_count: int = 0,
    influential_citation_count: int = 0,
    add_to_zotero: bool = True,
) -> dict[str, Any]:
    target_url = (pdf_url or "").strip()
    if not target_url:
        return {"ok": False, "msg": "Aucun lien PDF Open Access disponible."}

    try:
        content = await _download_binary(target_url)
    except Exception as exc:
        logger.debug("Recovered from an error in download_and_store_paper_pdf.", exc_info=True)
        return {"ok": False, "msg": f"Echec telechargement PDF: {exc}"}

    if len(content) < 150 or (not content.startswith(b"%PDF")):
        return {"ok": False, "msg": "Le fichier telecharge ne ressemble pas a un PDF valide."}

    base_dir = Path(src.config.DATA_PATH)
    target_dir = base_dir / "discovery_imports"
    target_dir.mkdir(parents=True, exist_ok=True)

    stem = _short_title_slug(title=title, doi=doi)
    filename = f"{stem}.pdf"
    out_path = target_dir / filename
    counter = 2
    while out_path.exists():
        out_path = target_dir / f"{stem}_{counter}.pdf"
        counter += 1

    out_path.write_bytes(content)
    rel_path = out_path.relative_to(base_dir).as_posix()

    if add_to_zotero:
        added_to_zotero, zotero_msg = await _attempt_local_zotero_connector_add(
            title=title,
            doi=doi,
            pdf_abs=out_path,
            semantic_url=semantic_url,
            citation_count=_safe_int(citation_count, 0, 0, 10_000_000),
            influential_citation_count=_safe_int(
                influential_citation_count,
                0,
                0,
                10_000_000,
            ),
        )
    else:
        added_to_zotero, zotero_msg = False, ""

    return {
        "ok": True,
        "msg": (
            "PDF telecharge localement."
            + (
                " Ajoute a Zotero via connector local."
                if added_to_zotero
                else (f" {zotero_msg}" if zotero_msg else "")
            )
        ),
        "saved_rel_path": rel_path,
        "saved_abs_path": str(out_path),
        "added_to_zotero": added_to_zotero,
        "zotero_msg": zotero_msg,
        "citation_count": _safe_int(citation_count, 0, 0, 10_000_000),
        "influential_citation_count": _safe_int(
            influential_citation_count,
            0,
            0,
            10_000_000,
        ),
    }
