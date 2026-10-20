"""Small shared helpers used across Bibliobrain modules.

Keep this module free of project imports so it can be used from ``src.config``
and from the earliest startup code without creating import cycles.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string with offset."""
    return datetime.now(UTC).isoformat()


def env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean environment variable.

    Blank or unset values return ``default``. Recognised truthy values are
    ``1/true/yes/on`` and falsy values ``0/false/no/off`` (case-insensitive).
    Any other text also returns ``default`` so a typo never silently flips a
    security-relevant switch.
    """
    raw = str(os.getenv(name, "") or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
    return bool(default)


def env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read an integer environment variable, falling back to ``default``.

    The result is clamped to ``[minimum, maximum]`` when bounds are given.
    """
    raw = str(os.getenv(name, "") or "").strip()
    try:
        value = int(raw) if raw else int(default)
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def json_object(raw_value: str | None) -> dict:
    """Parse a stored JSON object; anything else (blank, invalid, a list) gives ``{}``."""
    text = str(raw_value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}
