from __future__ import annotations

import importlib
import time
from pathlib import Path
from typing import Any, cast

import src.config as config


def _load_cache(workspace_tmp_dir: Path) -> Any:
    storage = workspace_tmp_dir / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    config.STORAGE_DIR = str(storage)
    module = cast(Any, importlib.import_module("src.discovery_cache_db"))
    module = cast(Any, importlib.reload(module))
    module.clear_memory_cache()
    return module


def test_discovery_api_cache_persists_successful_json(workspace_tmp_dir: Path) -> None:
    cache = _load_cache(workspace_tmp_dir)
    cache.set_cached_response(
        "semantic-search-key",
        "semanticscholar",
        {"data": [{"paperId": "p1"}]},
        ttl_seconds=60,
    )
    cache.clear_memory_cache()

    assert cache.get_cached_response("semantic-search-key") == {
        "data": [{"paperId": "p1"}]
    }


def test_discovery_api_cache_expires_rows(workspace_tmp_dir: Path) -> None:
    cache = _load_cache(workspace_tmp_dir)
    cache.set_cached_response(
        "expired-key",
        "semanticscholar",
        {"data": [1]},
        ttl_seconds=0.01,
    )
    time.sleep(0.03)

    assert cache.get_cached_response("expired-key") is None
