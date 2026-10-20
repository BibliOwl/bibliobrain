from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
KG_CSS = (ROOT / "static" / "css" / "knowledge-graph.css").read_text(
    encoding="utf-8"
)
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
SETTINGS_JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
RAG_JS = (ROOT / "static" / "js" / "app" / "rag.js").read_text(encoding="utf-8")
LIBRARY_JS = (ROOT / "static" / "js" / "app" / "library.js").read_text(
    encoding="utf-8"
)
STATIC_EVENT_BINDINGS_JS = (
    ROOT / "static" / "js" / "static-event-bindings.js"
).read_text(encoding="utf-8")
SETTINGS_CSS = (ROOT / "static" / "css" / "settings.css").read_text(encoding="utf-8")
KG_JS = (ROOT / "static" / "js" / "knowledge-graph.js").read_text(encoding="utf-8")


def test_primary_controls_inherit_the_application_font() -> None:
    control_rule = APP_CSS[APP_CSS.index("button,\ninput,") : APP_CSS.index(
        'input[type="text"]'
    )]
    assert "font-family: inherit" in control_rule


def test_discovery_collects_the_query_before_presenting_submit_action() -> None:
    query_actions = HTML[
        HTML.index('class="discovery-query-actions"') : HTML.index(
            'id="discovery-status"'
        )
    ]
    assert query_actions.index('id="discovery-input"') < query_actions.index(
        'id="discovery-run-btn"'
    )
    assert ".discovery-query-actions {" in APP_CSS
    assert "grid-template-columns: minmax(0, 1fr)" in APP_CSS


def test_mobile_library_uses_wrapped_controls_and_hides_empty_detail() -> None:
    mobile = APP_CSS[APP_CSS.rindex("@media (max-width: 760px)") :]
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in mobile
    assert "#lib-table thead" in mobile and "display: none" in mobile
    assert "#library-detail.empty" in mobile and "display: none" in mobile


def test_mobile_chat_and_writing_actions_wrap_without_horizontal_overflow() -> None:
    mobile = APP_CSS[APP_CSS.rindex("@media (max-width: 760px)") :]
    composer = mobile[mobile.index("#composer-toolbar {") :]
    assert "flex-wrap: wrap" in composer
    assert "overflow: visible" in composer
    assert ".drafting-copy-btn" in mobile
    assert "width: auto" in mobile


def test_mobile_connections_keep_one_vertical_scroll_surface() -> None:
    # The last mobile block is the layout one; an earlier one only styles the
    # expanded analysis panel.
    mobile = KG_CSS[KG_CSS.rindex("@media (max-width: 760px)") :]
    stage = mobile[mobile.index("#knowledge-graph-stage {") : mobile.index("#knowledge-graph-columns {")]
    assert "height: auto" in stage
    assert "overflow: visible" in stage
    assert "overflow-x: auto" not in stage
    assert "grid-template-columns: minmax(0, 1fr)" in mobile
    article_list = mobile[mobile.index("#kg-article-list {") :]
    assert "max-height: none" in article_list


def test_reviewed_static_strings_are_connected_to_i18n() -> None:
    for key in (
        "library.searchAria",
        "connections.viewLibrary",
        "connections.conceptSummary",
        "settings.defaultOllamaUrl",
        "settings.ai.modelTitle",
        "settings.ai.quickTest",
    ):
        assert f'data-i18n="{key}"' in HTML or f'data-i18n-aria-label="{key}"' in HTML
        assert f"'{key}'" in I18N_JS


def test_runtime_status_and_dynamic_counts_stay_readable_and_localized() -> None:
    assert 'id="rag-status-summary"' in HTML
    assert 'data-i18n="discovery.paperCount"' in HTML
    assert "settings.providerActive" in SETTINGS_JS
    assert "connections.subcategoryCount" in KG_JS


def test_library_dynamic_filter_checkboxes_have_localized_accessible_names() -> None:
    assert "aria-label=\"${escapeAttr(_ti('library.filterBy', { label: optionLabel }))}\"" in LIBRARY_JS
    assert "'library.filterBy'" in I18N_JS
    assert "fr: 'Filtrer par {label}'" in I18N_JS
    assert "en: 'Filter by {label}'" in I18N_JS


def test_library_supports_theme_and_zotero_collection_filters() -> None:
    assert "['rows', 'paths', 'metadata']" in LIBRARY_JS
    assert "state.libMetadata" in LIBRARY_JS
    assert "collections: [...f.collections]" in LIBRARY_JS
    assert "themes: [...f.themes]" in LIBRARY_JS
    assert "candidate.startsWith(`${wanted} / `)" in LIBRARY_JS
    assert "actualTheme.startsWith(`${wanted} / `)" in LIBRARY_JS
    assert 'id="lib-show-only-themes"' in HTML
    assert 'id="lib-show-only-collections"' in HTML
    assert 'id="lib-show-only-subcollections"' in HTML
    assert "theme: { labelKey: 'library.colTheme', sort: 'theme' }" in LIBRARY_JS


def test_dynamic_interface_hints_do_not_fall_back_to_french_in_english() -> None:
    assert "handle.title = _ti('layout.resizeHint');" in LIBRARY_JS
    assert "item.model ? `${_ti('nav.model')} ${item.model}`" in (
        ROOT / "static" / "js" / "app" / "chat.js"
    ).read_text(encoding="utf-8")
    assert "_ti('settings.refreshOllamaGpu')" in SETTINGS_JS
    assert "_ti('settings.gpuUnavailableProvider')" in SETTINGS_JS
    for key in (
        "layout.resizeHint",
        "settings.refreshOllamaGpu",
        "settings.gpuUnavailableProvider",
    ):
        assert f"'{key}'" in I18N_JS


def test_external_service_disclaimer_lists_every_transmitted_data_type() -> None:
    assert 'data-i18n="settings.externalDisclaimer"' in HTML
    translation = I18N_JS[I18N_JS.index("'settings.externalDisclaimer'") :]
    translation = translation[: translation.index("\n")]
    for expected in ("Query data", "DOIs", "concept labels", "short definitions"):
        assert expected in translation


def test_knowledge_graph_reload_timer_can_be_rescheduled() -> None:
    assert "let _kgLoadTimer = null;" in LIBRARY_JS
    assert "_kgLoadTimer = setTimeout(" in KG_JS


def test_rag_preparation_status_keeps_a_compact_stable_height() -> None:
    summary_rule = APP_CSS[
        APP_CSS.index("#rag-status-summary {") : APP_CSS.index("#bug-report-btn,")
    ]
    text_rule = SETTINGS_CSS[
        SETTINGS_CSS.index("#rag-status-text {") : SETTINGS_CSS.index("@keyframes pulse")
    ]

    assert "ragStatusProgress(event.frac, event.desc);" in RAG_JS
    assert "text.textContent = event.desc" not in RAG_JS
    assert "max-height: 30px" in summary_rule
    assert "overflow: hidden" in summary_rule
    assert "text-overflow: ellipsis" in text_rule
    assert "white-space: nowrap" in text_rule
