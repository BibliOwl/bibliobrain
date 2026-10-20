"""Single source of truth for the Bibliobrain application version.

The version lives in the ``VERSION`` file at the repository root so that the
Python runtime, the installer build and the Inno Setup script all read the
same value. ``BIBLIOBRAIN_VERSION`` may still override it for diagnostics.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"
_FALLBACK_VERSION = "0.0.0"


@lru_cache(maxsize=1)
def _read_version_file() -> str:
    try:
        value = _VERSION_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""
    return value.splitlines()[0].strip() if value else ""


def get_app_version() -> str:
    """Return the effective application version string."""
    override = str(os.getenv("BIBLIOBRAIN_VERSION", "") or "").strip()
    if override:
        return override
    return _read_version_file() or _FALLBACK_VERSION


__version__ = get_app_version()
