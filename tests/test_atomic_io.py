from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import src.atomic_io as atomic_io


def test_atomic_write_json_remains_valid_under_concurrent_writes(tmp_path: Path) -> None:
    target = tmp_path / "state.json"

    def write(index: int) -> None:
        atomic_io.atomic_write_json(target, {"index": index, "payload": "x" * 4096})

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(32)))

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["index"] in range(32)
    assert payload["payload"] == "x" * 4096
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_atomic_write_retries_transient_permission_errors(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    target = tmp_path / "state.json"
    real_replace = atomic_io.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporary file lock")
        real_replace(source, destination)

    monkeypatch.setattr(atomic_io.os, "replace", flaky_replace)
    monkeypatch.setattr(atomic_io.time, "sleep", lambda _delay: None)

    atomic_io.atomic_write_json(target, {"status": "ok"})

    assert attempts == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "ok"}
    assert not list(tmp_path.glob(".state.json.*.tmp"))
