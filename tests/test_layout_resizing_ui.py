
from html.parser import HTMLParser
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = read_app_javascript()
SETTINGS_JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
GRAPH_CSS = (ROOT / "static" / "css" / "knowledge-graph.css").read_text(
    encoding="utf-8"
)
STATIC_BINDINGS_JS = (
    ROOT / "static" / "js" / "static-event-bindings.js"
).read_text(encoding="utf-8")


class _LayoutParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.resizers: dict[str, dict[str, str | None]] = {}

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attr_map = dict(attrs)
        key = attr_map.get("data-layout-resizer")
        if key:
            self.resizers[key] = attr_map


def test_major_panes_expose_accessible_resizers() -> None:
    parser = _LayoutParser()
    parser.feed(HTML)

    assert set(parser.resizers) == {
        "sidebar",
        "library",
        "drafting-main",
        "drafting-bibliography",
        "knowledge-graph",
        "notebook-sources",
        "notebook-notes",
    }
    for attrs in parser.resizers.values():
        assert attrs["role"] == "separator"
        assert attrs["tabindex"] == "0"
        assert attrs.get("aria-label")
        assert attrs.get("aria-orientation") in {"vertical", "horizontal"}


def test_layout_resize_engine_persists_and_can_reset_sizes() -> None:
    assert "const LAYOUT_RESIZER_CONFIGS" in APP_JS
    assert "handle.addEventListener('pointerdown', _layoutStartResize)" in APP_JS
    assert "handle.addEventListener('keydown', _layoutHandleKeydown)" in APP_JS
    assert "handle.addEventListener('dblclick', _layoutResetHandle)" in APP_JS
    assert "localStorage.setItem(_layoutStorageKey" in APP_JS
    assert "function resetLayoutSizes()" in APP_JS
    reset_button = HTML[HTML.index('id="layout-reset-btn"') :][:320]
    assert "data-bb-static-onclick=" in reset_button
    assert "resetLayoutSizes()" in STATIC_BINDINGS_JS
    assert "initLayoutResizers();" in SETTINGS_JS


def test_layout_switches_by_content_width_and_disables_handles_on_mobile() -> None:
    assert "container: main-content / inline-size" in APP_CSS
    assert "@container main-content (max-width: 979px)" in APP_CSS
    assert ".layout-resizer { display: none !important; }" in APP_CSS
    assert "@container main-content (max-width: 900px)" in GRAPH_CSS
    assert "--library-primary-h" in APP_CSS
    assert "--knowledge-graph-primary-h" in GRAPH_CSS


def test_stacked_drafting_panel_scrolls_and_editor_height_is_resizable() -> None:
    workspaces_css = (ROOT / "static" / "css" / "workspaces.css").read_text(encoding="utf-8")
    stacked = workspaces_css.split(
        "@container main-content (max-width: 820px)", maxsplit=1
    )[1].split("@media", maxsplit=1)[0]
    config = APP_JS[APP_JS.index("'drafting-main': {") :][:900]

    # app.css hides the handle below 980px; the three-column grid needs it back.
    assert "#tab-drafting #drafting-main-resizer { display: block; }" in workspaces_css
    # One scroll per part: the editor is capped by the window so the assistant
    # row keeps at least 260px and scrolls inside its own frame.
    assert (
        "min(var(--drafting-editor-h, 420px), max(200px, calc(100vh - 470px)))"
        " var(--layout-resizer-size) minmax(260px, 1fr)"
    ) in stacked
    assert "#drafting-assistant #drafting-panel-assistant { overflow-y: auto; }" in stacked
    assert "axes: ['x', 'y']" in config
    assert "'--drafting-editor-h'" in config


def test_phone_layout_keeps_safe_areas_touch_targets_and_chat_content_visible() -> None:
    assert "viewport-fit=cover" in HTML
    assert "padding: calc(env(safe-area-inset-top, 0px) + 42px) 0 0" in APP_CSS
    assert "padding: 2px 7px calc(env(safe-area-inset-bottom, 0px) + 4px)" in APP_CSS
    assert "grid-template-rows: minmax(0, 1fr)" in APP_CSS
    assert "#chat-composer #msg-input" in APP_CSS
    assert "min-height: 44px" in APP_CSS
    assert "overflow-wrap: anywhere" in APP_CSS
    assert "setMobileNavOpen(false);" in APP_JS[APP_JS.index("function toggleSettings()") :][:900]


def test_mobile_install_metadata_uses_bibliobrain_icons() -> None:
    manifest = (ROOT / "static" / "manifest.webmanifest").read_text(encoding="utf-8")
    assert 'rel="apple-touch-icon"' in HTML
    assert 'rel="manifest"' in HTML
    assert 'name="apple-mobile-web-app-capable" content="yes"' in HTML
    assert '"display": "standalone"' in manifest
    for name in ("apple-touch-icon.png", "app-icon-192.png", "app-icon-512.png"):
        icon = ROOT / "static" / name
        assert icon.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert icon.stat().st_size > 10_000


def test_phone_library_uses_mobile_native_layout() -> None:
    assert "#lib-table tbody tr:nth-child(even)" in APP_CSS
    assert "content: attr(data-label)" in APP_CSS
    assert "scrollLibraryListIntoView" in APP_JS
    assert 'id="library-back-to-list-btn"' in HTML


def test_library_header_has_count_filter_columns_and_chips() -> None:
    header = HTML[HTML.index('id="lib-header"') : HTML.index('id="lib-table-wrap"')]
    # The statistics rail and the yellow refresh notice are gone (the pill covers them).
    assert 'id="lib-header-tools"' not in HTML and 'id="rag-refresh-notice"' not in HTML
    assert 'id="lib-count"' in header
    assert 'id="lib-columns"' in header and 'id="lib-columns-panel"' in header
    assert 'id="lib-filter-chips"' in header
    assert 'id="lib-thead-row"' in HTML
