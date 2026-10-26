
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = read_app_javascript()
SETTINGS_JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
WORKSPACES_CSS = (ROOT / "static" / "css" / "workspaces.css").read_text(encoding="utf-8")
BOOT_JS = (ROOT / "static" / "js" / "boot.js").read_text(encoding="utf-8")


def test_performance_mode_is_applied_before_stylesheets() -> None:
    boot_marker = '<script src="/static/js/boot.js'
    stylesheet_marker = '<link rel="stylesheet" href="/static/css/app.css'
    assert INDEX.index(boot_marker) < INDEX.index(stylesheet_marker)
    assert "data-performance-mode" in BOOT_JS
    assert "iassistant_performance-mode" in BOOT_JS
    assert "navigator.hardwareConcurrency" in BOOT_JS
    assert "prefers-reduced-motion: reduce" in BOOT_JS


def test_performance_control_is_hidden_in_debug_expert_diagnostics() -> None:
    advanced_start = INDEX.index('id="settings-pane-expert-diagnostics"')
    advanced_end = INDEX.index('<div id="kg-modal-overlay"')
    control_position = INDEX.index('id="performance-mode-select"')
    assert advanced_start < control_position < advanced_end
    nearby_markup = INDEX[control_position - 800 : control_position]
    assert 'data-debug-only="grid"' in nearby_markup
    assert '<option value="auto"' in INDEX
    assert '<option value="normal"' in INDEX
    assert '<option value="light"' in INDEX


def test_performance_preference_is_local_and_reacts_to_system_changes() -> None:
    assert "PERFORMANCE_MODE_STORAGE_KEY" in SETTINGS_JS
    assert "getAutomaticLightModeReasons" in SETTINGS_JS
    assert "applyPerformanceModePreference" in SETTINGS_JS
    assert "bibliobrain:performancechange" in SETTINGS_JS
    assert "setPerformanceMode" in SETTINGS_JS


def test_light_mode_reduces_rendering_work_without_changing_results() -> None:
    assert "return isLightPerformanceMode() ? 140 : 50" in APP_JS
    assert APP_JS.count("streamRenderIntervalMs()") >= 3
    # The citation graph drops its animation altogether in light mode and stops
    # its loop while the page is hidden or the graph closed.
    assert "isLightPerformanceMode === 'function' && isLightPerformanceMode()) return false" in APP_JS
    assert "if (!_nbCgIsOpen() || document.hidden" in APP_JS
    assert "scheduleMutationTranslations" in I18N_JS
    assert "window.setTimeout(flushMutationTranslations, 80)" in I18N_JS


def test_light_mode_disables_expensive_visual_effects() -> None:
    selector = 'html[data-performance-mode="light"] *'
    assert selector in APP_CSS
    assert "backdrop-filter: none !important" in APP_CSS
    assert "animation: none !important" in APP_CSS
    assert 'html[data-performance-mode="light"] .spinner' in APP_CSS


def test_micro_animations_are_subtle_and_disabled_in_light_mode() -> None:
    assert "@keyframes surface-enter" in APP_CSS
    assert ".library-detail-pane.active," in APP_CSS
    assert ".discovery-view-panel:not([hidden])," in APP_CSS
    assert "#toast.show { opacity: 1; transform: translate(-50%, 0) scale(1); }" in APP_CSS
    assert ".workspace-dialog[open] { animation: surface-enter" in WORKSPACES_CSS
    assert ".drafting-view:not([hidden]) { animation: surface-enter" in WORKSPACES_CSS

    light_mode_block = APP_CSS.index(
        'html[data-performance-mode="light"] .tab-panel.active,'
    )
    light_mode_end = APP_CSS.index(
        'html[data-performance-mode="light"] #toast', light_mode_block
    )
    disabled_selectors = APP_CSS[light_mode_block:light_mode_end]
    assert ".workspace-dialog[open]" in disabled_selectors
    assert ".drafting-view:not([hidden])" in disabled_selectors
    assert "animation: none !important" in disabled_selectors
    assert 'html[data-performance-mode="light"] .workspace-button,' in APP_CSS
    assert "transition: none !important" in APP_CSS
