from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import src.discovery_history_db as history_db


@pytest.fixture
def db(workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The module on a temporary SQLite file, restored after the test."""
    monkeypatch.setattr(history_db, "DB_DIR", workspace_tmp_dir)
    monkeypatch.setattr(history_db, "DB_PATH", workspace_tmp_dir / "discovery_history.sqlite3")
    history_db.init_discovery_history_db()
    return history_db


def _save(db: Any, **overrides: Any) -> dict[str, Any]:
    values = {
        "mode": "search",
        "input_text": "AC losses in HTS magnets",
        "result_count": 2,
        "response_payload": {"results": [{"title": "A"}, {"title": "B"}]},
    }
    values.update(overrides)
    return db.save_search(**values)


def test_search_again_replaces_results_in_place_and_keeps_the_title(db: Any) -> None:
    first = _save(db)["search"]
    assert db.rename_search(first["id"], "  Thesis,   chapter 2 ")

    again = _save(
        db,
        search_id=first["id"],
        result_count=3,
        response_payload={"results": [{"title": "C"}, {"title": "A"}, {"title": "B"}]},
    )

    assert again["search"]["id"] == first["id"]
    assert again["search"]["title"] == "Thesis, chapter 2"
    assert again["search"]["result_count"] == 3
    assert [r["title"] for r in again["response"]["results"]] == ["C", "A", "B"]
    assert again["search"]["created"] == first["created"]
    assert db.count_searches() == 1


def test_unknown_history_id_creates_a_new_search(db: Any) -> None:
    saved = _save(db, search_id="does-not-exist")["search"]

    assert saved["id"] != "does-not-exist"
    assert db.count_searches() == 1


def test_rename_rejects_blank_titles_and_unknown_ids(db: Any) -> None:
    saved = _save(db)["search"]

    assert not db.rename_search(saved["id"], "   ")
    assert not db.rename_search("missing", "Title")
    assert db.get_search(saved["id"])["search"]["title"] != ""
