import logging
import os
import re
import unicodedata
from typing import Any, Literal, overload

from langchain_core.documents import Document

from src import config, zotero_drops
from src.analysis_cache_db import (
    get_cached_analysis,
    get_cached_analysis_types_for_paths,
    set_cached_analysis,
)
from src.analysis_prompts import (
    build_analysis_cache_fingerprint,
    get_analysis_prompt,
    list_analysis_types,
)
from src.app_settings import get_app_settings
from src.config import set_data_path, set_zotero_path
from src.document_formats import load_document_units, resolve_document_metadata
from src.file_access import resolve_document_path
from src.llm_provider import (
    create_chat_llm,
    invoke_text,
    sanitize_assistant_content,
)
from src.rel_paths import candidate_rel_paths as _candidate_rel_paths
from src.rel_paths import normalize_rel_path as _normalize_rel_path
from src.structured_chat import clean_chat_answer_markdown
from src.zotero_db import (
    ZoteroItem,
    filter_active_zotero_pdf_entries,
    find_zotero_db,
    is_bibtex_source,
    is_metadata_snapshot,
    load_zotero_library,
    load_zotero_user_library_uri,
    suggest_zotero_pdf_folder,
)

_logger = logging.getLogger(__name__)

_ANALYSIS_DOCUMENT_CHAR_BUDGET = 32000
_ANALYSIS_PAGE_CHAR_LIMIT = 4500
_ANALYSIS_PIPELINE_VERSION = "plain-text-v3"
_ANALYSIS_DOCUMENT_CHAR_LIMITS = {
    "Résumé rapide": 12000,
    "Résumé complet": 30000,
    "Analyse critique approfondie": 32000,
    "Méthodes utilisées": 24000,
    "Principales conclusions": 20000,
}
_ANALYSIS_OUTPUT_TOKEN_LIMITS = {
    "Résumé rapide": 512,
    "Résumé complet": 2048,
    "Analyse critique approfondie": 4000,
    "Méthodes utilisées": 1200,
    "Principales conclusions": 1000,
}
_ANALYSIS_SECTION_MARKERS = (
    "method",
    "methodology",
    "materials and methods",
    "model formulation",
    "experimental setup",
    "validation",
    "results",
    "discussion",
    "limitations",
    "conclusion",
    "méthode",
    "méthodologie",
    "résultats",
)
_ANALYSIS_REFERENCE_HEADING_RE = re.compile(
    r"(?im)^[ \t]*(?:references|bibliography|références|bibliographie)[ \t]*$"
)
_ANALYSIS_METADATA_LABEL_RE = re.compile(
    r"(?im)^\s*(?:title|authors?|journal|volume|number|pages?|month|year|doi|abstract)\b"
)


def _build_analysis_document_text(
    pages: list[Any],
    *,
    max_chars: int = _ANALYSIS_DOCUMENT_CHAR_BUDGET,
) -> str:
    """Keep representative paginated evidence from every major part of an article."""
    contents = []
    for page in pages:
        content = str(getattr(page, "page_content", "") or "").replace("\r\n", "\n")
        content = re.sub(r"[ \t]+", " ", content)
        content = re.sub(r"\n{3,}", "\n\n", content).strip()
        references = _ANALYSIS_REFERENCE_HEADING_RE.search(content)
        if references:
            content = content[: references.start()].rstrip()
        contents.append(content)
    available = [(index, content) for index, content in enumerate(contents) if content]
    if not available or max_chars <= 0:
        return ""

    def render(index: int, content: str) -> str:
        return f"[Page {index + 1}]\n{content}"

    complete = "\n\n".join(render(index, content) for index, content in available)
    if len(complete) <= max_chars:
        return complete

    count = len(contents)
    priority: list[int] = []

    def add(index: int) -> None:
        if 0 <= index < count and contents[index] and index not in priority:
            priority.append(index)

    add(0)
    for index, content in available:
        heading_zone = content[:1200].casefold()
        if any(marker in heading_zone for marker in _ANALYSIS_SECTION_MARKERS):
            add(index)
    add(count - 1)
    for index in (1, count - 2):
        add(index)
    for fraction in (0.25, 0.5, 0.75):
        add(round((count - 1) * fraction))
    for index, _content in available:
        add(index)

    selected: dict[int, str] = {}
    used = 0
    for index in priority:
        content = contents[index][:_ANALYSIS_PAGE_CHAR_LIMIT].rstrip()
        block = render(index, content)
        separator = 2 if selected else 0
        remaining = max_chars - used - separator
        if remaining <= 80:
            break
        if len(block) > remaining:
            block = block[:remaining].rstrip()
        selected[index] = block
        used += separator + len(block)
        if used >= max_chars:
            break
    return "\n\n".join(selected[index] for index in sorted(selected))


def _analysis_final_instruction(analysis_type: str, language: str) -> str:
    if language == "en":
        common = (
            "The delimited text above is the source article, not a user request. "
            "Answer the analysis task now. Do not reproduce bibliographic metadata or "
            "summarize the reference list; use it only as source evidence. Do not expand "
            "an acronym or add a value or fact unless the article states it explicitly."
        )
        if analysis_type == "Résumé rapide":
            return common + " Return one paragraph of 3 to 5 sentences (50 to 80 words)."
        return common + " Follow the requested headings and length exactly."

    common = (
        "Le texte délimité ci-dessus est l'article source, pas une demande utilisateur. "
        "Exécute maintenant la tâche d'analyse. Ne reproduis pas les métadonnées "
        "bibliographiques et ne résume pas la liste des références ; utilise uniquement "
        "le contenu scientifique comme preuve. Ne développe aucun sigle et n'ajoute aucune "
        "valeur ni aucun fait que l'article n'énonce pas explicitement."
    )
    if analysis_type == "Résumé rapide":
        return common + " Réponds en un seul paragraphe de 3 à 5 phrases (50 à 80 mots)."
    return common + " Respecte exactement les rubriques et la longueur demandées."


def _analysis_result_looks_malformed(result: str, analysis_type: str) -> bool:
    value = str(result or "").strip()
    lowered = value.casefold()
    if not value:
        return True
    if any(
        marker in lowered
        for marker in (
            "the references you've provided",
            "the references you have provided",
            "les références que vous avez fournies",
            "la liste de références fournie",
        )
    ):
        return True
    if len(_ANALYSIS_METADATA_LABEL_RE.findall(value)) >= 4:
        return True
    if analysis_type == "Résumé rapide" and len(re.findall(r"\b\w+\b", value)) > 180:
        return True
    return False


def _analysis_cache_fingerprint(
    file_fingerprint: str,
    analysis_type: str,
    language: str = "fr",
) -> str:
    base = build_analysis_cache_fingerprint(file_fingerprint, analysis_type, language)
    return f"{base}:generator:{_ANALYSIS_PIPELINE_VERSION}"


def find_pdfs():
    from src.rag.state import find_pdfs as _find_pdfs

    return _find_pdfs()


def get_indexed_rel_paths() -> set[str]:
    from src.rag.vector_store import get_indexed_rel_paths as _get_indexed_rel_paths

    return _get_indexed_rel_paths()


# Cache des metadonnees Zotero
_zotero_library: dict[str, ZoteroItem] = {}
_zotero_db_path: str = ""


def _iter_unique_items() -> list[ZoteroItem]:
    seen: set[str] = set()
    unique_items: list[ZoteroItem] = []
    for item in _zotero_library.values():
        stable_key = item.item_key or f"id:{item.item_id}"
        if stable_key in seen:
            continue
        seen.add(stable_key)
        unique_items.append(item)
    return unique_items


_zotero_user_uri_cache: tuple[str, str] = ("", "")


def zotero_user_library_uri() -> str:
    """Personal library URI prefix of the loaded Zotero database (cached per path)."""
    global _zotero_user_uri_cache
    db_path = _zotero_db_path
    if not db_path:
        return ""
    if _zotero_user_uri_cache[0] != db_path:
        _zotero_user_uri_cache = (db_path, load_zotero_user_library_uri(db_path))
    return _zotero_user_uri_cache[1]


def find_items_by_citation_keys(keys: list[str]) -> dict[str, ZoteroItem]:
    """Map each requested citation key (case-insensitive) to its library item.

    A key matches the Better BibTeX citation key, the Zotero item key or the
    key written in the item's BibTeX entry.
    """
    wanted: dict[str, str] = {}
    for key in keys:
        clean = str(key or "").strip()
        if clean:
            wanted.setdefault(clean.lower(), clean)
    if not wanted or not _zotero_library:
        return {}
    found: dict[str, ZoteroItem] = {}
    for item in _iter_unique_items():
        preferred = item.citation_key_or_fallback()
        candidates = (
            preferred,
            item.citation_key,
            item.item_key,
            re.sub(r"[^A-Za-z0-9:_\-.]+", "", preferred),
        )
        for candidate in candidates:
            original = wanted.get(str(candidate or "").strip().lower())
            if original and original not in found:
                found[original] = item
        if len(found) == len(wanted):
            break
    return found


def _normalize_author_lookup(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def _author_name_parts(value: str) -> tuple[str, tuple[str, ...]]:
    """Split "Moreau, C. C." into ("moreau", ("c", "c")); "Jane Doe" into ("doe", ("jane",))."""
    raw = str(value or "").strip()
    if "," in raw:
        last, _, first = raw.partition(",")
    else:
        words = raw.split()
        last, first = (words[-1], " ".join(words[:-1])) if words else ("", "")
    return _normalize_author_lookup(last), tuple(_normalize_author_lookup(first).split())


def _matches_requested_author(candidate_author: str, requested: list[str]) -> bool:
    # Token-wise: "etienne rochepault" must match "Rochepault, Etienne".
    return any(
        all(token in candidate_author for token in requested_author.split())
        for requested_author in requested
    )


def _initials_compatible(given: tuple[str, ...], known: tuple[str, ...]) -> bool:
    """Whether initials such as ("c", "c") can abbreviate known given names ("claire",).

    Names are compared position by position; a middle initial missing on one
    side does not rule the match out.
    """
    if not given or not known:
        return False
    return all(
        token == reference or (len(token) == 1 and reference.startswith(token))
        for token, reference in zip(given, known, strict=False)
    )


def _requested_author_variants(requested: list[str]) -> dict[str, set[tuple[str, ...]]]:
    """Given names, by surname, of the library authors the request names in full.

    Zotero often stores the same person as "Moreau, Claire" on one item and
    "Moreau, C." on another: a search for "claire" must find both.
    """
    variants: dict[str, set[tuple[str, ...]]] = {}
    for item in _iter_unique_items():
        for value in item.authors:
            if not _matches_requested_author(_normalize_author_lookup(value), requested):
                continue
            surname, given = _author_name_parts(value)
            if surname and any(len(token) > 1 for token in given):
                variants.setdefault(surname, set()).add(given)
    return variants


def find_library_articles_by_authors(
    authors: list[str],
    *,
    indexed_only: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Return local article metadata matching any requested author.

    This lookup intentionally avoids loading the RAG runtime: author-only chat
    requests are structured metadata queries and should not require embeddings
    or an LLM.  The boolean reports whether author metadata was available, so
    callers can fall back to the indexed RAG metadata when needed.
    """
    requested = [normalized for value in authors if (normalized := _normalize_author_lookup(value))]
    if not requested:
        return [], bool(_zotero_library)

    metadata_available = bool(_zotero_library)
    pdf_entries = filter_active_zotero_pdf_entries(
        find_pdfs(),
        _zotero_library,
        data_path=config.DATA_PATH,
        zotero_db_path=config.ZOTERO_DB_PATH,
    )
    indexed_paths_norm: set[str] = set()
    if indexed_only:
        indexed_paths_norm = {
            candidate
            for path in get_indexed_rel_paths()
            for candidate in _candidate_rel_paths(path)
        }

    variants = _requested_author_variants(requested)

    def _author_matches(value: str) -> bool:
        if _matches_requested_author(_normalize_author_lookup(value), requested):
            return True
        surname, given = _author_name_parts(value)
        return any(_initials_compatible(given, known) for known in variants.get(surname, ()))

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rel_path, full_path in pdf_entries:
        if indexed_only and not _candidate_rel_paths(rel_path).intersection(indexed_paths_norm):
            continue
        item = _lookup_zotero(rel_path, full_path)
        if item is None:
            continue
        metadata_available = metadata_available or bool(item.authors)
        if not any(_author_matches(value) for value in item.authors):
            continue

        stable_key = item.item_key or item.doi or _normalize_rel_path(rel_path)
        if stable_key in seen:
            continue
        seen.add(stable_key)
        records.append(
            {
                "source": rel_path,
                "title": item.title or os.path.basename(rel_path),
                "authors": list(item.authors),
                "year": item.year,
                "journal": item.journal,
                "abstract": item.abstract,
                "doi": item.doi,
                "tags": list(item.tags),
                "collections": list(item.collections),
                "citation": item.citation_str(),
                "article_id": item.item_key,
            }
        )

    records.sort(
        key=lambda record: (
            str(record.get("title") or "").casefold(),
            str(record.get("year") or ""),
            str(record.get("source") or "").casefold(),
        )
    )
    return records, metadata_available


def _preview_join(values: list[str], *, sep: str, limit: int) -> str:
    cleaned = [value.strip() for value in values if value and value.strip()]
    if not cleaned:
        return "-"
    if len(cleaned) <= limit:
        return sep.join(cleaned)
    shown = sep.join(cleaned[:limit])
    return f"{shown} (+{len(cleaned) - limit})"


def _format_library_date_added(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return match.group(1) if match else text


def _analysis_fingerprint(full_path: str) -> str:
    try:
        stat = os.stat(full_path)
    except OSError:
        return ""
    mtime_ns = getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000))
    return f"{int(stat.st_size)}:{int(mtime_ns)}"


def init_zotero(db_path: str = "") -> str:
    """Charge (ou recharge) la bibliotheque Zotero depuis db_path."""
    global _zotero_library, _zotero_db_path

    if not db_path:
        db_path = find_zotero_db() or ""

    if not db_path:
        _zotero_library = {}
        _zotero_db_path = ""
        set_zotero_path("")
        return "Zotero non trouvé - affichage des noms de fichiers."

    if not os.path.exists(db_path):
        _zotero_library = {}
        _zotero_db_path = ""
        set_zotero_path("")
        return f"Fichier introuvable : {db_path}"

    source_is_bibtex = is_bibtex_source(db_path)

    try:
        load_zotero_library.cache_clear()
        _zotero_library = load_zotero_library(db_path, zotero_drops.fingerprints())
        _zotero_db_path = db_path
        set_zotero_path(db_path)
        entry_count = len(
            {
                item.item_key or item.citation_key or f"id:{item.item_id}"
                for item in _zotero_library.values()
            }
        )
        if source_is_bibtex and is_metadata_snapshot(db_path):
            return f"Notices importees OK - {entry_count} entrees chargees."
        if source_is_bibtex:
            return f"BibTeX OK - {entry_count} entrees associees chargees."
        storage_path = suggest_zotero_pdf_folder(db_path)
        # Zotero's storage/ is only a default. A folder the user chose stays:
        # with ZotMoov or linked files the PDFs live elsewhere, and storage/
        # then holds only the leftovers (slides, videos) Zotero kept.
        if storage_path and not get_app_settings().get("data_path_configured"):
            set_data_path(storage_path)
            return (
                f"Zotero OK - {entry_count} entrees chargees. "
                f"Dossier PDF par defaut: {storage_path}"
            )
        return f"Zotero OK - {entry_count} entrees chargees."
    except Exception as exc:
        _logger.debug("Recovered from an error in init_zotero.", exc_info=True)
        _zotero_library = {}
        _zotero_db_path = ""
        set_zotero_path("")
        if source_is_bibtex:
            return f"Erreur BibTeX : {exc}"
        return f"Erreur Zotero : {exc}"


def _same_db_path(left: str, right: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(
            os.path.realpath(right)
        )
    except (OSError, ValueError):
        return False


def adopt_zotero_library(db_path: str, zotero_library: dict[str, ZoteroItem]) -> None:
    """Take the Zotero library an index reload just loaded.

    The indexer reloads Zotero on every run; this copy only followed a forced
    reload, so a PDF added to Zotero since was indexed but filtered out of the
    Library tab. A library from another database is ignored: switching sources
    goes through ``init_zotero``.
    """
    global _zotero_library, _zotero_db_path
    if not db_path:
        return
    if _zotero_db_path and not _same_db_path(db_path, _zotero_db_path):
        return
    _zotero_library = dict(zotero_library)
    _zotero_db_path = _zotero_db_path or db_path


def clear_metadata_library() -> str:
    """Disconnect Zotero/BibTeX without triggering automatic detection."""
    global _zotero_library, _zotero_db_path
    _zotero_library = {}
    _zotero_db_path = ""
    set_zotero_path("")
    return "Zotero non utilise - metadonnees des documents extraites localement."


def _lookup_zotero(
    rel_path: str,
    full_path: str = "",
    *,
    enrich_external: bool = False,
) -> ZoteroItem | None:
    if not full_path:
        resolved = resolve_document_path(config.DATA_PATH, rel_path)
        full_path = str(resolved) if resolved is not None else ""
    return resolve_document_metadata(
        rel_path,
        full_path,
        _zotero_library,
        allow_external_enrichment=enrich_external,
    )


def _normalize_collection_path(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip().lower())
    text = re.sub(r"\s*/\s*", " / ", text)
    return text.strip()


def _collection_path_matches(
    collection_path: str,
    requested_path: str,
    *,
    include_descendants: bool = True,
) -> bool:
    candidate = _normalize_collection_path(collection_path)
    requested = _normalize_collection_path(requested_path)
    if not candidate or not requested:
        return False
    if candidate == requested:
        return True
    return include_descendants and candidate.startswith(f"{requested} / ")


def list_zotero_collection_pdfs(
    collection_path: str,
    *,
    include_descendants: bool = True,
) -> list[dict[str, Any]]:
    """Retourne les PDF locaux rattaches a une collection Zotero."""
    requested = str(collection_path or "").strip()
    if not requested or not _zotero_library:
        return []

    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rel_path, full_path in find_pdfs():
        item = _lookup_zotero(rel_path, full_path)
        if not item:
            continue
        if not any(
            _collection_path_matches(
                path,
                requested,
                include_descendants=include_descendants,
            )
            for path in item.collections
        ):
            continue
        stable_id = item.item_key or item.citation_key or f"id:{item.item_id}" or rel_path
        if stable_id in seen:
            continue
        seen.add(stable_id)
        records.append(
            {
                "path": rel_path,
                "full_path": full_path,
                "title": item.title or os.path.splitext(os.path.basename(rel_path))[0],
                "item": item,
            }
        )

    records.sort(key=lambda row: str(row.get("title") or row.get("path") or "").lower())
    return records


def get_zotero_facets() -> dict:
    """Retourne tags + collections (avec profondeur) pour pilotage UI/filtrage."""
    if not _zotero_library:
        return {"tags": [], "collections": [], "stats": {"items": 0, "tags": 0, "collections": 0}}

    tag_counts: dict[str, int] = {}
    collection_counts: dict[str, int] = {}
    items = _iter_unique_items()

    for item in items:
        for tag in item.tags:
            clean = tag.strip()
            if clean:
                tag_counts[clean] = tag_counts.get(clean, 0) + 1
        for collection_path in item.collections:
            clean = collection_path.strip()
            if clean:
                collection_counts[clean] = collection_counts.get(clean, 0) + 1

    tags = [
        {"name": name, "count": count}
        for name, count in sorted(tag_counts.items(), key=lambda it: it[0].lower())
    ]
    collections = [
        {
            "path": path,
            "count": count,
            "depth": path.count(" / "),
        }
        for path, count in sorted(collection_counts.items(), key=lambda it: it[0].lower())
    ]

    return {
        "tags": tags,
        "collections": collections,
        "stats": {
            "items": len(items),
            "tags": len(tags),
            "collections": len(collections),
        },
    }


def get_library_filter_metadata(paths: list[str]) -> list[dict[str, Any]]:
    """Return unabridged metadata used by client-side library filters.

    Keep this separate from ``list_library`` rows: those rows are a positional,
    display-oriented API and collection previews are intentionally truncated.
    """
    metadata: list[dict[str, Any]] = []
    for rel_path in paths:
        item = _lookup_zotero(str(rel_path or ""))
        collections = []
        if item:
            collections = [
                str(path).strip()
                for path in item.collections
                if str(path).strip()
            ]
        metadata.append({"collections": collections})
    return metadata


@overload
def list_library(
    indexed_only: bool = False,
    *,
    include_stats: Literal[False] = False,
) -> tuple[list[list], list[str]]: ...


@overload
def list_library(
    indexed_only: bool = False,
    *,
    include_stats: Literal[True],
) -> tuple[list[list], list[str], dict]: ...


def list_library(
    indexed_only: bool = False,
    *,
    include_stats: bool = False,
) -> tuple[list[list], list[str]] | tuple[list[list], list[str], dict]:
    """
    Scanne DATA_PATH recursivement (structure Zotero).
    Retourne (lignes pour Dataframe/API, chemins relatifs).
    """
    rows: list[list] = []
    names: list[str] = []
    pdf_entries = filter_active_zotero_pdf_entries(
        find_pdfs(),
        _zotero_library,
        data_path=config.DATA_PATH,
        zotero_db_path=config.ZOTERO_DB_PATH,
    )
    indexed_paths_raw: set[str] = set()
    indexed_paths_norm: set[str] = set()

    if indexed_only:
        indexed_paths_raw = get_indexed_rel_paths()
        indexed_paths_norm = {
            candidate for path in indexed_paths_raw for candidate in _candidate_rel_paths(path)
        }

    analysis_types = list_analysis_types()
    path_fingerprints: dict[str, str | dict[str, str]] = {
        _normalize_rel_path(rel_path): {
            analysis_type: _analysis_cache_fingerprint(
                _analysis_fingerprint(full_path),
                analysis_type,
            )
            for analysis_type in analysis_types
        }
        for rel_path, full_path in pdf_entries
    }
    cached_types_by_path = get_cached_analysis_types_for_paths(path_fingerprints)

    for rel_path, full_path in pdf_entries:
        if indexed_only:
            rel_candidates = _candidate_rel_paths(rel_path)
            if not rel_candidates.intersection(indexed_paths_norm):
                continue

        size_mb = round(os.path.getsize(full_path) / 1_048_576, 2)
        norm_rel_path = _normalize_rel_path(rel_path)
        cached_types = cached_types_by_path.get(norm_rel_path, set())
        cached = [analysis for analysis in analysis_types if analysis in cached_types]
        cached_str = ", ".join(cached) if cached else "-"

        item = _lookup_zotero(rel_path, full_path)
        if item and item.title:
            title = item.title
            author = item.first_author() or "-"
            all_authors = "; ".join(a for a in item.authors if a.strip()) or "-"
            year = item.year or "-"
            item_type = item.item_type or "-"
            date_added = _format_library_date_added(item.date_added)
            tags = _preview_join(item.tags, sep=", ", limit=3)
            collections = _preview_join(item.collections, sep=" | ", limit=2)
        else:
            title = os.path.basename(rel_path)
            author = "-"
            all_authors = "-"
            year = "-"
            item_type = "-"
            date_added = "-"
            tags = "-"
            collections = "-"

        rows.append(
            [
                title,
                author,
                year,
                item_type,
                tags,
                collections,
                f"{size_mb} Mo",
                cached_str,
                date_added,
                all_authors,
            ]
        )
        names.append(rel_path)

    if include_stats:
        stats_indexed_paths = indexed_paths_raw if indexed_only else get_indexed_rel_paths()
        return (
            rows,
            names,
            get_library_stats(
                pdf_entries=pdf_entries,
                indexed_paths_raw=stats_indexed_paths,
            ),
        )

    return rows, names


def get_library_stats(
    pdf_entries: list[tuple[str, str]] | None = None,
    indexed_paths_raw: set[str] | None = None,
) -> dict:
    pdf_entries = (
        pdf_entries
        if pdf_entries is not None
        else filter_active_zotero_pdf_entries(
            find_pdfs(),
            _zotero_library,
            data_path=config.DATA_PATH,
            zotero_db_path=config.ZOTERO_DB_PATH,
        )
    )
    total_pdf_count = len(pdf_entries)

    indexed_paths_raw = (
        indexed_paths_raw if indexed_paths_raw is not None else get_indexed_rel_paths()
    )
    indexed_paths_norm = {
        candidate for path in indexed_paths_raw for candidate in _candidate_rel_paths(path)
    }

    indexed_pdf_count = 0
    for rel_path, _full_path in pdf_entries:
        rel_candidates = _candidate_rel_paths(rel_path)
        if rel_candidates.intersection(indexed_paths_norm):
            indexed_pdf_count += 1

    facets = get_zotero_facets()
    facet_stats = facets.get("stats", {}) if isinstance(facets, dict) else {}

    return {
        "total_pdf_count": total_pdf_count,
        "indexed_pdf_count": indexed_pdf_count,
        "total_document_count": total_pdf_count,
        "indexed_document_count": indexed_pdf_count,
        "indexed_manifest_count": len(indexed_paths_raw),
        "zotero_item_count": int(facet_stats.get("items", 0) or 0),
        "zotero_tag_count": int(facet_stats.get("tags", 0) or 0),
        "zotero_collection_count": int(facet_stats.get("collections", 0) or 0),
    }


def get_item_detail(rel_path: str) -> str:
    """Retourne les metadonnees bibliographiques formatees en Markdown pour un article."""
    item = _lookup_zotero(rel_path, enrich_external=True)
    if item:
        return item.as_markdown()
    return f"_Aucune metadonnee bibliographique pour_ `{os.path.basename(rel_path)}`"


def get_item_citations(rel_path: str) -> dict[str, Any]:
    item = _lookup_zotero(rel_path, enrich_external=True)
    if not item:
        basename = os.path.basename(rel_path)
        return {
            "found": False,
            "title": basename,
            "citation_key": "",
            "apa": f"{basename}.",
            "bibtex": f"@misc{{{re.sub(r'[^A-Za-z0-9]+', '', basename) or 'item'},\n  title = {{{basename}}}\n}}",
            "zotero_select_uri": "",
        }

    zotero_select_uri = ""
    if item.item_key:
        if (item.library_type or "").lower() == "group" and item.group_id:
            zotero_select_uri = f"zotero://select/groups/{item.group_id}/items/{item.item_key}"
        else:
            zotero_select_uri = f"zotero://select/library/items/{item.item_key}"

    return {
        "found": True,
        "title": item.title or os.path.basename(rel_path),
        "citation_key": item.citation_key_or_fallback(),
        "apa": item.apa_citation(),
        "bibtex": item.bibtex_entry(),
        "zotero_select_uri": zotero_select_uri,
        "citation_count": int(item.citation_count or 0),
        "influential_citation_count": int(item.influential_citation_count or 0),
    }


def _export_item_identity(item: ZoteroItem | None, rel_path: str) -> str:
    if item:
        metadata_identity = "|".join([item.title, item.year, *item.authors])
        for value in (
            item.doi,
            metadata_identity if item.title else "",
            item.citation_key,
            item.item_key,
        ):
            normalized = str(value or "").strip().lower()
            if normalized:
                return f"zotero:{normalized}"
    return f"path:{_normalize_rel_path(rel_path).lower()}"


def _fallback_export_item(row: list[Any], rel_path: str) -> ZoteroItem:
    author = str(row[1] if len(row) > 1 else "").strip()
    year = str(row[2] if len(row) > 2 else "").strip()
    item_type = str(row[3] if len(row) > 3 else "").strip()
    all_authors = str(row[9] if len(row) > 9 else "").strip()
    if all_authors and all_authors != "-":
        authors = [name.strip() for name in all_authors.split(";") if name.strip()]
    else:
        authors = [author] if author and author != "-" else []
    return ZoteroItem(
        title=str(row[0] if row else os.path.basename(rel_path)).strip(),
        authors=authors,
        year=year if year != "-" else "",
        item_type=item_type if item_type != "-" else "misc",
        citation_key=re.sub(
            r"[^A-Za-z0-9:_\-.]+",
            "",
            os.path.splitext(os.path.basename(rel_path))[0],
        ),
    )


def _unique_bibtex_entry(entry: str, used_keys: set[str]) -> str:
    match = re.match(r"^(@[A-Za-z]+\{)([^,]+)", entry)
    if not match:
        return entry

    base_key = match.group(2).strip() or "item"
    key = base_key
    suffix = 2
    while key.lower() in used_keys:
        key = f"{base_key}{suffix}"
        suffix += 1
    used_keys.add(key.lower())
    if key == base_key:
        return entry
    return f"{match.group(1)}{key}{entry[match.end():]}"


def build_library_export(
    export_format: Literal["markdown", "bibtex"],
    rel_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Build an export for all indexed papers or an explicit visible subset."""
    rows, paths = list_library(indexed_only=True)
    row_by_path = {path: rows[index] for index, path in enumerate(paths)}
    selected_paths = (
        paths
        if rel_paths is None
        else [path for path in dict.fromkeys(rel_paths) if path in row_by_path]
    )

    records: list[tuple[ZoteroItem, str]] = []
    seen_items: set[str] = set()
    for rel_path in selected_paths:
        item = _lookup_zotero(rel_path, enrich_external=True)
        identity = _export_item_identity(item, rel_path)
        if identity in seen_items:
            continue
        seen_items.add(identity)
        records.append((item or _fallback_export_item(row_by_path[rel_path], rel_path), rel_path))

    if export_format == "bibtex":
        used_keys: set[str] = set()
        content = "\n\n".join(
            _unique_bibtex_entry(item.bibtex_entry(), used_keys) for item, _rel_path in records
        )
        if content:
            content += "\n"
        return {
            "content": content,
            "count": len(records),
            "filename": "bibliobrain-articles.bib",
            "media_type": "application/x-bibtex; charset=utf-8",
        }

    lines = ["# Bibliobrain - Articles", "", f"Nombre d'articles : {len(records)}"]
    for item, rel_path in records:
        lines.extend(["", f"## {item.title or os.path.basename(rel_path)}"])
        if item.authors:
            lines.append(f"- **Auteurs :** {', '.join(item.authors)}")
        if item.year:
            lines.append(f"- **Année :** {item.year}")
        if item.journal:
            lines.append(f"- **Publication :** {item.journal}")
        if item.doi:
            lines.append(f"- **DOI :** https://doi.org/{item.doi}")
        lines.append(f"- **Clé de citation :** `{item.citation_key_or_fallback()}`")
        if item.tags:
            lines.append(f"- **Tags :** {', '.join(item.tags)}")
        if item.collections:
            lines.append(f"- **Collections :** {' | '.join(item.collections)}")
        lines.append(f"- **Document :** `{rel_path}`")

    return {
        "content": "\n".join(lines).rstrip() + "\n",
        "count": len(records),
        "filename": "bibliobrain-articles.md",
        "media_type": "text/markdown; charset=utf-8",
    }


def get_cached_summary(rel_path: str, analysis_type: str) -> str:
    """Retourne une analyse deja mise en cache pour un article/type, si disponible."""
    if not rel_path or not analysis_type:
        return ""

    full_path = os.path.join(config.DATA_PATH, rel_path)
    if not os.path.exists(full_path):
        return ""

    ui_language = str(get_app_settings().get("ui_language") or "").strip().lower()
    if ui_language not in ("fr", "en"):
        ui_language = "fr"

    fingerprint = _analysis_cache_fingerprint(
        _analysis_fingerprint(full_path),
        analysis_type,
        ui_language,
    )
    cached = clean_chat_answer_markdown(
        get_cached_analysis(rel_path, analysis_type, fingerprint) or ""
    )
    return "" if _analysis_result_looks_malformed(cached, analysis_type) else cached


def generate_summary(
    rel_path: str,
    analysis_type: str,
    model_name: str,
    thinking_enabled: bool = False,
) -> str:
    """Genere (ou recupere depuis le cache) une analyse via le LLM."""
    _ui_lang = str(get_app_settings().get("ui_language") or "").strip().lower()
    if _ui_lang not in ("fr", "en"):
        _ui_lang = "fr"

    if not rel_path:
        if _ui_lang == "en":
            return "_Select an article from the list._"
        return "_Sélectionnez un article dans la liste._"

    resolved_path = resolve_document_path(config.DATA_PATH, rel_path)
    if resolved_path is None:
        if _ui_lang == "en":
            return "File not found or outside the configured library."
        return "Fichier introuvable ou hors de la bibliothèque configurée."
    full_path = str(resolved_path)

    fingerprint = _analysis_cache_fingerprint(
        _analysis_fingerprint(full_path),
        analysis_type,
        _ui_lang,
    )
    cached = get_cached_analysis(rel_path, analysis_type, fingerprint)
    if cached:
        cleaned_cached = clean_chat_answer_markdown(cached)
        if not _analysis_result_looks_malformed(cleaned_cached, analysis_type):
            return cleaned_cached

    try:
        units = load_document_units(full_path)
        pages = [
            Document(
                page_content=unit.text,
                metadata={
                    "page": unit.page,
                    "content_type": unit.content_type,
                    "location_label": unit.location_label,
                },
            )
            for unit in units
        ]
        text = _build_analysis_document_text(
            pages,
            max_chars=_ANALYSIS_DOCUMENT_CHAR_LIMITS.get(
                analysis_type,
                _ANALYSIS_DOCUMENT_CHAR_BUDGET,
            ),
        )
    except Exception as exc:
        _logger.debug("Recovered from an error in generate_summary.", exc_info=True)
        if _ui_lang == "en":
            return f"Error reading document: {exc}"
        return f"Erreur de lecture du document : {exc}"

    prompt = get_analysis_prompt(analysis_type, language=_ui_lang)
    try:
        final_instruction = _analysis_final_instruction(analysis_type, _ui_lang)

        def format_analysis_prompt(document_text: str) -> str:
            return (
                prompt.format(text=f"<ARTICLE_SOURCE>\n{document_text}\n</ARTICLE_SOURCE>")
                + f"\n\n# CONSIGNE FINALE\n{final_instruction}"
            )

        formatted_prompt = format_analysis_prompt(text)
        # Analysis prompts already prescribe the final Markdown shape. Forcing
        # the whole answer through ChatAnswer JSON makes some local models echo
        # the document into a field until the structured-output token ceiling
        # is reached. Generate the requested prose directly instead.
        llm = create_chat_llm(
            model_name,
            max_output_tokens=_ANALYSIS_OUTPUT_TOKEN_LIMITS.get(analysis_type, 2048),
            thinking_enabled=thinking_enabled,
        )
        raw_result = invoke_text(llm, formatted_prompt)
        result = clean_chat_answer_markdown(
            sanitize_assistant_content(raw_result, thinking_enabled=False)
        )
        if _analysis_result_looks_malformed(result, analysis_type):
            compact_text = _build_analysis_document_text(pages, max_chars=8000)
            retry_instruction = (
                "The previous attempt did not follow the task. Return only the requested "
                "analysis, without bibliography or metadata."
                if _ui_lang == "en"
                else "La tentative précédente n'a pas respecté la tâche. Produis uniquement "
                "l'analyse demandée, sans bibliographie ni métadonnées."
            )
            retry_prompt = format_analysis_prompt(compact_text) + f"\n\n{retry_instruction}"
            if thinking_enabled:
                # Some models (granite4.2) reason inline even when Ollama is
                # asked to think: retry with reasoning off so the answer is clean.
                llm = create_chat_llm(
                    model_name,
                    max_output_tokens=_ANALYSIS_OUTPUT_TOKEN_LIMITS.get(analysis_type, 2048),
                    thinking_enabled=False,
                )
            raw_result = invoke_text(llm, retry_prompt)
            result = clean_chat_answer_markdown(
                sanitize_assistant_content(raw_result, thinking_enabled=False)
            )
        if _analysis_result_looks_malformed(result, analysis_type):
            raise RuntimeError("le modèle n'a pas produit une analyse exploitable")
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "generate_summary failed for %s / %s", rel_path, analysis_type
        )
        if _ui_lang == "en":
            raise RuntimeError(f"Analysis generation failed: {exc}") from exc
        raise RuntimeError(f"Échec de la génération de l'analyse : {exc}") from exc

    set_cached_analysis(
        rel_path=rel_path,
        analysis_type=analysis_type,
        content=result,
        fingerprint=fingerprint,
        model_name=model_name,
    )
    return result


def _zotero_select_uri(item: ZoteroItem) -> str:
    if not item.item_key:
        return ""
    if (item.library_type or "").lower() == "group" and item.group_id:
        return f"zotero://select/groups/{item.group_id}/items/{item.item_key}"
    return f"zotero://select/library/items/{item.item_key}"


def list_metadata_issues() -> list[dict[str, Any]]:
    """Zotero records whose title, year or authors look wrong.

    Only records backed by a Zotero item are listed: a loose PDF has nothing
    to correct in Zotero. Broken titles come first, then missing years/authors.
    """
    from src.metadata_quality import metadata_issues

    pdf_entries = filter_active_zotero_pdf_entries(
        find_pdfs(),
        _zotero_library,
        data_path=config.DATA_PATH,
        zotero_db_path=config.ZOTERO_DB_PATH,
    )
    found: list[dict[str, Any]] = []
    seen_items: set[str] = set()
    for rel_path, full_path in pdf_entries:
        item = _lookup_zotero(rel_path, full_path)
        if item is None or not item.item_key or item.item_key in seen_items:
            continue
        issues = metadata_issues(item, rel_path)
        if not issues:
            continue
        seen_items.add(item.item_key)
        found.append(
            {
                "rel_path": rel_path,
                "title": item.title or os.path.basename(rel_path),
                "year": item.year,
                "authors": list(item.authors[:3]),
                "issues": issues,
                "zotero_select_uri": _zotero_select_uri(item),
            }
        )
    title_first = {"titre manquant", "le titre ressemble à une page de garde",
                   "le titre reprend le nom du fichier", "titre très court"}
    found.sort(key=lambda row: (not (title_first & set(row["issues"])), row["title"].lower()))
    return found


def propose_metadata_correction(rel_path: str) -> dict[str, Any]:
    from src.metadata_quality import propose_metadata_fix

    resolved = resolve_document_path(config.DATA_PATH, rel_path)
    full_path = str(resolved) if resolved is not None else ""
    return propose_metadata_fix(_lookup_zotero(rel_path, full_path), full_path)


def _file_fingerprint(full_path: str) -> str:
    """Size plus a hash of the first 256 KB: same PDF, whatever its name."""
    import hashlib

    try:
        size = os.path.getsize(full_path)
        with open(full_path, "rb") as handle:
            head = handle.read(256 * 1024)
    except OSError:
        return ""
    return f"{size}:{hashlib.sha1(head).hexdigest()}"


def _title_key(title: str, year: str) -> str:
    normalized = unicodedata.normalize("NFKD", title or "")
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    words = re.findall(r"[a-z0-9]+", normalized.casefold())
    return f"{' '.join(words)}|{year or ''}" if len(words) >= 3 else ""


def list_duplicate_documents() -> list[dict[str, Any]]:
    """Groups of library documents that are probably the same work.

    Duplicates fill several retrieval slots with one passage and inflate the
    index. Matching uses the DOI, then title + year, then identical file
    content; each document appears in at most one group.
    """
    pdf_entries = filter_active_zotero_pdf_entries(
        find_pdfs(),
        _zotero_library,
        data_path=config.DATA_PATH,
        zotero_db_path=config.ZOTERO_DB_PATH,
    )
    records: list[dict[str, Any]] = []
    for rel_path, full_path in pdf_entries:
        item = _lookup_zotero(rel_path, full_path)
        title = (item.title if item else "") or os.path.basename(rel_path)
        year = (item.year if item else "") or ""
        records.append(
            {
                "rel_path": rel_path,
                "title": title,
                "year": year,
                "zotero_select_uri": _zotero_select_uri(item) if item else "",
                "_item_key": item.item_key if item else "",
                "_keys": [
                    ("même DOI", f"doi:{item.doi.strip().lower()}") if item and item.doi else None,
                    ("même titre et même année", _title_key(title, year)),
                    ("fichier PDF identique", _file_fingerprint(full_path)),
                ],
            }
        )
    groups: list[dict[str, Any]] = []
    grouped: set[str] = set()
    for reason_index in range(3):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for record in records:
            if record["rel_path"] in grouped:
                continue
            entry = record["_keys"][reason_index]
            if entry and entry[1]:
                buckets.setdefault(entry[1], []).append(record)
        for members in buckets.values():
            if len(members) < 2:
                continue
            reason = members[0]["_keys"][reason_index][0]
            item_keys = {member["_item_key"] for member in members}
            # One record with several PDF attachments (re-downloaded or
            # annotated versions) is fixed differently from separate records.
            same_record = len(item_keys) == 1 and "" not in item_keys
            grouped.update(member["rel_path"] for member in members)
            groups.append(
                {
                    "reason": (
                        f"{len(members)} PDF joints à la même notice Zotero"
                        if same_record
                        else f"{len(members)} notices Zotero distinctes ({reason})"
                    ),
                    "same_record": same_record,
                    "items": [
                        {key: value for key, value in member.items() if not key.startswith("_")}
                        for member in members
                    ],
                }
            )
    groups.sort(key=lambda group: (-len(group["items"]), group["items"][0]["title"].lower()))
    return groups
