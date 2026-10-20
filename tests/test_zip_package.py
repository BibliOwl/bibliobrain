"""ZIP package: the installer's files without an executable, and its update path.

Some antivirus software (Symantec Download Insight) quarantines a new unsigned
``Bibliobrain-Setup.exe``. The ZIP package installs with ``Install-Bibliobrain.cmd``
and must keep updating from the next ZIP, never from the executable.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_release_tools():
    spec = importlib.util.spec_from_file_location("release_tools", ROOT / "scripts/release_tools.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


release_tools = _load_release_tools()


def _inno_sources() -> set[str]:
    inno = (ROOT / "packaging/Bibliobrain.iss").read_text(encoding="utf-8")
    files = inno.split("\n[Files]", 1)[1].split("\n[", 1)[0]
    sources: set[str] = set()
    for line in files.splitlines():
        match = re.match(r'Source: "([^"]+)"', line)
        if not match or "dontcopy" in line or "SmokeInstallScript" in match.group(1):
            continue
        source = match.group(1)
        if source == "{#PayloadVersionFile}":
            sources.add("VERSION")
            continue
        sources.add(source.removeprefix("{#SourceRoot}\\").removesuffix("\\*").replace("\\", "/"))
    return sources


def test_zip_package_ships_exactly_the_files_of_the_installer() -> None:
    packaged = set(release_tools.PACKAGE_FILES) | set(release_tools.PACKAGE_DIRECTORIES)

    assert packaged == _inno_sources()


def _fake_source(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in release_tools.PACKAGE_FILES:
        (root / name).write_text(name, encoding="utf-8")
    (root / "VERSION").write_text("9.0.0\n", encoding="utf-8")
    for directory in release_tools.PACKAGE_DIRECTORIES:
        (root / directory).mkdir()
        (root / directory / "kept.txt").write_text(directory, encoding="utf-8")
    (root / "src" / "nested").mkdir()
    (root / "src" / "nested" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "module.cpython-314.pyc").write_bytes(b"\0")
    (root / "scripts" / "stale.pyc").write_bytes(b"\0")
    # Never packaged: not in the allowlist.
    (root / "tests").mkdir()
    (root / "tests" / "secret.txt").write_text("local", encoding="utf-8")
    (root / "docs" / "1-INSTALLATION").mkdir(parents=True)
    (root / "docs" / "1-INSTALLATION" / "beta-tester-guide.md").write_text("guide", encoding="utf-8")
    return root


def test_build_package_writes_the_allowlist_under_one_folder(workspace_tmp_dir: Path) -> None:
    source = _fake_source(workspace_tmp_dir / "source")
    output = workspace_tmp_dir / "dist" / "Bibliobrain-Windows.zip"

    names = release_tools.build_package(source, output)

    with zipfile.ZipFile(output) as archive:
        assert sorted(archive.namelist()) == sorted(names)
        assert archive.read("Bibliobrain/VERSION").strip() == b"9.0.0"
        assert b"never Bibliobrain-Setup-<version>.exe" in archive.read("Bibliobrain/ZIP-PACKAGE.txt")
    assert all(name.startswith("Bibliobrain/") for name in names)
    assert "Bibliobrain/Install-Bibliobrain.cmd" in names
    assert "Bibliobrain/src/nested/module.py" in names
    assert "Bibliobrain/BETA_TESTER_GUIDE.md" not in names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names)
    assert not any("tests/" in name or "docs/" in name for name in names)
    assert not output.with_name(output.name + ".part").exists()


def test_build_package_refuses_an_incomplete_source(workspace_tmp_dir: Path) -> None:
    source = _fake_source(workspace_tmp_dir)
    (source / "install.ps1").unlink()

    with pytest.raises(SystemExit, match="install.ps1"):
        release_tools.build_package(source, workspace_tmp_dir / "out.zip")


def test_repository_package_holds_no_executable(workspace_tmp_dir: Path) -> None:
    """The whole point of the ZIP: nothing a reputation filter quarantines."""
    names = release_tools.build_package(ROOT, workspace_tmp_dir / "Bibliobrain-Windows.zip")
    assert "Bibliobrain/wiki/Home.md" in names
    assert "Bibliobrain/wiki/Parametres.md" in names

    executables = [name for name in names if Path(name).suffix.lower() in {".exe", ".dll", ".msi", ".scr"}]
    assert executables == []


def test_release_publishes_the_zip_package_and_describes_it() -> None:
    builder = (ROOT / "packaging/build-installer.ps1").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")

    assert "release_tools.py\") package" in builder
    assert "--archive $ArchivePath" in builder
    assert '"dist/Bibliobrain-Windows.zip"' in workflow
    # Explorer marks every extracted file as downloaded; install.ps1 clears it
    # before anything else runs.
    main_flow = installer.split("Start-Transcript -Path $InstallLogPath", 1)[1]
    assert main_flow.index("Remove-DownloadMark") < main_flow.index("Select-LocalModels")


MANAGER_HARNESS = r"""
$ErrorActionPreference = "Stop"
$manager = $env:TEST_MANAGER
$ast = [System.Management.Automation.Language.Parser]::ParseFile($manager, [ref]$null, [ref]$null)
$wanted = @("ConvertTo-BibliobrainVersion", "Compare-BibliobrainVersion", "Test-BibliobrainArchive", "Update-ZipBibliobrain", "Update-Bibliobrain")
foreach ($function in $ast.FindAll({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    if ($wanted -contains $function.Name) { . ([scriptblock]::Create($function.Extent.Text)) }
}
$RepoRoot = $env:TEST_INSTALL
$ArchivePath = ""
$InstallerPath = ""
$ZipPackageMarker = "ZIP-PACKAGE.txt"
$ZipPackageRoot = "Bibliobrain"
$script:calls = @()
function Backup-BeforeUpdate { $script:calls += "backup" }
function Repair-Bibliobrain { $script:calls += "repair" }
function Update-PackagedBibliobrain { $script:calls += "setup" }
function Select-BibliobrainFile { return $env:TEST_ARCHIVE }
try {
    Update-Bibliobrain
    Write-Output ("calls=" + ($script:calls -join ","))
}
catch {
    Write-Output ("error=" + $_.Exception.Message)
    Write-Output ("calls=" + ($script:calls -join ","))
}
"""


def _run_manager_update(install: Path, archive: Path) -> str:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", MANAGER_HARNESS],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "TEST_MANAGER": str(ROOT / "scripts/bibliobrain-manager.ps1"),
            "TEST_INSTALL": str(install),
            "TEST_ARCHIVE": str(archive),
        },
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout


def _zip_install(root: Path, version: str) -> Path:
    root.mkdir(parents=True)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "ZIP-PACKAGE.txt").write_text("zip", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "removed_module.py").write_text("old", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "kept.txt").write_text("venv", encoding="utf-8")
    return root


@pytest.mark.skipif(os.name != "nt", reason="The maintenance manager is a Windows PowerShell script")
def test_manager_updates_a_zip_install_from_the_next_package(workspace_tmp_dir: Path) -> None:
    source = _fake_source(workspace_tmp_dir / "source")
    archive = workspace_tmp_dir / "Bibliobrain-Windows.zip"
    release_tools.build_package(source, archive)
    install = _zip_install(workspace_tmp_dir / "install", "1.0.0")

    output = _run_manager_update(install, archive)

    assert "error=" not in output, output
    assert "calls=backup,repair" in output
    assert (install / "VERSION").read_text(encoding="utf-8") == "9.0.0\n"
    assert (install / "src" / "nested" / "module.py").is_file()
    # Same as [InstallDelete]: a module the new version removed is gone...
    assert not (install / "src" / "removed_module.py").exists()
    # ...while the Python environment is reused.
    assert (install / ".venv" / "kept.txt").is_file()
    assert (install / "ZIP-PACKAGE.txt").is_file()


@pytest.mark.skipif(os.name != "nt", reason="The maintenance manager is a Windows PowerShell script")
def test_manager_refuses_an_older_or_foreign_zip(workspace_tmp_dir: Path) -> None:
    source = _fake_source(workspace_tmp_dir / "source")
    archive = workspace_tmp_dir / "Bibliobrain-Windows.zip"
    release_tools.build_package(source, archive)
    install = _zip_install(workspace_tmp_dir / "newer", "10.0.0")

    output = _run_manager_update(install, archive)

    assert "error=Refusing to replace Bibliobrain 10.0.0 with older version 9.0.0." in output
    assert output.rstrip().endswith("calls=")
    assert (install / "src" / "removed_module.py").is_file()

    foreign = workspace_tmp_dir / "other.zip"
    with zipfile.ZipFile(foreign, "w") as handle:
        handle.writestr("Bibliobrain/VERSION", "99.0.0\n")
    output = _run_manager_update(install, foreign)
    assert "error=The selected file is not a Bibliobrain ZIP package." in output

    damaged = workspace_tmp_dir / "damaged.zip"
    damaged.write_bytes(b"not a zip at all")
    output = _run_manager_update(install, damaged)
    assert "error=The selected file is not a valid ZIP archive." in output
