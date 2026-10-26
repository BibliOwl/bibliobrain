from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from src.zotero_db import (
    ZoteroItem,
    filter_active_zotero_pdf_entries,
    is_bibtex_source,
    load_zotero_library,
    lookup_zotero_item,
)


def test_load_bibtex_library_accepts_text_export(workspace_tmp_dir: Path) -> None:
    bib_path = workspace_tmp_dir / "export.txt"
    bib_path.write_text(
        """
@article{First2024,
  title = {First paper},
  author = {Example, Alice},
  year = {2024}
}
@article{Second2025,
  title = {Second paper},
  author = {Example, Bob},
  year = {2025}
}
""".strip(),
        encoding="utf-8",
    )

    load_zotero_library.cache_clear()
    library = load_zotero_library(str(bib_path))
    unique_items = {id(item): item for item in library.values()}

    assert is_bibtex_source(str(bib_path)) is True
    assert len(unique_items) == 2
    first_item = lookup_zotero_item("First2024.pdf", library)
    assert first_item is not None
    assert first_item.title == "First paper"


def test_load_bibtex_library_parses_jabref_pdf_fields_and_groups(
    workspace_tmp_dir: Path,
) -> None:
    bib_path = workspace_tmp_dir / "example.bib"
    bib_path.write_text(
        """
@Article{Minenna2026,
  author   = {Minenna, Damien F. G. and Dilasser, Guillaume},
  title    = {Data-Driven Optimisation of Superconducting Magnets at CEA Paris-Saclay},
  year     = {2026},
  abstract = {Superconducting magnets and artificial intelligence.},
  doi      = {10.48550/ARXIV.2603.29740},
  file     = {:https\\://arxiv.org/pdf/2603.29740v2:PDF},
  groups   = {SubGroup1},
  keywords = {Accelerator Physics (physics.acc-ph), Plasma Physics (physics.plasm-ph)}
}

@Article{Moreau2025,
  author  = {Moreau, Camille and Benoist, Emeric},
  journal = {IEEE Transactions on Applied Superconductivity},
  title   = {Racetrack-Shaped HTS Coils With Metal-as-Insulation},
  year    = {2025},
  doi     = {10.1109/tasc.2025.3542344},
  file    = {:C\\:/Users/demo/Documents/Racetrack-Shaped_HTS_Coils.pdf:PDF}
}

@Article{Minenna2019,
  author  = {Minenna, Damien F. G. and Andre, Frederic},
  journal = {The European Physical Journal H},
  title   = {The traveling-wave tube in the history of telecommunication},
  year    = {2019},
  doi     = {10.1140/epjh/e2018-90023-1},
  file    = {:Minenna2019 - The Traveling Wave Tube.pdf:PDF:https\\://example.org/file.pdf},
  groups  = {Groupe1}
}

@Comment{jabref-meta: databaseType:bibtex;}
""".strip(),
        encoding="utf-8",
    )

    load_zotero_library.cache_clear()
    library = load_zotero_library(str(bib_path))
    unique_items = {id(item): item for item in library.values()}

    assert len(unique_items) == 3

    minenna_2026 = lookup_zotero_item("Minenna2026.pdf", library)
    assert minenna_2026 is not None
    assert minenna_2026.title == "Data-Driven Optimisation of Superconducting Magnets at CEA Paris-Saclay"
    assert minenna_2026.year == "2026"
    assert minenna_2026.doi == "10.48550/ARXIV.2603.29740"
    assert minenna_2026.collections == ["SubGroup1"]
    assert minenna_2026.tags == [
        "Accelerator Physics (physics.acc-ph)",
        "Plasma Physics (physics.plasm-ph)",
    ]

    moreau = lookup_zotero_item("papers/Racetrack-Shaped_HTS_Coils.pdf", library)
    assert moreau is not None
    assert moreau.citation_key == "Moreau2025"
    assert moreau.journal == "IEEE Transactions on Applied Superconductivity"

    minenna_2019 = lookup_zotero_item(
        "Minenna2019 - The Traveling Wave Tube.pdf",
        library,
    )
    assert minenna_2019 is not None
    assert minenna_2019.collections == ["Groupe1"]


def _create_minimal_zotero_sqlite(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT);
            CREATE TABLE groups (libraryID INTEGER, groupID INTEGER);
            CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT);
            CREATE TABLE items (
                itemID INTEGER PRIMARY KEY,
                key TEXT,
                libraryID INTEGER,
                itemTypeID INTEGER
            );
            CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
            CREATE TABLE itemAttachments (
                itemID INTEGER PRIMARY KEY,
                parentItemID INTEGER,
                path TEXT,
                contentType TEXT
            );
            CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT);
            CREATE TABLE itemData (itemID INTEGER, fieldID INTEGER, valueID INTEGER);
            CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT);
            CREATE TABLE itemCreators (itemID INTEGER, creatorID INTEGER, creatorTypeID INTEGER, orderIndex INTEGER);
            CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, lastName TEXT, firstName TEXT);
            CREATE TABLE creatorTypes (creatorTypeID INTEGER PRIMARY KEY, creatorType TEXT);
            CREATE TABLE itemTags (itemID INTEGER, tagID INTEGER);
            CREATE TABLE tags (tagID INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE collections (
                collectionID INTEGER PRIMARY KEY,
                collectionName TEXT,
                parentCollectionID INTEGER
            );
            CREATE TABLE collectionItems (itemID INTEGER, collectionID INTEGER);
            CREATE TABLE itemNotes (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, title TEXT, note TEXT);
            CREATE TABLE itemAnnotations (
                itemID INTEGER PRIMARY KEY,
                parentItemID INTEGER,
                text TEXT,
                comment TEXT,
                color TEXT,
                pageLabel TEXT
            );
            """
        )
        conn.execute("INSERT INTO libraries VALUES (1, 'user')")
        conn.execute("INSERT INTO itemTypes VALUES (1, 'journalArticle')")
        rows = [
            (1, "PARENT1", 1, 1),
            (2, "ATTACH1", 1, 1),
            (3, "PARENT_DELETED", 1, 1),
            (4, "ATTACH_PARENT_DELETED", 1, 1),
            (5, "PARENT2", 1, 1),
            (6, "ATTACH_DELETED", 1, 1),
        ]
        conn.executemany("INSERT INTO items VALUES (?, ?, ?, ?)", rows)
        attachments = [
            (2, 1, "storage:ATTACH1/active.pdf", "application/pdf"),
            (4, 3, "storage:ATTACH_PARENT_DELETED/deleted-parent.pdf", "application/pdf"),
            (6, 5, "storage:ATTACH_DELETED/deleted-attachment.pdf", "application/pdf"),
        ]
        conn.executemany("INSERT INTO itemAttachments VALUES (?, ?, ?, ?)", attachments)
        conn.executemany("INSERT INTO deletedItems VALUES (?)", [(3,), (6,)])
        conn.execute("INSERT INTO fields VALUES (1, 'title')")
        conn.executemany(
            "INSERT INTO itemDataValues VALUES (?, ?)",
            [
                (1, "Active paper"),
                (2, "Deleted parent paper"),
                (3, "Deleted attachment paper"),
            ],
        )
        conn.executemany(
            "INSERT INTO itemData VALUES (?, ?, ?)",
            [(1, 1, 1), (3, 1, 2), (5, 1, 3)],
        )
        conn.commit()
    finally:
        conn.close()


def test_load_zotero_library_ignores_deleted_parent_and_attachment(
    workspace_tmp_dir: Path,
) -> None:
    db_path = workspace_tmp_dir / "zotero.sqlite"
    _create_minimal_zotero_sqlite(db_path)

    load_zotero_library.cache_clear()
    library = load_zotero_library(str(db_path))

    active = lookup_zotero_item("ATTACH1/active.pdf", library)
    assert active is not None
    assert active.item_key == "PARENT1"
    assert active.attachment_key == "ATTACH1"
    assert lookup_zotero_item("ATTACH_PARENT_DELETED/deleted-parent.pdf", library) is None
    assert lookup_zotero_item("ATTACH_DELETED/deleted-attachment.pdf", library) is None


def test_load_zotero_library_reads_date_added_when_available(
    workspace_tmp_dir: Path,
) -> None:
    db_path = workspace_tmp_dir / "zotero.sqlite"
    _create_minimal_zotero_sqlite(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("ALTER TABLE items ADD COLUMN dateAdded TEXT")
        conn.execute("UPDATE items SET dateAdded = '2026-07-14 08:12:00' WHERE itemID = 1")

    load_zotero_library.cache_clear()
    library = load_zotero_library(str(db_path))

    active = lookup_zotero_item("ATTACH1/active.pdf", library)
    assert active is not None
    assert active.date_added == "2026-07-14 08:12:00"


def test_filter_active_zotero_pdf_entries_keeps_only_known_active_attachments(
    workspace_tmp_dir: Path,
) -> None:
    db_path = workspace_tmp_dir / "zotero.sqlite"
    storage = workspace_tmp_dir / "storage"
    storage.mkdir()
    db_path.write_bytes(b"sqlite placeholder")
    library = {
        "ATCHKEY1".lower(): ZoteroItem(item_key="PARENT1", attachment_key="ATCHKEY1"),
    }
    pdf_entries = [
        ("ATCHKEY1/active.pdf", str(storage / "ATCHKEY1" / "active.pdf")),
        ("ORPHAN/orphan.pdf", str(storage / "ORPHAN" / "orphan.pdf")),
    ]

    filtered = filter_active_zotero_pdf_entries(
        pdf_entries,
        library,
        data_path=str(storage),
        zotero_db_path=str(db_path),
    )

    assert filtered == [pdf_entries[0]]


def test_load_zotero_library_indexes_only_the_standalone_pdfs_dropped_into_bibliobrain(
    workspace_tmp_dir: Path,
) -> None:
    db_path = workspace_tmp_dir / "zotero.sqlite"
    _create_minimal_zotero_sqlite(db_path)
    storage = workspace_tmp_dir / "storage"
    pdfs = {
        "DROPPED1": ("Audit UI-UX.pdf", b"%PDF-1.4 dropped"),
        "OTHER001": ("other.pdf", b"%PDF-1.4 never dropped"),
        "TRASHED1": ("trashed.pdf", b"%PDF-1.4 dropped then trashed"),
    }
    for key, (name, body) in pdfs.items():
        (storage / key).mkdir(parents=True)
        (storage / key / name).write_bytes(body)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO items VALUES (?, ?, 1, 1)",
            [(10, "DROPPED1"), (11, "OTHER001"), (12, "TRASHED1")],
        )
        conn.executemany(
            "INSERT INTO itemAttachments VALUES (?, NULL, ?, 'application/pdf')",
            [(10, "storage:Audit UI-UX.pdf"), (11, "storage:other.pdf"), (12, "storage:trashed.pdf")],
        )
        conn.execute("INSERT INTO deletedItems VALUES (12)")
        # Zotero titles a standalone attachment after the file it was given.
        conn.execute("INSERT INTO itemDataValues VALUES (10, 'Audit UI-UX.pdf')")
        conn.execute("INSERT INTO itemData VALUES (10, 1, 10)")
    dropped = frozenset(
        (len(body), hashlib.sha256(body).hexdigest())
        for name, body in (pdfs["DROPPED1"], pdfs["TRASHED1"])
    )

    load_zotero_library.cache_clear()
    without_drops = load_zotero_library(str(db_path))
    library = load_zotero_library(str(db_path), dropped)

    assert lookup_zotero_item("DROPPED1/Audit UI-UX.pdf", without_drops) is None
    dropped_item = lookup_zotero_item("DROPPED1/Audit UI-UX.pdf", library)
    assert dropped_item is not None
    assert dropped_item.title == "Audit UI-UX"
    assert dropped_item.item_key == dropped_item.attachment_key == "DROPPED1"
    assert dropped_item.authors == [] and dropped_item.year == ""
    assert lookup_zotero_item("OTHER001/other.pdf", library) is None
    assert lookup_zotero_item("TRASHED1/trashed.pdf", library) is None
    # Parented attachments are loaded as before.
    attached = lookup_zotero_item("ATTACH1/active.pdf", library)
    assert attached is not None and attached.item_key == "PARENT1"
    entries = [
        (f"{key}/{name}", str(storage / key / name)) for key, (name, _body) in pdfs.items()
    ]
    assert filter_active_zotero_pdf_entries(
        entries, library, data_path=str(storage), zotero_db_path=str(db_path)
    ) == [entries[0]]
    # With the option on, every standalone PDF still in the library is indexed.
    everything = load_zotero_library(str(db_path), frozenset(), True)
    other = lookup_zotero_item("OTHER001/other.pdf", everything)
    assert other is not None and other.title == "other"
    assert lookup_zotero_item("DROPPED1/Audit UI-UX.pdf", everything) is not None
    assert lookup_zotero_item("TRASHED1/trashed.pdf", everything) is None
    assert lookup_zotero_item("ATTACH1/active.pdf", everything) is not None


def test_load_zotero_library_reads_a_copy_when_zotero_is_writing(
    workspace_tmp_dir: Path, monkeypatch: Any
) -> None:
    # Read as immutable, the live database can show half-written pages while
    # Zotero syncs: the read is retried once from a copy.
    from src import zotero_db

    db_path = workspace_tmp_dir / "zotero.sqlite"
    _create_minimal_zotero_sqlite(db_path)
    real_open = zotero_db._open_db
    opened: list[str] = []

    def flaky_open(path: str, **kwargs: Any) -> sqlite3.Connection:
        opened.append(path)
        if len(opened) == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real_open(path, **kwargs)

    monkeypatch.setattr(zotero_db, "_open_db", flaky_open)
    monkeypatch.setattr(zotero_db, "_ZOTERO_RETRY_DELAY_SECONDS", 0)
    load_zotero_library.cache_clear()

    library = load_zotero_library(str(db_path))

    assert len(opened) == 2 and opened[1] != str(db_path)
    attached = lookup_zotero_item("ATTACH1/active.pdf", library)
    assert attached is not None and attached.item_key == "PARENT1"


def test_load_zotero_library_sees_changes_still_in_the_wal(workspace_tmp_dir: Path) -> None:
    # An immutable read ignores the WAL: a title Zotero committed there stayed
    # invisible until the next checkpoint.
    db_path = workspace_tmp_dir / "zotero.sqlite"
    _create_minimal_zotero_sqlite(db_path)
    writer = sqlite3.connect(db_path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE itemDataValues SET value = 'Renamed paper' WHERE valueID = 1")
        writer.commit()
        assert (workspace_tmp_dir / "zotero.sqlite-wal").stat().st_size > 32
        before = set(os.listdir(tempfile.gettempdir()))
        load_zotero_library.cache_clear()

        library = load_zotero_library(str(db_path))

        attached = lookup_zotero_item("ATTACH1/active.pdf", library)
        assert attached is not None and attached.title == "Renamed paper"
        # The private copy read for it is gone.
        leftovers = set(os.listdir(tempfile.gettempdir())) - before
        assert not [name for name in leftovers if name.startswith("bibliobrain-zotero-")]
    finally:
        writer.close()
        load_zotero_library.cache_clear()
