from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
SETTINGS_JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
SETTINGS_CSS = (ROOT / "static" / "css" / "settings.css").read_text(encoding="utf-8")
APP_JS = read_app_javascript()
CORE_APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")


def test_gpu_status_refresh_does_not_write_provider_configuration() -> None:
    assert "await syncOllamaBaseUrl({ silent: true, skipGpuRefresh: true });" not in SETTINGS_JS


def test_redundant_provider_url_response_keeps_frontend_rag_ready() -> None:
    start = SETTINGS_JS.index("async function syncOllamaBaseUrl(options = {})")
    setter = SETTINGS_JS[start : SETTINGS_JS.index("async function loadEmbeddingModels", start)]
    unchanged_branch = setter.index("if (d.changed === false)")
    assert setter.index("return d.info;", unchanged_branch) < setter.index("state.ragReady = false")


def test_loaded_gpu_models_are_debug_only() -> None:
    assert 'id="gpu-debug-panel" data-debug-only="block" style="display:none"' in INDEX_HTML
    assert INDEX_HTML.index('id="gpu-status"') > INDEX_HTML.index('id="gpu-debug-panel"')
    assert "function isGpuDebugPanelEnabled()" in SETTINGS_JS
    assert "if (!isGpuDebugPanelEnabled()) return false;" in SETTINGS_JS
    assert SETTINGS_JS.count("if (!isGpuDebugPanelEnabled()) {") == 1


def test_gpu_model_chips_wrap_instead_of_clipping_text() -> None:
    chip_rule = APP_CSS.split(".gpu-chip {", 1)[1].split("}", 1)[0]
    assert "overflow-wrap: anywhere" in chip_rule
    assert "white-space: normal" in chip_rule
    assert "text-overflow: ellipsis" not in chip_rule


def test_sidebar_runtime_status_truncates_without_moving_action_buttons() -> None:
    row_rule = APP_CSS.split("\n#sidebar-bottom-row {", 1)[1].split("}", 1)[0]
    actions_rule = APP_CSS.split("\n#sidebar-bottom-actions {", 1)[1].split("}", 1)[0]
    summary_rule = APP_CSS.split("\n#rag-status-summary {", 1)[1].split("}", 1)[0]
    status_rule = SETTINGS_CSS.split("#rag-status-text {", 1)[1].split("}", 1)[0]
    assert "flex-wrap: wrap" in row_rule
    assert "flex: 0 0 auto" in actions_rule
    assert "flex: 1 1 72px" in summary_rule
    assert "justify-content: flex-start" in summary_rule
    assert "overflow: hidden" in status_rule
    assert "white-space: nowrap" in status_rule
    assert "text-overflow: ellipsis" in status_rule


def test_ready_runtime_status_uses_the_active_interface_language() -> None:
    assert "setRagIndicator('ready', 'Pret'" not in APP_JS
    assert APP_JS.count("ragStatusReady('ui.ready'") >= 2


def test_dynamic_runtime_status_is_not_overwritten_by_static_i18n() -> None:
    status_tag = INDEX_HTML.split('<span id="rag-status-text"', 1)[1].split(">", 1)[0]
    assert "data-i18n=" not in status_tag
    assert "const translatedKey = _ti(raw);" in CORE_APP_JS
    assert "const translatedLabel = _ragStatusText(label);" in CORE_APP_JS


def test_switching_chat_model_does_not_restart_ready_rag() -> None:
    start = SETTINGS_JS.index("document.getElementById('model-select').addEventListener('change'")
    end = SETTINGS_JS.index("\n}\n\n// ---------------------------------------------\n//  INIT", start)
    handler = SETTINGS_JS[start:end]

    assert "rememberSelectedChatModel(selectedModel)" in handler
    assert "await syncAppSettings({ silent: false })" in handler
    assert "await initRAG(" not in handler
    assert "state.ragReady = false" not in handler


def test_sidebar_runtime_dot_is_vertically_centered() -> None:
    dot_rule = SETTINGS_CSS.split("#rag-status-dot {", 1)[1].split("}", 1)[0]
    assert "margin: 0" in dot_rule
    assert "align-self: center" in dot_rule
    assert "margin-bottom" not in dot_rule
