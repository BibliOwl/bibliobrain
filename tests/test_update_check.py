from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import app_settings, config, update_check
from src.update_manifest import (
    DEFAULT_MANIFEST_URL,
    ManifestError,
    build_manifest,
    changelog_section,
    compare_versions,
    validate_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_URL = "https://github.com/owner/repo/releases/latest/download/latest.json"


def _manifest(version: str = "9.0.0", **installer: object) -> dict:
    return {
        "schema": 1,
        "version": version,
        "published": "2026-10-08",
        "notes": "### Ajouté\n\n- Une nouveauté.",
        "notes_url": "https://github.com/owner/repo/releases/tag/v" + version,
        "installer": {
            "url": "https://github.com/owner/repo/releases/download/v9.0.0/Bibliobrain-Setup.exe",
            "sha256": "a" * 64,
            "size": 10,
            **installer,
        },
    }


@pytest.fixture
def isolated_home(workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    monkeypatch.setattr(config, "RUNTIME_DIR", str(workspace_tmp_dir / "runtime"))
    monkeypatch.delenv("BIBLIOBRAIN_UPDATE_MANIFEST_URL", raising=False)
    monkeypatch.delenv("BIBLIOBRAIN_MANAGED_SERVER", raising=False)
    monkeypatch.setattr(update_check, "get_app_version", lambda: "1.0.0")
    with update_check._job_lock:
        update_check._job.update(phase="idle", percent=None, error="")
    return workspace_tmp_dir


VERSION_ORDER_CASES = [
    ("0.7.35", "0.7.34", 1),
    ("0.7.34", "0.7.34", 0),
    ("0.7.9", "0.7.10", -1),
    ("1.0.0-beta.1", "1.0.0", -1),
    ("1.0.0", "1.0.0-beta.1", 1),
    ("1.0.0-beta.2", "1.0.0-beta.10", -1),
    ("1.0.0-beta", "1.0.0-alpha", 1),
    ("1.0.0-beta", "1.0.0-beta.1", -1),
    ("1.0.0-rc.1", "1.0.0-1", 1),
    ("1.0.0-beta.1", "0.99.99", 1),
    ("1.0.0", "0.99.99", 1),
]


@pytest.mark.parametrize(("left", "right", "expected"), VERSION_ORDER_CASES)
def test_versions_compare_like_semver(left: str, right: str, expected: int) -> None:
    assert compare_versions(left, right) == expected


@pytest.mark.skipif(os.name != "nt", reason="The maintenance manager is a Windows PowerShell script")
def test_manager_orders_versions_like_the_application() -> None:
    """The manager accepts or refuses an installer with the application's own ordering.

    ``[version]`` rejected the prerelease suffix, so a beta could neither be
    installed nor replaced through ``Update-Bibliobrain.cmd``.
    """
    manager = ROOT / "scripts" / "bibliobrain-manager.ps1"
    pairs = ", ".join(f"@('{left}', '{right}')" for left, right, _ in VERSION_ORDER_CASES)
    script = (
        f"$ast = [System.Management.Automation.Language.Parser]::ParseFile('{manager}', [ref]$null, [ref]$null); "
        "$functions = $ast.FindAll({ param($node) "
        "$node -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and $node.Name -like '*-BibliobrainVersion' }, $false); "
        "foreach ($function in $functions) { . ([scriptblock]::Create($function.Extent.Text)) }; "
        f"foreach ($pair in @({pairs})) {{ "
        "Compare-BibliobrainVersion -Left (ConvertTo-BibliobrainVersion $pair[0]) "
        "-Right (ConvertTo-BibliobrainVersion $pair[1]) }; "
        "if ($null -ne (ConvertTo-BibliobrainVersion 'not-a-version')) { exit 3 }"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert [int(value) for value in result.stdout.split()] == [
        expected for _, _, expected in VERSION_ORDER_CASES
    ]


def test_manifest_validation_accepts_a_release_from_the_manifest_host() -> None:
    normalized = validate_manifest(_manifest(), manifest_url=MANIFEST_URL)

    assert normalized["version"] == "9.0.0"
    assert normalized["installer"]["sha256"] == "a" * 64


@pytest.mark.parametrize(
    "installer",
    [
        {"url": "http://github.com/owner/repo/Bibliobrain-Setup.exe"},
        {"url": "https://evil.example/Bibliobrain-Setup.exe"},
        {"url": "https://user:secret@github.com/owner/repo/Bibliobrain-Setup.exe"},
        {"sha256": "not-a-digest"},
        {"size": -1},
    ],
)
def test_manifest_validation_rejects_unsafe_installers(installer: dict) -> None:
    with pytest.raises(ManifestError):
        validate_manifest(_manifest(**installer), manifest_url=MANIFEST_URL)


ARCHIVE = {
    "url": "https://github.com/owner/repo/releases/download/v9.0.0/Bibliobrain-Windows.zip",
    "sha256": "b" * 64,
    "size": 12,
}


def test_manifest_archive_is_optional_and_checked_like_the_installer() -> None:
    assert validate_manifest(_manifest(), manifest_url=MANIFEST_URL)["archive"] is None

    normalized = validate_manifest({**_manifest(), "archive": ARCHIVE}, manifest_url=MANIFEST_URL)
    assert normalized["archive"] == ARCHIVE

    foreign = {**ARCHIVE, "url": "https://evil.example/Bibliobrain-Windows.zip"}
    with pytest.raises(ManifestError, match="archive.url"):
        validate_manifest({**_manifest(), "archive": foreign}, manifest_url=MANIFEST_URL)


def test_manifest_validation_rejects_unknown_schema_and_bad_versions() -> None:
    with pytest.raises(ManifestError):
        validate_manifest({**_manifest(), "schema": 2})
    with pytest.raises(ManifestError):
        validate_manifest({**_manifest(), "version": "latest"})


def test_default_manifest_is_a_static_github_release_asset() -> None:
    # One static file: no API, no query string, nothing about the user.
    assert DEFAULT_MANIFEST_URL.startswith("https://github.com/")
    assert DEFAULT_MANIFEST_URL.endswith("/releases/latest/download/latest.json")
    assert "?" not in DEFAULT_MANIFEST_URL


def test_changelog_section_stops_at_the_next_version_or_links() -> None:
    changelog = (
        "# Changelog\n\n## [Non publié]\n\n- WIP\n\n## [0.8.0] - 2026-10-20\n\n"
        "### Ajouté\n\n- Mises à jour.\n\n## [0.7.3] - 2026-08-23\n\n- Ancien.\n\n"
        "[Non publié]: https://example.org/compare/v0.8.0...HEAD\n"
    )

    assert changelog_section(changelog, "0.8.0") == "### Ajouté\n\n- Mises à jour."
    assert changelog_section(changelog, "0.7.3") == "- Ancien."
    assert changelog_section(changelog, "9.9.9") == ""


def test_build_manifest_hashes_the_installer(workspace_tmp_dir: Path) -> None:
    installer = workspace_tmp_dir / "Bibliobrain-Setup.exe"
    installer.write_bytes(b"installer bytes")

    manifest = build_manifest(
        version="0.8.0",
        installer_path=installer,
        release_base_url="https://github.com/owner/repo/releases/download/v0.8.0/",
    )

    assert manifest["installer"] == {
        "url": "https://github.com/owner/repo/releases/download/v0.8.0/Bibliobrain-Setup.exe",
        "sha256": hashlib.sha256(b"installer bytes").hexdigest(),
        "size": len(b"installer bytes"),
    }


def test_build_manifest_describes_the_zip_package(workspace_tmp_dir: Path) -> None:
    installer = workspace_tmp_dir / "Bibliobrain-Setup.exe"
    installer.write_bytes(b"installer bytes")
    archive = workspace_tmp_dir / "Bibliobrain-Windows.zip"
    archive.write_bytes(b"zip bytes")

    manifest = build_manifest(
        version="0.8.0",
        installer_path=installer,
        archive_path=archive,
        release_base_url="https://github.com/owner/repo/releases/download/v0.8.0",
    )

    assert manifest["archive"] == {
        "url": "https://github.com/owner/repo/releases/download/v0.8.0/Bibliobrain-Windows.zip",
        "sha256": hashlib.sha256(b"zip bytes").hexdigest(),
        "size": len(b"zip bytes"),
    }


def _load_release_tools():
    spec = importlib.util.spec_from_file_location("release_tools", ROOT / "scripts/release_tools.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_prepare_release_dates_the_unreleased_section_and_links() -> None:
    release_tools = _load_release_tools()
    changelog = (
        "## [Non publié]\n\n### Ajouté\n\n- Mises à jour.\n\n## [0.7.3] - 2026-08-23\n\n"
        "[Non publié]: https://example.org/repo/compare/v0.7.3...HEAD\n"
        "[0.7.3]: https://example.org/repo/compare/v0.7.2...v0.7.3\n"
    )

    prepared = release_tools.prepare_changelog(changelog, "0.8.0", "2026-10-20")

    assert prepared.startswith("## [Non publié]\n\n## [0.8.0] - 2026-10-20\n\n### Ajouté")
    assert "[Non publié]: https://example.org/repo/compare/v0.8.0...HEAD" in prepared
    assert "[0.8.0]: https://example.org/repo/compare/v0.7.3...v0.8.0" in prepared
    with pytest.raises(SystemExit):
        release_tools.prepare_changelog(prepared, "0.8.0", "2026-10-21")


def test_nothing_is_fetched_before_the_user_answered(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(update_check, "_fetch_manifest", lambda url: calls.append(url) or {})

    status = update_check.check_now()

    assert calls == []
    assert status["enabled"] is None
    assert status["checked_at"] == ""


def test_enabled_check_runs_once_per_day_and_reports_the_update(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake_fetch(url: str) -> dict:
        calls.append(url)
        return validate_manifest(_manifest(), manifest_url=url)

    monkeypatch.setattr(update_check, "_fetch_manifest", fake_fetch)
    app_settings.set_app_settings(update_check_enabled=True)

    first = update_check.check_now()
    second = update_check.check_now()

    assert calls == [DEFAULT_MANIFEST_URL]
    assert first["update_available"] is True
    assert first["latest_version"] == "9.0.0"
    assert first["notes"].startswith("### Ajouté")
    assert second["checked_at"] == first["checked_at"]

    state = json.loads((isolated_home / "storage/update_check.json").read_text(encoding="utf-8"))
    state["checked_at"] = time.time() - update_check.CHECK_INTERVAL_SECONDS - 1
    (isolated_home / "storage/update_check.json").write_text(json.dumps(state), encoding="utf-8")
    update_check.check_now()
    assert len(calls) == 2


def test_manual_check_works_even_when_daily_checks_are_off(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(url: str) -> dict:
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))

    monkeypatch.setattr(update_check, "_fetch_manifest", missing)
    app_settings.set_app_settings(update_check_enabled=False)

    status = update_check.check_now(force=True)

    assert status["enabled"] is False
    assert status["last_error"] == "not_found"
    assert status["update_available"] is False


def test_configured_manifest_url_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_UPDATE_MANIFEST_URL", "https://git.example.org/latest.json")

    assert update_check.manifest_url() == "https://git.example.org/latest.json"


def test_existing_installs_are_asked_once_their_setup_is_done(isolated_home: Path) -> None:
    assert update_check.get_status()["ask_consent"] is False

    app_settings.set_app_settings(onboarding_done_v1=True)
    assert update_check.get_status()["ask_consent"] is True

    app_settings.set_app_settings(update_check_enabled=False)
    assert update_check.get_status()["ask_consent"] is False


def _client_with_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.Client
    monkeypatch.setattr(
        update_check.httpx,
        "Client",
        functools.partial(real_client, transport=httpx.MockTransport(handler)),
    )


def test_installer_download_is_verified_against_the_manifest(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"signed installer"
    _client_with_transport(monkeypatch, lambda request: httpx.Response(200, content=payload))
    installer = {
        "url": "https://github.com/owner/repo/releases/download/v9.0.0/Bibliobrain-Setup.exe",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }

    path = update_check._download_installer(installer, "9.0.0")

    assert path.name == "Bibliobrain-Setup-9.0.0.exe"
    assert path.read_bytes() == payload


def test_tampered_installer_is_deleted_and_never_launched(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _client_with_transport(monkeypatch, lambda request: httpx.Response(200, content=b"tampered!!"))
    launched: list[list[str]] = []
    monkeypatch.setattr(update_check, "_launch_manager", launched.append)
    monkeypatch.setattr(update_check, "install_mode", lambda: "packaged")

    update_check._run_update(validate_manifest(_manifest()))

    assert launched == []
    assert update_check._job_snapshot()["phase"] == "failed"
    assert update_check._job_snapshot()["error"] == "invalid_manifest"
    assert list((isolated_home / "storage/updates").iterdir()) == []


def test_manager_command_runs_the_app_driven_update() -> None:
    command = update_check._manager_command(Path("C:/updates/Bibliobrain-Setup-9.0.0.exe"))

    assert command[:6] == [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(update_check.MANAGER_SCRIPT),
    ]
    assert ["-Action", "update"] == command[6:8]
    assert "-FromApp" in command and "-NoBrowser" in command
    assert command[-2:] == ["-InstallerPath", str(Path("C:/updates/Bibliobrain-Setup-9.0.0.exe"))]


def test_manager_command_passes_a_zip_package_as_archive() -> None:
    command = update_check._manager_command(Path("C:/updates/Bibliobrain-Windows-9.0.0.zip"))

    assert command[-2:] == ["-ArchivePath", str(Path("C:/updates/Bibliobrain-Windows-9.0.0.zip"))]
    assert "-InstallerPath" not in command


def test_zip_installs_update_from_the_archive_never_the_installer(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_root = isolated_home / "install"
    install_root.mkdir()
    monkeypatch.setattr(update_check, "REPO_ROOT", install_root)
    assert update_check.install_mode() == "packaged"
    (install_root / "ZIP-PACKAGE.txt").write_text("zip", encoding="utf-8")
    assert update_check.install_mode() == "zip"

    monkeypatch.setattr(update_check, "MANAGER_SCRIPT", ROOT / "scripts/bibliobrain-manager.ps1")
    monkeypatch.setattr(update_check.os, "name", "nt")
    monkeypatch.setenv("BIBLIOBRAIN_MANAGED_SERVER", "1")
    # A release without a ZIP package must not fall back to Setup.exe.
    assert update_check._install_blocker(validate_manifest(_manifest())) == "no_archive"

    payload = b"zip package"
    archive = {**ARCHIVE, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
    requested: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=payload)

    _client_with_transport(monkeypatch, serve)
    launched: list[list[str]] = []
    monkeypatch.setattr(update_check, "_launch_manager", launched.append)
    manifest = validate_manifest({**_manifest(), "archive": archive})
    assert update_check._install_blocker(manifest) == ""

    update_check._run_update(manifest)

    assert requested == [ARCHIVE["url"]]
    downloaded = isolated_home / "storage/updates/Bibliobrain-Windows-9.0.0.zip"
    assert downloaded.read_bytes() == payload
    assert launched[0][-2:] == ["-ArchivePath", str(downloaded)]


def test_development_servers_never_update_themselves(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_check, "_fetch_manifest", lambda url: validate_manifest(_manifest()))
    update_check.check_now(force=True)
    monkeypatch.setattr(update_check.os, "name", "nt")

    result = update_check.start_update()

    assert result == {"ok": False, "code": "not_managed"}


def test_update_routes(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.api import updates

    monkeypatch.setattr(update_check, "_fetch_manifest", lambda url: validate_manifest(_manifest()))
    app = FastAPI()
    app.include_router(updates.router)
    client = TestClient(app)

    assert client.get("/api/update/status").json()["update_available"] is False
    checked = client.post("/api/update/check?force=true").json()
    assert checked["update_available"] is True
    started = client.post("/api/update/start").json()
    assert started["ok"] is False
    assert started["code"] in {"not_managed", "unsupported"}


def test_packaging_supports_the_in_app_update() -> None:
    inno = (ROOT / "packaging/Bibliobrain.iss").read_text(encoding="utf-8")
    manager = (ROOT / "scripts/bibliobrain-manager.ps1").read_text(encoding="utf-8")
    builder = (ROOT / "packaging/build-installer.ps1").read_text(encoding="utf-8")

    install_delete = inno.split("[InstallDelete]", 1)[1].split("[Files]", 1)[0]
    assert 'Name: "{app}\\src"' in install_delete
    assert 'Name: "{app}\\static"' in install_delete
    assert 'Name: "{app}\\.venv"' not in install_delete
    assert "InstallScriptArguments," in inno
    assert "{param:UPDATEMODE|0}" in inno
    # No second browser tab, and no console question about Ollama models.
    assert "Result := Result + ' -SkipOllamaSetup'" in inno
    assert "-SkipShortcuts -NoLaunch" in inno
    # User guides ship from wiki/; private docs/ is never packaged.
    assert '\\wiki\\*"; DestDir: "{app}\\wiki"' in inno
    assert "\\docs\\" not in inno

    assert "[switch]$FromApp" in manager
    assert '"/UPDATEMODE=1"' in manager
    assert "function Invoke-UpdateFromApp" in manager
    assert "update-result.json" in manager
    assert "Bibliobrain-Backup-BeforeUpdate-" in manager
    # Setup returns 20 when install.ps1 failed; the page then points to the repair.
    assert "$process.ExitCode -eq 20" in manager
    assert "run Repair-Bibliobrain.cmd to finish the update" in manager
    assert '$installArguments += "-SkipOllamaSetup"' in manager
    assert "[version]::TryParse" not in manager

    assert "$ReleaseBaseUrl" in builder
    assert "release_tools.py" in builder
