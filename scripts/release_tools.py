"""Release helpers: prepare a version, package it, then describe the release.

    python scripts/release_tools.py prepare 0.8.0
        Writes VERSION and turns the CHANGELOG "Unreleased" section into a dated
        "0.8.0" section. Commit, tag ``v0.8.0`` and push the tag yourself.

    python scripts/release_tools.py package --source <clean export> --output dist/Bibliobrain-Windows.zip
        Writes the ZIP package: the files of Setup.exe plus Install-Bibliobrain.cmd,
        for machines whose antivirus quarantines the unsigned installer.

    python scripts/release_tools.py manifest --installer dist/Bibliobrain-Setup-0.8.0.exe \
        --archive dist/Bibliobrain-Windows.zip \
        --release-base-url https://github.com/<owner>/<repo>/releases/download/v0.8.0
        Writes dist/latest.json and dist/release-notes.md for the release.

Standard library only (plus src/update_manifest.py), so it runs on a bare CI runner.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.update_manifest import (  # noqa: E402
    ARCHIVE_FILENAME,
    MANIFEST_FILENAME,
    ZIP_PACKAGE_MARKER,
    build_manifest,
    changelog_section,
    compare_versions,
    is_valid_version,
)

UNRELEASED_HEADING = "## [Unreleased]"
UNRELEASED_TEMPLATE = f"{UNRELEASED_HEADING}\n\n"

# Folder the user gets when extracting the ZIP package.
PACKAGE_ROOT = "Bibliobrain"
# Same allowlist as the [Files] section of packaging/Bibliobrain.iss (a test
# compares them). Never package a working tree wholesale: it can hold local
# PDFs, benchmark excerpts, logs and secrets.
PACKAGE_FILES = (
    "app_api.py",
    "VERSION",
    "install.ps1",
    "Install-Bibliobrain.cmd",
    "requirements.txt",
    "requirements.lock",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "configuration.example.json",
)
PACKAGE_DIRECTORIES = ("THIRD_PARTY_LICENSES", "src", "static", "scripts", "wiki")
# docs/ is private, even when packaging a maintainer's working tree.
ZIP_PACKAGE_MARKER_TEXT = (
    "This Bibliobrain folder was installed from the ZIP package.\n"
    "Update-Bibliobrain.cmd and the update offered in the application install the\n"
    f"next {ARCHIVE_FILENAME}, never Bibliobrain-Setup-<version>.exe.\n"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def prepare_changelog(changelog: str, version: str, today: str) -> str:
    """Move "Unreleased" under a dated version heading and refresh the compare links."""
    if UNRELEASED_HEADING not in changelog:
        raise SystemExit(f"CHANGELOG.md has no '{UNRELEASED_HEADING}' section.")
    if re.search(rf"^## \[{re.escape(version)}\]", changelog, re.MULTILINE):
        raise SystemExit(f"CHANGELOG.md already has a section for {version}.")
    changelog = changelog.replace(
        UNRELEASED_HEADING,
        f"{UNRELEASED_TEMPLATE}## [{version}] - {today}",
        1,
    )
    link = re.search(
        r"^\[Unreleased\]: (?P<base>\S+)/compare/(?P<previous>v[^.\s]\S*?)\.\.\.HEAD$",
        changelog,
        re.MULTILINE,
    )
    if link:
        base, previous = link.group("base"), link.group("previous")
        changelog = changelog.replace(
            link.group(0),
            f"[Unreleased]: {base}/compare/v{version}...HEAD\n"
            f"[{version}]: {base}/compare/{previous}...v{version}",
            1,
        )
    return changelog


def command_prepare(args: argparse.Namespace) -> None:
    version = args.version.strip().removeprefix("v")
    if not is_valid_version(version):
        raise SystemExit(f"Invalid version: {version}")
    version_path = ROOT / "VERSION"
    current = _read(version_path).strip()
    if is_valid_version(current) and compare_versions(version, current) <= 0:
        raise SystemExit(f"{version} is not newer than the current version {current}.")
    changelog_path = ROOT / "CHANGELOG.md"
    _write(changelog_path, prepare_changelog(_read(changelog_path), version, date.today().isoformat()))
    _write(version_path, version + "\n")
    print(f"VERSION and CHANGELOG.md now describe {version}. Review them, then:")
    print(f'  git commit -am "release: v{version}"')
    print(f'  git tag -a v{version} -m "Bibliobrain {version}"')
    print(f"  git push origin HEAD v{version}")


def _package_members(source: Path) -> list[tuple[Path, str]]:
    """``(file on disk, name inside the package root)`` pairs, in a stable order."""
    members: list[tuple[Path, str]] = []
    missing = [name for name in PACKAGE_FILES if not (source / name).is_file()]
    missing += [name for name in PACKAGE_DIRECTORIES if not (source / name).is_dir()]
    if missing:
        raise SystemExit(f"The source folder lacks files of the package: {', '.join(missing)}")
    members += [(source / name, name) for name in PACKAGE_FILES]
    for directory in PACKAGE_DIRECTORIES:
        for path in sorted((source / directory).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            members.append((path, path.relative_to(source).as_posix()))
    return members


def build_package(source: Path, output: Path) -> list[str]:
    """Write the ZIP package of ``source`` (a clean export) and return its entry names."""
    members = _package_members(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".part")
    names: list[str] = []
    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, name in members:
            names.append(f"{PACKAGE_ROOT}/{name}")
            archive.write(path, names[-1])
        names.append(f"{PACKAGE_ROOT}/{ZIP_PACKAGE_MARKER}")
        archive.writestr(names[-1], ZIP_PACKAGE_MARKER_TEXT)
    partial.replace(output)
    return names


def command_package(args: argparse.Namespace) -> None:
    output = Path(args.output)
    names = build_package(Path(args.source), output)
    print(f"{output.name} ready: {len(names)} files.")


def command_manifest(args: argparse.Namespace) -> None:
    version = (args.version or _read(ROOT / "VERSION")).strip()
    notes = changelog_section(_read(ROOT / "CHANGELOG.md"), version)
    if not notes:
        print(f"Warning: CHANGELOG.md has no section for {version}; the notes stay empty.")
    base_url = args.release_base_url.rstrip("/")
    notes_url = args.notes_url or re.sub(r"/releases/download/", "/releases/tag/", base_url)
    manifest = build_manifest(
        version=version,
        installer_path=args.installer,
        release_base_url=base_url,
        archive_path=args.archive or None,
        notes=notes,
        notes_url=notes_url,
    )
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / MANIFEST_FILENAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _write(output_dir / "release-notes.md", (notes or f"Bibliobrain {version}") + "\n")
    print(f"{MANIFEST_FILENAME} ready for {version}: {manifest['installer']['url']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare", help="Set VERSION and date the CHANGELOG section.")
    prepare.add_argument("version")
    prepare.set_defaults(handler=command_prepare)

    package = commands.add_parser("package", help="Write the ZIP package from a clean export.")
    package.add_argument("--source", required=True)
    package.add_argument("--output", default=str(ROOT / "dist" / ARCHIVE_FILENAME))
    package.set_defaults(handler=command_package)

    manifest = commands.add_parser("manifest", help="Write latest.json for a built release.")
    manifest.add_argument("--installer", required=True)
    manifest.add_argument("--archive", default="")
    manifest.add_argument("--release-base-url", required=True)
    manifest.add_argument("--version", default="")
    manifest.add_argument("--notes-url", default="")
    manifest.add_argument("--output-dir", default=str(ROOT / "dist"))
    manifest.set_defaults(handler=command_manifest)

    args = parser.parse_args(argv)
    args.handler(args)


if __name__ == "__main__":
    main()
