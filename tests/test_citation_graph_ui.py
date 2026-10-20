from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
GRAPH_JS = ROOT / "static" / "js" / "app" / "citation-graph.js"


class _ElementContractParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.by_id: dict[str, tuple[str, dict[str, str | None]]] = {}
        self.roles: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attr_map = dict(attrs)
        element_id = attr_map.get("id")
        if element_id:
            self.by_id[element_id] = (tag, attr_map)
        if attr_map.get("role"):
            self.roles.append((tag, attr_map))


def _parse_index() -> _ElementContractParser:
    parser = _ElementContractParser()
    parser.feed((ROOT / "static" / "index.html").read_text(encoding="utf-8"))
    return parser


def _extract_js_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    brace_start = source.index("{", source.index(")", start))
    depth = 0
    for index in range(brace_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Unterminated JavaScript function: {name}")


def test_citation_graph_modal_exposes_an_accessible_structure() -> None:
    parser = _parse_index()

    modal_tag, modal = parser.by_id["nb-citation-graph-modal"]
    assert modal_tag == "div"
    assert modal["role"] == "dialog"
    assert modal["aria-modal"] == "true"
    assert modal["aria-labelledby"] == "nb-cg-title"

    _, graph = parser.by_id["nb-cg-svg"]
    assert graph["role"] == "group"
    assert graph.get("aria-label")

    _, loading = parser.by_id["nb-cg-loading"]
    assert loading["role"] == "status"
    assert loading["aria-live"] == "polite"

    # The header chips are both the legend and the filters.
    for key in ("refs", "cits", "internal"):
        tag, chip = parser.by_id[f"nb-cg-filter-{key}"]
        assert tag == "button"
        assert chip["aria-pressed"] == "true"
    assert parser.by_id["nb-cg-filters"][1]["role"] == "group"
    assert parser.by_id["nb-cg-search"][1]["type"] == "search"
    assert parser.by_id["nb-cg-tooltip"][1]["role"] == "tooltip"
    assert "nb-cg-resume-open" in parser.by_id

    # Removed by the redesign: left list, legend panel, zoom, year slider.
    for removed in (
        "nb-cg-leftpanel",
        "nb-cg-tabs",
        "nb-cg-legend",
        "nb-cg-controls",
        "nb-cg-year-filter",
        "nb-cg-mode-toggle",
        "nb-cg-stats",
    ):
        assert removed not in parser.by_id, removed


def test_cartography_workspace_restores_identity_search_with_rgpd_notice() -> None:
    parser = _parse_index()
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    app_js = read_app_javascript()
    app_css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
    bindings = (ROOT / "static" / "js" / "static-event-bindings.js").read_text(encoding="utf-8")

    workspace_tag, workspace = parser.by_id["research-scope-workspace"]
    assert workspace_tag == "section"
    assert workspace["aria-labelledby"] == "research-scope-title"
    assert parser.by_id["cartography-citation-workspace"]
    assert parser.by_id["kg-view-mode"][1]["aria-label"] == "Type de cartographie"
    cartography_tabs = [
        attrs
        for tag, attrs in parser.roles
        if tag == "button" and attrs.get("role") == "tab" and attrs.get("data-cartography-view")
    ]
    assert len(cartography_tabs) == 4
    assert sum(attrs.get("aria-selected") == "true" for attrs in cartography_tabs) == 1
    assert parser.by_id["research-scope-results"][1]["role"] == "listbox"
    assert parser.by_id["research-scope-query"][1]["maxlength"] == "200"
    assert 'value="150"' in index
    assert 'value="balanced"' in index
    assert "Nom, ORCID ou ROR" in index
    assert "métadonnées bibliographiques publiques pouvant identifier" in index
    assert "responsable du traitement" in index
    assert "cache mémoire temporaire de 15 minutes" in index
    assert "sont transmis à l’API OpenAlex" in index
    assert 'href="https://openalex.org"' in index
    assert 'href="https://openalex.org/legal"' in index
    assert 'href="https://www.cnil.fr/fr/informer-les-personnes"' in index
    assert 'id="research-scope-clear-cache"' in index
    assert 'id="research-scope-enabled"' in index
    assert 'data-i18n="connections.experimental">Expérimental' in index
    assert "journaux d’accès standards" in index

    assert "/api/citation-graph/entities" in app_js
    assert "body: JSON.stringify({ entity_type: entityType, query, limit: 8 })" in app_js
    assert "/api/citation-graph/entities?" not in app_js
    assert "/api/citation-graph/scope" in app_js
    assert "/api/citation-graph/search-cache" in app_js
    assert "method: 'POST'" in app_js
    assert "embedded: true" in app_js
    assert "function _nbCgMount(embedded = false)" in app_js
    assert (
        "document.getElementById('nb-cg-close')?.addEventListener('click', closeNbCitationGraph)"
        in app_js
    )
    assert 'id="nb-cg-close" type="button" aria-label="Fermer"' in index
    assert "returnFocusEl" in app_js
    assert "setCartographyView(this.dataset.cartographyView)" in bindings
    assert "#research-scope-panel" in app_css
    assert "#nb-citation-graph-modal.is-embedded" in app_css
    assert "@media (max-width: 720px)" in app_css
    assert ".research-scope-result { grid-template-columns: minmax(0, 1fr); gap: 4px; }" in app_css

    knowledge_js = (ROOT / "static" / "js" / "knowledge-graph.js").read_text(encoding="utf-8")
    settings_js = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    assert "function setResearchScopeFeatureEnabled(enabled)" in knowledge_js
    assert "requestedView === 'research' && !_researchScopeFeatureEnabled" in knowledge_js
    assert "research_scope_enabled:" in settings_js


def test_citation_graph_layout_rules_and_interaction_contracts() -> None:
    app_js = read_app_javascript()
    graph_js = GRAPH_JS.read_text(encoding="utf-8")
    app_css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    # One paper and nothing explored: the left/right view; otherwise bands.
    assert "model.corpus.length === 1 && _nbCg.expandedIds.size === 0" in graph_js
    # Labels and ticks are HTML over the SVG, not SVG <text>.
    assert "<text" not in graph_js
    assert "new ResizeObserver(" in graph_js
    # No zoom or pan any more.
    for removed in ("function _nbCgZoom", "function _nbCgFitView", "wheel", "_nbCgApplyYearFilter"):
        assert removed not in graph_js, removed
    # Motion respects the light performance mode and reduced motion, and only
    # restarts when the targets change.
    assert "isLightPerformanceMode()" in graph_js
    assert "prefers-reduced-motion: reduce" in graph_js
    assert "sig !== _nbCg.layoutSig" in graph_js
    # Leaving for Discovery keeps the graph (and its explorations).
    discovery = _extract_js_function(graph_js, "_nbCgSearchInDiscovery")
    assert "_nbCgSuspend()" in discovery
    assert "closeNbCitationGraph" not in discovery
    # Exploration keeps the server route and its OpenAlex fallback.
    assert "/api/citation-graph/expand" in graph_js
    assert "_nbCgExpandDirectlyFromOpenAlex" in graph_js
    assert "'citationGraph.explore'" in graph_js
    # Keyboard: nodes are buttons, Escape clears the selection before closing.
    assert 'role="button" tabindex="0"' in graph_js
    assert "event.key !== 'Enter' && event.key !== ' '" in graph_js
    escape = _extract_js_function(graph_js, "_nbCgEscapeKeyHandler")
    assert escape.index("_nbCgSelectNode('')") < escape.index("closeNbCitationGraph()")
    assert "abortController.signal" in app_js
    # Main action: the paper in the library when it is there, else the publisher.
    actions = _extract_js_function(graph_js, "_nbCgActions")
    assert actions.index("open-library") < actions.index("citationGraph.viewAtPublisher")
    assert "discoveryTitleKey(node.title)" in graph_js
    # Adding to a notebook reuses Discovery's picker.
    assert "function openNotebookPickerForItems(items, onAdded = null)" in app_js

    assert (
        ".nb-cg-node-wrap.kind-prior .nb-cg-node { fill: var(--cg-prior-fill); stroke: var(--cg-prior-stroke); }"
        in app_css
    )
    assert (
        ".nb-cg-node-wrap.kind-derivative .nb-cg-node { fill: var(--cg-derivative-fill); stroke: var(--cg-derivative-stroke); }"
        in app_css
    )
    assert "stroke-dasharray: 3 2" in app_css
    assert '[data-theme="light"] #nb-citation-graph-modal' in app_css
    assert "body.citation-graph-open" in app_css
    assert "@media (max-width: 1180px)" in app_css
    assert "@media (max-width: 820px)" in app_css
    assert "@media (max-width: 760px)" in app_css
    for removed in (".nb-cg-tab", "#nb-cg-year-filter", "#nb-cg-controls", ".nb-cg-list-item"):
        assert removed not in app_css, removed


def _run_graph_script(body: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the citation graph layout test")
    source = GRAPH_JS.read_text(encoding="utf-8")
    names = [
        "_nbCgYear",
        "_nbCgClamp",
        "_nbCgSurname",
        "_nbCgAuthorLastName",
        "_nbCgNodeLabel",
        "_nbCgPlainWork",
        "_nbCgWorkIndex",
        "_nbCgBuildModel",
        "_nbCgIsSingle",
        "_nbCgPlacer",
        "_nbCgLayoutSingle",
        "_nbCgLayoutBands",
    ]
    functions = "\n\n".join(_extract_js_function(source, name) for name in names)
    script = f"""
const _NB_CG_NOW = 2026;
const _ti = (key) => key;
const _nbCg = {{
  data: null,
  expandedIds: new Set(),
  expansions: new Map(),
  filters: {{ refs: true, cits: true, internal: true }},
  showAll: {{ refs: false, cits: false }},
}};
{functions}
{body}
"""
    completed = subprocess.run(
        [node, "-e", script], check=True, capture_output=True, encoding="utf-8", cwd=ROOT
    )
    return json.loads(completed.stdout)


def test_citation_graph_model_and_layouts_place_works_by_time() -> None:
    payload = _run_graph_script(
        """
const work = (id, year, cited = 10) => ({ id, openalex_id: id, year, cited_by_count: cited, title: id, authors: [`A ${id}`] });
const out = {};

// One paper: references left of it, citing works right of it.
_nbCg.data = {
  nodes: [work('S', 2016, 40)],
  edges: [],
  prior_works: [work('R1', 1995), work('R2', 2010), work('R3', null)].map(w => ({ ...w, cited_by_notebook_ids: ['S'] })),
  derivative_works: [work('C1', 2018), work('C2', 2024)].map(w => ({ ...w, cites_notebook_ids: ['S'] })),
};
let model = _nbCgBuildModel();
const single = _nbCgLayoutSingle(model, 1100, 640);
const x = id => single.targets.get(id).x;
out.singleMode = _nbCgIsSingle(model);
out.sourceAtCentre = x('S') === single.cx;
out.refsLeft = ['R1', 'R2'].every(id => x(id) < single.cx);
out.citsRight = ['C1', 'C2'].every(id => x(id) > single.cx);
out.refsByYear = x('R1') < x('R2');
out.citsByYear = x('C1') < x('C2');
// An undated work cannot sit on a time axis: it is left out of the layout.
out.undatedHidden = !single.targets.has('R3') && model.byId.has('R3');

// Several papers, then an exploration that promotes a reference.
_nbCg.data = {
  nodes: [work('A', 2015), work('B', 2019)],
  edges: [{ source: 'B', target: 'A' }],
  prior_works: [
    { ...work('R1', 1990), cited_by_notebook_ids: ['A', 'B'] },
    { ...work('R2', 2005), cited_by_notebook_ids: ['A'] },
  ],
  derivative_works: [{ ...work('D1', 2022), cites_notebook_ids: ['B'] }],
};
const before = _nbCgBuildModel();
out.beforeInternal = before.internal.length;
out.beforeShared = before.byId.get('R1').links.length;
_nbCg.expansions.set('R2', { center: work('R2', 2005), prior_works: [work('X1', 1980), work('R1', 1990)], derivative_works: [] });
_nbCg.expandedIds.add('R2');
model = _nbCgBuildModel();
const r2 = model.byId.get('R2');
out.promoted = {
  kind: r2._kind,
  explored: r2.explored,
  internal: model.internal.length,
  r1Links: model.byId.get('R1').links.length,
  single: _nbCgIsSingle(model),
};
const bands = _nbCgLayoutBands(model, 1100, 700);
const target = id => bands.targets.get(id);
out.bandsOrder = ['R1', 'A', 'D1', 'R2'].map(id => target(id).band);
out.allInBands = ['R1', 'A', 'B', 'D1', 'R2', 'X1'].every((id) => {
  const t = target(id);
  const [top, h] = bands.band[t.band];
  return t.y >= top && t.y <= top + h;
});
out.bandsVertical = bands.band.prior[0] < bands.band.corpus[0] && bands.band.corpus[0] < bands.band.derivative[0];
out.olderLeft = target('X1').x < target('A').x;

// Caps: many references, only the best ranked are drawn until "show all".
_nbCg.expansions.clear();
_nbCg.expandedIds.clear();
_nbCg.data = {
  nodes: [work('A', 2015), work('B', 2019)],
  edges: [],
  // P119 is the oldest work and the lowest ranked one: hidden by the cap.
  prior_works: Array.from({ length: 120 }, (_, i) => ({ ...work(`P${i}`, i === 119 ? 1950 : 1996 + (i % 18)), cited_by_notebook_ids: i < 5 ? ['A', 'B'] : ['A'] })),
  derivative_works: [],
};
model = _nbCgBuildModel();
const capped = _nbCgLayoutBands(model, 1100, 700);
out.cappedRefs = [...capped.targets.values()].filter(t => t.band === 'prior').length;
out.cappedMore = capped.more.refs;
out.sharedFirst = ['P0', 'P1', 'P2', 'P3', 'P4'].every(id => capped.targets.has(id));
out.sharedLabel = capped.targets.get('P0').label;
// The axis starts at the oldest reference on screen, not at one hidden by the cap.
out.cappedAxisStart = Math.min(...capped.ticks.map(t => Number(t.label)));
_nbCg.showAll.refs = true;
const all = _nbCgLayoutBands(model, 1100, 700);
out.allRefs = [...all.targets.values()].filter(t => t.band === 'prior').length;
out.allAxisStart = Math.min(...all.ticks.map(t => Number(t.label)));

out.surnames = ['Zhou D', 'Li X-Y', 'Charles P. Bean', 'J.-H. Kim', 'Bean, C. P.', 'Shinya NARIKI'].map(_nbCgSurname);

console.log(JSON.stringify(out));
"""
    )

    assert payload["singleMode"] is True
    assert payload["sourceAtCentre"] is True
    assert payload["refsLeft"] is True
    assert payload["citsRight"] is True
    assert payload["refsByYear"] is True
    assert payload["citsByYear"] is True
    assert payload["undatedHidden"] is True
    assert payload["beforeInternal"] == 1
    assert payload["beforeShared"] == 2
    # The explored reference joins the corpus: A citing it becomes an internal
    # link, and R1 is now cited by three corpus papers.
    assert payload["promoted"] == {
        "kind": "corpus",
        "explored": True,
        "internal": 2,
        "r1Links": 3,
        "single": False,
    }
    assert payload["bandsOrder"] == ["prior", "corpus", "derivative", "corpus"]
    assert payload["allInBands"] is True
    assert payload["bandsVertical"] is True
    assert payload["olderLeft"] is True
    assert 6 <= payload["cappedRefs"] <= 40
    assert payload["cappedMore"] is True
    assert payload["sharedFirst"] is True
    assert payload["allRefs"] == 120
    assert payload["cappedAxisStart"] >= 1995
    # Initials after the surname ("Zhou D") are not taken for the name.
    assert payload["surnames"] == ["Zhou", "Li", "Bean", "Kim", "Bean", "NARIKI"]
    assert payload["allAxisStart"] <= 1950
    assert payload["sharedLabel"].endswith(" · 2×")
