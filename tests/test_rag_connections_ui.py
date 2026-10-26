from __future__ import annotations

from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = read_app_javascript()
GRAPH_JS = (ROOT / "static" / "js" / "knowledge-graph.js").read_text(encoding="utf-8")
GRAPH_CSS = (ROOT / "static" / "css" / "knowledge-graph.css").read_text(encoding="utf-8")


def test_cartography_ui_defaults_to_article_graph_and_keeps_secondary_views() -> None:
    assert 'id="kg-view-mode"' in HTML
    assert '<option value="articles" selected' in HTML
    assert 'data-cartography-view="articles"' in HTML
    assert 'data-cartography-view="research"' in HTML
    assert 'data-cartography-view="retrieval"' in HTML
    assert 'data-cartography-view="library"' in HTML
    # "Qui cite qui" (citations between library papers) is gone.
    assert 'data-cartography-view="citations"' not in HTML
    assert "/api/library/internal-citation-graph" not in GRAPH_JS
    assert 'id="kg-engine-skip-wiki"' not in HTML
    assert 'data-i18n="nav.connections">Cartographies</span>' in HTML
    assert "citation-has-focus" in GRAPH_JS
    assert "classList.contains('citation-network')" in GRAPH_JS
    assert "svg.style.width = '100%'" in GRAPH_JS
    assert "svg.style.height = '100%'" in GRAPH_JS
    assert "svg.style.removeProperty(property)" in GRAPH_JS
    assert "_buildKnowledgeLayout(nodes, edges)" in GRAPH_JS
    assert "_assignKnowledgeComponents" in GRAPH_JS
    assert "_fitKnowledgeGraphView" in GRAPH_JS
    assert "Math.min(8" in GRAPH_JS
    assert "kg-highlight-active" in GRAPH_JS
    assert "svg.setAttribute('viewBox'" in GRAPH_JS
    assert "xMidYMid meet" in GRAPH_JS
    assert 'id="kg-component-filter"' in HTML
    assert 'id="kg-network-controls"' in HTML
    assert 'id="kg-network-breadcrumb"' in HTML
    assert 'class="kg-header-main"' in HTML
    assert 'class="kg-context-controls"' in HTML
    assert 'class="kg-action-controls"' in HTML
    assert ".cartography-view-card.is-priority" in GRAPH_CSS
    assert "@container main-content (max-width: 980px)" in GRAPH_CSS
    assert "function setCartographyView" in GRAPH_JS
    assert "function _renderCitationCommunityOverview" in GRAPH_JS
    assert "function openKnowledgeCommunity" in GRAPH_JS
    assert "function showCitationCommunityOverview" in GRAPH_JS
    assert "communityLayouts: new Map()" in GRAPH_JS
    assert ".kg-community-node" in GRAPH_CSS
    assert ".kg-community-edge" in GRAPH_CSS
    assert ".kg-isolated-summary" in GRAPH_CSS
    assert "kg-highlight-active" in GRAPH_CSS
    assert "citation-network:not(.citation-has-focus) #knowledge-graph-detail" in GRAPH_CSS
    assert "#knowledge-graph-wrap.citation-network #kg-article-list" in GRAPH_CSS
    assert "#knowledge-graph-wrap.citation-network #kg-concept-summary" in GRAPH_CSS


def test_connections_article_list_fills_available_detail_height() -> None:
    # The base rule, not the citation-network or media-query overrides.
    article_list_css = GRAPH_CSS[GRAPH_CSS.index("\n#kg-article-list {") :]
    article_list_css = article_list_css[: article_list_css.index("}")]

    assert "flex: 1 1 auto" in article_list_css
    assert "min-height: 0" in article_list_css
    assert "max-height: none" in article_list_css
    assert "max-height: min(42vh, 360px)" not in article_list_css


def test_short_desktop_connections_prioritize_the_graph_canvas() -> None:
    stacked_start = GRAPH_CSS.index("@container main-content (max-width: 900px)")
    stacked = GRAPH_CSS[
        stacked_start : GRAPH_CSS.index("@media (max-width: 760px)", stacked_start)
    ]
    compact = stacked[stacked.index("@media (min-width: 761px) and (max-height: 820px)") :]

    assert "citation-network:not(.citation-has-focus)" in stacked
    assert "grid-template-rows: minmax(0, 1fr)" in stacked
    assert "#cartography-description" in compact
    assert "display: none" in compact
    assert "min-height: 40px" in compact
    assert "#knowledge-graph-stage" in compact
    assert "padding: 10px" in compact


def test_rag_retrieval_graph_is_built_from_returned_sources() -> None:
    assert "function rememberLastRagRetrieval" in GRAPH_JS
    assert "function _buildRagRetrievalGraphPayload" in GRAPH_JS
    assert "function openRagRetrievalGraph" in GRAPH_JS
    assert "rag_retrieval_score" not in GRAPH_JS  # public field is retrieval_score
    assert "graphButton.textContent = _tr('Graphe RAG')" in APP_JS
    assert "graphButton.title = _tr('Voir le graphe de récupération RAG')" in APP_JS
    assert "semantic_content_clusters" in GRAPH_JS
    assert "Regroupement sémantique" in GRAPH_JS
