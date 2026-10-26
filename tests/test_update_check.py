from __future__ import annotations

import functools
import hashlib
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.skipif(
    os.name != "nt", reason="The maintenance manager is a Windows PowerShell script"
)
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
        "# Changelog\n\n## [Unreleased]\n\n- WIP\n\n## [0.8.0] - 2026-10-20\n\n"
        "### Added\n\n- Updates.\n\n## [0.7.3] - 2026-08-23\n\n- Older.\n\n"
        "[Unreleased]: https://example.org/compare/v0.8.0...HEAD\n"
    )

    assert changelog_section(changelog, "0.8.0") == "### Added\n\n- Updates."
    assert changelog_section(changelog, "0.7.3") == "- Older."
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
    spec = importlib.util.spec_from_file_location(
        "release_tools", ROOT / "scripts/release_tools.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_release_dates_the_unreleased_section_and_links() -> None:
    release_tools = _load_release_tools()
    changelog = (
        "## [Unreleased]\n\n### Added\n\n- Updates.\n\n## [0.7.3] - 2026-08-23\n\n"
        "[Unreleased]: https://example.org/repo/compare/v0.7.3...HEAD\n"
        "[0.7.3]: https://example.org/repo/compare/v0.7.2...v0.7.3\n"
    )

    prepared = release_tools.prepare_changelog(changelog, "0.8.0", "2026-10-20")

    assert prepared.startswith("## [Unreleased]\n\n## [0.8.0] - 2026-10-20\n\n### Added")
    assert "[Unreleased]: https://example.org/repo/compare/v0.8.0...HEAD" in prepared
    assert "[0.8.0]: https://example.org/repo/compare/v0.7.3...v0.8.0" in prepared
    with pytest.raises(SystemExit):
        release_tools.prepare_changelog(prepared, "0.8.0", "2026-10-21")


def test_nothing_is_fetched_before_the_user_answered(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fetch_manifest(url: str) -> dict:
        calls.append(url)
        return {}

    monkeypatch.setattr(update_check, "_fetch_manifest", fetch_manifest)

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
        raise httpx.HTTPStatusError(
            "404", request=request, response=httpx.Response(404, request=request)
        )

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


def test_zip_installs_update_from_the_archive_never_the_installer(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_root = isolated_home / "install"
    install_root.mkdir()
    monkeypatch.setattr(update_check, "REPO_ROOT", install_root)
    monkeypatch.delenv("BIBLIOBRAIN_INSTALL_MODE", raising=False)
    assert update_check.install_mode() == "packaged"
    (install_root / "ZIP-PACKAGE.txt").write_text("zip", encoding="utf-8")
    assert update_check.install_mode() == "zip"

    monkeypatch.setattr(update_check, "MANAGER_SCRIPT", ROOT / "scripts/bibliobrain-manager.ps1")
    monkeypatch.setattr(update_check, "_is_windows", lambda: True)
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


def test_zip_installs_also_get_the_manual_steps_and_a_background_download(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_INSTALL_MODE", "zip")
    monkeypatch.setattr(update_check, "_is_windows", lambda: True)
    monkeypatch.setattr(update_check, "_last_auto_attempt", 0.0)
    payload = b"zip package"
    archive = {**ARCHIVE, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
    manifest = validate_manifest({**_manifest(), "archive": archive})
    update_check._save_state({"checked_at": time.time(), "manifest": manifest})
    requested: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=payload)

    _client_with_transport(monkeypatch, serve)
    app_settings.set_app_settings(update_check_enabled=True)

    assert update_check.get_status()["zip_guide"] == {
        "download_url": ARCHIVE["url"],
        "releases_url": manifest["notes_url"],
        "install_dir": str(update_check.REPO_ROOT),
        "file_name": "Bibliobrain-Windows.zip",
    }
    assert update_check.start_background_download() is True
    _wait_for_job("ready")

    assert requested == [ARCHIVE["url"]]
    assert update_check.get_status()["downloaded"] is True
    launched: list[list[str]] = []
    monkeypatch.setattr(update_check, "_launch_manager", launched.append)
    update_check._run_update(manifest)
    assert len(requested) == 1
    assert launched[0][-2:] == [
        "-ArchivePath",
        str(isolated_home / "storage/updates/Bibliobrain-Windows-9.0.0.zip"),
    ]


def test_manager_command_passes_a_zip_package_as_archive() -> None:
    command = update_check._manager_command(Path("C:/updates/Bibliobrain-Windows-9.0.0.zip"))

    assert command[-2:] == ["-ArchivePath", str(Path("C:/updates/Bibliobrain-Windows-9.0.0.zip"))]
    assert "-InstallerPath" not in command


def test_install_mode_can_be_forced_to_preview_its_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_INSTALL_MODE", "zip")
    assert update_check.install_mode() == "zip"
    monkeypatch.setenv("BIBLIOBRAIN_INSTALL_MODE", "nonsense")
    assert update_check.install_mode() in {"git", "zip", "packaged"}


def _packaged_windows(isolated_home: Path, monkeypatch: pytest.MonkeyPatch, payload: bytes) -> dict:
    monkeypatch.setenv("BIBLIOBRAIN_INSTALL_MODE", "packaged")
    monkeypatch.setattr(update_check, "_is_windows", lambda: True)
    monkeypatch.setattr(update_check, "_last_auto_attempt", 0.0)
    manifest = validate_manifest(
        _manifest(sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))
    )
    update_check._save_state({"checked_at": time.time(), "manifest": manifest})
    return manifest


def _wait_for_job(*phases: str) -> dict:
    deadline = time.time() + 5
    while time.time() < deadline:
        job = update_check._job_snapshot()
        if job["phase"] in phases:
            return job
        time.sleep(0.02)
    raise AssertionError(update_check._job_snapshot())


def test_packaged_installs_download_the_installer_ahead_of_the_click(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"signed installer"
    manifest = _packaged_windows(isolated_home, monkeypatch, payload)
    requested: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=payload)

    _client_with_transport(monkeypatch, serve)

    # No consent yet: nothing leaves the machine.
    assert update_check.start_background_download() is False
    app_settings.set_app_settings(update_check_enabled=True)
    assert update_check.start_background_download() is True
    _wait_for_job("ready")

    assert update_check._ready_package(manifest, "packaged") is not None
    assert update_check.get_status()["downloaded"] is True
    # Already on disk: no second download.
    assert update_check.start_background_download() is False
    assert len(requested) == 1

    # The click installs the verified file without downloading it again.
    launched: list[list[str]] = []
    monkeypatch.setattr(update_check, "_launch_manager", launched.append)
    update_check._run_update(manifest)
    assert len(requested) == 1
    assert launched[0][-2:] == [
        "-InstallerPath",
        str(isolated_home / "storage/updates/Bibliobrain-Setup-9.0.0.exe"),
    ]


def test_an_installer_altered_on_disk_is_downloaded_again(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"signed installer"
    manifest = _packaged_windows(isolated_home, monkeypatch, payload)
    _client_with_transport(monkeypatch, lambda request: httpx.Response(200, content=payload))
    update_check._download_installer(manifest["installer"], "9.0.0")
    update_check._remember_download(manifest, manifest["installer"])
    target = isolated_home / "storage/updates/Bibliobrain-Setup-9.0.0.exe"
    target.write_bytes(b"x" * len(payload))

    assert update_check._verified_ready_package(manifest, "packaged") is None
    assert not target.exists()


def test_a_click_during_the_background_download_installs_when_it_ends(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"signed installer"
    manifest = _packaged_windows(isolated_home, monkeypatch, payload)
    _client_with_transport(monkeypatch, lambda request: httpx.Response(200, content=payload))
    launched: list[list[str]] = []
    monkeypatch.setattr(update_check, "_launch_manager", launched.append)
    with update_check._job_lock:
        update_check._job.update(phase="downloading", percent=10, background=True)
    monkeypatch.setenv("BIBLIOBRAIN_MANAGED_SERVER", "1")
    monkeypatch.setattr(update_check, "MANAGER_SCRIPT", ROOT / "scripts/bibliobrain-manager.ps1")

    assert update_check.start_update() == {"ok": True, "code": ""}
    assert update_check._job_snapshot()["install_when_ready"] is True

    update_check._run_background_download(manifest, "packaged")

    assert update_check._job_snapshot()["phase"] == "restarting"
    assert launched and launched[0][-1].endswith("Bibliobrain-Setup-9.0.0.exe")


def test_a_failed_background_download_stays_quiet(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _packaged_windows(isolated_home, monkeypatch, b"signed installer")
    _client_with_transport(
        monkeypatch, lambda request: httpx.Response(200, content=b"tampered!!!!!!!!")
    )

    update_check._run_background_download(manifest, "packaged")

    assert update_check._job_snapshot()["phase"] == "idle"
    assert update_check._ready_package(manifest, "packaged") is None


def _git_status(monkeypatch: pytest.MonkeyPatch, output: str) -> None:
    monkeypatch.setattr(update_check.shutil, "which", lambda name: "/usr/bin/git")
    monkeypatch.setattr(
        update_check,
        "_git",
        lambda *args: subprocess.CompletedProcess(args, 0, stdout=output, stderr=""),
    )


def test_git_state_reads_branch_upstream_and_local_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    _git_status(
        monkeypatch, "## main...gitea/main [ahead 1, behind 3]\n M src/app.py\nA  notes.md\n"
    )

    state = update_check.git_state()

    assert state["branch"] == "main"
    assert state["upstream"] == "gitea/main"
    assert (state["ahead"], state["behind"]) == (1, 3)
    assert state["changes"] == ["src/app.py", "notes.md"]
    assert state["change_count"] == 2


@pytest.mark.parametrize(
    ("output", "blocker"),
    [
        ("## main...origin/main\n", ""),
        ("## main...origin/main\n M src/app.py\n", "git_dirty"),
        ("## feature/x\n", "git_no_upstream"),
        ("## HEAD (no branch)\n", "git_no_upstream"),
    ],
)
def test_git_pull_is_refused_while_tracked_files_are_modified(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, output: str, blocker: str
) -> None:
    _git_status(monkeypatch, output)
    monkeypatch.setenv("BIBLIOBRAIN_INSTALL_MODE", "git")
    monkeypatch.setenv("BIBLIOBRAIN_MANAGED_SERVER", "1")
    monkeypatch.setattr(update_check, "_is_windows", lambda: True)
    monkeypatch.setattr(update_check, "MANAGER_SCRIPT", ROOT / "scripts/bibliobrain-manager.ps1")
    launched: list[list[str]] = []
    monkeypatch.setattr(update_check, "_launch_manager", launched.append)

    # No release needed: a checkout can pull whenever its remote moved.
    status = update_check.get_status()
    assert status["update_available"] is False
    assert status["install_blocker"] == blocker
    result = update_check.start_update()

    if blocker:
        assert result == {"ok": False, "code": blocker}
        assert launched == []
    else:
        assert result == {"ok": True, "code": ""}
        deadline = time.time() + 5
        while not launched and time.time() < deadline:
            time.sleep(0.02)
        assert launched and "-InstallerPath" not in launched[0]


def test_development_servers_never_update_themselves(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_check, "_fetch_manifest", lambda url: validate_manifest(_manifest()))
    update_check.check_now(force=True)
    monkeypatch.setattr(update_check, "_is_windows", lambda: True)

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


def test_a_cut_download_leaves_no_partial_package(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cut(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    _client_with_transport(monkeypatch, cut)
    installer = {
        "url": "https://github.com/owner/repo/releases/download/v9.0.0/Bibliobrain-Setup.exe",
        "sha256": "0" * 64,
        "size": 10,
    }

    with pytest.raises(httpx.ReadError):
        update_check._download_installer(installer, "9.0.0")

    assert list((isolated_home / "storage/updates").iterdir()) == []


def test_the_update_is_refused_up_front_on_a_full_disk(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200)

    _client_with_transport(monkeypatch, record)
    monkeypatch.setattr(update_check.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1000))
    installer = {
        "url": "https://github.com/owner/repo/releases/download/v9.0.0/Bibliobrain-Setup.exe",
        "sha256": "0" * 64,
        "size": 1000,
    }

    with pytest.raises(ManifestError, match="disk space"):
        update_check._download_installer(installer, "9.0.0")
    assert requested == []
