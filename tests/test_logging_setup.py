from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src import logging_setup
from src.common import env_bool, env_int, utc_now_iso


@pytest.fixture
def fresh_logging(monkeypatch: pytest.MonkeyPatch):
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    monkeypatch.setitem(logging_setup._state, "configured", False)
    monkeypatch.setitem(logging_setup._state, "path", None)
    for handler in saved_handlers:
        if getattr(handler, logging_setup._HANDLER_TAG, False):
            root.removeHandler(handler)
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if getattr(handler, logging_setup._HANDLER_TAG, False):
                handler.close()
                root.removeHandler(handler)
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
        root.setLevel(saved_level)


def test_configure_logging_writes_rotating_file(
    fresh_logging, workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_LOG_LEVEL", "debug")
    log_dir = workspace_tmp_dir / "logs"

    path = logging_setup.configure_logging(log_dir)

    assert path == log_dir / logging_setup.LOG_FILE_NAME
    assert logging.getLogger().level == logging.DEBUG
    logging.getLogger("bibliobrain.test").info("hello from the test")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert "hello from the test" in path.read_text(encoding="utf-8")
    assert logging.getLogger("uvicorn.access").propagate is True


def test_configure_logging_is_idempotent(fresh_logging, workspace_tmp_dir: Path) -> None:
    first = logging_setup.configure_logging(workspace_tmp_dir / "a")
    second = logging_setup.configure_logging(workspace_tmp_dir / "b")

    assert first == second
    tagged = [
        handler
        for handler in logging.getLogger().handlers
        if getattr(handler, logging_setup._HANDLER_TAG, False)
    ]
    assert len(tagged) == 2


def test_resolve_log_level_falls_back_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_LOG_LEVEL", "not-a-level")
    assert logging_setup.resolve_log_level() == logging.INFO
    assert logging_setup.resolve_log_level("warning") == logging.WARNING


def test_env_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BB_TEST_BOOL", " Yes ")
    assert env_bool("BB_TEST_BOOL") is True
    monkeypatch.setenv("BB_TEST_BOOL", "off")
    assert env_bool("BB_TEST_BOOL", True) is False
    monkeypatch.setenv("BB_TEST_BOOL", "maybe")
    assert env_bool("BB_TEST_BOOL", True) is True
    monkeypatch.delenv("BB_TEST_BOOL")
    assert env_bool("BB_TEST_BOOL", True) is True

    monkeypatch.setenv("BB_TEST_INT", "500")
    assert env_int("BB_TEST_INT", 10, maximum=60) == 60
    monkeypatch.setenv("BB_TEST_INT", "abc")
    assert env_int("BB_TEST_INT", 10, minimum=20) == 20
    monkeypatch.delenv("BB_TEST_INT")
    assert env_int("BB_TEST_INT", 7) == 7

    assert utc_now_iso().endswith("+00:00")


def test_uvicorn_cli_handlers_do_not_duplicate_access_lines(
    fresh_logging, workspace_tmp_dir: Path
) -> None:
    # What `uvicorn app_api:app` installs before importing the app.
    access = logging.getLogger("uvicorn.access")
    parent = logging.getLogger("uvicorn")
    cli_access, cli_default = logging.StreamHandler(), logging.StreamHandler()
    access.addHandler(cli_access)
    parent.addHandler(cli_default)
    try:
        logging_setup.configure_logging(workspace_tmp_dir / "logs")

        assert cli_access not in access.handlers
        assert cli_default not in parent.handlers
        assert access.propagate is True
    finally:
        access.removeHandler(cli_access)
        parent.removeHandler(cli_default)
