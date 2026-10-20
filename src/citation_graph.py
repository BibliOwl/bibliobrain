"""Build a paper-to-paper citation graph for a notebook using OpenAlex.

Nodes = notebook sources resolved to OpenAlex works (via DOI, with a title
fallback for single library papers whose local metadata is incomplete).
Edges = directed "A cites B" when B's OpenAlex ID is in A's referenced_works.
Missing links = OpenAlex IDs cited by >= 2 notebook papers but absent from
the notebook (recommendation candidates).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
import os
import re
import time
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import quote

import httpx

from src import config
from src import library as library_module
from src.article_discovery import (
    _OPENALEX_BASE,
    _canonicalize_doi,
    _get_json_with_retry,
)
from src.library import _lookup_zotero, list_library
from src.notebooks import get_notebook

_logger = logging.getLogger(__name__)

_BATCH_DOI_LIMIT = 50
_BATCH_OAID_LIMIT = 50
_MAX_PRIOR_WORKS = 50
_MAX_DERIVATIVE_WORKS = 50
_TOP_CITERS_PER_PAPER = 30
_HTTP_TIMEOUT_SECONDS = 30.0
_SIMILARITY_TOP_PAIRS_FACTOR = 3
_SIMILARITY_MIN_SCORE = 0.05
# Bound the heavy phases so big notebooks don't hang on per-paper OpenAlex fan-out
# (the OpenAlex throttle serializes all calls behind a ~0.35s lock).
_MAX_PAPERS_FOR_DERIVATIVES = 30
_MAX_NODES_FOR_SIMILARITY = 80
_MAX_EXPANSION_PER_DIRECTION = 24
_MAX_EXPANSION_REFERENCE_CANDIDATES = 72
_OPENALEX_BATCH_CONCURRENCY = 3
_CROSSREF_BASE = "https://api.crossref.org"
_INTERNAL_GRAPH_CACHE_TTL_SECONDS = 15 * 60
_internal_graph_cache: dict[str, Any] = {}
_RESEARCH_SCOPE_CACHE_TTL_SECONDS = 15 * 60
_RESEARCH_SCOPE_CACHE_MAX_ENTRIES = 16
_research_scope_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
_zotero_autoload_key: str | None = None
_COMMUNITY_MAX_ARTICLES = 48
_COMMUNITY_RESOLUTION = 1.25

_SELECT_FULL = (
    "id,display_name,doi,publication_year,authorships,"
    "cited_by_count,referenced_works,abstract_inverted_index,primary_location"
)
_SELECT_LIGHT = (
    "id,display_name,doi,publication_year,authorships,"
    "cited_by_count,abstract_inverted_index,primary_location"
)


def _strip_oa_prefix(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("https://openalex.org/"):
        return text[len("https://openalex.org/") :]
    return text


def _normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def _paper_lookup_hints(rel_path: str, item: Any | None) -> tuple[str, str, str]:
    """Return (title, year, first-author) hints, including Zotero-style filenames."""
    title = str(getattr(item, "title", "") or "").strip()
    year = str(getattr(item, "year", "") or "").strip()
    authors = getattr(item, "authors", None) or []
    author = str(authors[0] if authors else "").strip()
    if title:
        return title, year, author

    stem = os.path.splitext(os.path.basename(str(rel_path or "")))[0].strip()
    parts = [part.strip() for part in re.split(r"\s+-\s+", stem) if part.strip()]
    for index, part in enumerate(parts[:-1]):
        match = re.fullmatch(r"(?:19|20)\d{2}[a-z]?", part, re.IGNORECASE)
        if not match:
            continue
        year = year or part[:4]
        author = author or " - ".join(parts[:index])
        title = " - ".join(parts[index + 1 :])
        break
    return (title or stem), year, author


async def _fetch_work_by_title(
    client: httpx.AsyncClient,
    title: str,
    *,
    year: str = "",
    author: str = "",
) -> dict[str, Any] | None:
    """Resolve a work when local Zotero metadata has no usable DOI.

    OpenAlex search is deliberately followed by a conservative local title
    match. Prefix matching covers Zotero filenames truncated near 100 chars.
    """
    query_title = str(title or "").strip()
    normalized_query = _normalize_title(query_title)
    if len(normalized_query) < 18:
        return None
    payload = await _get_json_with_retry(
        client,
        f"{_OPENALEX_BASE}/works",
        params={"search": query_title, "per-page": 8, "select": _SELECT_FULL},
        api_name="openalex",
    )
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None

    expected_year = int(year[:4]) if re.fullmatch(r"(?:19|20)\d{2}", year[:4]) else None
    author_key = _normalize_title(author.split()[0] if author else "")
    best: tuple[float, float, dict[str, Any]] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        work = _normalize_oa_work(row)
        candidate_title = _normalize_title(work.get("title"))
        if not candidate_title:
            continue
        prefix_match = (
            len(normalized_query) >= 25
            and (
                candidate_title.startswith(normalized_query)
                or normalized_query.startswith(candidate_title)
            )
        )
        title_score = SequenceMatcher(None, normalized_query, candidate_title).ratio()
        score = title_score
        candidate_year = work.get("year")
        if expected_year and candidate_year:
            score += 0.05 if candidate_year == expected_year else -0.08
        if author_key:
            candidate_authors = _normalize_title(" ".join(work.get("authors") or []))
            if author_key in candidate_authors.split():
                score += 0.03
        if not prefix_match and title_score < 0.84:
            continue
        ranked = (score, title_score, work)
        if best is None or ranked[:2] > best[:2]:
            best = ranked
    return best[2] if best else None


def _decode_abstract(inverted: Any) -> str:
    if not isinstance(inverted, dict):
        return ""
    positions: dict[int, str] = {}
    for word, idx_list in inverted.items():
        if not isinstance(idx_list, list):
            continue
        for idx in idx_list:
            if isinstance(idx, int):
                positions[idx] = str(word)
    if not positions:
        return ""
    max_pos = max(positions)
    parts = [positions.get(i, "") for i in range(max_pos + 1)]
    return " ".join(p for p in parts if p).strip()


def _normalize_oa_work(raw: dict[str, Any]) -> dict[str, Any]:
    oa_id = _strip_oa_prefix(raw.get("id"))
    refs = [_strip_oa_prefix(r) for r in (raw.get("referenced_works") or [])]
    refs = [r for r in refs if r]
    authors: list[str] = []
    for record in (raw.get("authorships") or [])[:5]:
        author = (record or {}).get("author") or {}
        name = str(author.get("display_name") or "").strip()
        if name:
            authors.append(name)
    cited_by = raw.get("cited_by_count")
    try:
        cited_by_int = int(cited_by) if cited_by is not None else 0
    except (TypeError, ValueError):
        cited_by_int = 0
    year = raw.get("publication_year")
    try:
        year_int = int(year) if year else None
    except (TypeError, ValueError):
        year_int = None
    primary_raw = raw.get("primary_location")
    primary_loc: dict[str, Any] = primary_raw if isinstance(primary_raw, dict) else {}
    source_raw = primary_loc.get("source")
    source: dict[str, Any] = source_raw if isinstance(source_raw, dict) else {}
    journal_name = str(source.get("display_name") or "").strip()
    landing_url = str(primary_loc.get("landing_page_url") or "").strip()
    return {
        "openalex_id": oa_id,
        "title": str(raw.get("display_name") or "").strip(),
        "year": year_int,
        "doi": _canonicalize_doi(str(raw.get("doi") or "")),
        "authors": authors,
        "cited_by_count": cited_by_int,
        "referenced_works": refs,
        "abstract": _decode_abstract(raw.get("abstract_inverted_index")),
        "journal": journal_name,
        "landing_page_url": landing_url,
    }


def _research_entity_kind(entity_type: str) -> tuple[str, str, str]:
    normalized = str(entity_type or "").strip().lower()
    if normalized == "author":
        return "authors", "A", "authorships.author.id"
    if normalized == "institution":
        return "institutions", "I", "authorships.institutions.id"
    raise ValueError("Type d'entité invalide.")


def _normalize_research_entity(raw: dict[str, Any], entity_type: str) -> dict[str, Any]:
    endpoint, _prefix, _work_filter = _research_entity_kind(entity_type)
    entity_id = _strip_oa_prefix(raw.get("id"))
    label = str(raw.get("display_name") or "").strip()
    works_count = int(raw.get("works_count") or 0)
    cited_by_count = int(raw.get("cited_by_count") or 0)

    if endpoint == "authors":
        institution_rows = raw.get("last_known_institutions") or []
        if not isinstance(institution_rows, list):
            institution_rows = []
        legacy_institution = raw.get("last_known_institution")
        if isinstance(legacy_institution, dict) and not institution_rows:
            institution_rows = [legacy_institution]
        institution_names = [
            str(row.get("display_name") or "").strip()
            for row in institution_rows
            if isinstance(row, dict) and str(row.get("display_name") or "").strip()
        ]
        subtitle = ", ".join(institution_names[:2])
        external_id = str(raw.get("orcid") or "").strip()
        country_code = ""
    else:
        institution_type = str(raw.get("type") or "").strip()
        country_code = str(raw.get("country_code") or "").strip().upper()
        subtitle = " · ".join(value for value in (institution_type, country_code) if value)
        external_id = str(raw.get("ror") or "").strip()

    return {
        "id": entity_id,
        "openalex_id": entity_id,
        "entity_type": entity_type,
        "label": label,
        "subtitle": subtitle,
        "external_id": external_id,
        "country_code": country_code,
        "works_count": works_count,
        "cited_by_count": cited_by_count,
        "openalex_url": f"https://openalex.org/{entity_id}" if entity_id else "",
    }


async def search_research_entities(
    entity_type: str,
    query: str,
    *,
    limit: int = 8,
) -> dict[str, Any]:
    """Search OpenAlex identities before a citation corpus is requested."""
    normalized_type = str(entity_type or "").strip().lower()
    endpoint, prefix, _work_filter = _research_entity_kind(normalized_type)
    cleaned_query = str(query or "").strip()
    if len(cleaned_query) < 2:
        raise ValueError("Saisissez au moins deux caractères.")
    if len(cleaned_query) > 200:
        raise ValueError("La recherche est trop longue.")
    result_limit = min(max(1, int(limit)), 12)
    exact_external_id = ""
    external_tail = cleaned_query.rstrip("/").rsplit("/", 1)[-1]
    if normalized_type == "author" and re.fullmatch(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}", external_tail):
        exact_external_id = f"https://orcid.org/{external_tail.upper()}"
    elif normalized_type == "institution" and re.fullmatch(r"0[0-9a-z]{8}", external_tail.lower()):
        exact_external_id = f"https://ror.org/{external_tail.lower()}"
    headers = {"User-Agent": "Bibliobrain/1.0 (mailto:bibliobrain@local)"}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, headers=headers) as client:
        payload: dict[str, Any] = {}
        if exact_external_id:
            try:
                payload = await _get_json_with_retry(
                    client,
                    f"{_OPENALEX_BASE}/{endpoint}/{exact_external_id}",
                    api_name="openalex",
                )
            except Exception:
                _logger.debug("Recovered from an error in search_research_entities.", exc_info=True)
                payload = {}
        if not payload.get("id"):
            payload = await _get_json_with_retry(
                client,
                f"{_OPENALEX_BASE}/{endpoint}",
                params={"search": cleaned_query, "per-page": result_limit},
                api_name="openalex",
            )
    rows = [payload] if payload.get("id") else payload.get("results", [])
    entities = [
        _normalize_research_entity(row, normalized_type)
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and _strip_oa_prefix(row.get("id")).startswith(prefix)
    ]
    return {
        "ok": True,
        "entity_type": normalized_type,
        "query": cleaned_query,
        "entities": entities,
        "count": len(entities),
    }


async def _fetch_research_entity(
    client: httpx.AsyncClient,
    entity_type: str,
    openalex_id: str,
) -> dict[str, Any] | None:
    endpoint, _prefix, _work_filter = _research_entity_kind(entity_type)
    payload = await _get_json_with_retry(
        client,
        f"{_OPENALEX_BASE}/{endpoint}/{openalex_id}",
        api_name="openalex",
    )
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    return _normalize_research_entity(payload, entity_type)


async def _fetch_research_entity_works(
    client: httpx.AsyncClient,
    entity_type: str,
    openalex_id: str,
    *,
    from_year: int | None,
    to_year: int | None,
    max_works: int,
    strategy: str,
) -> tuple[list[dict[str, Any]], int]:
    _endpoint, _prefix, work_filter = _research_entity_kind(entity_type)
    filters = [f"{work_filter}:{openalex_id}"]
    if from_year is not None or to_year is not None:
        filters.append(f"publication_year:{from_year or 1800}-{to_year or 2200}")

    async def fetch_sorted(sort: str) -> tuple[list[dict[str, Any]], int]:
        payload = await _get_json_with_retry(
            client,
            f"{_OPENALEX_BASE}/works",
            params={
                "filter": ",".join(filters),
                "sort": sort,
                "per-page": max_works,
                "select": _SELECT_FULL,
            },
            api_name="openalex",
        )
        rows = payload.get("results") if isinstance(payload, dict) else []
        meta = payload.get("meta") if isinstance(payload, dict) else {}
        total = int(meta.get("count") or 0) if isinstance(meta, dict) else 0
        works = [
            _normalize_oa_work(row)
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict)
        ]
        return works, total

    if strategy == "most_cited":
        return await fetch_sorted("cited_by_count:desc")
    if strategy == "recent":
        return await fetch_sorted("publication_date:desc")

    (cited_works, cited_total), (recent_works, recent_total) = await asyncio.gather(
        fetch_sorted("cited_by_count:desc"),
        fetch_sorted("publication_date:desc"),
    )
    cited_quota = max(1, math.ceil(max_works * 0.6))
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_rows(rows: list[dict[str, Any]], stop_at: int) -> None:
        for work in rows:
            work_id = str(work.get("openalex_id") or "")
            if not work_id or work_id in seen:
                continue
            seen.add(work_id)
            selected.append(work)
            if len(selected) >= stop_at:
                break

    add_rows(cited_works, cited_quota)
    add_rows(recent_works, max_works)
    if len(selected) < max_works:
        add_rows(cited_works, max_works)
    return selected[:max_works], max(cited_total, recent_total)


def _research_scope_cache_get(key: tuple[Any, ...]) -> dict[str, Any] | None:
    cached = _research_scope_cache.get(key)
    if not cached:
        return None
    if time.monotonic() - float(cached.get("created_at") or 0) >= _RESEARCH_SCOPE_CACHE_TTL_SECONDS:
        _research_scope_cache.pop(key, None)
        return None
    payload = copy.deepcopy(cached.get("payload") or {})
    payload.setdefault("stats", {})["cached"] = True
    return payload


def _research_scope_cache_put(key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    if len(_research_scope_cache) >= _RESEARCH_SCOPE_CACHE_MAX_ENTRIES:
        oldest_key = min(
            _research_scope_cache,
            key=lambda cache_key: float(_research_scope_cache[cache_key].get("created_at") or 0),
        )
        _research_scope_cache.pop(oldest_key, None)
    _research_scope_cache[key] = {
        "created_at": time.monotonic(),
        "payload": copy.deepcopy(payload),
    }


async def _fetch_works_by_doi(
    client: httpx.AsyncClient,
    dois: list[str],
) -> list[dict[str, Any]]:
    if not dois:
        return []
    batches = [
        dois[start : start + _BATCH_DOI_LIMIT]
        for start in range(0, len(dois), _BATCH_DOI_LIMIT)
    ]
    semaphore = asyncio.Semaphore(_OPENALEX_BATCH_CONCURRENCY)

    async def _fetch_batch(index: int, batch: list[str]) -> tuple[int, list[dict[str, Any]]]:
        params: dict[str, Any] = {
            "filter": "doi:" + "|".join(batch),
            "per-page": len(batch),
            "select": _SELECT_FULL,
        }
        async with semaphore:
            payload = await _get_json_with_retry(
                client,
                f"{_OPENALEX_BASE}/works",
                params=params,
                api_name="openalex",
            )
        rows = payload.get("results")
        if not isinstance(rows, list):
            return index, []
        return index, [
            _normalize_oa_work(row)
            for row in rows
            if isinstance(row, dict)
        ]

    gathered = await asyncio.gather(*(
        _fetch_batch(index, batch)
        for index, batch in enumerate(batches)
    ))
    out: list[dict[str, Any]] = []
    for _, rows in sorted(gathered, key=lambda item: item[0]):
        out.extend(rows)
    return out


async def _fetch_works_by_oa_ids(
    client: httpx.AsyncClient,
    oa_ids: list[str],
) -> list[dict[str, Any]]:
    if not oa_ids:
        return []
    out: list[dict[str, Any]] = []
    for start in range(0, len(oa_ids), _BATCH_OAID_LIMIT):
        batch = oa_ids[start : start + _BATCH_OAID_LIMIT]
        params: dict[str, Any] = {
            "filter": "openalex_id:" + "|".join(batch),
            "per-page": len(batch),
            "select": _SELECT_LIGHT,
        }
        payload = await _get_json_with_retry(
            client,
            f"{_OPENALEX_BASE}/works",
            params=params,
            api_name="openalex",
        )
        rows = payload.get("results")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                out.append(_normalize_oa_work(row))
    return out


def _crossref_reference_text(raw: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, list):
            value = next((item for item in value if str(item or "").strip()), "")
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if text:
            return text
    return ""


_NAME_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "du", "le", "la", "dos", "das"}


def _unstructured_reference_hints(text: str) -> tuple[str, int | None]:
    """First author's surname and year read from a raw citation string.

    Crossref often gives only ``unstructured`` text for a reference
    ("C. P. Bean, Rev. Mod. Phys. 36, 31 (1964)"); without these hints the
    citation graph shows it as an anonymous, undated node.
    """
    text = re.sub(r"^\s*(?:\[\d+\]|\d+[.)])\s*", "", str(text or "")).strip()
    if not text:
        return "", None
    max_year = time.localtime().tm_year + 1
    year: int | None = None
    candidates = re.findall(r"\((?:19|20)\d{2}[a-z]?\)", text) or re.findall(
        r"(?<!\d)(?:19|20)\d{2}(?!\d)", text
    )
    for candidate in reversed(candidates):
        value = int(re.sub(r"\D", "", candidate)[:4])
        if value <= max_year:
            year = value
            break

    # The first author is what comes before the first separator. Keep it only
    # when it reads like a name: few words, no digits, capitalised words apart
    # from particles and initials ("C. P. Bean", "Bean CP", "Tomita M").
    lead = re.split(r",|;|:|\(| and | et | & ", text, maxsplit=1)[0].strip(" .")
    # "Jiang Z 2004 Supercond…": the name stops at the first number, but then
    # initials are required, so "Nature 421" is not read as an author.
    cut_at_number = bool(re.search(r"\s\d", lead))
    lead = re.split(r"\s(?=\d)", lead, maxsplit=1)[0].strip(" .")
    words = lead.split()
    if not words or len(words) > 5 or len(lead) > 60 or re.search(r"\d", lead):
        return "", year
    surnames: list[str] = []
    initials = 0
    for word in words:
        bare = word.strip(".")
        if bare.lower() in _NAME_PARTICLES:
            continue
        compact = bare.replace(".", "").replace("-", "")
        if re.fullmatch(r"[A-Z]{1,3}", compact):
            initials += 1
            continue
        if not bare[:1].isupper() or not re.fullmatch(r"[^\W\d_][\w'’-]*", bare):
            return "", year
        surnames.append(bare)
    if not surnames or (cut_at_number and not initials):
        return "", year
    return surnames[-1], year


def _normalize_crossref_reference(
    raw: dict[str, Any],
    *,
    center_id: str,
    ordinal: int,
) -> dict[str, Any]:
    doi = _canonicalize_doi(str(raw.get("DOI") or raw.get("doi") or ""))
    title = _crossref_reference_text(
        raw,
        "article-title",
        "volume-title",
        "series-title",
        "standard-designator",
        "unstructured",
    )
    author_value = raw.get("author")
    authors: list[str] = []
    if isinstance(author_value, list):
        for value in author_value:
            if isinstance(value, dict):
                name = " ".join(
                    part
                    for part in (
                        str(value.get("given") or "").strip(),
                        str(value.get("family") or "").strip(),
                    )
                    if part
                )
            else:
                name = str(value or "").strip()
            if name:
                authors.append(name)
    elif isinstance(author_value, dict):
        name = " ".join(
            part
            for part in (
                str(author_value.get("given") or "").strip(),
                str(author_value.get("family") or "").strip(),
            )
            if part
        )
        if name:
            authors.append(name)
    else:
        author = str(author_value or "").strip()
        if author:
            authors.append(author)

    year_match = re.search(r"(?:19|20)\d{2}", str(raw.get("year") or ""))
    year = int(year_match.group(0)) if year_match else None
    if not authors or year is None:
        hint_author, hint_year = _unstructured_reference_hints(str(raw.get("unstructured") or ""))
        if not authors and hint_author:
            authors.append(hint_author)
        if year is None:
            year = hint_year
    fallback_id = f"crossref-doi:{doi}" if doi else f"crossref-ref:{center_id}:{ordinal}"
    return {
        "id": fallback_id,
        "openalex_id": "",
        "title": title or f"Référence {ordinal}",
        "year": year,
        "doi": doi,
        "authors": authors,
        "cited_by_count": 0,
        "referenced_works": [],
        "abstract": "",
        "journal": _crossref_reference_text(raw, "journal-title"),
        "landing_page_url": f"https://doi.org/{doi}" if doi else "",
        "reference_ordinal": ordinal,
    }


async def _fetch_crossref_references(
    client: httpx.AsyncClient,
    doi: str,
    *,
    center_id: str,
) -> list[dict[str, Any]]:
    safe_doi = _canonicalize_doi(doi)
    if not safe_doi:
        return []
    payload = await _get_json_with_retry(
        client,
        f"{_CROSSREF_BASE}/works/{quote(safe_doi, safe='')}",
        api_name="crossref",
    )
    message = payload.get("message") if isinstance(payload, dict) else None
    rows = message.get("reference") if isinstance(message, dict) else None
    if not isinstance(rows, list):
        return []
    return [
        _normalize_crossref_reference(row, center_id=center_id, ordinal=index)
        for index, row in enumerate(rows, start=1)
        if isinstance(row, dict)
    ]


def _reference_match(
    reference: dict[str, Any],
    candidates: list[dict[str, Any]],
    used_ids: set[str],
) -> dict[str, Any] | None:
    doi = str(reference.get("doi") or "")
    normalized_title = _normalize_title(reference.get("title"))
    for candidate in candidates:
        candidate_id = str(candidate.get("openalex_id") or "")
        if not candidate_id or candidate_id in used_ids:
            continue
        if doi and doi == str(candidate.get("doi") or ""):
            return candidate
    if not normalized_title or re.fullmatch(r"reference \d+", normalized_title):
        return None
    for candidate in candidates:
        candidate_id = str(candidate.get("openalex_id") or "")
        if not candidate_id or candidate_id in used_ids:
            continue
        candidate_title = _normalize_title(candidate.get("title"))
        if candidate_title and candidate_title == normalized_title:
            return candidate
    return None


async def _merge_crossref_reference_works(
    client: httpx.AsyncClient,
    *,
    center_id: str,
    openalex_works: list[dict[str, Any]],
    crossref_references: list[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], int]:
    """Merge ordered Crossref bibliography entries with resolvable OpenAlex works."""
    if not crossref_references:
        return openalex_works[:limit], 0

    candidates = list(openalex_works)
    known_dois = {str(work.get("doi") or "") for work in candidates}
    unresolved_dois = list(
        dict.fromkeys(
            str(reference.get("doi") or "")
            for reference in crossref_references
            if reference.get("doi") and reference.get("doi") not in known_dois
        )
    )
    if unresolved_dois:
        try:
            resolved_by_doi = await _fetch_works_by_doi(client, unresolved_dois)
        except (RuntimeError, TimeoutError, httpx.HTTPError):
            resolved_by_doi = []
        known_ids = {str(work.get("openalex_id") or "") for work in candidates}
        candidates.extend(
            work
            for work in resolved_by_doi
            if work.get("openalex_id") and work.get("openalex_id") not in known_ids
        )

    merged: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for reference in crossref_references:
        match = _reference_match(reference, candidates, used_ids)
        if match:
            work = dict(match)
            for key in ("title", "year", "doi", "authors", "journal", "landing_page_url"):
                if not work.get(key) and reference.get(key):
                    work[key] = reference[key]
            used_ids.add(str(work.get("openalex_id") or ""))
        else:
            work = dict(reference)
        merged.append(work)

    for work in candidates:
        work_id = str(work.get("openalex_id") or "")
        if work_id and work_id != center_id and work_id not in used_ids:
            merged.append(work)
            used_ids.add(work_id)
    limited = merged[:limit]
    fallback_count = sum(1 for work in limited if not work.get("openalex_id"))
    return limited, fallback_count


async def _fetch_full_work_by_oa_id(
    client: httpx.AsyncClient,
    oa_id: str,
) -> dict[str, Any] | None:
    """Fetch one OpenAlex work including its outbound references."""
    clean_id = _strip_oa_prefix(oa_id)
    if not clean_id:
        return None
    payload = await _get_json_with_retry(
        client,
        f"{_OPENALEX_BASE}/works/{clean_id}",
        params={"select": _SELECT_FULL},
        api_name="openalex",
    )
    if not isinstance(payload, dict) or not payload.get("id"):
        return None
    return _normalize_oa_work(payload)


async def _fetch_top_citers(
    client: httpx.AsyncClient,
    oa_id: str,
    limit: int = _TOP_CITERS_PER_PAPER,
) -> list[dict[str, Any]]:
    """Top papers citing ``oa_id`` (sorted by citation count desc)."""
    params: dict[str, Any] = {
        "filter": f"cites:{oa_id}",
        "per-page": min(max(1, limit), 50),
        "sort": "cited_by_count:desc",
        "select": _SELECT_LIGHT,
    }
    try:
        payload = await _get_json_with_retry(
            client,
            f"{_OPENALEX_BASE}/works",
            params=params,
            api_name="openalex",
        )
    except Exception:
        _logger.debug("Recovered from an error in _fetch_top_citers.", exc_info=True)
        return []
    rows = payload.get("results")
    if not isinstance(rows, list):
        return []
    return [_normalize_oa_work(r) for r in rows if isinstance(r, dict)]


def _resolve_notebook_sources(
    nb: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map notebook sources to records with a DOI.

    Returns (resolved, skipped) where ``skipped`` is a list of
    ``{title, reason}`` so the UI can explain to the user exactly why a
    source isn't in the graph.
    """
    resolved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for source in nb.get("sources") or []:
        title = str(source.get("title") or "(sans titre)").strip() or "(sans titre)"
        rel_path = str(source.get("source_path") or "").strip()
        if not rel_path:
            skipped.append({"title": title, "reason": "Source texte (pas de PDF lié)"})
            continue
        item = _lookup_zotero(rel_path)
        if item is None:
            skipped.append({"title": title, "reason": "PDF introuvable dans Zotero"})
            continue
        doi = _canonicalize_doi(str(getattr(item, "doi", "") or ""))
        if not doi:
            skipped.append({"title": title, "reason": "Pas de DOI dans Zotero"})
            continue
        resolved.append(
            {
                "source_id": source.get("id"),
                "source_title": source.get("title") or "",
                "source_path": rel_path,
                "doi": doi,
            }
        )
    return resolved, skipped


def _empty_payload(
    skipped: list[dict[str, Any]],
    started: float,
    msg: str = "",
) -> dict[str, Any]:
    return {
        "ok": True,
        "nodes": [],
        "edges": [],
        "similarity_edges": [],
        "prior_works": [],
        "derivative_works": [],
        "missing_links": [],
        "skipped_sources": skipped,
        "stats": {
            "papers_with_doi": 0,
            "papers_without_doi": len(skipped),
            "edges_count": 0,
            "similarity_edges_count": 0,
            "prior_works_count": 0,
            "derivative_works_count": 0,
            "missing_links_count": 0,
            "build_ms": int((time.perf_counter() - started) * 1000),
        },
        "msg": msg,
    }


async def build_notebook_citation_graph(nb_id: str) -> dict[str, Any]:
    started = time.perf_counter()
    nb = get_notebook(nb_id)
    if nb is None:
        return {
            "ok": False,
            "msg": "Carnet introuvable.",
            "nodes": [],
            "edges": [],
            "missing_links": [],
            "stats": {},
        }

    resolved, skipped = _resolve_notebook_sources(nb)
    total_sources = len(nb.get("sources") or [])
    return await _build_graph_from_resolved(
        resolved,
        skipped,
        started,
        total_sources=total_sources,
        empty_msg="Aucune source du carnet n'a de DOI utilisable.",
    )


async def build_paper_citation_graph(rel_path: str) -> dict[str, Any]:
    """Citation graph for a single library paper, treated as a 1-source graph.

    Reuses the notebook builder so prior/derivative/similarity logic stays
    identical (with a single-node graph the similarity pass is trivially empty).
    """
    started = time.perf_counter()
    rel = str(rel_path or "").strip()
    if not rel:
        return {
            "ok": False,
            "msg": "Chemin de fichier manquant.",
            "nodes": [],
            "edges": [],
            "missing_links": [],
            "stats": {},
        }
    item = _lookup_zotero(rel)
    title, year, author = _paper_lookup_hints(rel, item)
    doi = _canonicalize_doi(str(getattr(item, "doi", "") or ""))
    preloaded_work: dict[str, Any] | None = None
    if not doi:
        headers = {"User-Agent": "Bibliobrain/1.0 (mailto:bibliobrain@local)"}
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, headers=headers) as client:
            preloaded_work = await _fetch_work_by_title(
                client,
                title,
                year=year,
                author=author,
            )
        if not preloaded_work:
            reason = (
                "Métadonnées Zotero absentes et titre introuvable sur OpenAlex"
                if item is None
                else "Pas de DOI dans Zotero et titre introuvable sur OpenAlex"
            )
            return _empty_payload(
                [{"title": title or rel, "reason": reason}],
                started,
                msg="Cet article n'a pas pu être identifié sur OpenAlex.",
            )
    resolved = [
        {
            "source_id": getattr(item, "item_key", "") or rel,
            "source_title": title,
            "source_path": rel,
            "doi": doi,
            "openalex_work": preloaded_work,
        }
    ]
    return await _build_graph_from_resolved(
        resolved,
        [],
        started,
        total_sources=1,
        empty_msg="Cet article n'a pas pu être identifié sur OpenAlex.",
        enrich_crossref_references=True,
    )


async def build_research_scope_citation_graph(
    entity_type: str,
    openalex_id: str,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    max_works: int = 80,
    strategy: str = "balanced",
) -> dict[str, Any]:
    """Build a citation graph from one resolved OpenAlex author or institution."""
    started = time.perf_counter()
    normalized_type = str(entity_type or "").strip().lower()
    _endpoint, expected_prefix, _work_filter = _research_entity_kind(normalized_type)
    entity_id = _strip_oa_prefix(openalex_id)
    if not re.fullmatch(r"[AI]\d+", entity_id) or not entity_id.startswith(expected_prefix):
        raise ValueError("L'identifiant OpenAlex ne correspond pas au type d'entité.")
    if from_year is not None and to_year is not None and from_year > to_year:
        raise ValueError("L'année de début doit précéder l'année de fin.")
    result_limit = min(max(10, int(max_works)), 150)
    normalized_strategy = str(strategy or "balanced").strip().lower()
    if normalized_strategy not in {"balanced", "most_cited", "recent"}:
        raise ValueError("Stratégie de sélection invalide.")

    cache_key = (
        normalized_type,
        entity_id,
        from_year,
        to_year,
        result_limit,
        normalized_strategy,
    )
    cached = _research_scope_cache_get(cache_key)
    if cached is not None:
        cached.setdefault("stats", {})["build_ms"] = int((time.perf_counter() - started) * 1000)
        return cached

    headers = {"User-Agent": "Bibliobrain/1.0 (mailto:bibliobrain@local)"}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, headers=headers) as client:
        entity = await _fetch_research_entity(client, normalized_type, entity_id)
        if entity is None:
            return {
                "ok": False,
                "msg": "Auteur ou institution introuvable sur OpenAlex.",
                "nodes": [],
                "edges": [],
                "stats": {},
            }
        works, total_works = await _fetch_research_entity_works(
            client,
            normalized_type,
            entity_id,
            from_year=from_year,
            to_year=to_year,
            max_works=result_limit,
            strategy=normalized_strategy,
        )

    resolved = [
        {
            "source_id": work.get("openalex_id") or "",
            "source_title": work.get("title") or "",
            "source_path": "",
            "doi": work.get("doi") or "",
            "openalex_work": work,
        }
        for work in works
        if work.get("openalex_id")
    ]
    payload = await _build_graph_from_resolved(
        resolved,
        [],
        started,
        total_sources=len(resolved),
        empty_msg="Aucune publication trouvée pour cette entité et cette période.",
    )
    payload.update(
        {
            "engine": "openalex",
            "graph_type": "research_entity_citations",
            "scope": {
                **entity,
                "from_year": from_year,
                "to_year": to_year,
                "strategy": normalized_strategy,
                "total_works": total_works,
                "selected_works": len(resolved),
            },
            "data_handling": {
                "source": "OpenAlex",
                "personal_data_categories": [
                    "name",
                    "orcid_or_ror",
                    "affiliations",
                    "publications",
                    "citation_links",
                ],
                "processing": "on_device",
                "storage": "volatile_memory_cache",
                "cache_ttl_seconds": _RESEARCH_SCOPE_CACHE_TTL_SECONDS,
                "automated_decision_making": False,
            },
        }
    )
    stats = payload.setdefault("stats", {})
    stats["papers_in_scope"] = len(payload.get("nodes") or [])
    stats["papers_available"] = total_works
    warnings = payload.setdefault("warnings", [])
    if total_works > len(resolved):
        warnings.insert(
            0,
            f"Corpus limité à {len(resolved)} publications sur {total_works} disponibles.",
        )
    _research_scope_cache_put(cache_key, payload)
    return payload


def _bibliographic_identifier(query: str) -> tuple[str, str]:
    """Return a supported explicit identifier kind and its canonical value."""
    cleaned = str(query or "").strip()
    tail = cleaned.rstrip("/").rsplit("/", 1)[-1]
    openalex_id = _strip_oa_prefix(cleaned).upper()
    if re.fullmatch(r"W\d+", openalex_id):
        return "work", openalex_id
    if re.fullmatch(r"A\d+", openalex_id):
        return "author", openalex_id
    if re.fullmatch(r"I\d+", openalex_id):
        return "institution", openalex_id
    if re.fullmatch(r"\d{4}-\d{4}-\d{4}-[\dXx]{4}", tail):
        return "orcid", f"https://orcid.org/{tail.upper()}"
    if re.fullmatch(r"0[0-9a-z]{8}", tail.lower()):
        return "ror", f"https://ror.org/{tail.lower()}"
    doi = _canonicalize_doi(cleaned)
    if re.fullmatch(r"10\.\d{4,9}/\S+", doi, re.IGNORECASE):
        return "doi", doi
    return "text", cleaned


def _bibliographic_entity_match_score(query: str, label: str) -> float:
    normalized_query = _normalize_title(query)
    normalized_label = _normalize_title(label)
    if not normalized_query or not normalized_label:
        return 0.0
    if normalized_query == normalized_label:
        return 1.0
    if len(normalized_query) >= 5 and normalized_query in normalized_label:
        return 0.92
    if len(normalized_label) >= 5 and normalized_label in normalized_query:
        return 0.88
    return SequenceMatcher(None, normalized_query, normalized_label).ratio()


async def _search_bibliographic_entities(
    client: httpx.AsyncClient,
    entity_type: str,
    query: str,
) -> list[dict[str, Any]]:
    endpoint, prefix, _work_filter = _research_entity_kind(entity_type)
    try:
        payload = await _get_json_with_retry(
            client,
            f"{_OPENALEX_BASE}/{endpoint}",
            params={"search": query, "per-page": 3},
            api_name="openalex",
        )
    except Exception:
        # Identity hints only improve recall for name-like queries. A transient
        # failure here must not block the publication search itself.
        _logger.debug("Recovered from an error in _search_bibliographic_entities.", exc_info=True)
        return []
    rows = payload.get("results") if isinstance(payload, dict) else []
    entities = [
        _normalize_research_entity(row, entity_type)
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and _strip_oa_prefix(row.get("id")).startswith(prefix)
    ]
    return [
        entity
        for entity in entities
        if _bibliographic_entity_match_score(query, entity.get("label") or "") >= 0.82
    ][:1]


async def _fetch_bibliographic_text_works(
    client: httpx.AsyncClient,
    query: str,
    *,
    from_year: int | None,
    to_year: int | None,
    max_works: int,
    strategy: str,
) -> tuple[list[dict[str, Any]], int]:
    filters: list[str] = []
    if from_year is not None or to_year is not None:
        filters.append(f"publication_year:{from_year or 1800}-{to_year or 2200}")

    async def fetch_sorted(sort: str) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {
            "search": query,
            "sort": sort,
            "per-page": max_works,
            "select": _SELECT_FULL,
        }
        if filters:
            params["filter"] = ",".join(filters)
        payload = await _get_json_with_retry(
            client,
            f"{_OPENALEX_BASE}/works",
            params=params,
            api_name="openalex",
        )
        rows = payload.get("results") if isinstance(payload, dict) else []
        meta = payload.get("meta") if isinstance(payload, dict) else {}
        total = int(meta.get("count") or 0) if isinstance(meta, dict) else 0
        return (
            [
                _normalize_oa_work(row)
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict)
            ],
            total,
        )

    if strategy == "most_cited":
        return await fetch_sorted("cited_by_count:desc")
    if strategy == "recent":
        return await fetch_sorted("publication_date:desc")
    (cited, cited_total), (recent, recent_total) = await asyncio.gather(
        fetch_sorted("cited_by_count:desc"),
        fetch_sorted("publication_date:desc"),
    )
    return _select_bibliographic_works(cited + recent, max_works, "balanced"), max(
        cited_total, recent_total
    )


def _select_bibliographic_works(
    works: list[dict[str, Any]],
    max_works: int,
    strategy: str,
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for work in works:
        work_id = str(work.get("openalex_id") or "")
        if work_id and work_id not in unique:
            unique[work_id] = work
    rows = list(unique.values())
    if strategy == "most_cited":
        return sorted(
            rows,
            key=lambda work: int(work.get("cited_by_count") or 0),
            reverse=True,
        )[:max_works]
    if strategy == "recent":
        return sorted(
            rows,
            key=lambda work: int(work.get("year") or 0),
            reverse=True,
        )[:max_works]

    cited = sorted(
        rows,
        key=lambda work: int(work.get("cited_by_count") or 0),
        reverse=True,
    )
    recent = sorted(rows, key=lambda work: int(work.get("year") or 0), reverse=True)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(source: list[dict[str, Any]], stop_at: int) -> None:
        for work in source:
            work_id = str(work.get("openalex_id") or "")
            if not work_id or work_id in seen:
                continue
            seen.add(work_id)
            selected.append(work)
            if len(selected) >= stop_at:
                break

    add(cited, max(1, math.ceil(max_works * 0.6)))
    add(recent, max_works)
    add(cited, max_works)
    return selected[:max_works]


async def build_bibliographic_search_citation_graph(
    query: str,
    *,
    from_year: int | None = None,
    to_year: int | None = None,
    max_works: int = 80,
    strategy: str = "balanced",
) -> dict[str, Any]:
    """Build a publication-centric graph from a general bibliographic query."""
    started = time.perf_counter()
    cleaned_query = str(query or "").strip()
    if len(cleaned_query) < 2:
        raise ValueError("Saisissez au moins deux caractères.")
    if len(cleaned_query) > 300:
        raise ValueError("La recherche est trop longue.")
    if from_year is not None and to_year is not None and from_year > to_year:
        raise ValueError("L'année de début doit précéder l'année de fin.")
    result_limit = min(max(10, int(max_works)), 150)
    normalized_strategy = str(strategy or "balanced").strip().lower()
    if normalized_strategy not in {"balanced", "most_cited", "recent"}:
        raise ValueError("Stratégie de sélection invalide.")

    identifier_kind, identifier_value = _bibliographic_identifier(cleaned_query)
    cache_key = (
        "bibliographic_search",
        cleaned_query.casefold(),
        from_year,
        to_year,
        result_limit,
        normalized_strategy,
    )
    cached = _research_scope_cache_get(cache_key)
    if cached is not None:
        cached.setdefault("stats", {})["build_ms"] = int(
            (time.perf_counter() - started) * 1000
        )
        return cached

    works: list[dict[str, Any]] = []
    total_works = 0
    matches: list[dict[str, str]] = []
    headers = {"User-Agent": "Bibliobrain/1.0 (mailto:bibliobrain@local)"}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, headers=headers) as client:
        if identifier_kind == "doi":
            works = await _fetch_works_by_doi(client, [identifier_value])
            total_works = len(works)
        elif identifier_kind == "work":
            works = await _fetch_works_by_oa_ids(client, [identifier_value])
            total_works = len(works)
        elif identifier_kind in {"author", "institution", "orcid", "ror"}:
            entity_type = (
                "author"
                if identifier_kind in {"author", "orcid"}
                else "institution"
            )
            endpoint, _prefix, _work_filter = _research_entity_kind(entity_type)
            entity_payload = await _get_json_with_retry(
                client,
                f"{_OPENALEX_BASE}/{endpoint}/{identifier_value}",
                api_name="openalex",
            )
            entity = _normalize_research_entity(entity_payload, entity_type)
            entity_id = str(entity.get("id") or "")
            if entity_id:
                matches.append(
                    {
                        "kind": entity_type,
                        "label": str(entity.get("label") or entity_id),
                    }
                )
                works, total_works = await _fetch_research_entity_works(
                    client,
                    entity_type,
                    entity_id,
                    from_year=from_year,
                    to_year=to_year,
                    max_works=result_limit,
                    strategy=normalized_strategy,
                )
        else:
            (text_result, author_matches, institution_matches) = await asyncio.gather(
                _fetch_bibliographic_text_works(
                    client,
                    cleaned_query,
                    from_year=from_year,
                    to_year=to_year,
                    max_works=result_limit,
                    strategy=normalized_strategy,
                ),
                _search_bibliographic_entities(client, "author", cleaned_query),
                _search_bibliographic_entities(client, "institution", cleaned_query),
            )
            text_works, text_total = text_result
            works.extend(text_works)
            total_works = text_total
            entity_matches = author_matches + institution_matches
            if entity_matches:
                entity_results = await asyncio.gather(
                    *[
                        _fetch_research_entity_works(
                            client,
                            str(entity.get("entity_type") or "author"),
                            str(entity.get("id") or ""),
                            from_year=from_year,
                            to_year=to_year,
                            max_works=result_limit,
                            strategy=normalized_strategy,
                        )
                        for entity in entity_matches
                    ]
                )
                for entity, (entity_works, entity_total) in zip(
                    entity_matches, entity_results
                ):
                    matches.append(
                        {
                            "kind": str(entity.get("entity_type") or ""),
                            "label": str(entity.get("label") or ""),
                        }
                    )
                    works.extend(entity_works)
                    total_works = max(total_works, entity_total)

    if from_year is not None or to_year is not None:
        minimum_year = from_year or 1800
        maximum_year = to_year or 2200
        works = [
            work
            for work in works
            if minimum_year <= int(work.get("year") or 0) <= maximum_year
        ]
    selected_works = _select_bibliographic_works(works, result_limit, normalized_strategy)
    resolved = [
        {
            "source_id": work.get("openalex_id") or "",
            "source_title": work.get("title") or "",
            "source_path": "",
            "doi": work.get("doi") or "",
            "openalex_work": work,
        }
        for work in selected_works
        if work.get("openalex_id")
    ]
    payload = await _build_graph_from_resolved(
        resolved,
        [],
        started,
        total_sources=len(resolved),
        empty_msg="Aucune publication trouvée pour cette recherche et cette période.",
    )
    payload.update(
        {
            "engine": "openalex",
            "graph_type": "bibliographic_search_citations",
            "scope": {
                "kind": "bibliographic_search",
                "query": cleaned_query,
                "resolved_as": identifier_kind,
                "matches": matches,
                "from_year": from_year,
                "to_year": to_year,
                "strategy": normalized_strategy,
                "total_works": total_works,
                "selected_works": len(resolved),
            },
            "data_handling": {
                "source": "OpenAlex",
                "processing": "on_device",
                "storage": "volatile_memory_cache",
                "cache_ttl_seconds": _RESEARCH_SCOPE_CACHE_TTL_SECONDS,
            },
        }
    )
    stats = payload.setdefault("stats", {})
    stats["papers_in_scope"] = len(payload.get("nodes") or [])
    stats["papers_available"] = total_works
    warnings = payload.setdefault("warnings", [])
    if total_works > len(resolved):
        warnings.insert(
            0,
            f"Corpus limité à {len(resolved)} publications sur {total_works} disponibles.",
        )
    _research_scope_cache_put(cache_key, payload)
    return payload


def clear_bibliographic_search_cache() -> int:
    cleared_entries = len(_research_scope_cache)
    _research_scope_cache.clear()
    return cleared_entries


def _ensure_zotero_metadata_loaded() -> None:
    """Lazily restore Zotero metadata after an import-based server startup."""
    global _zotero_autoload_key

    if library_module._zotero_library:
        return
    configured_path = str(config.ZOTERO_DB_PATH or "").strip()
    if not configured_path or _zotero_autoload_key == configured_path:
        return
    _zotero_autoload_key = configured_path
    library_module.init_zotero(configured_path)


def _resolve_indexed_library_sources() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect one DOI-backed record per indexed library article."""
    _ensure_zotero_metadata_loaded()
    rows, rel_paths = list_library(indexed_only=True)
    resolved: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, rel_path in enumerate(rel_paths):
        item = _lookup_zotero(rel_path)
        fallback_title = str(rows[index][0] if index < len(rows) and rows[index] else rel_path)
        title = str(getattr(item, "title", "") or fallback_title).strip() or rel_path
        if item is None:
            skipped.append({"title": title, "path": rel_path, "reason": "Métadonnées Zotero absentes"})
            continue

        doi = _canonicalize_doi(str(getattr(item, "doi", "") or ""))
        identity = str(getattr(item, "item_key", "") or doi or rel_path).strip().lower()
        if identity in seen:
            continue
        seen.add(identity)
        if not doi:
            skipped.append({"title": title, "path": rel_path, "reason": "Pas de DOI dans Zotero"})
            continue

        resolved.append(
            {
                "source_id": getattr(item, "item_key", "") or rel_path,
                "source_title": title,
                "source_path": rel_path,
                "doi": doi,
                "author": str(item.first_author() or "").strip(),
                "year": str(getattr(item, "year", "") or "").strip(),
            }
        )
    return resolved, skipped


def _connected_component_metadata(
    node_ids: set[str],
    edge_pairs: set[tuple[str, str]],
) -> tuple[dict[str, int], dict[int, int]]:
    """Return stable undirected component ids and their sizes.

    Components with at least two papers are numbered by decreasing size.
    Isolated papers share the special component ``-1`` so that the UI can
    render them as one neutral collection instead of assigning a rainbow of
    meaningless one-paper groups.
    """
    adjacency: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for source_id, target_id in edge_pairs:
        if source_id in adjacency and target_id in adjacency:
            adjacency[source_id].add(target_id)
            adjacency[target_id].add(source_id)

    visited: set[str] = set()
    connected: list[list[str]] = []
    isolated: list[str] = []
    for node_id in sorted(node_ids):
        if node_id in visited:
            continue
        stack = [node_id]
        component: list[str] = []
        visited.add(node_id)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in adjacency[current]:
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        if len(component) == 1:
            isolated.extend(component)
        else:
            connected.append(sorted(component))

    connected.sort(key=lambda component: (-len(component), component[0]))
    component_by_node: dict[str, int] = {}
    component_sizes: dict[int, int] = {}
    for component_id, component in enumerate(connected):
        component_sizes[component_id] = len(component)
        for node_id in component:
            component_by_node[node_id] = component_id
    if isolated:
        component_sizes[-1] = len(isolated)
        for node_id in isolated:
            component_by_node[node_id] = -1
    return component_by_node, component_sizes


_COMMUNITY_STOPWORDS = {
    "about", "after", "among", "analysis", "article", "based", "between",
    "dans", "des", "development", "different", "during", "effects", "etude",
    "field", "from", "high", "into", "model", "modeling", "modelling",
    "paper", "properties", "review", "study", "theory", "their", "through",
    "toward", "towards", "using", "with", "without", "pour", "avec", "une",
    "dans", "sur", "les", "and", "for", "the", "this", "that", "these",
}


def _community_title_tokens(title: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", str(title or "")).encode(
        "ascii", "ignore"
    ).decode("ascii").lower()
    return {
        token
        for token in re.findall(r"[a-z][a-z0-9-]{3,}", normalized)
        if token not in _COMMUNITY_STOPWORDS and not token.isdigit()
    }


def _community_token_key(token: str) -> str:
    """Collapse obvious singular/plural variants in short community labels."""
    if token.endswith("ies") and len(token) > 5:
        return f"{token[:-3]}y"
    if token.endswith("es") and len(token) > 5:
        return token[:-2]
    if token.endswith("s") and len(token) > 4 and not token.endswith("ss"):
        return token[:-1]
    return token


def _bounded_graph_partitions(
    graph: Any,
    max_articles: int = _COMMUNITY_MAX_ARTICLES,
) -> list[set[str]]:
    """Detect deterministic Louvain communities and bound oversized results."""
    import networkx as nx

    partitions: list[set[str]] = []
    for component_nodes in nx.connected_components(graph):
        component = graph.subgraph(component_nodes).copy()
        if len(component) <= 2:
            partitions.append(set(component.nodes))
            continue
        resolution = _COMMUNITY_RESOLUTION
        detected: list[set[str]] = [set(component.nodes)]
        for _ in range(4):
            detected = [
                set(group)
                for group in nx.community.louvain_communities(
                    component,
                    weight="weight",
                    resolution=resolution,
                    seed=42,
                )
            ]
            if max((len(group) for group in detected), default=0) <= max_articles:
                break
            resolution *= 1.45
        for group in detected:
            if len(group) <= max_articles:
                partitions.append(group)
                continue
            ordered = sorted(
                group,
                key=lambda node_id: (-int(graph.degree(node_id)), str(node_id)),
            )
            partitions.extend(
                set(ordered[start : start + max_articles])
                for start in range(0, len(ordered), max_articles)
            )
    return partitions


def _build_citation_communities(
    node_ids: set[str],
    edge_pairs: set[tuple[str, str]],
    work_by_id: dict[str, dict[str, Any]],
    degrees: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    """Return article communities and their aggregated citation links."""
    connected_ids = {node_id for edge in edge_pairs for node_id in edge}
    if not connected_ids:
        return [], {}, []

    try:
        import networkx as nx

        graph = nx.Graph()
        graph.add_nodes_from(connected_ids)
        for source_id, target_id in edge_pairs:
            if source_id in connected_ids and target_id in connected_ids:
                if graph.has_edge(source_id, target_id):
                    graph[source_id][target_id]["weight"] += 1.0
                else:
                    graph.add_edge(source_id, target_id, weight=1.0)
        partitions = _bounded_graph_partitions(graph)
    except (ImportError, AttributeError):
        adjacency: dict[str, set[str]] = {node_id: set() for node_id in connected_ids}
        for source_id, target_id in edge_pairs:
            adjacency[source_id].add(target_id)
            adjacency[target_id].add(source_id)
        partitions = []
        remaining = set(connected_ids)
        while remaining:
            seed = max(remaining, key=lambda node_id: (degrees.get(node_id, 0), node_id))
            queue = [seed]
            group: set[str] = set()
            remaining.remove(seed)
            while queue and len(group) < _COMMUNITY_MAX_ARTICLES:
                current = queue.pop(0)
                group.add(current)
                for neighbor in sorted(adjacency[current], key=lambda node_id: (-degrees.get(node_id, 0), node_id)):
                    if neighbor in remaining and len(group) + len(queue) < _COMMUNITY_MAX_ARTICLES:
                        remaining.remove(neighbor)
                        queue.append(neighbor)
            partitions.append(group)

    partitions = [group for group in partitions if group]
    partitions.sort(
        key=lambda group: (
            -len(group),
            -max((degrees.get(node_id, 0) for node_id in group), default=0),
            min(group),
        )
    )
    community_by_node: dict[str, int] = {}
    for community_id, group in enumerate(partitions):
        for node_id in group:
            community_by_node[node_id] = community_id

    title_tokens = {
        node_id: _community_title_tokens(work_by_id.get(node_id, {}).get("title") or "")
        for node_id in connected_ids
    }
    global_df = Counter(token for tokens in title_tokens.values() for token in tokens)
    total_documents = max(1, len(connected_ids))
    internal_edges: Counter[int] = Counter()
    aggregate_edges: Counter[tuple[int, int]] = Counter()
    for source_id, target_id in edge_pairs:
        source_community = community_by_node.get(source_id)
        target_community = community_by_node.get(target_id)
        if source_community is None or target_community is None:
            continue
        if source_community == target_community:
            internal_edges[source_community] += 1
        else:
            aggregate_edges[(source_community, target_community)] += 1

    communities: list[dict[str, Any]] = []
    for community_id, group in enumerate(partitions):
        token_counts = Counter(token for node_id in group for token in title_tokens[node_id])
        ranked_tokens = sorted(
            token_counts,
            key=lambda token: (
                -(token_counts[token] * math.log((total_documents + 1) / (global_df[token] + 1))),
                -token_counts[token],
                token,
            ),
        )
        repeated = [token for token in ranked_tokens if token_counts[token] >= 2]
        label_tokens: list[str] = []
        label_token_keys: set[str] = set()
        for token in repeated or ranked_tokens:
            token_key = _community_token_key(token)
            if token_key in label_token_keys:
                continue
            label_tokens.append(token)
            label_token_keys.add(token_key)
            if len(label_tokens) == 3:
                break
        representative_ids = sorted(
            group,
            key=lambda node_id: (-degrees.get(node_id, 0), str(work_by_id.get(node_id, {}).get("title") or "")),
        )[:3]
        representative_title = str(
            work_by_id.get(representative_ids[0], {}).get("title") or "Communauté scientifique"
        ) if representative_ids else "Communauté scientifique"
        years = [
            int(work_by_id[node_id]["year"])
            for node_id in group
            if str(work_by_id.get(node_id, {}).get("year") or "").isdigit()
        ]
        communities.append(
            {
                "id": community_id,
                "label": " · ".join(token.title() for token in label_tokens) or representative_title[:54],
                "size": len(group),
                "node_ids": sorted(group),
                "internal_edges": internal_edges[community_id],
                "representative_ids": representative_ids,
                "representative_title": representative_title,
                "year_min": min(years) if years else None,
                "year_max": max(years) if years else None,
            }
        )

    community_edges = [
        {
            "source": source_id,
            "target": target_id,
            "count": count,
        }
        for (source_id, target_id), count in sorted(aggregate_edges.items())
    ]
    return communities, community_by_node, community_edges


async def build_library_internal_citation_graph(
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Build the directed citation subgraph induced by the indexed library.

    OpenAlex is used only to resolve the library papers and their outbound
    references. No cited reference or citing work outside the local library is
    added to the payload.
    """
    started = time.perf_counter()
    resolved, skipped = _resolve_indexed_library_sources()
    total_library_articles = len(resolved) + len(skipped)
    if not resolved:
        payload = _empty_payload(
            skipped,
            started,
            msg="Aucun article indexé avec un DOI exploitable.",
        )
        payload.update(
            {
                "engine": "openalex_internal",
                "graph_type": "library_citations",
                "warning": payload["msg"],
            }
        )
        return payload

    cache_key = "|".join(sorted(
        f"{record['doi']}::{record['source_path']}"
        for record in resolved
    ))
    cached_at = float(_internal_graph_cache.get("created_at") or 0)
    if (
        not force_refresh
        and _internal_graph_cache.get("key") == cache_key
        and time.monotonic() - cached_at < _INTERNAL_GRAPH_CACHE_TTL_SECONDS
    ):
        cached_payload = copy.deepcopy(_internal_graph_cache["payload"])
        cached_payload.setdefault("stats", {})["cached"] = True
        cached_payload["stats"]["build_ms"] = int((time.perf_counter() - started) * 1000)
        return cached_payload

    headers = {"User-Agent": "Bibliobrain/1.0 (mailto:bibliobrain@local)"}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, headers=headers) as client:
        works = await _fetch_works_by_doi(client, [record["doi"] for record in resolved])

    work_by_doi = {work["doi"]: work for work in works if work.get("doi")}
    record_by_oa_id: dict[str, dict[str, Any]] = {}
    work_by_oa_id: dict[str, dict[str, Any]] = {}
    for record in resolved:
        work = work_by_doi.get(record["doi"])
        if not work or not work.get("openalex_id"):
            skipped.append(
                {
                    "title": record["source_title"],
                    "path": record["source_path"],
                    "reason": f"DOI {record['doi']} introuvable sur OpenAlex",
                }
            )
            continue
        oa_id = str(work["openalex_id"])
        record_by_oa_id[oa_id] = record
        work_by_oa_id[oa_id] = work

    library_oa_ids = set(record_by_oa_id)
    edge_pairs: set[tuple[str, str]] = set()
    for source_id, work in work_by_oa_id.items():
        for target_id in work.get("referenced_works") or []:
            if target_id in library_oa_ids and target_id != source_id:
                edge_pairs.add((source_id, target_id))

    in_degree = {oa_id: 0 for oa_id in library_oa_ids}
    out_degree = {oa_id: 0 for oa_id in library_oa_ids}
    edges: list[dict[str, Any]] = []
    for source_id, target_id in sorted(edge_pairs):
        out_degree[source_id] += 1
        in_degree[target_id] += 1
        edges.append(
            {
                "source": source_id,
                "target": target_id,
                "kind": "citation",
                "score": 1.0,
            }
        )

    component_by_node, component_sizes = _connected_component_metadata(
        library_oa_ids,
        edge_pairs,
    )
    degree_by_id = {
        oa_id: in_degree[oa_id] + out_degree[oa_id]
        for oa_id in library_oa_ids
    }
    communities, community_by_node, community_edges = _build_citation_communities(
        library_oa_ids,
        edge_pairs,
        work_by_oa_id,
        degree_by_id,
    )
    community_size_by_id = {
        int(community["id"]): int(community["size"])
        for community in communities
    }

    nodes: list[dict[str, Any]] = []
    for oa_id, record in record_by_oa_id.items():
        work = work_by_oa_id[oa_id]
        degree = in_degree[oa_id] + out_degree[oa_id]
        authors = work.get("authors") or []
        nodes.append(
            {
                "id": oa_id,
                "openalex_id": oa_id,
                "path": record["source_path"],
                "title": work.get("title") or record["source_title"],
                "author": (authors[0] if authors else record.get("author")) or "Auteur inconnu",
                "authors": authors,
                "year": work.get("year") or record.get("year") or "n.d.",
                "doi": work.get("doi") or record["doi"],
                "journal": work.get("journal") or "",
                "cited_by_count": int(work.get("cited_by_count") or 0),
                "degree": degree,
                "in_degree": in_degree[oa_id],
                "out_degree": out_degree[oa_id],
                "size": round(6.0 + min(11.0, degree * 1.35), 2),
                "component_id": component_by_node.get(oa_id, -1),
                "component_size": component_sizes.get(component_by_node.get(oa_id, -1), 1),
                "community_id": community_by_node.get(oa_id, -1),
                "community_size": community_size_by_id.get(community_by_node.get(oa_id, -1), 1),
                "group": (
                    "isolated"
                    if component_by_node.get(oa_id, -1) == -1
                    else f"component-{component_by_node[oa_id]}"
                ),
            }
        )
    nodes.sort(key=lambda node: (-node["degree"], str(node["title"]).lower()))

    warning = ""
    if nodes and not edges:
        warning = "Aucune citation directe détectée entre les articles identifiés de la bibliothèque."
    payload = {
        "ok": True,
        "engine": "openalex_internal",
        "graph_type": "library_citations",
        "nodes": nodes,
        "edges": edges,
        "communities": communities,
        "community_edges": community_edges,
        "skipped_sources": skipped,
        "stats": {
            "library_articles": total_library_articles,
            "resolved_articles": len(nodes),
            "unresolved_articles": len(skipped),
            "connected_articles": sum(1 for node in nodes if node["degree"] > 0),
            "isolated_articles": sum(1 for node in nodes if node["degree"] == 0),
            "connected_components": sum(1 for component_id in component_sizes if component_id >= 0),
            "largest_component": max(
                (size for component_id, size in component_sizes.items() if component_id >= 0),
                default=0,
            ),
            "communities_count": len(communities),
            "largest_community": max((community["size"] for community in communities), default=0),
            "edges_count": len(edges),
            "build_ms": int((time.perf_counter() - started) * 1000),
            "cached": False,
        },
        "warning": warning,
        "msg": "",
    }
    _internal_graph_cache.clear()
    _internal_graph_cache.update(
        key=cache_key,
        created_at=time.monotonic(),
        payload=copy.deepcopy(payload),
    )
    return payload


def _expansion_work(meta: dict[str, Any], relation: str) -> dict[str, Any]:
    oa_id = str(meta.get("openalex_id") or "")
    work_id = oa_id or str(meta.get("id") or "")
    return {
        "id": work_id,
        "openalex_id": oa_id,
        "title": meta.get("title") or "",
        "year": meta.get("year"),
        "doi": meta.get("doi") or "",
        "authors": meta.get("authors") or [],
        "cited_by_count": meta.get("cited_by_count") or 0,
        "abstract": meta.get("abstract") or "",
        "journal": meta.get("journal") or "",
        "landing_page_url": meta.get("landing_page_url") or "",
        "expansion_kind": relation,
    }


async def expand_citation_graph_work(
    openalex_id: str,
    limit: int = 18,
) -> dict[str, Any]:
    """Return one additional citation layer around an OpenAlex work.

    ``prior_works`` are references cited by the selected work, while
    ``derivative_works`` are works that cite it. The bounded result is designed
    to be merged into an already displayed graph and expanded recursively.
    """
    started = time.perf_counter()
    center_id = _strip_oa_prefix(openalex_id)
    if not center_id:
        return {"ok": False, "msg": "Identifiant OpenAlex manquant."}
    result_limit = min(max(1, int(limit)), _MAX_EXPANSION_PER_DIRECTION)
    headers = {"User-Agent": "Bibliobrain/1.0 (mailto:bibliobrain@local)"}

    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, headers=headers) as client:
        center = await _fetch_full_work_by_oa_id(client, center_id)
        if not center or not center.get("openalex_id"):
            return {"ok": False, "msg": "Article introuvable sur OpenAlex."}

        import asyncio as _asyncio

        reference_ids = [
            ref
            for ref in (center.get("referenced_works") or [])
            if ref and ref != center_id
        ][:_MAX_EXPANSION_REFERENCE_CANDIDATES]
        gathered: tuple[Any, Any, Any] = await _asyncio.gather(
            _fetch_works_by_oa_ids(client, reference_ids),
            _fetch_top_citers(client, center_id, limit=result_limit),
            _fetch_crossref_references(
                client,
                str(center.get("doi") or ""),
                center_id=center_id,
            ),
            return_exceptions=True,
        )
        prior_result, derivative_result, crossref_result = gathered
        prior_meta = prior_result if isinstance(prior_result, list) else []
        derivative_meta = derivative_result if isinstance(derivative_result, list) else []
        crossref_references = crossref_result if isinstance(crossref_result, list) else []
        if crossref_references:
            prior_meta, fallback_count = await _merge_crossref_reference_works(
                client,
                center_id=center_id,
                openalex_works=prior_meta,
                crossref_references=crossref_references,
                limit=result_limit,
            )
        else:
            fallback_count = 0
            prior_meta.sort(
                key=lambda work: (
                    -(work.get("cited_by_count") or 0),
                    -(work.get("year") or 0),
                )
            )

    prior_works = [
        _expansion_work(work, "prior")
        for work in prior_meta[:result_limit]
        if (work.get("id") or work.get("openalex_id")) and work.get("openalex_id") != center_id
    ]
    derivative_works = [
        _expansion_work(work, "derivative")
        for work in derivative_meta[:result_limit]
        if work.get("openalex_id") and work.get("openalex_id") != center_id
    ]
    edges = [
        {"source": center_id, "target": work["id"], "kind": "prior"}
        for work in prior_works
    ] + [
        {"source": work["id"], "target": center_id, "kind": "derivative"}
        for work in derivative_works
    ]
    return {
        "ok": True,
        "center": _expansion_work(center, "center"),
        "prior_works": prior_works,
        "derivative_works": derivative_works,
        "edges": edges,
        "stats": {
            "prior_count": len(prior_works),
            "derivative_count": len(derivative_works),
            "references_available": max(
                len(center.get("referenced_works") or []),
                len(crossref_references),
            ),
            "crossref_references_count": len(crossref_references),
            "fallback_references_count": fallback_count,
            "limit_per_direction": result_limit,
            "build_ms": int((time.perf_counter() - started) * 1000),
        },
        "msg": "",
    }


async def _build_graph_from_resolved(
    resolved: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    started: float,
    *,
    total_sources: int,
    empty_msg: str,
    enrich_crossref_references: bool = False,
) -> dict[str, Any]:
    if not resolved:
        return _empty_payload(skipped, started, msg=empty_msg)

    headers = {"User-Agent": "Bibliobrain/1.0 (mailto:bibliobrain@local)"}
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS, headers=headers) as client:
        works = await _fetch_works_by_doi(
            client,
            [
                r["doi"]
                for r in resolved
                if r.get("doi") and not isinstance(r.get("openalex_work"), dict)
            ],
        )
        preloaded_works = [
            r["openalex_work"]
            for r in resolved
            if isinstance(r.get("openalex_work"), dict)
        ]
        known_ids = {work.get("openalex_id") for work in works}
        works.extend(
            work for work in preloaded_works if work.get("openalex_id") not in known_ids
        )
        work_by_doi = {w["doi"]: w for w in works if w.get("doi")}

        def work_for_record(record: dict[str, Any]) -> dict[str, Any] | None:
            preloaded = record.get("openalex_work")
            if isinstance(preloaded, dict):
                return preloaded
            return work_by_doi.get(record.get("doi"))

        nodes: list[dict[str, Any]] = []
        oa_id_to_resolved: dict[str, dict[str, Any]] = {}
        work_by_oa_id: dict[str, dict[str, Any]] = {}
        for record in resolved:
            work = work_for_record(record)
            if not work:
                continue
            oa_id = work["openalex_id"]
            if not oa_id:
                continue
            oa_id_to_resolved[oa_id] = record
            work_by_oa_id[oa_id] = work
            nodes.append(
                {
                    "id": oa_id,
                    "openalex_id": oa_id,
                    "title": work["title"] or record["source_title"],
                    "year": work["year"],
                    "doi": work["doi"],
                    "authors": work["authors"],
                    "cited_by_count": work["cited_by_count"],
                    "abstract": work.get("abstract", ""),
                    "journal": work.get("journal", ""),
                    "landing_page_url": work.get("landing_page_url", ""),
                    "in_notebook": True,
                    "source_id": record["source_id"],
                    "source_path": record["source_path"],
                }
            )

        in_notebook_ids = set(oa_id_to_resolved.keys())
        edges: list[dict[str, str]] = []
        ref_counts: dict[str, int] = {}
        ref_citers: dict[str, list[str]] = {}
        for record in resolved:
            work = work_for_record(record)
            if not work:
                continue
            source_oa = work["openalex_id"]
            for ref in work.get("referenced_works") or []:
                if not ref or ref == source_oa:
                    continue
                if ref in in_notebook_ids:
                    edges.append({"source": source_oa, "target": ref})
                else:
                    ref_counts[ref] = ref_counts.get(ref, 0) + 1
                    ref_citers.setdefault(ref, []).append(source_oa)

        # Prior works = anything cited by >= 1 notebook paper, capped & sorted
        prior_candidates = sorted(
            ((oa_id, count) for oa_id, count in ref_counts.items()),
            key=lambda pair: (-pair[1], pair[0]),
        )[:_MAX_PRIOR_WORKS]

        prior_works: list[dict[str, Any]] = []
        prior_metas: list[dict[str, Any]] = []
        if prior_candidates:
            metas = await _fetch_works_by_oa_ids(
                client, [oa for oa, _ in prior_candidates]
            )
            prior_metas = metas
            metas_by_id = {m["openalex_id"]: m for m in metas if m.get("openalex_id")}
            for oa_id, count in prior_candidates:
                meta = metas_by_id.get(oa_id)
                if not meta:
                    continue
                prior_works.append(
                    {
                        "id": oa_id,
                        "openalex_id": oa_id,
                        "title": meta["title"],
                        "year": meta["year"],
                        "doi": meta["doi"],
                        "authors": meta["authors"],
                        "cited_by_count": meta["cited_by_count"],
                        "abstract": meta.get("abstract", ""),
                        "journal": meta.get("journal", ""),
                        "landing_page_url": meta.get("landing_page_url", ""),
                        "citers_in_notebook": count,
                        "cited_by_notebook_ids": ref_citers.get(oa_id, []),
                        "kind": "prior",
                    }
                )

        crossref_references_count = 0
        fallback_references_count = 0
        if enrich_crossref_references and len(nodes) == 1:
            center_id = str(nodes[0].get("openalex_id") or "")
            center_work = work_by_oa_id.get(center_id) or {}
            try:
                crossref_references = await _fetch_crossref_references(
                    client,
                    str(center_work.get("doi") or ""),
                    center_id=center_id,
                )
            except (RuntimeError, TimeoutError, httpx.HTTPError):
                crossref_references = []
            crossref_references_count = len(crossref_references)
            if crossref_references:
                merged_references, fallback_references_count = (
                    await _merge_crossref_reference_works(
                        client,
                        center_id=center_id,
                        openalex_works=prior_metas,
                        crossref_references=crossref_references,
                        limit=_MAX_PRIOR_WORKS,
                    )
                )
                prior_works = []
                for meta in merged_references:
                    work_id = str(meta.get("openalex_id") or meta.get("id") or "")
                    if not work_id or work_id == center_id:
                        continue
                    prior_works.append(
                        {
                            "id": work_id,
                            "openalex_id": str(meta.get("openalex_id") or ""),
                            "title": meta.get("title") or "",
                            "year": meta.get("year"),
                            "doi": meta.get("doi") or "",
                            "authors": meta.get("authors") or [],
                            "cited_by_count": meta.get("cited_by_count") or 0,
                            "abstract": meta.get("abstract") or "",
                            "journal": meta.get("journal") or "",
                            "landing_page_url": meta.get("landing_page_url") or "",
                            "citers_in_notebook": 1,
                            "cited_by_notebook_ids": [center_id],
                            "kind": "prior",
                        }
                    )

        # Derivative works = papers citing notebook papers, aggregated.
        # Only fetch citers for the top-N most-cited notebook papers; otherwise
        # the per-paper fan-out blocks behind the OpenAlex throttle.
        derivative_works: list[dict[str, Any]] = []
        derivatives_capped_from = 0
        if in_notebook_ids:
            import asyncio as _asyncio

            ranked_for_deriv = sorted(
                in_notebook_ids,
                key=lambda oa: -(work_by_oa_id.get(oa, {}).get("cited_by_count") or 0),
            )
            if len(ranked_for_deriv) > _MAX_PAPERS_FOR_DERIVATIVES:
                derivatives_capped_from = len(ranked_for_deriv)
                ranked_for_deriv = ranked_for_deriv[:_MAX_PAPERS_FOR_DERIVATIVES]

            citer_results = await _asyncio.gather(
                *[_fetch_top_citers(client, oa_id) for oa_id in ranked_for_deriv],
                return_exceptions=True,
            )
            citer_counts: dict[str, set[str]] = {}
            citer_meta: dict[str, dict[str, Any]] = {}
            for cited_oa, result in zip(ranked_for_deriv, citer_results):
                if isinstance(result, BaseException) or not isinstance(result, list):
                    continue
                for citer in result:
                    cid = citer.get("openalex_id")
                    if not cid or cid in in_notebook_ids:
                        continue
                    citer_counts.setdefault(cid, set()).add(cited_oa)
                    citer_meta[cid] = citer
            for cid, cites_set in citer_counts.items():
                meta = citer_meta[cid]
                derivative_works.append(
                    {
                        "id": cid,
                        "openalex_id": cid,
                        "title": meta["title"],
                        "year": meta["year"],
                        "doi": meta["doi"],
                        "authors": meta["authors"],
                        "cited_by_count": meta["cited_by_count"],
                        "abstract": meta.get("abstract", ""),
                        "journal": meta.get("journal", ""),
                        "landing_page_url": meta.get("landing_page_url", ""),
                        "cites_in_notebook": len(cites_set),
                        "cites_notebook_ids": sorted(cites_set),
                        "kind": "derivative",
                    }
                )
            derivative_works.sort(
                key=lambda d: (-d["cites_in_notebook"], -(d["cited_by_count"] or 0))
            )
            derivative_works = derivative_works[:_MAX_DERIVATIVE_WORKS]

    for record in resolved:
        if not work_for_record(record):
            doi = record.get("doi") or ""
            skipped.append(
                {
                    "title": record["source_title"] or "(sans titre)",
                    "reason": (
                        f"DOI {doi} introuvable sur OpenAlex"
                        if doi
                        else "Titre introuvable sur OpenAlex"
                    ),
                }
            )

    similarity_capped_from = 0
    if len(nodes) > _MAX_NODES_FOR_SIMILARITY:
        similarity_capped_from = len(nodes)
        nodes_for_similarity = sorted(
            nodes, key=lambda n: -(n.get("cited_by_count") or 0)
        )[:_MAX_NODES_FOR_SIMILARITY]
    else:
        nodes_for_similarity = nodes
    similarity_edges = _compute_similarity_edges(nodes_for_similarity, work_by_oa_id)

    warnings: list[str] = []
    if derivatives_capped_from:
        warnings.append(
            f"Travaux citants calculés uniquement pour les "
            f"{_MAX_PAPERS_FOR_DERIVATIVES} articles les plus cités du carnet "
            f"(sur {derivatives_capped_from})."
        )
    if similarity_capped_from:
        warnings.append(
            f"Similarité bibliographique calculée sur les "
            f"{_MAX_NODES_FOR_SIMILARITY} articles les plus cités "
            f"(sur {similarity_capped_from})."
        )

    # Backwards-compat alias kept so older clients still work.
    missing_links = [p for p in prior_works if p.get("citers_in_notebook", 0) >= 2]

    return {
        "ok": True,
        "nodes": nodes,
        "edges": edges,
        "similarity_edges": similarity_edges,
        "prior_works": prior_works,
        "derivative_works": derivative_works,
        "missing_links": missing_links,
        "skipped_sources": skipped,
        "warnings": warnings,
        "stats": {
            "papers_with_doi": len(nodes),
            "papers_without_doi": len(skipped),
            "papers_in_notebook": total_sources,
            "edges_count": len(edges),
            "similarity_edges_count": len(similarity_edges),
            "prior_works_count": len(prior_works),
            "crossref_references_count": crossref_references_count,
            "fallback_references_count": fallback_references_count,
            "derivative_works_count": len(derivative_works),
            "missing_links_count": len(missing_links),
            "build_ms": int((time.perf_counter() - started) * 1000),
        },
        "msg": "",
    }


def _compute_similarity_edges(
    nodes: list[dict[str, Any]],
    work_by_oa_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bibliographic coupling: edges between papers sharing references.

    Score = Jaccard similarity of their referenced_works sets. We only return
    the strongest pairs (top ~3*N) above a small threshold to keep the
    payload light and the visual layout focused.
    """
    refs_by_oa: dict[str, set[str]] = {}
    for node in nodes:
        oa_id = node.get("openalex_id")
        if not oa_id:
            continue
        work = work_by_oa_id.get(oa_id) or {}
        refs = set(work.get("referenced_works") or [])
        if refs:
            refs_by_oa[oa_id] = refs

    pairs: list[dict[str, Any]] = []
    oa_ids = list(refs_by_oa.keys())
    for i in range(len(oa_ids)):
        a = oa_ids[i]
        refs_a = refs_by_oa[a]
        for j in range(i + 1, len(oa_ids)):
            b = oa_ids[j]
            refs_b = refs_by_oa[b]
            inter = len(refs_a & refs_b)
            if inter == 0:
                continue
            union = len(refs_a | refs_b)
            score = inter / union if union else 0.0
            if score < _SIMILARITY_MIN_SCORE:
                continue
            pairs.append({"a": a, "b": b, "score": round(score, 4), "shared": inter})
    pairs.sort(key=lambda p: -p["score"])
    cap = max(len(nodes) * _SIMILARITY_TOP_PAIRS_FACTOR, 12)
    return pairs[:cap]
