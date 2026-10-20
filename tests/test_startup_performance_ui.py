
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]


def _function_body(source: str, start: str, end: str = "\n}\n") -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def test_startup_shell_is_released_before_network_boot() -> None:
    javascript = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    handler = javascript[javascript.index("window.addEventListener('DOMContentLoaded', async () => {") :]

    assert handler.index("hideStartupBootSplash();") < handler.index("await Promise.all([")
    assert handler.index("const bootPromise = bootApplicationAuto();") < handler.index(
        "await loadChatHistory();"
    )
    assert "await bootPromise;" in handler


def test_automatic_boot_does_not_repeat_slow_startup_probes() -> None:
    javascript = read_app_javascript()
    data_path_bootstrap = _function_body(
        javascript,
        "async function bootstrapDataPath()",
        "async function bootApplicationAuto()",
    )
    automatic_boot = _function_body(
        javascript,
        "async function bootApplicationAuto()",
    )

    assert "loadLibrary(" not in data_path_bootstrap
    assert "ollamaHealthPromise" not in automatic_boot
    assert "state.startupStatus" in automatic_boot
    assert automatic_boot.count("fetchStartupStatus()") == 1
    assert "hasLibrarySnapshot ? Promise.resolve(zoteroState) : loadLibrary()" in automatic_boot


def test_first_run_waits_for_the_assistant_before_indexing() -> None:
    javascript = read_app_javascript()
    automatic_boot = _function_body(
        javascript,
        "async function bootApplicationAuto()",
    )
    guide_needed = _function_body(
        javascript,
        "function isStartupGuideNeeded(",
        "function _startupGuideBuilding(",
    )
    settings_js = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    first_init = settings_js[settings_js.index("async function runFirstRagInit()") :]

    assert "onboarding_done_v1" in guide_needed
    assert "first_rag_init_done" in guide_needed
    assert automatic_boot.index("isStartupGuideNeeded(startup)") < automatic_boot.index(
        "scheduleStartupAutoInit("
    )
    assert first_init.index("openStartupAssistant();") < first_init.index(
        "runStartupAutoInit(true)"
    )


def test_settings_preparation_shows_a_single_status_line() -> None:
    javascript = read_app_javascript()
    status = _function_body(
        javascript,
        "function renderStartupStatus(",
        "async function runStartupAutoInit(",
    )

    assert "setStartupGuideStatus('startup-status-line'" in status
    assert "innerHTML" not in status
