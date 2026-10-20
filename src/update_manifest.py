"""Update manifest (``latest.json``): version rules, validation and generation.

Standard library only: the release tooling (``scripts/release_tools.py``) runs
it on a bare CI runner, and the in-app checker (:mod:`src.update_check`)
validates what it downloads with the very same rules.

The manifest is a small static file published next to each release::

    {
      "schema": 1,
      "version": "0.8.0",
      "published": "2026-10-20",
      "notes": "Plain text taken from the CHANGELOG section of that version.",
      "notes_url": "https://github.com/<owner>/<repo>/releases/tag/v0.8.0",
      "installer": {
        "url": "https://github.com/<owner>/<repo>/releases/download/v0.8.0/Bibliobrain-Setup-0.8.0.exe",
        "sha256": "<64 hex characters>",
        "size": 12345678
      },
      "archive": {
        "url": "https://github.com/<owner>/<repo>/releases/download/v0.8.0/Bibliobrain-Windows.zip",
        "sha256": "<64 hex characters>",
        "size": 12345678
      }
    }

``archive`` is the ZIP package (``scripts/release_tools.py package``) for
machines whose antivirus quarantines the unsigned ``Setup.exe``. Installs made
from it update from the next ZIP, never from an executable. It is optional:
older clients ignore it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MANIFEST_SCHEMA = 1
MANIFEST_FILENAME = "latest.json"
# Release asset and the file that marks a folder installed from it; read by
# src/update_check.py and scripts/bibliobrain-manager.ps1 to choose the update path.
ARCHIVE_FILENAME = "Bibliobrain-Windows.zip"
ZIP_PACKAGE_MARKER = "ZIP-PACKAGE.txt"
# Public repository that publishes the releases. Until its first release
# exists, the check fails like an offline machine.
UPDATE_REPOSITORY = "bibliOwl/bibliobrain"
DEFAULT_MANIFEST_URL = (
    f"https://github.com/{UPDATE_REPOSITORY}/releases/latest/download/{MANIFEST_FILENAME}"
)
MAX_NOTES_CHARS = 4000
# Same grammar as packaging/build-installer.ps1 and the VERSION file.
_VERSION_PATTERN = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:[-.]([0-9A-Za-z.]+))?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(ValueError):
    """Raised when a manifest is malformed or unsafe to follow."""


def parse_version(value: Any) -> tuple[tuple[int, int, int], tuple[Any, ...] | None]:
    """Split ``X.Y.Z[-pre]`` into a numeric core and optional prerelease parts."""
    match = _VERSION_PATTERN.match(str(value or "").strip())
    if not match:
        raise ManifestError(f"Invalid version: {value!r}")
    core = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    prerelease = match.group(4)
    if not prerelease:
        return core, None
    parts = tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in prerelease.split(".")
        if part
    )
    return core, parts


def is_valid_version(value: Any) -> bool:
    try:
        parse_version(value)
    except ManifestError:
        return False
    return True


def compare_versions(left: Any, right: Any) -> int:
    """Return -1, 0 or 1. A release ranks above its own prereleases (SemVer)."""
    left_core, left_pre = parse_version(left)
    right_core, right_pre = parse_version(right)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    if left_pre == right_pre:
        return 0
    if left_pre is None:
        return 1
    if right_pre is None:
        return -1
    return -1 if left_pre < right_pre else 1


def _https_url(value: Any, field: str) -> str:
    url = str(value or "").strip()
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ManifestError(f"{field} must be an https URL")
    if parts.username or parts.password:
        raise ManifestError(f"{field} must not embed credentials")
    return url


def _validate_asset(asset: Any, field: str, manifest_url: str) -> dict[str, Any]:
    if not isinstance(asset, Mapping):
        raise ManifestError(f"{field} must be an object")
    url = _https_url(asset.get("url"), f"{field}.url")
    if manifest_url:
        expected_host = urlsplit(manifest_url).hostname
        if urlsplit(url).hostname != expected_host:
            raise ManifestError(f"{field}.url must be served by the manifest host")
    sha256 = str(asset.get("sha256") or "").strip().lower()
    if not _SHA256_PATTERN.match(sha256):
        raise ManifestError(f"{field}.sha256 must be 64 hexadecimal characters")
    try:
        size = int(asset.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{field}.size must be an integer") from exc
    if size < 0:
        raise ManifestError(f"{field}.size must be positive")
    return {"url": url, "sha256": sha256, "size": size}


def validate_manifest(payload: Any, *, manifest_url: str = "") -> dict[str, Any]:
    """Return a normalized copy of ``payload`` or raise :class:`ManifestError`.

    When ``manifest_url`` is given, the installer and the archive must be
    served by the same host:a compromised manifest mirror cannot point users at another server.
    Redirects from that host (GitHub sends assets to its CDN) stay possible;
    the SHA-256 is what proves the downloaded file is the published one.
    """
    if not isinstance(payload, Mapping):
        raise ManifestError("The manifest must be a JSON object")
    try:
        schema = int(payload.get("schema", 0))
    except (TypeError, ValueError) as exc:
        raise ManifestError("Invalid manifest schema") from exc
    if schema != MANIFEST_SCHEMA:
        raise ManifestError(f"Unsupported manifest schema: {schema}")

    version = str(payload.get("version") or "").strip()
    if not is_valid_version(version):
        raise ManifestError(f"Invalid version: {version!r}")

    notes_url = str(payload.get("notes_url") or "").strip()
    if notes_url:
        notes_url = _https_url(notes_url, "notes_url")

    published = str(payload.get("published") or "").strip()
    if published:
        try:
            published = date.fromisoformat(published[:10]).isoformat()
        except ValueError:
            published = ""

    normalized: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "published": published,
        "notes": str(payload.get("notes") or "").strip()[:MAX_NOTES_CHARS],
        "notes_url": notes_url,
        "installer": None,
        "archive": None,
    }
    for field in ("installer", "archive"):
        asset = payload.get(field)
        if asset is not None:
            normalized[field] = _validate_asset(asset, field, manifest_url)
    return normalized


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def changelog_section(changelog_text: str, version: str) -> str:
    """Return the body of the ``## [version]`` section, without its heading."""
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*$", re.MULTILINE)
    match = heading.search(changelog_text)
    if not match:
        return ""
    rest = changelog_text[match.end() :]
    following = re.search(r"^## |^\[[^\]]+\]: ", rest, re.MULTILINE)
    body = rest[: following.start()] if following else rest
    return body.strip()


def _describe_asset(path: Path, base_url: str) -> dict[str, Any]:
    return {
        "url": f"{base_url}/{path.name}",
        "sha256": sha256_file(path),
        "size": path.stat().st_size,
    }


def build_manifest(
    *,
    version: str,
    installer_path: str | Path,
    release_base_url: str,
    archive_path: str | Path | None = None,
    notes: str = "",
    notes_url: str = "",
    published: str = "",
) -> dict[str, Any]:
    """Describe a built release; the result always passes :func:`validate_manifest`."""
    base_url = _https_url(release_base_url, "release_base_url").rstrip("/")
    manifest: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "version": version,
        "published": published or date.today().isoformat(),
        "notes": notes,
        "notes_url": notes_url,
        "installer": _describe_asset(Path(installer_path), base_url),
    }
    if archive_path is not None:
        manifest["archive"] = _describe_asset(Path(archive_path), base_url)
    return validate_manifest(manifest)
