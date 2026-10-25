"""Local bibliographic metadata extraction for standalone or incomplete PDFs."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import replace
from difflib import SequenceMatcher
from functools import lru_cache
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from pypdf import PdfReader

from src import config
from src.app_settings import get_app_settings
from src.document_guard import check_document
from src.zotero_db import ZoteroItem, lookup_zotero_item

_logger = logging.getLogger(__name__)
_cache_lock = threading.RLock()
_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_metadata_cache (
    path TEXT PRIMARY KEY,
    file_size INTEGER NOT NULL,
    modified_ns INTEGER NOT NULL,
    payload TEXT NOT NULL,
    touched_at INTEGER NOT NULL
)
"""
_ENRICHMENT_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_metadata_enrichment_cache (
    lookup_key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    matched INTEGER NOT NULL,
    touched_at INTEGER NOT NULL
)
"""

_CROSSREF_BASE = "https://api.crossref.org"
_CROSSREF_TIMEOUT_SECONDS = 5.0
_OPENALEX_BASE = "https://api.openalex.org"
_OPENALEX_TIMEOUT_SECONDS = 5.0
_ENRICHMENT_MATCH_TTL_SECONDS = 180 * 24 * 60 * 60
_ENRICHMENT_MISS_TTL_SECONDS = 7 * 24 * 60 * 60
_crossref_lock = threading.Lock()
_crossref_last_call = 0.0
_crossref_consecutive_failures = 0
_crossref_open_until = 0.0
_openalex_lock = threading.Lock()
_openalex_last_call = 0.0
_openalex_consecutive_failures = 0
_openalex_open_until = 0.0

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
_CREATION_YEAR_RE = re.compile(r"(?:D:)?((?:19|20)\d{2})")
_ABSTRACT_RE = re.compile(
    r"(?is)(?:^|\n)\s*abstract\s*[:.\-—]?\s*(.+?)"
    r"(?=\n\s*(?:keywords?|index terms?|introduction|1[.\s]+introduction)\b|$)"
)
_TITLE_NOISE_RE = re.compile(
    r"(?i)\b(?:doi|issn|journal|proceedings|conference|volume|vol\.|copyright|"
    r"arxiv|preprint|university|department|www\.|https?://|received|accepted)\b"
)
_AUTHOR_NOISE_RE = re.compile(
    r"(?i)\b(?:university|department|institute|laboratory|laboratoire|faculty|"
    r"school|college|research center|corresponding author)\b"
)
_GENERIC_TITLES = {
    "",
    "untitled",
    "document",
    "article",
    "paper",
    "microsoft word",
}

_CROSSREF_ITEM_TYPES = {
    "book": "book",
    "book-chapter": "incollection",
    "dissertation": "phdthesis",
    "journal-article": "article",
    "monograph": "book",
    "posted-content": "unpublished",
    "proceedings": "proceedings",
    "proceedings-article": "inproceedings",
    "report": "techreport",
    "report-component": "techreport",
}


def _clean_text(value: Any) -> str:
    text = str(value or "").replace("\x00", " ")
    return re.sub(r"\s+", " ", text).strip()


def _metadata_value(metadata: Any, *keys: str) -> str:
    if metadata is None:
        return ""
    for key in keys:
        try:
            value = metadata.get(key, "")
        except (AttributeError, TypeError):
            value = ""
        clean = _clean_text(value)
        if clean:
            return clean
    return ""


def _clean_title(value: Any, full_path: str) -> str:
    title = _clean_text(value)
    normalized = title.casefold().removesuffix(".pdf").strip()
    filename = Path(full_path).name.casefold().removesuffix(".pdf").strip()
    if normalized in _GENERIC_TITLES or normalized == filename:
        return ""
    if normalized.startswith("microsoft word -"):
        title = title.split("-", 1)[1].strip()
    return title.strip(" ._-")


def _split_authors(value: Any) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    text = re.sub(r"(?i)^\s*by\s+", "", text)
    parts = re.split(r"\s*(?:;|\band\b|\bet\b|&)\s*", text)
    if len(parts) == 1 and text.count(",") >= 1:
        comma_parts = [_clean_text(part) for part in text.split(",")]
        if len(comma_parts) > 1 and all(len(part.split()) >= 2 for part in comma_parts):
            parts = comma_parts
    result: list[str] = []
    for part in parts:
        clean = re.sub(r"\s*\[[^\]]+\]\s*$", "", _clean_text(part)).strip(" ,;")
        if clean and clean not in result:
            result.append(clean)
    return result


def _split_keywords(value: Any) -> list[str]:
    keywords: list[str] = []
    for part in re.split(r"[,;|]", _clean_text(value)):
        clean = _clean_text(part).strip(".")
        if clean and clean.casefold() not in {item.casefold() for item in keywords}:
            keywords.append(clean)
    return keywords


def _extract_doi(text: str) -> str:
    match = _DOI_RE.search(text or "")
    if not match:
        return ""
    return match.group(0).rstrip(".,;:)]}")


def _extract_abstract(text: str) -> str:
    match = _ABSTRACT_RE.search(text or "")
    if not match:
        return ""
    abstract = _clean_text(match.group(1))
    return abstract[:4000].strip()


def _page_lines(text: str) -> list[str]:
    return [_clean_text(line) for line in str(text or "").splitlines() if _clean_text(line)]


def _title_from_first_page(lines: list[str], full_path: str) -> tuple[str, int]:
    best_title = ""
    best_index = -1
    best_score = float("-inf")
    for index, line in enumerate(lines[:30]):
        lowered = line.casefold()
        if lowered in {"abstract", "résumé", "resume"}:
            break
        words = line.split()
        if (
            len(line) < 16
            or len(line) > 240
            or len(words) < 3
            or len(words) > 32
            or "@" in line
            or _TITLE_NOISE_RE.search(line)
            or _DOI_RE.search(line)
        ):
            continue
        digit_count = sum(char.isdigit() for char in line)
        comma_penalty = max(0, line.count(",") - 1) * 2
        score = min(len(words), 14) + max(0, 10 - index) - digit_count - comma_penalty
        if line.endswith((".", ";")):
            score -= 3
        if score > best_score:
            best_title = line
            best_index = index
            best_score = score
    return _clean_title(best_title, full_path), best_index


def _authors_from_first_page(lines: list[str], title_index: int) -> list[str]:
    if title_index < 0:
        return []
    for line in lines[title_index + 1 : title_index + 7]:
        if line.casefold() in {"abstract", "résumé", "resume"}:
            break
        if (
            len(line) > 180
            or "@" in line
            or _AUTHOR_NOISE_RE.search(line)
            or _DOI_RE.search(line)
            or sum(char.isdigit() for char in line) > 2
        ):
            continue
        raw_tokens = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ'’-]+", line)
        if not 2 <= len(raw_tokens) <= 18:
            continue
        name_like = sum(
            1
            for token in raw_tokens
            if len(token) == 1 or token[:1].isupper() or token.casefold() in {"and", "et"}
        )
        if name_like / len(raw_tokens) < 0.7:
            continue
        authors = _split_authors(line)
        if authors:
            return authors
    return []


def _metadata_year(metadata: Any, text: str) -> str:
    raw_date = _metadata_value(metadata, "/CreationDate", "creation_date", "/ModDate")
    match = _CREATION_YEAR_RE.search(raw_date)
    if match:
        return match.group(1)
    match = _YEAR_RE.search(text[:8000])
    return match.group(1) if match else ""


def _item_payload(item: ZoteroItem) -> dict[str, Any]:
    return {
        "title": item.title,
        "authors": list(item.authors),
        "year": item.year,
        "journal": item.journal,
        "abstract": item.abstract,
        "item_type": item.item_type,
        "tags": list(item.tags),
        "doi": item.doi,
    }


def _item_from_payload(payload: dict[str, Any]) -> ZoteroItem:
    return ZoteroItem(
        title=str(payload.get("title") or ""),
        authors=[str(value) for value in payload.get("authors") or []],
        year=str(payload.get("year") or ""),
        journal=str(payload.get("journal") or ""),
        abstract=str(payload.get("abstract") or ""),
        item_type=str(payload.get("item_type") or ""),
        tags=[str(value) for value in payload.get("tags") or []],
        doi=str(payload.get("doi") or ""),
    )


def _normalized_doi(value: Any) -> str:
    text = _clean_text(value)
    text = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"(?i)^doi\s*:\s*", "", text)
    match = _DOI_RE.search(text)
    return match.group(0).rstrip(".,;:)]}").lower() if match else ""


def _normalized_match_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", _clean_text(value)).casefold()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def _title_similarity(left: Any, right: Any) -> float:
    left_text = _normalized_match_text(left)
    right_text = _normalized_match_text(right)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    left_tokens = set(left_text.split())
    right_tokens = set(right_text.split())
    token_score = len(left_tokens & right_tokens) / max(1, len(left_tokens | right_tokens))
    sequence_score = SequenceMatcher(None, left_text, right_text).ratio()
    return 0.65 * sequence_score + 0.35 * token_score


def _crossref_first_text(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return _clean_text(value)


def _crossref_year(raw: dict[str, Any]) -> str:
    for name in ("published-print", "published-online", "published", "issued", "created"):
        value = raw.get(name)
        if not isinstance(value, dict):
            continue
        date_parts = value.get("date-parts")
        if (
            isinstance(date_parts, list)
            and date_parts
            and isinstance(date_parts[0], list)
            and date_parts[0]
        ):
            year = str(date_parts[0][0] or "").strip()
            if _YEAR_RE.fullmatch(year):
                return year
    return ""


def _crossref_authors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    authors: list[str] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        literal = _clean_text(raw.get("name"))
        given = _clean_text(raw.get("given"))
        family = _clean_text(raw.get("family"))
        author = literal or " ".join(part for part in (given, family) if part)
        if author and author not in authors:
            authors.append(author)
    return authors


def _crossref_abstract(value: Any) -> str:
    text = re.sub(r"<[^>]+>", " ", str(value or ""))
    return _clean_text(unescape(text))[:4000]


def _crossref_item(raw: dict[str, Any]) -> ZoteroItem | None:
    title = _crossref_first_text(raw.get("title"))
    doi = _normalized_doi(raw.get("DOI"))
    authors = _crossref_authors(raw.get("author"))
    year = _crossref_year(raw)
    if not any((title, doi, authors, year)):
        return None
    raw_type = _clean_text(raw.get("type")).lower()
    return ZoteroItem(
        title=title,
        authors=authors,
        year=year,
        journal=_crossref_first_text(raw.get("container-title")),
        abstract=_crossref_abstract(raw.get("abstract")),
        item_type=_CROSSREF_ITEM_TYPES.get(raw_type, "document"),
        doi=doi,
    )


def _crossref_get_json(
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Fetch one fixed-origin Crossref response with light process-wide throttling."""
    global _crossref_consecutive_failures, _crossref_last_call, _crossref_open_until
    headers = {
        "Accept": "application/json",
        "User-Agent": "Bibliobrain/1.0 (mailto:local@bibliobrain.local)",
    }
    if time.monotonic() < _crossref_open_until:
        return None
    try:
        with _crossref_lock:
            if time.monotonic() < _crossref_open_until:
                return None
            remaining = 0.35 - (time.monotonic() - _crossref_last_call)
            if remaining > 0:
                time.sleep(remaining)
            response = httpx.get(
                f"{_CROSSREF_BASE}{path}",
                params=params,
                headers=headers,
                timeout=_CROSSREF_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
            _crossref_last_call = time.monotonic()
        if response.status_code == 404:
            _crossref_consecutive_failures = 0
            _crossref_open_until = 0.0
            return {"message": {}}
        response.raise_for_status()
        payload = response.json()
        _crossref_consecutive_failures = 0
        _crossref_open_until = 0.0
        return payload if isinstance(payload, dict) else None
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        _crossref_consecutive_failures += 1
        if _crossref_consecutive_failures >= 2:
            _crossref_open_until = time.monotonic() + 60.0
        _logger.debug("Crossref metadata lookup failed for %s: %s", path, exc)
        return None


def _author_matches(local: ZoteroItem, candidate: ZoteroItem) -> bool:
    if not local.authors or not candidate.authors:
        return False
    local_names = {_normalized_match_text(author) for author in local.authors}
    candidate_names = {_normalized_match_text(author) for author in candidate.authors}
    for left in local_names:
        left_parts = set(left.split())
        for right in candidate_names:
            right_parts = set(right.split())
            if left == right or (left_parts and right_parts and left_parts & right_parts):
                return True
    return False


def _select_crossref_title_match(
    local: ZoteroItem,
    rows: list[Any],
) -> ZoteroItem | None:
    ranked: list[tuple[float, ZoteroItem]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        candidate = _crossref_item(raw)
        if candidate is None or not candidate.title:
            continue
        title_score = _title_similarity(local.title, candidate.title)
        score = title_score
        if local.year and candidate.year and local.year == candidate.year:
            score += 0.03
        if _author_matches(local, candidate):
            score += 0.05
        ranked.append((min(score, 1.0), candidate))

    ranked.sort(key=lambda row: row[0], reverse=True)
    if not ranked:
        return None
    best_score, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    # Require a strong title identity. Supporting author/year evidence may rescue
    # a small OCR variation, but ambiguous near-ties are deliberately rejected.
    if best_score < 0.86:
        return None
    if best_score < 0.94 and second_score > best_score - 0.06:
        return None
    return best


def _crossref_lookup(local: ZoteroItem) -> tuple[ZoteroItem | None, bool]:
    doi = _normalized_doi(local.doi)
    if doi:
        payload = _crossref_get_json(f"/works/{quote(doi, safe='')}")
        if payload is None:
            return None, False
        raw = payload.get("message")
        candidate = _crossref_item(raw) if isinstance(raw, dict) else None
        if candidate and local.title and _title_similarity(local.title, candidate.title) < 0.55:
            return None, True
        return candidate, True

    title = _clean_text(local.title)
    if len(title) < 16 or len(_normalized_match_text(title).split()) < 3:
        return None, True
    params: dict[str, Any] = {"query.bibliographic": title, "rows": 5}
    if local.first_author():
        params["query.author"] = local.first_author()
    payload = _crossref_get_json("/works", params=params)
    if payload is None:
        return None, False
    message = payload.get("message")
    rows = message.get("items") if isinstance(message, dict) else []
    return _select_crossref_title_match(local, rows if isinstance(rows, list) else []), True


def _openalex_abstract(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in value.items():
        if not isinstance(positions, list):
            continue
        for position in positions:
            try:
                positioned.append((int(position), str(word)))
            except (TypeError, ValueError):
                continue
    positioned.sort(key=lambda row: row[0])
    return _clean_text(" ".join(word for _position, word in positioned))[:4000]


def _openalex_item(raw: dict[str, Any]) -> ZoteroItem | None:
    title = _clean_text(raw.get("display_name") or raw.get("title"))
    authors: list[str] = []
    for authorship in raw.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author")
        name = _clean_text(author.get("display_name")) if isinstance(author, dict) else ""
        if name and name not in authors:
            authors.append(name)
    primary_location = raw.get("primary_location")
    source = primary_location.get("source") if isinstance(primary_location, dict) else None
    journal = _clean_text(source.get("display_name")) if isinstance(source, dict) else ""
    year = str(raw.get("publication_year") or "").strip()
    if not _YEAR_RE.fullmatch(year):
        year = ""
    doi = _normalized_doi(raw.get("doi"))
    if not any((title, authors, year, journal, doi)):
        return None
    raw_type = _clean_text(raw.get("type")).lower()
    item_type = {
        "article": "article",
        "book": "book",
        "book-chapter": "incollection",
        "dissertation": "phdthesis",
        "preprint": "unpublished",
        "report": "techreport",
    }.get(raw_type, "document")
    return ZoteroItem(
        title=title,
        authors=authors,
        year=year,
        journal=journal,
        abstract=_openalex_abstract(raw.get("abstract_inverted_index")),
        item_type=item_type,
        doi=doi,
    )


def _openalex_get_json(
    path: str,
    *,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Fetch one fixed-origin OpenAlex response with throttling and a circuit breaker."""
    global _openalex_consecutive_failures, _openalex_last_call, _openalex_open_until
    if time.monotonic() < _openalex_open_until:
        return None
    headers = {"Accept": "application/json", "User-Agent": "Bibliobrain/1.0"}
    api_key = os.getenv("OPENALEX_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with _openalex_lock:
            if time.monotonic() < _openalex_open_until:
                return None
            remaining = 0.35 - (time.monotonic() - _openalex_last_call)
            if remaining > 0:
                time.sleep(remaining)
            response = httpx.get(
                f"{_OPENALEX_BASE}/{path.lstrip('/')}",
                params=params,
                headers=headers,
                timeout=_OPENALEX_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
            _openalex_last_call = time.monotonic()
        if response.status_code == 404:
            _openalex_consecutive_failures = 0
            _openalex_open_until = 0.0
            return {}
        response.raise_for_status()
        payload = response.json()
        _openalex_consecutive_failures = 0
        _openalex_open_until = 0.0
        return payload if isinstance(payload, dict) else None
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        _openalex_consecutive_failures += 1
        if _openalex_consecutive_failures >= 2:
            _openalex_open_until = time.monotonic() + 60.0
        _logger.debug("OpenAlex metadata lookup failed for %s: %s", path, exc)
        return None


def _select_openalex_title_match(
    local: ZoteroItem,
    rows: list[Any],
) -> ZoteroItem | None:
    ranked: list[tuple[float, ZoteroItem]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        candidate = _openalex_item(raw)
        if candidate is None or not candidate.title:
            continue
        score = _title_similarity(local.title, candidate.title)
        if _author_matches(local, candidate):
            score += 0.05
        if local.year and local.year == candidate.year:
            score += 0.03
        ranked.append((min(score, 1.0), candidate))
    ranked.sort(key=lambda row: row[0], reverse=True)
    if not ranked:
        return None
    best_score, best = ranked[0]
    second_score = ranked[1][0] if len(ranked) > 1 else 0.0
    if best_score < 0.88 or (best_score < 0.94 and second_score > best_score - 0.06):
        return None
    return best


def _openalex_lookup(local: ZoteroItem) -> tuple[ZoteroItem | None, bool]:
    doi = _normalized_doi(local.doi)
    if doi:
        payload = _openalex_get_json(f"works/{quote(f'doi:{doi}', safe=':')}")
        if payload is None:
            return None, False
        candidate = _openalex_item(payload)
        if candidate and local.title and _title_similarity(local.title, candidate.title) < 0.55:
            return None, True
        return candidate, True

    title = _clean_text(local.title)
    if len(title) < 16 or len(_normalized_match_text(title).split()) < 3:
        return None, True
    payload = _openalex_get_json("works", params={"search": title[:500], "per_page": 5})
    if payload is None:
        return None, False
    rows = payload.get("results")
    return _select_openalex_title_match(local, rows if isinstance(rows, list) else []), True


def _enrichment_lookup_key(item: ZoteroItem) -> str:
    doi = _normalized_doi(item.doi)
    if doi:
        return f"doi:{doi}"
    title = _normalized_match_text(item.title)
    if not title:
        return ""
    author = _normalized_match_text(item.first_author())
    return f"title:{title}|year:{item.year}|author:{author}"


def _enrichment_cache_get(lookup_key: str) -> tuple[bool, ZoteroItem | None]:
    db_path = _persistent_cache_path()
    if not lookup_key or not db_path.exists():
        return False, None
    try:
        with _cache_lock, contextlib.closing(sqlite3.connect(db_path, timeout=2.0)) as conn, conn:
            conn.execute(_ENRICHMENT_CACHE_SCHEMA)
            row = conn.execute(
                """
                SELECT payload, matched, touched_at
                FROM pdf_metadata_enrichment_cache WHERE lookup_key = ?
                """,
                (lookup_key,),
            ).fetchone()
        if row is None:
            return False, None
        matched = bool(row[1])
        ttl = _ENRICHMENT_MATCH_TTL_SECONDS if matched else _ENRICHMENT_MISS_TTL_SECONDS
        if int(time.time()) - int(row[2]) > ttl:
            return False, None
        if not matched:
            return True, None
        payload = json.loads(str(row[0]))
        return True, _item_from_payload(payload)
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        _logger.debug("PDF metadata enrichment cache read failed: %s", exc)
        return False, None


def _enrichment_cache_put(lookup_key: str, item: ZoteroItem | None) -> None:
    if not lookup_key:
        return
    db_path = _persistent_cache_path()
    payload = json.dumps(
        _item_payload(item) if item else {}, ensure_ascii=False, separators=(",", ":")
    )
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with _cache_lock, contextlib.closing(sqlite3.connect(db_path, timeout=2.0)) as conn, conn:
            conn.execute(_ENRICHMENT_CACHE_SCHEMA)
            conn.execute(
                """
                INSERT INTO pdf_metadata_enrichment_cache(lookup_key, payload, matched, touched_at)
                VALUES (?, ?, ?, unixepoch())
                ON CONFLICT(lookup_key) DO UPDATE SET
                    payload = excluded.payload,
                    matched = excluded.matched,
                    touched_at = excluded.touched_at
                """,
                (lookup_key, payload, int(item is not None)),
            )
    except (OSError, sqlite3.Error) as exc:
        _logger.debug("PDF metadata enrichment cache write failed: %s", exc)


def _pdf_metadata_enrichment_enabled() -> bool:
    try:
        settings = get_app_settings()
        return bool(
            settings.get("allow_external_api_requests")
            and settings.get("auto_enrich_pdf_metadata")
            and settings.get("metadata_source") != "zotero"
        )
    except (OSError, ValueError, TypeError):
        return False


def enrich_pdf_metadata(
    item: ZoteroItem | None,
    *,
    allow_network: bool = False,
) -> ZoteroItem | None:
    """Return a confident Crossref/OpenAlex match, checking the local cache first."""
    if item is None or not _pdf_metadata_enrichment_enabled():
        return None
    lookup_key = _enrichment_lookup_key(item)
    cached, result = _enrichment_cache_get(lookup_key)
    if cached:
        return result
    if not allow_network:
        return None
    result, crossref_definitive = _crossref_lookup(item)
    openalex_definitive = False
    if result is None:
        result, openalex_definitive = _openalex_lookup(item)
    definitive = bool(result is not None or (crossref_definitive and openalex_definitive))
    if definitive:
        _enrichment_cache_put(lookup_key, result)
    return result


@lru_cache(maxsize=8192)
def _extract_pdf_metadata_cached(
    full_path: str,
    file_size: int,
    modified_ns: int,
) -> ZoteroItem | None:
    del file_size, modified_ns
    try:
        check_document(full_path)
        reader = PdfReader(full_path, strict=False)
        metadata = reader.metadata
        page_texts: list[str] = []
        for page in reader.pages[:2]:
            try:
                page_texts.append(str(page.extract_text() or ""))
            except Exception:
                _logger.debug(
                    "Recovered from an error in _extract_pdf_metadata_cached.", exc_info=True
                )
                page_texts.append("")
    except Exception as exc:
        _logger.debug("PDF metadata extraction failed for %s: %s", full_path, exc)
        return None

    first_page = page_texts[0] if page_texts else ""
    searchable_text = "\n".join(
        (
            _metadata_value(metadata, "/Subject", "subject"),
            _metadata_value(metadata, "/Keywords", "keywords"),
            *page_texts,
        )
    )
    lines = _page_lines(first_page)
    title = _clean_title(_metadata_value(metadata, "/Title", "title"), full_path)
    title_index = -1
    if not title:
        title, title_index = _title_from_first_page(lines, full_path)

    authors = _split_authors(_metadata_value(metadata, "/Author", "author"))
    if not authors:
        if title_index < 0 and title:
            try:
                title_index = next(
                    index for index, line in enumerate(lines) if _clean_text(line) == title
                )
            except StopIteration:
                title_index = -1
        authors = _authors_from_first_page(lines, title_index)

    abstract = _extract_abstract(first_page)
    doi = _extract_doi(searchable_text)
    year = _metadata_year(metadata, first_page)
    tags = _split_keywords(_metadata_value(metadata, "/Keywords", "keywords"))
    subject = _metadata_value(metadata, "/Subject", "subject")
    if not abstract and subject and not _DOI_RE.fullmatch(subject):
        abstract = subject[:4000]

    if not any((title, authors, year, abstract, doi, tags)):
        return None
    return ZoteroItem(
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        item_type="document",
        tags=tags,
        doi=doi,
    )


def _persistent_cache_path() -> Path:
    return Path(config.STORAGE_DIR) / "pdf_metadata_cache.sqlite3"


def _persistent_cache_get(full_path: str, file_size: int, modified_ns: int) -> ZoteroItem | None:
    db_path = _persistent_cache_path()
    if not db_path.exists():
        return None
    try:
        with _cache_lock, contextlib.closing(sqlite3.connect(db_path, timeout=2.0)) as conn, conn:
            row = conn.execute(
                """
                SELECT payload FROM pdf_metadata_cache
                WHERE path = ? AND file_size = ? AND modified_ns = ?
                """,
                (full_path, file_size, modified_ns),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        return _item_from_payload(payload)
    except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError) as exc:
        _logger.debug("Persistent PDF metadata cache read failed: %s", exc)
        return None


def _persistent_cache_put(
    full_path: str,
    file_size: int,
    modified_ns: int,
    item: ZoteroItem,
) -> None:
    payload = json.dumps(
        _item_payload(item),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    db_path = _persistent_cache_path()
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with _cache_lock, contextlib.closing(sqlite3.connect(db_path, timeout=2.0)) as conn, conn:
            conn.execute(_CACHE_SCHEMA)
            conn.execute(
                """
                INSERT INTO pdf_metadata_cache(path, file_size, modified_ns, payload, touched_at)
                VALUES (?, ?, ?, ?, unixepoch())
                ON CONFLICT(path) DO UPDATE SET
                    file_size = excluded.file_size,
                    modified_ns = excluded.modified_ns,
                    payload = excluded.payload,
                    touched_at = excluded.touched_at
                """,
                (full_path, file_size, modified_ns, payload),
            )
            conn.execute(
                """
                DELETE FROM pdf_metadata_cache
                WHERE path IN (
                    SELECT path FROM pdf_metadata_cache
                    ORDER BY touched_at DESC LIMIT -1 OFFSET 10000
                )
                """
            )
    except (OSError, sqlite3.Error) as exc:
        _logger.debug("Persistent PDF metadata cache write failed: %s", exc)


def extract_pdf_metadata(full_path: str) -> ZoteroItem | None:
    """Extract bibliographic fields locally, caching by file signature."""
    try:
        normalized = os.path.realpath(str(full_path or ""))
        stat = os.stat(normalized)
    except OSError:
        return None
    file_size = int(stat.st_size)
    modified_ns = int(getattr(stat, "st_mtime_ns", stat.st_mtime * 1_000_000_000))
    persisted = _persistent_cache_get(normalized, file_size, modified_ns)
    if persisted is not None:
        return persisted
    item = _extract_pdf_metadata_cached(normalized, file_size, modified_ns)
    if item is not None:
        _persistent_cache_put(normalized, file_size, modified_ns, item)
    return item


def merge_missing_metadata(
    primary: ZoteroItem | None,
    fallback: ZoteroItem | None,
) -> ZoteroItem | None:
    """Keep source metadata authoritative and fill only its missing fields."""
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return replace(
        primary,
        title=primary.title or fallback.title,
        authors=list(primary.authors or fallback.authors),
        year=primary.year or fallback.year,
        journal=primary.journal or fallback.journal,
        abstract=primary.abstract or fallback.abstract,
        item_type=primary.item_type or fallback.item_type,
        tags=list(primary.tags or fallback.tags),
        doi=primary.doi or fallback.doi,
    )


def resolve_pdf_metadata(
    rel_path: str,
    full_path: str,
    library: dict[str, ZoteroItem],
    *,
    allow_external_enrichment: bool = False,
) -> ZoteroItem | None:
    """Resolve authoritative, remote-cached, then local PDF metadata."""
    primary = lookup_zotero_item(rel_path, library, full_path)
    enrichment_enabled = _pdf_metadata_enrichment_enabled()
    needs_local_fallback = primary is None or not (primary.title and primary.authors)
    needs_fallback = needs_local_fallback or enrichment_enabled
    if not needs_fallback:
        return primary
    local = extract_pdf_metadata(full_path) if needs_local_fallback else None
    lookup_item = merge_missing_metadata(primary, local)
    external = enrich_pdf_metadata(
        lookup_item,
        allow_network=allow_external_enrichment,
    )
    # Zotero/BibTeX remains authoritative. A confident Crossref record is more
    # bibliographically reliable than heuristic first-page extraction, while
    # local fields still complete anything Crossref omits.
    enriched_fallback = merge_missing_metadata(external, local)
    return merge_missing_metadata(primary, enriched_fallback)


def clear_pdf_metadata_cache() -> None:
    _extract_pdf_metadata_cached.cache_clear()
    try:
        _persistent_cache_path().unlink(missing_ok=True)
    except OSError:
        pass
