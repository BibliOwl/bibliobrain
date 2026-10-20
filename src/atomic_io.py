from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

_WRITE_LOCKS = tuple(threading.Lock() for _ in range(64))
_REPLACE_ATTEMPTS = 5
_REPLACE_RETRY_DELAY_SECONDS = 0.01


def _write_lock(path: Path) -> threading.Lock:
    normalized = os.path.normcase(str(path.resolve(strict=False)))
    return _WRITE_LOCKS[hash(normalized) % len(_WRITE_LOCKS)]


def _replace_with_retry(source: Path, target: Path) -> None:
    """Tolerate short-lived file locks from Windows scanners and indexers."""
    delay = _REPLACE_RETRY_DELAY_SECONDS
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay *= 2


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write a text file atomically without sharing a predictable temp filename."""
    target = Path(path)
    with _write_lock(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        os.close(descriptor)
        try:
            temporary.write_text(content, encoding=encoding)
            _replace_with_retry(temporary, target)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise


def atomic_write_json(
    path: Path,
    payload: Any,
    *,
    ensure_ascii: bool = False,
    indent: int | None = 2,
) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent),
        encoding="utf-8",
    )
