from __future__ import annotations

import logging

import pytest

from src.network_security import (
    UvicornAccessQueryRedactionFilter,
    is_loopback_bind_host,
    require_safe_bind_host,
)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "[::1]", "localhost"])
def test_loopback_bind_is_allowed(host: str) -> None:
    assert is_loopback_bind_host(host)
    require_safe_bind_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "server.local"])
def test_non_loopback_bind_is_refused(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(RuntimeError, match="Refusing to bind Bibliobrain"):
        require_safe_bind_host(host)


def test_uvicorn_access_filter_removes_the_entire_query_string() -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "192.168.1.50:50000",
            "GET",
            "/?view=library&access_token=shared-secret",
            "1.1",
            303,
        ),
        exc_info=None,
    )

    assert UvicornAccessQueryRedactionFilter().filter(record)
    assert isinstance(record.args, tuple)
    assert record.args[2] == "/"
    assert "shared-secret" not in record.getMessage()


def test_a_fallback_port_is_an_allowed_origin(monkeypatch) -> None:
    from src.http_security import parse_allowed_origins

    monkeypatch.setenv("BIBLIOBRAIN_PORT", "7862")
    allowed = parse_allowed_origins("http://localhost:7860,http://127.0.0.1:7860")
    assert "http://localhost:7862" in allowed
    assert "http://127.0.0.1:7862" in allowed
    monkeypatch.setenv("BIBLIOBRAIN_PORT", "7860")
    assert parse_allowed_origins("http://localhost:7860") == ["http://localhost:7860"]
