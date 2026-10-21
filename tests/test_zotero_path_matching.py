"""Documents are matched to Zotero records by path first, file name last."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import zotero_db
from src.rag.zotero_integration import _zotero_signature
from src.zotero_db import ZoteroItem, load_zotero_library, lookup_zotero_item

_SCHEMA = """
CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT);
CREATE TABLE groups (libraryID INTEGER, groupID INTEGER);
CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT, libraryID INTEGER, itemTypeID INTEGER);
CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
CREATE TABLE itemAttachments (
    itemID INTEGER PRIMARY KEY, parentItemID INTEGER, path TEXT, contentType TEXT
);
CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
CREATE TABLE itemCreators (
    itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER
);
CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, lastName TEXT, firstName TEXT);
CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE collections (
    collectionID INTEGER PRIMARY KEY, collectionName TEXT, parentCollectionID INTEGER
);
CREATE TABLE collectionItems (itemID INTEGER, collectionID INTEGER);
CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, title TEXT, note TEXT);
CREATE TABLE itemAnnotations (
    itemID INTEGER PRIMARY KEY, parentItemID INTEGER, text TEXT, comment TEXT,
    color TEXT, pageLabel TEXT
);
INSERT INTO libraries VALUES (1, 'user');
INSERT INTO itemTypes VALUES (1, 'journalArticle');
INSERT INTO fields VALUES (1, 'title');
"""


def _library(db_path: Path, records: list[tuple[str, str, str, str]]) -> dict[str, ZoteroItem]:
    """records: (parent key, attachment key, title, attachment path)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_SCHEMA)
        for index, (parent_key, attachment_key, title, path) in enumerate(records):
            parent_id, attachment_id = 2 * index + 1, 2 * index + 2
            conn.execute("INSERT INTO items VALUES (?, ?, 1, 1)", (parent_id, parent_key))
            conn.execute("INSERT INTO items VALUES (?, ?, 1, 1)", (attachment_id, attachment_key))
            conn.execute(
                "INSERT INTO itemAttachments VALUES (?, ?, ?, 'application/pdf')",
                (attachment_id, parent_id, path),
            )
            conn.execute("INSERT INTO itemDataValues VALUES (?, ?)", (parent_id, title))
            conn.execute("INSERT INTO itemData VALUES (?, 1, ?)", (parent_id, parent_id))
        conn.commit()
    finally:
        conn.close()
    load_zotero_library.cache_clear()
    return load_zotero_library(str(db_path))


@pytest.fixture(autouse=True)
def _no_real_zotero_prefs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(zotero_db, "read_zotero_prefs", lambda: {})
    yield
    load_zotero_library.cache_clear()


def test_storage_files_with_the_same_name_keep_their_own_record(workspace_tmp_dir: Path) -> None:
    library = _library(
        workspace_tmp_dir / "zotero.sqlite",
        [
            ("ITEMAAAA", "KEYAAAAA", "Bobines HTS (2018)", "storage:fulltext.pdf"),
            ("ITEMBBBB", "KEYBBBBB", "Cryogénie appliquée", "storage:fulltext.pdf"),
        ],
    )

    first = lookup_zotero_item("KEYAAAAA/fulltext.pdf", library)
    second = lookup_zotero_item("KEYBBBBB/fulltext.pdf", library)

    assert (first.title, second.title) == ("Bobines HTS (2018)", "Cryogénie appliquée")
    # Outside storage/ the shared name says nothing: no guess.
    assert lookup_zotero_item("copies/fulltext.pdf", library) is None


def test_unique_file_name_still_matches_outside_storage(workspace_tmp_dir: Path) -> None:
    library = _library(
        workspace_tmp_dir / "zotero.sqlite",
        [("ITEMAAAA", "KEYAAAAA", "Quench", "storage:Quench 2020.pdf")],
    )

    # A lower-case folder of 8 letters is not a Zotero key.
    assert lookup_zotero_item("articles/Quench 2020.pdf", library).title == "Quench"
    # An upper-case key folder Zotero no longer knows is an orphan.
    assert lookup_zotero_item("GONEKEY9/Quench 2020.pdf", library) is None


def test_linked_files_with_the_same_name_match_by_absolute_path(workspace_tmp_dir: Path) -> None:
    articles = workspace_tmp_dir / "Articles"
    first = articles / "Aimants" / "article.pdf"
    second = articles / "Cryo" / "article.pdf"
    library = _library(
        workspace_tmp_dir / "zotero.sqlite",
        [
            ("ITEMAAAA", "KEYAAAAA", "Aimants", str(first)),
            ("ITEMBBBB", "KEYBBBBB", "Cryo", str(second)),
        ],
    )

    assert lookup_zotero_item("Aimants/article.pdf", library, str(first)).title == "Aimants"
    assert lookup_zotero_item("Cryo/article.pdf", library, str(second)).title == "Cryo"
    assert lookup_zotero_item("Cryo/article.pdf", library) is None


def test_relative_linked_files_resolve_against_the_base_directory(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = workspace_tmp_dir / "Base"
    monkeypatch.setattr(
        zotero_db,
        "read_zotero_prefs",
        lambda: {"extensions.zotero.baseAttachmentPath": str(base)},
    )
    library = _library(
        workspace_tmp_dir / "zotero.sqlite",
        [
            ("ITEMAAAA", "KEYAAAAA", "Un", "attachments:Revue/a.pdf"),
            ("ITEMBBBB", "KEYBBBBB", "Deux", "attachments:Autre/a.pdf"),
        ],
    )

    found = lookup_zotero_item("Autre/a.pdf", library, str(base / "Autre" / "a.pdf"))
    assert found is not None and found.title == "Deux"


def test_only_records_matched_wrongly_before_get_a_new_signature(workspace_tmp_dir: Path) -> None:
    library = _library(
        workspace_tmp_dir / "zotero.sqlite",
        [
            ("ITEMAAAA", "KEYAAAAA", "Premier", "storage:fulltext.pdf"),
            ("ITEMBBBB", "KEYBBBBB", "Dernier lu", "storage:fulltext.pdf"),
            ("ITEMCCCC", "KEYCCCCC", "Seul", "storage:seul.pdf"),
        ],
    )
    first, last, alone = (library[key] for key in ("keyaaaaa", "keybbbbb", "keyccccc"))

    # Matching by name gave every "fulltext.pdf" the last record read: only
    # the files of the other record were indexed under the wrong one.
    assert (first.shared_filename, last.shared_filename, alone.shared_filename) == (
        True,
        False,
        False,
    )
    unchanged = ZoteroItem(**{**alone.__dict__, "shared_filename": False})
    assert _zotero_signature(alone) == _zotero_signature(unchanged)
    moved = ZoteroItem(**{**first.__dict__, "shared_filename": False})
    assert _zotero_signature(first) != _zotero_signature(moved)


def test_one_record_with_the_same_pdf_twice_keeps_its_file_name(workspace_tmp_dir: Path) -> None:
    db_path = workspace_tmp_dir / "zotero.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.execute("INSERT INTO items VALUES (1, 'ITEMAAAA', 1, 1)")
    conn.execute("INSERT INTO items VALUES (2, 'KEYAAAAA', 1, 1)")
    conn.execute("INSERT INTO items VALUES (3, 'KEYBBBBB', 1, 1)")
    conn.execute("INSERT INTO itemAttachments VALUES (2, 1, 'storage:a.pdf', 'application/pdf')")
    conn.execute("INSERT INTO itemAttachments VALUES (3, 1, 'storage:a.pdf', 'application/pdf')")
    conn.commit()
    conn.close()
    load_zotero_library.cache_clear()
    library = load_zotero_library(str(db_path))

    assert lookup_zotero_item("copies/a.pdf", library).item_key == "ITEMAAAA"
    assert not any(item.shared_filename for item in library.values())
