from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

import src.config as config


def _load_analysis_cache_db(tmp_path: Path) -> Any:
    """Charge le module avec un STORAGE_DIR temporaire avant son initialisation SQLite."""
    config.STORAGE_DIR = str(tmp_path)
    analysis_cache_db = cast(Any, importlib.import_module("src.analysis_cache_db"))
    analysis_cache_db = cast(Any, importlib.reload(analysis_cache_db))
    analysis_cache_db.DB_DIR = tmp_path
    analysis_cache_db.DB_PATH = tmp_path / "analysis_cache.sqlite3"
    analysis_cache_db._MEM_CACHE.clear()
    analysis_cache_db.init_analysis_cache_db()
    return analysis_cache_db


def test_analysis_cache_respects_fingerprint(workspace_tmp_dir: Path) -> None:
    """Un cache doit être relu seulement si le fingerprint demandé correspond à celui stocké."""
    analysis_cache_db = _load_analysis_cache_db(workspace_tmp_dir)

    analysis_cache_db.set_cached_analysis(
        "papers/demo.pdf",
        "summary",
        "contenu",
        fingerprint="fp-1",
        model_name="model-a",
    )

    assert (
        analysis_cache_db.get_cached_analysis("papers/demo.pdf", "summary", fingerprint="fp-1")
        == "contenu"
    )
    assert (
        analysis_cache_db.get_cached_analysis("papers/demo.pdf", "summary", fingerprint="fp-2")
        is None
    )


def test_analysis_cache_replacement_evicts_stale_memory_entries(
    workspace_tmp_dir: Path,
) -> None:
    analysis_cache_db = _load_analysis_cache_db(workspace_tmp_dir)
    analysis_cache_db.set_cached_analysis(
        "papers/demo.pdf", "summary", "ancien", fingerprint="fp-1"
    )
    assert analysis_cache_db.get_cached_analysis("papers/demo.pdf", "summary") == "ancien"

    analysis_cache_db.set_cached_analysis(
        "papers/demo.pdf", "summary", "nouveau", fingerprint="fp-2"
    )

    assert analysis_cache_db.get_cached_analysis("papers/demo.pdf", "summary") == "nouveau"
    assert analysis_cache_db.get_cached_analysis(
        "papers/demo.pdf", "summary", fingerprint="fp-1"
    ) is None


def test_analysis_cache_types_filter_on_matching_fingerprint(workspace_tmp_dir: Path) -> None:
    """La liste des types doit ignorer les entrées dont le fingerprint ne correspond pas."""
    analysis_cache_db = _load_analysis_cache_db(workspace_tmp_dir)

    analysis_cache_db.set_cached_analysis("papers/a.pdf", "summary", "resume A", fingerprint="same")
    analysis_cache_db.set_cached_analysis("papers/a.pdf", "notes", "notes A", fingerprint="same")
    analysis_cache_db.set_cached_analysis(
        "papers/b.pdf", "summary", "resume B", fingerprint="other"
    )

    result = analysis_cache_db.get_cached_analysis_types_for_paths(
        {
            "papers/a.pdf": "same",
            "papers/b.pdf": "mismatch",
        }
    )

    assert result == {"papers/a.pdf": {"summary", "notes"}}
