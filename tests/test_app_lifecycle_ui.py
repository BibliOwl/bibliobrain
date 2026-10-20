
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]


def test_managed_app_exposes_visible_and_automatic_shutdown_controls() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    javascript = read_app_javascript()
    manager = (ROOT / "scripts" / "bibliobrain-manager.ps1").read_text(encoding="utf-8")

    assert 'id="app-quit-btn"' in html
    assert 'id="app-quit-settings-btn"' in html
    assert "function shutdownBibliobrain()" in javascript
    assert "'/api/app/shutdown'" in javascript
    assert "'/api/app/window/open'" in javascript
    assert "'/api/app/window/close'" in javascript
    assert "closeBibliobrainWindowSession();" in javascript
    assert "_bibliobrainHeartbeatTimer" not in javascript
    assert '"src.managed_server"' in manager


def test_managed_server_launcher_marks_the_runtime_as_desktop_managed() -> None:
    launcher = (ROOT / "src" / "managed_server.py").read_text(encoding="utf-8")

    assert 'os.environ["BIBLIOBRAIN_MANAGED_SERVER"] = "1"' in launcher
    assert 'uvicorn.run("app_api:app"' in launcher
    assert "configure_logging(LOG_DIR)" in launcher
    assert "log_config=None" in launcher


def test_manager_keeps_runtime_state_with_the_user_data() -> None:
    manager = (ROOT / "scripts" / "bibliobrain-manager.ps1").read_text(encoding="utf-8")

    # Runtime state and logs live with the user data, never in the code folder.
    assert '$StateDir = Join-Path $BibliobrainHome "runtime"' in manager
    assert '$LogDir = Join-Path $BibliobrainHome "logs"' in manager
    assert '$LegacyStateDir = Join-Path $RepoRoot ".bibliobrain"' in manager
    assert "function Move-LegacyRuntimeState" in manager
    assert 'Join-Path $LogDir "server.err.log"' in manager
    assert 'Join-Path $StateDir "server.err.log"' not in manager
    assert '$BindHost = "127.0.0.1"' in manager
