"""Tests for the extracted prepared-article cache (PR 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from src import config as src_config
from src.chunk_cache import (
    compute_prepared_article_cache_key,
    load_cached_prepared_article,
    save_cached_prepared_article,
)


@pytest.fixture
def isolated_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(src_config, "STORAGE_DIR", str(tmp_path))
    return tmp_path


def test_cache_key_is_stable_across_calls(isolated_storage: Path) -> None:
    key_a = compute_prepared_article_cache_key(
        rel_path="papers/p1.pdf", file_sig="sig", zotero_sig="z", chunk_size=1000
    )
    key_b = compute_prepared_article_cache_key(
        rel_path="papers/p1.pdf", file_sig="sig", zotero_sig="z", chunk_size=1000
    )
    assert key_a == key_b


def test_cache_key_changes_when_chunk_size_changes(isolated_storage: Path) -> None:
    key_a = compute_prepared_article_cache_key(
        rel_path="papers/p1.pdf", file_sig="sig", zotero_sig="z", chunk_size=1000
    )
    key_b = compute_prepared_article_cache_key(
        rel_path="papers/p1.pdf", file_sig="sig", zotero_sig="z", chunk_size=1500
    )
    assert key_a != key_b


def test_round_trip_save_then_load(isolated_storage: Path) -> None:
    article = {"article_id": "p1", "title": "Hello", "pdf_chunks": [{"text": "x"}]}
    cache_key = compute_prepared_article_cache_key(
        rel_path="papers/p1.pdf", file_sig="sig", zotero_sig="z", chunk_size=1000
    )
    save_cached_prepared_article(
        article, cache_key=cache_key, file_sig="sig", zotero_sig="z", chunk_size=1000
    )
    loaded = load_cached_prepared_article("p1", cache_key)
    assert loaded is not None
    assert loaded["title"] == "Hello"


def test_load_returns_none_for_missing_or_mismatching_key(
    isolated_storage: Path,
) -> None:
    article = {"article_id": "p1"}
    save_cached_prepared_article(
        article, cache_key="key-A", file_sig="sig", zotero_sig="z", chunk_size=500
    )
    assert load_cached_prepared_article("p1", "key-B") is None
    assert load_cached_prepared_article("p2", "key-A") is None


def test_save_skips_when_article_id_missing(isolated_storage: Path) -> None:
    save_cached_prepared_article(
        {"article_id": ""}, cache_key="k", file_sig="s", zotero_sig="z", chunk_size=1
    )
    # No file should have been created in the cache dir.
    cache_dir = isolated_storage / "wiki_build" / "articles"
    assert not cache_dir.exists() or not list(cache_dir.iterdir())
