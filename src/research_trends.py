"""Publication trend aggregation backed by OpenAlex.

The module deliberately consumes OpenAlex grouped counts instead of counting
the small, relevance-ranked result set returned by article discovery.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from src.article_discovery import (
    _OPENALEX_BASE,
    _get_json_with_retry,
    _openalex_api_key,
)
from src.discovery_cache_db import get_cached_response, set_cached_response

logger = logging.getLogger(__name__)

_MAX_TOPICS = 3
_MAX_YEAR_SPAN = 200
_CURRENT_CACHE_TTL_SECONDS = 24 * 60 * 60
_HISTORICAL_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_SUPPORTED_WORK_TYPES = {
    "article",
    "book-chapter",
    "dissertation",
    "preprint",
    "report",
    "review",
}


class OpenAlexConfigurationError(RuntimeError):
    """Raised when publication trends cannot authenticate with OpenAlex."""


class OpenAlexTrendsError(RuntimeError):
    """Raised when OpenAlex returns an unusable trends response."""


def _cache_key(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"research-trends:openalex:v1:{digest}"


def _validated_topics(topics: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not 1 <= len(topics) <= _MAX_TOPICS:
        raise ValueError("Ajoutez entre un et trois sujets à comparer.")
    normalized: list[dict[str, str]] = []
    for index, topic in enumerate(topics):
        query = str(topic.get("query") or "").strip()
        label = str(topic.get("label") or query).strip()
        if len(query) < 2:
            raise ValueError(f"La requête du sujet {index + 1} est trop courte.")
        normalized.append({"label": label[:100] or query[:100], "query": query})
    return normalized


def _validated_years(from_year: int, to_year: int) -> tuple[int, int]:
    current_year = datetime.now(UTC).year
    start = int(from_year)
    end = int(to_year)
    if start > end:
        raise ValueError("L'année de début doit précéder l'année de fin.")
    if end > current_year:
        raise ValueError("La période ne peut pas dépasser l'année courante.")
    if end - start + 1 > _MAX_YEAR_SPAN:
        raise ValueError(f"La période est limitée à {_MAX_YEAR_SPAN} ans.")
    return start, end


def _validated_work_types(work_types: list[str]) -> list[str]:
    normalized = sorted({str(value or "").strip().lower() for value in work_types})
    normalized = [value for value in normalized if value]
    if not normalized:
        return ["article"]
    unsupported = [value for value in normalized if value not in _SUPPORTED_WORK_TYPES]
    if unsupported:
        raise ValueError(f"Type de publication non pris en charge : {unsupported[0]}")
    return normalized


def _openalex_filter(
    *,
    from_year: int,
    to_year: int,
    work_types: list[str],
    open_access_only: bool,
) -> str:
    filters = [f"publication_year:{from_year}-{to_year}"]
    if work_types:
        filters.append(f"type:{'|'.join(work_types)}")
    if open_access_only:
        filters.append("open_access.is_oa:true")
    return ",".join(filters)


def _counts_from_grouped_payload(
    payload: dict[str, Any],
    *,
    from_year: int,
    to_year: int,
) -> tuple[dict[int, int], int]:
    grouped = payload.get("group_by")
    if not isinstance(grouped, list):
        raise OpenAlexTrendsError("OpenAlex n'a pas renvoyé d'agrégation annuelle exploitable.")

    counts = {year: 0 for year in range(from_year, to_year + 1)}
    for row in grouped:
        if not isinstance(row, dict):
            continue
        try:
            year_value = row.get("key")
            if year_value is None:
                continue
            year = int(year_value)
            count = max(0, int(row.get("count") or 0))
        except (TypeError, ValueError):
            continue
        if from_year <= year <= to_year:
            counts[year] = count

    raw_meta = payload.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    try:
        total = max(0, int(meta.get("count") or sum(counts.values())))
    except (TypeError, ValueError):
        total = sum(counts.values())
    return counts, total


async def _fetch_grouped_counts(
    client: httpx.AsyncClient,
    *,
    query: str,
    filter_value: str,
    from_year: int,
    to_year: int,
) -> tuple[dict[int, int], int]:
    params: dict[str, Any] = {
        "filter": filter_value,
        "group_by": "publication_year",
        "per-page": _MAX_YEAR_SPAN,
    }
    if query:
        params["search"] = query
    payload = await _get_json_with_retry(
        client,
        f"{_OPENALEX_BASE}/works",
        params=params,
        api_name="openalex",
    )
    return _counts_from_grouped_payload(
        payload,
        from_year=from_year,
        to_year=to_year,
    )


def _percentage_change(current: int, previous: int) -> float | None:
    if previous <= 0:
        return None
    return round(((current - previous) / previous) * 100.0, 1)


def _series_stats(points: list[dict[str, Any]], *, current_year: int) -> dict[str, Any]:
    complete = [point for point in points if int(point["year"]) < current_year]
    comparison = complete or points
    peak = max(comparison, key=lambda point: int(point["count"]), default=None)
    latest = comparison[-1] if comparison else None
    previous = comparison[-2] if len(comparison) >= 2 else None
    five_year_previous = comparison[-6] if len(comparison) >= 6 else None
    return {
        "total": sum(int(point["count"]) for point in points),
        "peak_year": int(peak["year"]) if peak else None,
        "peak_count": int(peak["count"]) if peak else 0,
        "latest_complete_year": int(latest["year"]) if latest else None,
        "latest_complete_count": int(latest["count"]) if latest else 0,
        "year_over_year_pct": (
            _percentage_change(int(latest["count"]), int(previous["count"]))
            if latest and previous
            else None
        ),
        "five_year_growth_pct": (
            _percentage_change(int(latest["count"]), int(five_year_previous["count"]))
            if latest and five_year_previous
            else None
        ),
    }


async def build_research_trends(
    *,
    topics: list[dict[str, Any]],
    from_year: int,
    to_year: int,
    work_types: list[str],
    open_access_only: bool = False,
) -> dict[str, Any]:
    """Return comparable annual publication counts for one to three topics."""

    if not _openalex_api_key():
        raise OpenAlexConfigurationError(
            "Configurez la cle OpenAlex dans Reglages > Avance > Cles et acces API pour utiliser les tendances."
        )

    normalized_topics = _validated_topics(topics)
    start, end = _validated_years(from_year, to_year)
    normalized_types = _validated_work_types(work_types)
    request_fingerprint = {
        "topics": normalized_topics,
        "from_year": start,
        "to_year": end,
        "work_types": normalized_types,
        "open_access_only": bool(open_access_only),
    }
    cache_key = _cache_key(request_fingerprint)
    try:
        cached = get_cached_response(cache_key)
    except Exception:
        logger.debug("Recovered from an error in build_research_trends.", exc_info=True)
        cached = None
    if isinstance(cached, dict) and cached.get("ok"):
        response = json.loads(json.dumps(cached, ensure_ascii=False))
        response.setdefault("meta", {})["cached"] = True
        return response

    filter_value = _openalex_filter(
        from_year=start,
        to_year=end,
        work_types=normalized_types,
        open_access_only=bool(open_access_only),
    )
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        tasks = [
            _fetch_grouped_counts(
                client,
                query=topic["query"],
                filter_value=filter_value,
                from_year=start,
                to_year=end,
            )
            for topic in normalized_topics
        ]
        tasks.append(
            _fetch_grouped_counts(
                client,
                query="",
                filter_value=filter_value,
                from_year=start,
                to_year=end,
            )
        )
        fetched = await asyncio.gather(*tasks)

    topic_results = fetched[:-1]
    baseline_counts, baseline_total = fetched[-1]
    global_max = max(
        (count for counts, _total in topic_results for count in counts.values()),
        default=0,
    )
    current_year = datetime.now(UTC).year
    series: list[dict[str, Any]] = []
    for topic, (counts, provider_total) in zip(normalized_topics, topic_results, strict=True):
        points: list[dict[str, Any]] = []
        for year in range(start, end + 1):
            count = counts.get(year, 0)
            baseline = baseline_counts.get(year, 0)
            points.append(
                {
                    "year": year,
                    "count": count,
                    "share_per_million": (
                        round((count / baseline) * 1_000_000, 2) if baseline > 0 else 0.0
                    ),
                    "index": round((count / global_max) * 100.0, 1) if global_max > 0 else 0.0,
                    "incomplete": year == current_year,
                }
            )
        series.append(
            {
                "label": topic["label"],
                "query": topic["query"],
                "provider_total": provider_total,
                "points": points,
                "stats": _series_stats(points, current_year=current_year),
            }
        )

    response = {
        "ok": True,
        "series": series,
        "baseline": {
            "label": "Publications correspondant aux filtres",
            "total": baseline_total,
            "points": [
                {"year": year, "count": baseline_counts.get(year, 0)}
                for year in range(start, end + 1)
            ],
        },
        "meta": {
            "source": "OpenAlex",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "cached": False,
            "from_year": start,
            "to_year": end,
            "work_types": normalized_types,
            "open_access_only": bool(open_access_only),
            "current_year_incomplete": start <= current_year <= end,
            "current_year": current_year,
            "count_semantics": "Approximate indexed works matching the visible OpenAlex query",
        },
    }
    ttl = (
        _CURRENT_CACHE_TTL_SECONDS
        if end >= current_year
        else _HISTORICAL_CACHE_TTL_SECONDS
    )
    try:
        set_cached_response(cache_key, "openalex-trends", response, ttl_seconds=ttl)
    except Exception:
        logger.debug("Unable to cache the OpenAlex trends response", exc_info=True)
    return response
