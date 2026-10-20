"""Graphs HTTP routes and feature-specific helpers."""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.api.common import (
    ExternalApiAllowed,
    api_error,
    research_scope_enabled,
)
from src.api_models import (
    ArticleTaxonomyRegenerateRequest,
    BibliographicSearchGraphRequest,
    CitationEntitySearchRequest,
    CitationScopeGraphRequest,
)
from src.library_taxonomy import build_article_taxonomy_payload, clear_article_taxonomy_cache

_logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/knowledge-graph")
async def api_knowledge_graph(
    mode: str = "hybrid",
    max_nodes: int = 90,
    min_score: float = 0.22,
    tags: str = "",
    collections: str = "",
    engine: str = "auto",
):
    """Deterministic article taxonomy for the Connections overview.

    Legacy query parameters remain accepted for bookmarked URLs.
    """
    try:
        return await asyncio.to_thread(build_article_taxonomy_payload, model_name="")
    except Exception:
        _logger.exception("Knowledge graph construction failed")
        return api_error(
            "knowledge_graph_unavailable",
            status_code=503,
            message="Graphe des articles indisponible.",
            extra={
                "engine": "rag_article_taxonomy",
                "graph_type": "article_taxonomy",
                "nodes": [],
                "edges": [],
            },
        )


def _citation_graph_error(
    code: str,
    detail: str,
    *,
    status_code: int,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    payload_extra: dict[str, Any] = {
        "nodes": [],
        "edges": [],
        "missing_links": [],
        "stats": {},
    }
    if extra:
        payload_extra.update(extra)
    return api_error(
        code,
        status_code=status_code,
        message=detail,
        extra=payload_extra,
    )


@router.get("/api/notebooks/{nb_id}/citation-graph")
async def api_notebook_citation_graph(nb_id: str, *, external_api_allowed: ExternalApiAllowed):
    if not external_api_allowed:
        return _citation_graph_error(
            "external_api_permission_required",
            "Autorisation des requêtes API externes requise.",
            status_code=403,
        )
    from src.citation_graph import build_notebook_citation_graph

    try:
        return await build_notebook_citation_graph(nb_id)
    except Exception:
        _logger.exception("Notebook citation graph construction failed")
        return _citation_graph_error(
            "citation_graph_unavailable",
            "Construction du graphe de citations indisponible.",
            status_code=502,
        )


@router.get("/api/library/citation-graph")
async def api_library_citation_graph(path: str, *, external_api_allowed: ExternalApiAllowed):
    if not external_api_allowed:
        return _citation_graph_error(
            "external_api_permission_required",
            "Autorisation des requêtes API externes requise.",
            status_code=403,
        )
    from src.citation_graph import build_paper_citation_graph

    try:
        return await build_paper_citation_graph(path)
    except Exception:
        _logger.exception("Paper citation graph construction failed")
        return _citation_graph_error(
            "citation_graph_unavailable",
            "Construction du graphe de citations indisponible.",
            status_code=502,
        )


@router.get("/api/library/internal-citation-graph")
async def api_library_internal_citation_graph(
    refresh: bool = False,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    if not external_api_allowed:
        return _citation_graph_error(
            "external_api_permission_required",
            "Autorisation des requêtes API externes requise.",
            status_code=403,
            extra={
                "engine": "openalex_internal",
                "graph_type": "library_citations",
                "warning": "Autorisation des requêtes API externes requise.",
            },
        )
    from src.citation_graph import build_library_internal_citation_graph

    try:
        return await build_library_internal_citation_graph(force_refresh=refresh)
    except Exception:
        _logger.exception("Internal citation graph construction failed")
        return _citation_graph_error(
            "internal_citation_graph_unavailable",
            "Construction du graphe de citations internes indisponible.",
            status_code=502,
            extra={
                "engine": "openalex_internal",
                "graph_type": "library_citations",
                "warning": "Construction du graphe de citations internes indisponible.",
            },
        )


@router.get("/api/citation-graph/expand")
async def api_expand_citation_graph(
    openalex_id: str,
    limit: int = 18,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    if not external_api_allowed:
        return _citation_graph_error(
            "external_api_permission_required",
            "Autorisation des requêtes API externes requise.",
            status_code=403,
        )
    from src.citation_graph import expand_citation_graph_work

    try:
        return await expand_citation_graph_work(openalex_id, limit=limit)
    except Exception:
        _logger.exception("Citation graph expansion failed")
        return _citation_graph_error(
            "citation_graph_expansion_unavailable",
            "Développement du graphe de citations indisponible.",
            status_code=502,
        )


@router.post("/api/citation-graph/entities")
async def api_search_citation_graph_entities(
    req: CitationEntitySearchRequest,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    if not research_scope_enabled():
        return _citation_graph_error(
            "experimental_research_scope_disabled",
            "Fonction expérimentale auteur/laboratoire désactivée dans les réglages.",
            status_code=403,
        )
    if not external_api_allowed:
        return _citation_graph_error(
            "external_api_permission_required",
            "Autorisation des requêtes API externes requise.",
            status_code=403,
        )
    from src.citation_graph import search_research_entities

    try:
        return await search_research_entities(
            req.entity_type,
            req.query,
            limit=req.limit,
        )
    except ValueError as exc:
        return _citation_graph_error(
            "invalid_research_entity_search",
            str(exc),
            status_code=400,
        )
    except Exception:
        # Deliberately omit exc_info: HTTP client exceptions may embed the full
        # OpenAlex URL, including the searched name.
        _logger.error("OpenAlex research entity search failed")
        return _citation_graph_error(
            "research_entity_search_unavailable",
            "Recherche d'auteurs ou d'institutions indisponible.",
            status_code=502,
        )


@router.post("/api/citation-graph/scope")
async def api_research_scope_citation_graph(
    req: CitationScopeGraphRequest,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    if not research_scope_enabled():
        return _citation_graph_error(
            "experimental_research_scope_disabled",
            "Fonction expérimentale auteur/laboratoire désactivée dans les réglages.",
            status_code=403,
        )
    if not external_api_allowed:
        return _citation_graph_error(
            "external_api_permission_required",
            "Autorisation des requêtes API externes requise.",
            status_code=403,
        )
    from src.citation_graph import build_research_scope_citation_graph

    try:
        return await build_research_scope_citation_graph(
            req.entity_type,
            req.openalex_id,
            from_year=req.from_year,
            to_year=req.to_year,
            max_works=req.max_works,
            strategy=req.strategy,
        )
    except ValueError as exc:
        return _citation_graph_error(
            "invalid_research_scope",
            str(exc),
            status_code=400,
        )
    except Exception:
        # OpenAlex identifiers can identify a person; keep them out of logs too.
        _logger.error("Research scope citation graph construction failed")
        return _citation_graph_error(
            "research_scope_citation_graph_unavailable",
            "Construction du graphe de l'auteur ou de l'institution indisponible.",
            status_code=502,
        )


@router.post("/api/citation-graph/search")
async def api_bibliographic_search_citation_graph(
    req: BibliographicSearchGraphRequest,
    *,
    external_api_allowed: ExternalApiAllowed,
):
    if not external_api_allowed:
        return _citation_graph_error(
            "external_api_permission_required",
            "Autorisation des requêtes API externes requise.",
            status_code=403,
        )
    from src.citation_graph import build_bibliographic_search_citation_graph

    try:
        return await build_bibliographic_search_citation_graph(
            req.query,
            from_year=req.from_year,
            to_year=req.to_year,
            max_works=req.max_works,
            strategy=req.strategy,
        )
    except ValueError as exc:
        return _citation_graph_error(
            "invalid_bibliographic_search",
            str(exc),
            status_code=400,
        )
    except Exception:
        _logger.exception("Bibliographic citation graph construction failed")
        return _citation_graph_error(
            "bibliographic_search_unavailable",
            "Construction du graphe bibliographique indisponible.",
            status_code=502,
        )


@router.delete("/api/citation-graph/search-cache")
def api_bibliographic_search_cache_clear():
    from src.citation_graph import clear_bibliographic_search_cache

    return {
        "ok": True,
        "cleared_entries": clear_bibliographic_search_cache(),
    }


@router.post("/api/article-taxonomy/regenerate")
def api_article_taxonomy_regenerate(req: ArticleTaxonomyRegenerateRequest):
    # LLM classification is opt-in through an explicit model in the request;
    # the normal UI sends none and performs a metadata-only rebuild.
    model_name = str(req.model or "").strip()
    return build_article_taxonomy_payload(force_rebuild=True, model_name=model_name)


@router.delete("/api/article-taxonomy/cache")
def api_article_taxonomy_cache_clear():
    clear_article_taxonomy_cache()
    return {"ok": True}
