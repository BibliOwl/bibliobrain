from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
from pathlib import Path, PurePath, PurePosixPath

from packaging.utils import canonicalize_name

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
LOCKFILE_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)==")
NOTICE_PREFIXES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")
REPOSITORY_NOTICE_FILES = (
    Path("THIRD_PARTY_NOTICES.md"),
    Path("static/vendor/marked/LICENSE"),
    Path("static/vendor/katex/LICENSE"),
    Path("static/vendor/dompurify/LICENSE"),
)


def locked_distribution_names(lockfile: Path) -> list[str]:
    """Return every emitted distribution plus PyTorch, which is omitted from the lock."""
    names = {
        match.group(1)
        for line in lockfile.read_text(encoding="utf-8-sig").splitlines()
        if (match := LOCKFILE_PATTERN.match(line))
    }
    names.add("torch")
    return sorted(names, key=str.casefold)


def is_license_entry(entry: str | PurePath) -> bool:
    normalized = str(entry).replace("\\", "/")
    parts = (part.upper() for part in PurePosixPath(normalized).parts)
    return any(part.startswith(NOTICE_PREFIXES) for part in parts)


def safe_entry_name(entry: str | PurePath) -> str:
    normalized = str(entry).replace("\\", "/")
    parts = [part for part in PurePosixPath(normalized).parts if part not in {".", "..", "/"}]
    return "__".join(parts)


def collect_licenses(lockfile: Path, output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    repository_output = output / "repository"
    python_output = output / "python"
    copied_repository_files: list[str] = []

    for relative in REPOSITORY_NOTICE_FILES:
        source = REPOSITORY_ROOT / relative
        destination = repository_output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied_repository_files.append(relative.as_posix())

    bundled_licenses = REPOSITORY_ROOT / "THIRD_PARTY_LICENSES"
    shutil.copytree(
        bundled_licenses,
        repository_output / "THIRD_PARTY_LICENSES",
        dirs_exist_ok=True,
    )

    distributions: list[dict[str, object]] = []
    for requested_name in locked_distribution_names(lockfile):
        try:
            distribution = importlib.metadata.distribution(requested_name)
        except importlib.metadata.PackageNotFoundError as error:
            raise RuntimeError(
                f"Locked distribution is not installed: {requested_name}"
            ) from error

        distribution_name = distribution.metadata.get("Name") or requested_name
        destination_directory = (
            python_output / f"{canonicalize_name(distribution_name)}-{distribution.version}"
        )
        copied: list[str] = []

        for entry in distribution.files or ():
            if not is_license_entry(entry):
                continue
            source = Path(str(distribution.locate_file(entry)))
            if not source.is_file():
                continue
            destination_directory.mkdir(parents=True, exist_ok=True)
            destination_name = safe_entry_name(entry)
            shutil.copy2(source, destination_directory / destination_name)
            copied.append(destination_name)

        distributions.append(
            {
                "name": distribution_name,
                "requested_name": requested_name,
                "version": distribution.version,
                "license_expression": distribution.metadata.get("License-Expression"),
                "license_files": sorted(copied),
            }
        )

    manifest: dict[str, object] = {
        "lockfile": lockfile.name,
        "repository_notice_files": sorted(copied_repository_files),
        "distributions": distributions,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect third-party license and notice files for a binary distribution."
    )
    parser.add_argument(
        "--lockfile",
        type=Path,
        default=REPOSITORY_ROOT / "requirements.lock",
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    manifest = collect_licenses(arguments.lockfile.resolve(), arguments.output.resolve())
    distributions = manifest["distributions"]
    assert isinstance(distributions, list)
    without_files = [item["name"] for item in distributions if not item["license_files"]]
    print(f"Collected notices for {len(distributions)} Python distributions.")
    if without_files:
        print(
            "No packaged license file found for: " + ", ".join(str(name) for name in without_files)
        )
        print("Keep THIRD_PARTY_NOTICES.md with the distribution for their license references.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
