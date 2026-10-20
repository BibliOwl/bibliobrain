"""Network exposure guards shared by Bibliobrain launchers and the API."""

from __future__ import annotations

import ipaddress
import logging
from typing import Any


def is_loopback_bind_host(host: str) -> bool:
    """Return whether a bind host is explicitly limited to loopback."""
    normalized = str(host or "").strip().lower()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def require_safe_bind_host(host: str) -> None:
    """Refuse to listen outside loopback: Bibliobrain only serves this computer."""
    if is_loopback_bind_host(host):
        return
    detail = "Bibliobrain only listens on this computer (127.0.0.1)"
    raise RuntimeError(f"Refusing to bind Bibliobrain to {host}: {detail}.")


class UvicornAccessQueryRedactionFilter(logging.Filter):
    """Remove query strings from Uvicorn access-log request targets."""

    def filter(self, record: logging.LogRecord) -> bool:
        args: Any = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            target = str(args[2])
            path, separator, _query = target.partition("?")
            if separator:
                sanitized = list(args)
                sanitized[2] = path
                record.args = tuple(sanitized)
        return True


def install_uvicorn_access_query_redaction() -> None:
    """Install one process-wide access-log query redaction filter."""
    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(item, UvicornAccessQueryRedactionFilter) for item in access_logger.filters
    ):
        access_logger.addFilter(UvicornAccessQueryRedactionFilter())
