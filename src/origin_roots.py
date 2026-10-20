"""Historical roots of a topic: the older works its literature keeps citing.

A light Reference Publication Year Spectroscopy (RPYS): take the works
OpenAlex finds for the topic, count how many of them cite each referenced
work, and keep the most cited references that are older than the corpus.
A reference cited by 40 % of the papers on a topic, twenty years before
them, is a root of that topic: a candidate origin, not a proof.

Network: only the topic text is sent (OpenAlex search), with the user's
consent and OpenAlex key, as the publication trends do.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import statistics
from collections import Counter
from typing import Any

import httpx

from src.article_discovery import _OPENALEX_BASE, _get_json_with_retry, _openalex_api_key
from src.discovery_cache_db import get_cached_response, set_cached_response

logger = logging.getLogger(__name__)

_CORPUS_PAGES = 2
_CORPUS_PAGE_SIZE = 100
_FETCHED_REFERENCES = 200
_ID_BATCH = 50
_MAX_ROOTS = 12
_MIN_CITING_SHARE = 0.05
_MIN_CITING_COUNT = 3
_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_MAX_TOPIC_CHARS = 300


class OriginRootsError(RuntimeError):
    """OpenAlex could not provide a usable corpus for the topic."""


class OriginRootsConfigurationError(RuntimeError):
    """No OpenAlex key: the search is refused rather than run unauthenticated."""


def _strip_id(value: Any) -> str:
    return str(value or "").rsplit("/", 1)[-1].strip()


def _work_payload(raw: dict[str, Any]) -> dict[str, Any]:
    authors = [
        str(((record or {}).get("author") or {}).get("display_name") or "").strip()
        for record in (raw.get("authorships") or [])[:4]
    ]
    authors = [name for name in authors if name]
    location = raw.get("primary_location") if isinstance(raw.get("primary_location"), dict) else {}
    source = location.get("source") if isinstance(location.get("source"), dict) else {}
    doi = re.sub(r"^https?://doi\.org/", "", str(raw.get("doi") or ""), flags=re.I).strip().lower()
    try:
        year = int(raw.get("publication_year")) if raw.get("publication_year") else None
    except (TypeError, ValueError):
        year = None
    return {
        "openalex_id": _strip_id(raw.get("id")),
        "title": str(raw.get("display_name") or "").strip(),
        "year": year,
        "authors": authors,
        "journal": str(source.get("display_name") or "").strip(),
        "doi": doi,
        "url": f"https://doi.org/{doi}" if doi else str(location.get("landing_page_url") or ""),
        "cited_by_count": int(raw.get("cited_by_count") or 0),
    }


async def _fetch_corpus(client: httpx.AsyncClient, topic: str) -> list[dict[str, Any]]:
    works: list[dict[str, Any]] = []
    for page in range(1, _CORPUS_PAGES + 1):
        payload = await _get_json_with_retry(
            client,
            f"{_OPENALEX_BASE}/works",
            params={
                "search": topic,
                "filter": "type:article|review|book-chapter|preprint",
                "select": "id,publication_year,referenced_works",
                "per-page": _CORPUS_PAGE_SIZE,
                "page": page,
            },
            api_name="openalex",
        )
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        works.extend(row for row in rows if isinstance(row, dict))
        if len(rows) < _CORPUS_PAGE_SIZE:
            break
    return works


async def _fetch_works(client: httpx.AsyncClient, ids: list[str]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), _ID_BATCH):
        batch = ids[start:start + _ID_BATCH]
        payload = await _get_json_with_retry(
            client,
            f"{_OPENALEX_BASE}/works",
            params={
                "filter": "openalex_id:" + "|".join(batch),
                "select": "id,display_name,publication_year,authorships,primary_location,doi,cited_by_count",
                "per-page": _ID_BATCH,
            },
            api_name="openalex",
        )
        for row in payload.get("results") or [] if isinstance(payload, dict) else []:
            if isinstance(row, dict):
                work = _work_payload(row)
                if work["openalex_id"]:
                    found[work["openalex_id"]] = work
    return found


def spectrum_peaks(counts_by_year: dict[int, int]) -> dict[int, float]:
    """RPYS deviation: each year's citations minus the median of the five years around it."""

    if not counts_by_year:
        return {}
    first, last = min(counts_by_year), max(counts_by_year)
    deviations: dict[int, float] = {}
    for year in range(first, last + 1):
        window = [counts_by_year.get(other, 0) for other in range(year - 2, year + 3)]
        deviations[year] = counts_by_year.get(year, 0) - statistics.median(window)
    return deviations


def select_roots(
    corpus: list[dict[str, Any]],
    references: dict[str, dict[str, Any]],
    citing_counts: Counter[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Most co-cited references older than the corpus, with the spectrum of all fetched references."""

    corpus_years = sorted(int(work["publication_year"]) for work in corpus if work.get("publication_year"))
    median_year = int(statistics.median(corpus_years)) if corpus_years else None
    size = len(corpus)
    counts_by_year: Counter[int] = Counter()
    for work_id, work in references.items():
        if work.get("year"):
            counts_by_year[int(work["year"])] += citing_counts[work_id]
    roots = []
    for work_id, count in citing_counts.most_common():
        work = references.get(work_id)
        if not work or not work.get("year"):
            continue
        if median_year is not None and work["year"] >= median_year:
            continue
        if count < _MIN_CITING_COUNT or count < _MIN_CITING_SHARE * size:
            continue
        roots.append({**work, "citing_count": count, "citing_share": round(count / size, 3)})
        if len(roots) >= _MAX_ROOTS:
            break
    deviations = spectrum_peaks(dict(counts_by_year))
    spectrum = [
        {"year": year, "count": counts_by_year.get(year, 0), "deviation": deviations.get(year, 0.0)}
        for year in sorted(deviations)
    ]
    meta = {
        "corpus_size": size,
        "corpus_first_year": corpus_years[0] if corpus_years else None,
        "corpus_median_year": median_year,
        "references_counted": len(citing_counts),
        "references_fetched": len(references),
    }
    return roots, {"spectrum": spectrum, **meta}


def _mark_library(roots: list[dict[str, Any]]) -> None:
    from src.origin_tracing import _library_index, _public_record

    try:
        library = _library_index()
    except Exception:  # noqa: BLE001 - the roots stay useful without the marks
        logger.debug("Library index unavailable for origin roots.", exc_info=True)
        return
    by_doi = {record["_doi_key"]: record for record in library if record.get("_doi_key")}
    for root in roots:
        record = by_doi.get(root.get("doi") or "")
        root["library"] = _public_record(record) if record else None


async def find_topic_roots(topic: str) -> dict[str, Any]:
    """Roots of ``topic`` in the OpenAlex literature, cached a week."""

    topic = re.sub(r"\s+", " ", str(topic or "")).strip()[:_MAX_TOPIC_CHARS]
    if len(topic) < 3:
        raise ValueError("Sujet trop court.")
    if not _openalex_api_key():
        raise OriginRootsConfigurationError(
            "Ajoutez une clé OpenAlex dans Paramètres > Réglages experts > Clés et connexions "
            "pour chercher les racines d'un sujet."
        )
    cache_key = "origin-roots:" + hashlib.sha256(topic.lower().encode("utf-8")).hexdigest()
    try:
        cached = get_cached_response(cache_key)
    except Exception:  # noqa: BLE001
        cached = None
    if isinstance(cached, dict) and cached.get("ok"):
        response = json.loads(json.dumps(cached, ensure_ascii=False))
        _mark_library(response.get("roots") or [])
        response.setdefault("meta", {})["cached"] = True
        return response

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        corpus = await _fetch_corpus(client, topic)
        if not corpus:
            raise OriginRootsError("OpenAlex n'a trouvé aucune publication pour ce sujet.")
        citing_counts: Counter[str] = Counter()
        for work in corpus:
            for reference in {_strip_id(ref) for ref in work.get("referenced_works") or []}:
                if reference:
                    citing_counts[reference] += 1
        top_ids = [work_id for work_id, _count in citing_counts.most_common(_FETCHED_REFERENCES)]
        references = await _fetch_works(client, top_ids) if top_ids else {}
    roots, meta = select_roots(corpus, references, citing_counts)
    response = {"ok": True, "topic": topic, "roots": roots, "spectrum": meta.pop("spectrum"), "meta": meta}
    try:
        set_cached_response(cache_key, "openalex-origin-roots", response, ttl_seconds=_CACHE_TTL_SECONDS)
    except Exception:  # noqa: BLE001
        logger.debug("Unable to cache the origin roots response", exc_info=True)
    response = json.loads(json.dumps(response, ensure_ascii=False))
    _mark_library(response["roots"])
    response["meta"]["cached"] = False
    return response
