"""Process-wide logging configuration for the Bibliobrain server.

The managed server runs hidden in the background, so console output alone is
not a usable diagnostic channel. This module installs a rotating file handler
under the Bibliobrain home directory in addition to the console stream, and
routes the Uvicorn loggers through the same handlers.
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FILE_NAME = "bibliobrain.log"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5
_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_HANDLER_TAG = "_bibliobrain_logging_handler"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_NOISY_LOGGERS = {
    "httpx": logging.WARNING,
    "httpcore": logging.WARNING,
    "chromadb": logging.WARNING,
    "sentence_transformers": logging.WARNING,
}

_state: dict[str, object] = {"configured": False, "path": None}


def resolve_log_level(value: str | None = None) -> int:
    """Map ``BIBLIOBRAIN_LOG_LEVEL`` (or an explicit value) to a logging level."""
    name = str(value or os.getenv("BIBLIOBRAIN_LOG_LEVEL", "") or "INFO").strip().upper()
    level = logging.getLevelName(name)
    return level if isinstance(level, int) else logging.INFO


def _tagged(handler: logging.Handler) -> logging.Handler:
    setattr(handler, _HANDLER_TAG, True)
    return handler


def _has_tagged_handler(logger: logging.Logger) -> bool:
    return any(getattr(handler, _HANDLER_TAG, False) for handler in logger.handlers)


def configure_logging(
    log_dir: str | os.PathLike[str] | None = None,
    *,
    level: str | None = None,
) -> Path | None:
    """Install console and rotating-file logging once per process.

    Returns the log file path, or ``None`` when the file handler could not be
    created (the console handler is still installed in that case).
    """
    if _state["configured"]:
        current = _state["path"]
        return current if isinstance(current, Path) else None

    root = logging.getLogger()
    root.setLevel(resolve_log_level(level))
    formatter = logging.Formatter(_FORMAT)

    if not _has_tagged_handler(root):
        console = _tagged(logging.StreamHandler(sys.stderr))
        console.setFormatter(formatter)
        root.addHandler(console)

    log_path: Path | None = None
    if log_dir:
        try:
            directory = Path(log_dir)
            directory.mkdir(parents=True, exist_ok=True)
            log_path = directory / LOG_FILE_NAME
            file_handler = _tagged(
                RotatingFileHandler(
                    log_path,
                    maxBytes=_MAX_BYTES,
                    backupCount=_BACKUP_COUNT,
                    encoding="utf-8",
                )
            )
            file_handler.setFormatter(formatter)
            root.addHandler(file_handler)
        except OSError as exc:
            log_path = None
            root.warning("Unable to open the Bibliobrain log file in %s: %s", log_dir, exc)

    # Uvicorn installs its own handlers with propagate=False when it owns the
    # logging config. The launchers pass log_config=None so these loggers fall
    # through to the root handlers configured above.
    # Started as ``uvicorn app_api:app``, Uvicorn has already configured its
    # own handlers: with propagation on, each access line was then printed by
    # uvicorn.access, again by uvicorn, and a third time by the root handler.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.propagate = True
        for handler in list(uvicorn_logger.handlers):
            if not getattr(handler, _HANDLER_TAG, False):
                uvicorn_logger.removeHandler(handler)
    for name, noisy_level in _NOISY_LOGGERS.items():
        logging.getLogger(name).setLevel(noisy_level)

    _state["configured"] = True
    _state["path"] = log_path
    return log_path

