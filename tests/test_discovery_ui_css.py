from pathlib import Path

from conftest import read_app_javascript

CSS = (Path(__file__).parents[1] / "static" / "css" / "app.css").read_text(
    encoding="utf-8"
)
HTML = (Path(__file__).parents[1] / "static" / "index.html").read_text(encoding="utf-8")
JS = read_app_javascript()
SETTINGS_JS = (Path(__file__).parents[1] / "static" / "js" / "settings.js").read_text(
    encoding="utf-8"
)
STATIC_BINDINGS_JS = (
    Path(__file__).parents[1] / "static" / "js" / "static-event-bindings.js"
).read_text(encoding="utf-8")


def _rule_body(selector: str, *, after: str = "", occurrence: int = 1) -> str:
    start_at = CSS.index(after) if after else 0
    selector_at = start_at
    for _ in range(occurrence):
        selector_at = CSS.index(selector, selector_at)
        selector_at += len(selector)
    selector_at -= len(selector)
    opening = CSS.index("{", selector_at + len(selector))
    depth = 1
    cursor = opening + 1
    while depth and cursor < len(CSS):
        depth += CSS[cursor] == "{"
        depth -= CSS[cursor] == "}"
        cursor += 1
    assert depth == 0, f"Unclosed CSS rule for {selector}"
    return CSS[opening + 1 : cursor - 1]


def test_discovery_history_lives_in_the_sidebar_only() -> None:
    # The in-tab history column, its toggle and its resizer are gone: no CSS
    # or JS may still size the tab for them.
    for selector in (
        "#discovery-history-wrap",
        "#discovery-history-head",
        "#discovery-new-btn",
        "#discovery-history-resizer",
        "history-collapsed",
        "--history-panel-w",
        ".history-toggle-btn",
    ):
        assert selector not in CSS
    assert "function setHistoryCollapsed" not in JS
    assert "restoreHistoryCollapseState" not in SETTINGS_JS
    layout = _rule_body("}\n\n#discovery-layout")
    assert "grid-template-columns: minmax(0, 1fr)" in layout
    assert 'id="sidebar-discovery-history"' in HTML
    assert 'id="discovery-history-popover"' in HTML


def test_discovery_long_content_wraps_in_result_cards() -> None:
    for selector in (
        ".discovery-title",
        ".discovery-meta",
        ".discovery-why-text",
        ".discovery-summary",
    ):
        assert "overflow-wrap: anywhere" in _rule_body(selector)


def test_discovery_action_button_stays_in_the_card_scroll_layer() -> None:
    button = _rule_body(".discovery-actions .btn")

    assert "transform: none" in button


def test_discovery_short_mobile_viewport_scrolls_the_panel() -> None:
    media = "@media (max-width: 760px) and (max-height: 700px)"
    panel = _rule_body("#discovery-panel", after=media)
    results = _rule_body(".discovery-results-shell", after=media, occurrence=2)

    assert "overflow-y: auto" in panel
    assert "min-height: 280px" in results


def test_discovery_starting_point_is_a_two_option_radio_group() -> None:
    switch = HTML[HTML.index('id="discovery-mode-switch"') : HTML.index('class="discovery-count-control"')]

    assert 'role="radiogroup"' in switch
    assert switch.count('role="radio"') == 2
    assert 'data-discovery-mode="search"' in switch
    assert 'data-discovery-mode="notebook"' in switch
    assert 'id="discovery-mode-menu"' not in HTML
    assert "function selectDiscoveryMode(mode" in JS
    assert "toggleDiscoveryModeMenu" not in JS + STATIC_BINDINGS_JS
    # The search accepts whole sentences, not only keywords.
    assert "Recherche par mots-clés" not in HTML


def test_notebook_discovery_has_a_primary_notebook_entry_and_custom_picker() -> None:
    assert "discoverRelatedFromNotebook()" in STATIC_BINDINGS_JS
    assert 'id="discovery-notebook-picker"' in HTML
    assert 'id="discovery-notebook-menu"' in HTML
    assert 'id="discovery-notebook-context"' in HTML
    assert "function toggleDiscoveryNotebookMenu(event)" in JS
    assert "function selectDiscoveryNotebook(notebookId)" in JS
    assert "function discoverRelatedFromNotebook()" in JS
    # Discovery results lead to the publisher's page; no automatic download.
    assert "'discovery.viewAtPublisher'" in JS
    assert "'discovery.whyNotebook'" in JS
    assert "Recommandations via DOI" not in HTML


def test_discovery_form_keeps_settings_on_one_line_and_submit_in_the_composer() -> None:
    controls = _rule_body("#discovery-controls")
    composer = HTML[HTML.index('class="discovery-query-actions"') : HTML.index('id="discovery-search-feedback"')]
    footer = composer[composer.index('class="discovery-query-footer"') :]

    assert "display: flex" in controls
    assert "space-between" in controls
    assert 'class="discovery-count-label"' in HTML
    assert 'id="discovery-controls"' in footer
    assert 'id="discovery-run-btn"' in footer
    assert "@media (max-width: 1280px)" not in CSS


def test_discovery_debug_is_removed_when_debug_mode_is_disabled() -> None:
    visibility = SETTINGS_JS[
        SETTINGS_JS.index("function clearDiscoveryDebug()")
        : SETTINGS_JS.index("async function loadAppSettings()")
    ]

    assert "document.getElementById('discovery-debug')" in visibility
    assert "debugEl.style.display = 'none'" in visibility
    assert "debugEl.innerHTML = ''" in visibility
    assert "if (!enabled)" in visibility
    assert "clearDiscoveryDebug();" in visibility
