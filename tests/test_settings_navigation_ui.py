from html.parser import HTMLParser
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = read_app_javascript()
SETTINGS_JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
SETTINGS_CSS = (ROOT / "static" / "css" / "settings.css").read_text(encoding="utf-8")
STATIC_BINDINGS_JS = (ROOT / "static" / "js" / "static-event-bindings.js").read_text(
    encoding="utf-8"
)
OCR_JS = (ROOT / "static" / "js" / "app" / "ocr.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")


def pane(name: str, next_name: str | None = None) -> str:
    """Markup of one settings pane, up to the next pane or the end of the dialog."""
    start = HTML.index(f'id="settings-pane-{name}"')
    end = (
        HTML.index(f'id="settings-pane-{next_name}"')
        if next_name
        else HTML.index('<div id="kg-modal-overlay"')
    )
    return HTML[start:end]


class _SettingsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tabs: dict[str, str] = {}
        self.panes: set[str] = set()
        self.debug_row_displays: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        classes = set((attr_map.get("class") or "").split())
        if "settings-tab" in classes:
            name = attr_map.get("data-settings-tab")
            target = attr_map.get("aria-controls")
            if name and target:
                self.tabs[name] = target
        if "settings-pane" in classes and attr_map.get("id"):
            self.panes.add(str(attr_map["id"]))
        if "settings-row" in classes and attr_map.get("data-debug-only"):
            self.debug_row_displays.append(str(attr_map["data-debug-only"]))


def test_settings_navigation_has_one_accessible_pane_per_category() -> None:
    parser = _SettingsParser()
    parser.feed(HTML)

    assert list(parser.tabs) == [
        "overview",
        "documents",
        "assistant",
        "online",
        "look",
        "expert-generation",
        "expert-search",
        "expert-indexing",
        "expert-cleanup",
        "expert-prompts",
        "expert-keys",
        "expert-diagnostics",
    ]
    assert {f"settings-pane-{name}" for name in parser.tabs} == set(parser.tabs.values())
    assert set(parser.tabs.values()) == parser.panes
    assert (
        'class="settings-tab active" role="tab" aria-selected="true" '
        'aria-controls="settings-pane-overview"' in HTML
    )
    assert 'class="settings-pane active" id="settings-pane-overview"' in HTML
    assert "hasCompletedFirstRagInit() && savedTab ? savedTab : 'overview'" in APP_JS
    # Saved preferences and older callers keep working.
    for old, new in (
        ("preparation", "overview"),
        ("general", "look"),
        ("library", "documents"),
        ("models", "assistant"),
        ("research", "online"),
        ("prompts", "expert-prompts"),
        ("advanced", "expert-diagnostics"),
    ):
        assert f"{old}: '{new}'" in APP_JS


def test_expert_settings_are_a_separate_door_with_a_warning() -> None:
    assert 'id="settings-mode-toggle"' in HTML
    assert 'data-bb-static-onclick="toggle-settings-mode"' in HTML
    assert "toggleSettingsMode()" in STATIC_BINDINGS_JS
    assert "function toggleSettingsMode()" in APP_JS
    banner = HTML[HTML.index('id="settings-expert-banner"') : HTML.index('id="settings-pane-overview"')]
    assert 'id="settings-expert-reset-all"' in banner
    assert "settings.expert.warning" in banner
    for name in ("overview", "documents", "assistant", "online", "look"):
        assert f'id="settings-pane-{name}" data-settings-mode="everyday"' in HTML
    for name in ("generation", "search", "indexing", "cleanup", "prompts", "keys", "diagnostics"):
        assert f'id="settings-pane-expert-{name}" data-settings-mode="expert"' in HTML
    assert '[data-settings-mode="expert"] #settings-tabs [data-settings-mode="everyday"]' in SETTINGS_CSS
    assert "settings-searching" in APP_JS
    # Every tuned expert value shows its default and has its own Reset.
    assert "function renderSettingsExpertDefaults()" in APP_JS
    assert "settings.expert.defaultValue" in APP_JS
    assert "function resetSettingsRow(row)" in APP_JS


def test_overview_has_one_line_per_area_and_one_button_where_needed() -> None:
    overview = pane("overview", "documents")
    for area in ("documents", "assistant", "online", "look"):
        assert f'data-overview-area="{area}"' in overview
        assert f'id="settings-overview-{area}-action" hidden' in overview
    assert 'id="tutorial-start-btn"' in overview
    assert "function settingsAreaStates()" in APP_JS
    assert "settings.overview.ready" in APP_JS
    for area in ("documents", "assistant", "online"):
        assert f'id="settings-{area}-pill"' in HTML


def test_update_check_lives_in_expert_diagnostics_and_the_sidebar_opens_it() -> None:
    assert 'id="settings-updates"' not in pane("overview", "documents")
    diagnostics = pane("expert-diagnostics")
    for control in ("settings-updates", "update-check-enabled", "update-check-now-btn", "update-install-btn"):
        assert f'id="{control}"' in diagnostics
    assert "openSettingsAt('expert-diagnostics', 'settings-updates')" in APP_JS
    assert "'update.sectionLabel'" in I18N_JS


def test_fix_this_chains_the_existing_ocr_endpoints_in_order() -> None:
    flow = OCR_JS[OCR_JS.index("async function runQuickFix()") : OCR_JS.index("async function refreshInventory()")]
    order = [flow.index(endpoint) for endpoint in (
        "/api/ocr/readiness", "/api/ocr/install", "/api/ocr/settings", "/api/ocr/start",
    )]
    assert order == sorted(order)
    # Pressing Start is the consent: no browser confirm, and the install says so.
    assert "window.confirm" not in flow
    assert "confirmed: true" in flow
    assert "installFailureCode(install.data.error)" in flow
    assert "started.data.code" in flow
    assert "window.openOcrQuickFix = openOcrQuickFix" in OCR_JS
    assert "fetch('/api/ocr/inventory'" in OCR_JS


def test_each_blocked_ocr_step_explains_why_in_both_languages() -> None:
    for code in ("ollama_down", "install_failed", "index_busy", "library_not_loaded", "failed"):
        for part in ("Title", "Lead", "Step1", "Step2"):
            key = f"'ocr.quickfix.{code}{part}': {{ fr: "
            assert key in I18N_JS, key


def test_pdf_folder_uses_one_apply_flow_in_settings_and_startup() -> None:
    input_position = HTML.index('id="data-path"')
    data_path_input = HTML[input_position - 40 : input_position + 180]
    assert 'value="./data"' not in data_path_input
    assert 'placeholder="./data"' in data_path_input

    assert "function syncPdfPathInputs(value, options = {})" in APP_JS
    assert "async function applyPdfDataPath(value)" in APP_JS
    assert APP_JS.count("fetch('/api/set-data-path'") == 1
    assert "const result = await applyPdfDataPath(path);" in APP_JS
    assert "const dataPathResult = await applyPdfDataPath(dataPath);" in APP_JS
    assert "syncPdfPathInputs(d.path || '');" in APP_JS

    assert 'id="data-path-browse-btn"' in HTML
    assert "selectSettingsPdfDirectory()" in STATIC_BINDINGS_JS
    assert 'id="startup-data-path-browse-btn"' in HTML
    assert 'data-i18n="startup.browsePdfFolder">Parcourir…</button>' in HTML
    assert "fetch('/api/select-data-directory'" in APP_JS
    assert 'id="startup-metadata-path-browse-btn"' in HTML
    assert 'id="zotero-path-browse-btn"' in HTML
    assert "fetch('/api/select-metadata-file'" in APP_JS
    assert "syncPdfPathInputs(data.path, { updateState: false });" in APP_JS
    assert "selectStartupPdfDirectory()" in STATIC_BINDINGS_JS

    sync_start = SETTINGS_JS.index("async function syncAppSettings(options = {})")
    sync_end = SETTINGS_JS.index("\nfunction _setVisionModelStatus", sync_start)
    assert "data_path:" not in SETTINGS_JS[sync_start:sync_end]


def test_document_sources_ask_plain_questions_with_explicit_actions() -> None:
    library = pane("documents", "assistant")

    assert 'data-i18n="settings.docs.folderTitle">Où sont vos PDF ?</span>' in library
    assert 'data-i18n="settings.docs.zoteroTitle">Utiliser ma bibliothèque Zotero</span>' in library
    assert 'id="data-path-browse-btn"' in library
    assert 'id="metadata-disconnect-btn"' in library
    assert "metadata_source" in SETTINGS_JS and "setMetadataSource('none')" in APP_JS
    assert 'id="data-path-apply-btn"' in library
    assert ">Enregistrer ce dossier</button>" in library
    assert ">Utiliser le dossier PDF de Zotero</button>" in library
    assert 'id="zotero-detect-btn"' in library
    assert 'id="zotero-load-btn"' in library
    assert ">Connecter ce fichier</button>" in library
    assert 'aria-describedby="pdf-folder-action-help"' in library
    assert 'aria-describedby="zotero-path-action-help"' in library

    assert "if (!r.ok || !d.path)" in APP_JS
    assert "showZoteroStatus(_ti('settings.metadataPathRequired'))" in APP_JS


def test_everyday_and_expert_controls_are_split_by_user_task() -> None:
    general = pane("look", "expert-generation")
    library = pane("documents", "assistant")
    preparation = pane("expert-indexing", "expert-cleanup") + pane("expert-diagnostics")

    for control_id in ("layout-reset-btn",):
        assert f'id="{control_id}"' in general
    assert 'data-theme-choice="dark"' in general
    assert 'data-language-choice="fr"' in general

    for control_id in (
        "data-path",
        "zotero-path",
    ):
        assert f'id="{control_id}"' in library

    for control_id in (
        "startup-status-panel",
        "embedding-model-select",
        "embedding-model-refresh-btn",
    ):
        assert f'id="{control_id}"' in preparation

    assert 'id="startup-status-panel"' not in library
    assert 'id="embedding-model-select"' not in library

    models = pane("assistant", "online")
    assert 'id="embedding-model-select"' not in models
    for control_id in ("provider-mode-select", "ollama-url", "remote-llm-api-key"):
        assert f'id="{control_id}"' in models
        assert f'id="{control_id}"' not in general


def test_llm_generation_settings_can_restore_recommended_defaults() -> None:
    models = pane("expert-generation", "expert-search")

    assert 'id="llm-generation-reset-btn"' in models
    assert "const LLM_GENERATION_RECOMMENDED_DEFAULTS" in SETTINGS_JS
    assert "temperature: 0.1" in SETTINGS_JS
    assert "repeatPenalty: 1.0" in SETTINGS_JS
    assert "topP: 0.5" in SETTINGS_JS
    assert "async function resetLlmGenerationSettings()" in SETTINGS_JS
    assert "successMessage: _ti('settings.recommendedValuesRestored')" in SETTINGS_JS
    assert "addEventListener('click', resetLlmGenerationSettings)" in SETTINGS_JS


def test_ollama_context_window_is_visible_and_persisted() -> None:
    models = pane("expert-generation", "expert-search")

    assert 'id="ollama-context-length"' in models
    assert '<option value="0">Auto</option>' in models
    assert '<option value="32768">32k</option>' in models
    assert 'id="ollama-context-status"' in models
    assert "ollama_context_length:" in SETTINGS_JS
    assert "async function refreshOllamaContextStatus()" in SETTINGS_JS


def test_writing_displays_token_estimate_and_segment_warning() -> None:
    assert 'id="drafting-token-estimate"' in HTML
    assert "function _draftingProofreadPlan(text)" in APP_JS
    assert "relecture automatique en environ" in APP_JS


def test_settings_search_filters_rows_and_categories() -> None:
    assert 'id="settings-search"' in HTML
    assert "filterSettings(this.value)" in STATIC_BINDINGS_JS
    assert "function filterSettings(value, options = {})" in APP_JS
    assert "settingsMatchCount" in APP_JS
    assert "settings-search-hidden" in APP_JS
    assert "settings.searchNoResults" in APP_JS


def test_web_permission_is_everyday_and_retrieval_tuning_is_expert() -> None:
    online = pane("online", "look")
    expert_search = pane("expert-search", "expert-indexing")

    assert 'id="allow-external-discovery" role="switch"' in online
    assert 'id="auto-enrich-pdf-metadata" role="switch"' in online
    assert 'id="unpaywall-email"' in online
    assert 'data-expert-focus="semantic-scholar-api-key"' in online
    assert 'id="allow-external-discovery"' not in expert_search
    assert 'data-i18n="settings.vectorSearch"' in expert_search
    assert 'id="discovery-query-planner"' in expert_search


def test_external_api_permission_errors_link_to_the_relevant_setting() -> None:
    notifications_js = (ROOT / "static" / "js" / "app" / "notifications.js").read_text(
        encoding="utf-8"
    )

    assert "function openExternalApiSettings()" in APP_JS
    assert "switchSettingsTab('online');" in APP_JS
    assert "document.getElementById('allow-external-discovery')" in APP_JS
    assert "_isExternalApiPermissionMessage(displayMsg)" in notifications_js
    assert "openExternalApiSettings();" in notifications_js
    assert "settings.openExternalApis" in notifications_js
    assert ".toast-settings-btn" in (ROOT / "static" / "css" / "app.css").read_text(
        encoding="utf-8"
    )


def test_vector_search_settings_can_restore_recommended_defaults() -> None:
    research = pane("expert-search", "expert-indexing")

    assert 'id="research-settings-reset-btn"' in research
    assert "const RESEARCH_RECOMMENDED_DEFAULTS" in SETTINGS_JS
    assert "threshold: 0.3" in SETTINGS_JS
    assert "maxChunks: 6" in SETTINGS_JS
    assert "rerankerEnabled: true" in SETTINGS_JS
    assert "rerankerProfile: 'auto'" in SETTINGS_JS
    assert "rerankerTranslateFrench: true" in SETTINGS_JS
    assert 'id="reranker-profile"' in research
    assert 'id="reranker-translate-french"' in research
    for profile in (
        "ettin_17m_cpu",
        "ettin_68m_cpu",
        "ettin_150m_cpu",
        "ettin_400m_gpu",
        "ettin_1b_gpu",
        "gte_modernbert_cpu",
        "bge_base_gpu",
        "bge_v2_m3_gpu",
    ):
        assert f'value="{profile}"' in research
    assert "settings.rerankerExcludedModels" in research
    assert "chunkSize: 1000" in SETTINGS_JS
    assert "async function resetResearchSettings()" in SETTINGS_JS
    assert "const appSaved = await syncAppSettings({ silent: false })" in SETTINGS_JS
    assert "const ragSaved = await syncRagSettings({ silent: false })" in SETTINGS_JS
    assert "addEventListener('click', resetResearchSettings)" in SETTINGS_JS


def test_pdf_metadata_enrichment_has_a_dedicated_source_aware_setting() -> None:
    assert 'id="auto-enrich-pdf-metadata"' in HTML
    assert "auto_enrich_pdf_metadata:" in SETTINGS_JS
    assert "function updatePdfMetadataEnrichmentUi()" in APP_JS
    assert "input.disabled = zoteroConnected;" in APP_JS
    assert "if (zoteroConnected) input.checked = false;" in APP_JS
    assert "settings.autoEnrichBibtexHint" in APP_JS


def test_analysis_prompt_editor_reloads_localized_prompt_payloads() -> None:
    assert "/api/analysis-types?language=${encodeURIComponent(language)}" in APP_JS
    assert "/api/analysis-prompts?language=${encodeURIComponent(language)}" in APP_JS
    assert "item.label || item.type" in APP_JS
    assert "Custom prompt active — fingerprint" in APP_JS
    assert "window.addEventListener('bibliobrain:languagechange'" in APP_JS


def test_settings_layout_uses_sidebar_navigation_and_mobile_rail() -> None:
    assert "width: min(1060px, calc(100vw - 40px))" in SETTINGS_CSS
    assert "grid-template-columns: 224px minmax(0, 1fr)" in SETTINGS_CSS
    assert (
        "flex-direction: column"
        in SETTINGS_CSS[
            SETTINGS_CSS.index("#settings-tabs") : SETTINGS_CSS.index(".settings-nav-label")
        ]
    )
    mobile = SETTINGS_CSS[SETTINGS_CSS.index("@media (max-width: 820px)") :]
    assert "grid-template-rows: auto auto minmax(0, 1fr)" in mobile
    assert "flex-direction: row" in mobile
    assert "overflow-x: auto" in mobile
    assert "#settings-overlay" in SETTINGS_CSS and "z-index: 109;" in SETTINGS_CSS
    assert "#settings-panel" in SETTINGS_CSS and "z-index: 110;" in SETTINGS_CSS


def test_debug_only_settings_rows_keep_grid_layout_when_revealed() -> None:
    parser = _SettingsParser()
    parser.feed(HTML)

    assert parser.debug_row_displays
    assert set(parser.debug_row_displays) == {"grid"}

    for control_id in ("init-btn", "force-thinking-small-models"):
        control_position = HTML.index(f'id="{control_id}"')
        debug_attr = HTML.rfind('data-debug-only="', 0, control_position)
        row_start = HTML.rfind('<div class="settings-row', 0, debug_attr)
        row_markup = HTML[row_start : HTML.index(">", debug_attr)]
        assert 'class="settings-row' in row_markup
        assert 'data-debug-only="grid"' in row_markup


def test_remote_embedding_selection_is_kept_in_expert_indexing() -> None:
    advanced = pane("expert-indexing", "expert-cleanup")

    assert 'id="remote-embedding-settings" hidden' in advanced
    assert 'id="remote-embedding-model-select"' in advanced
    assert 'id="remote-embedding-model-refresh-btn"' in advanced
    assert "fetch('/api/remote-embedding-models')" in SETTINGS_JS
    assert "provider: 'remote'" in SETTINGS_JS


def test_index_service_is_local_by_default_and_changed_in_expert_indexing() -> None:
    advanced = pane("expert-indexing", "expert-cleanup")

    assert 'id="embedding-source-select"' in advanced
    assert '<option value="local" data-i18n="settings.indexing.sourceLocal">' in advanced
    assert '<option value="chat" data-i18n="settings.indexing.sourceChat">' in advanced
    assert 'id="embedding-source-select"' not in pane("assistant", "online")
    assert "fetch('/api/set-embedding-source'" in SETTINGS_JS
    # The index has its own service: its checks never send the chat address.
    assert "fetch('/api/ollama-local-models')" in SETTINGS_JS
    assert "fetch('/api/ollama-health')" in SETTINGS_JS
    assert "fetch('/api/ollama-local-models')" in APP_JS


def test_scan_and_figure_tools_are_local_by_default_and_moved_in_expert() -> None:
    advanced = pane("expert-indexing", "expert-cleanup")

    assert 'id="document-tools-source-select"' in advanced
    assert '<option value="local" data-i18n="settings.tools.sourceLocal">' in advanced
    assert "fetch('/api/set-document-tools-source'" in SETTINGS_JS
    # The other computer is only offered once its Ollama address is known.
    assert "remoteOption.disabled = !otherComputer && source !== 'chat'" in SETTINGS_JS


def test_configured_embedding_model_is_presented_as_active() -> None:
    assert "? ' (actif)'" in SETTINGS_JS
    assert ": (!resolved && model === recommended ? ' (recommandé)' : '')" in SETTINGS_JS


def test_embeddinggemma_is_visible_and_recommended_before_installation() -> None:
    assert "EmbeddingGemma est recommandé" in HTML
    assert "EmbeddingGemma — recommandé, à installer" in SETTINGS_JS
    assert "ollama pull embeddinggemma" in SETTINGS_JS
    assert "normalized === 'embeddinggemma'" in SETTINGS_JS
    assert "d.recommended_embedding_model" in APP_JS
    assert "_normalizeOllamaModelName(backendRecommended) === 'embeddinggemma'" in SETTINGS_JS
    assert "silent: true" in SETTINGS_JS


def test_remote_embeddings_are_an_explicit_advanced_opt_in() -> None:
    advanced = pane("expert-indexing", "expert-cleanup")

    assert 'id="remote-embedding-settings"' in advanced
    assert 'id="remote-embedding-enabled"' in advanced
    assert 'id="remote-embedding-model-row" hidden' in advanced
    assert "Désactivés par défaut" in advanced
    assert ".settings-row[hidden] { display: none !important; }" in SETTINGS_CSS
    assert "remoteEmbeddingsActive = embeddingMode === 'remote'" in SETTINGS_JS
    assert "Index existant réactivé" in SETTINGS_JS
    assert "Aucune reconstruction nécessaire" in SETTINGS_JS


def test_local_embeddings_are_presented_as_the_stable_default() -> None:
    preparation = pane("expert-indexing", "expert-cleanup")

    assert "Modèle d’indexation" in preparation
    assert "Recommandé" in HTML
    assert "Indépendant du modèle de chat" in preparation
    assert "les autres index sont conserves" in (
        ROOT / "src" / "rag" / "vector_store.py"
    ).read_text(encoding="utf-8")


def test_remote_provider_choice_is_not_greyed_out_preemptively() -> None:
    assert "remoteOption.disabled = false" in SETTINGS_JS
    assert "remoteOption.disabled = !Boolean(d?.can_use_remote)" not in SETTINGS_JS


def test_lmstudio_is_available_as_a_network_provider() -> None:
    assert '<option value="lmstudio"' in HTML
    assert "if (mode === 'lmstudio') return 'lmstudio';" in SETTINGS_JS
    assert "if (normalized === 'lmstudio') return 'LM Studio';" in SETTINGS_JS
    assert "info?.lmstudio_base_url" in SETTINGS_JS


def test_compute_mode_can_be_verified_without_leaving_settings() -> None:
    models = pane("assistant", "online")
    for location in ("local", "network", "remote"):
        assert f'data-provider-location="{location}"' in models
    for software in ("ollama", "lmstudio"):
        assert f'data-provider-software="{software}"' in models
    assert "function chooseSettingsProviderLocation(location)" in APP_JS
    assert "function chooseSettingsProviderSoftware(software)" in APP_JS

    for control_id in (
        "settings-model-select",
        "verify-compute-btn",
        "compute-mode-status",
        "ollama-default-url-btn",
    ):
        assert f'id="{control_id}"' in models

    assert 'value="ollama" data-i18n="settings.localOllama" selected' in models
    assert "async function verifyComputeSettings(options = {})" in SETTINGS_JS
    assert "syncSettingsChatModelUi();" in SETTINGS_JS
    assert "forceRefresh: true" in SETTINGS_JS
    assert "defaultButton.hidden = location !== 'local'" in SETTINGS_JS


def test_ai_location_is_separate_from_the_local_software() -> None:
    # Ollama and LM Studio can both run here or on another computer.
    chooser_start = SETTINGS_JS.index("async function chooseProviderLocation(location, software = '')")
    setter_start = SETTINGS_JS.index("async function setProviderMode(mode, options = {})")
    chooser = SETTINGS_JS[chooser_start:setter_start]
    active_setter = SETTINGS_JS[
        setter_start : SETTINGS_JS.index(
            "async function loadModels(options = {})",
            setter_start,
        )
    ]

    assert "isThisComputerUrl(saved) ? saved : defaultLocalProviderUrl(mode)" in chooser
    assert "state.settingsLocationChoice = 'network'" in chooser
    assert "previousMode = activeProviderMode()" in active_setter
    assert "{ mode: normalizedMode, url: requestedUrl }" in active_setter
    assert "use_default_ollama_url" not in active_setter
    assert "effectiveMode !== normalizedMode" in active_setter


def test_remote_api_hides_the_unrelated_local_address_field() -> None:
    refresh_start = SETTINGS_JS.index("function refreshProviderUrlUi")
    refresh_end = SETTINGS_JS.index("\nfunction saveSettings", refresh_start)
    refresh_ui = SETTINGS_JS[refresh_start:refresh_end]

    assert "const remoteMode = isRemoteMode(mode)" in refresh_ui
    assert "urlInput.disabled = remoteMode" in refresh_ui
    assert "defaultButton.disabled = remoteMode" in refresh_ui
    assert "localRow.hidden = remoteMode" in refresh_ui
    assert ".settings-control input[type=text]:disabled" in SETTINGS_CSS


def test_api_keys_are_managed_as_masked_server_side_credentials() -> None:
    for field_id in (
        "remote-llm-api-key",
        "openalex-api-key",
        "semantic-scholar-api-key",
        "ollama-api-key",
        "ollama-auth-header",
        "huggingface-token",
        "bugsink-dsn",
    ):
        field_start = HTML.index(f'id="{field_id}"')
        field_markup = HTML[field_start - 40 : field_start + 220]
        assert 'type="password"' in field_markup
        assert 'autocomplete="new-password"' in field_markup

    credentials_js = SETTINGS_JS[
        SETTINGS_JS.index("const API_CREDENTIAL_FIELDS") : SETTINGS_JS.index(
            "function saveSettings()"
        )
    ]
    assert "fetch('/api/api-secrets'" in credentials_js
    assert "body: JSON.stringify(payload)" in credentials_js
    assert "input.value = '';" in credentials_js
    assert "localStorage.setItem" not in credentials_js
