"""Zotero libraries whose PDFs were moved out of storage/ (ZotMoov, linked files)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src import zotero_db


def _write_attachments_db(path: Path, attachment_paths: list[str]) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE itemAttachments (
                itemID INTEGER PRIMARY KEY, parentItemID INTEGER, path TEXT, contentType TEXT
            );
            CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY);
            """
        )
        conn.executemany(
            "INSERT INTO itemAttachments VALUES (?, ?, ?, 'application/pdf')",
            [(index + 100, index + 1, value) for index, value in enumerate(attachment_paths)],
        )
        conn.commit()
    finally:
        conn.close()
    return path


def _write_prefs(path: Path, lines: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(f'user_pref("{name}", {value});' for name, value in lines.items())
    path.write_text("// Mozilla User Preferences\n" + body + "\n", encoding="utf-8")
    return path


def test_read_zotero_prefs_keeps_zotero_and_zotmoov_values(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefs_file = _write_prefs(
        workspace_tmp_dir / "profile" / "prefs.js",
        {
            "extensions.zotero.dataDir": '"D:\\\\Recherche\\\\Zotero"',
            "extensions.zotero.useDataDir": "true",
            "extensions.zotmoov.dst_dir": '"D:\\\\Articles"',
            "browser.startup.page": "3",
        },
    )
    monkeypatch.setattr(zotero_db, "_zotero_prefs_files", lambda: [str(prefs_file)])

    prefs = zotero_db.read_zotero_prefs()

    assert prefs == {
        "extensions.zotero.dataDir": "D:\\Recherche\\Zotero",
        "extensions.zotero.useDataDir": True,
        "extensions.zotmoov.dst_dir": "D:\\Articles",
    }


def test_find_zotero_db_prefers_the_data_directory_set_in_zotero(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_dir = workspace_tmp_dir / "custom-zotero"
    custom_dir.mkdir()
    (custom_dir / "zotero.sqlite").write_bytes(b"")
    home = workspace_tmp_dir / "home"
    (home / "Zotero").mkdir(parents=True)
    (home / "Zotero" / "zotero.sqlite").write_bytes(b"")
    monkeypatch.setenv("APPDATA", str(workspace_tmp_dir / "no-appdata"))
    monkeypatch.setattr(zotero_db.os.path, "expanduser", lambda _value: str(home))
    monkeypatch.setattr(
        zotero_db,
        "read_zotero_prefs",
        lambda: {
            "extensions.zotero.dataDir": str(custom_dir),
            "extensions.zotero.useDataDir": True,
        },
    )

    assert zotero_db.find_zotero_db() == str(custom_dir / "zotero.sqlite")

    monkeypatch.setattr(zotero_db, "read_zotero_prefs", lambda: {})
    assert zotero_db.find_zotero_db() == str(home / "Zotero" / "zotero.sqlite")


def test_zotmoov_destination_is_suggested_instead_of_storage(workspace_tmp_dir: Path) -> None:
    zotero_dir = workspace_tmp_dir / "Zotero"
    (zotero_dir / "storage").mkdir(parents=True)
    articles = workspace_tmp_dir / "Articles"
    (articles / "Aimants").mkdir(parents=True)
    db_path = _write_attachments_db(
        zotero_dir / "zotero.sqlite",
        [
            str(articles / "Aimants" / "Bobine HTS.pdf"),
            str(articles / "Aimants" / "Quench.pdf"),
            str(articles / "Cryogenie.pdf"),
            "storage:ABCD1234/reste.pdf",
        ],
    )
    prefs = {"extensions.zotmoov.dst_dir": str(articles)}

    assert zotero_db.find_zotero_linked_pdf_folder(str(db_path), prefs=prefs) == str(articles)
    # Without ZotMoov's setting the common folder of the linked PDFs is used.
    assert zotero_db.find_zotero_linked_pdf_folder(str(db_path), prefs={}) == str(articles)


def test_relative_linked_files_use_zotero_base_directory(workspace_tmp_dir: Path) -> None:
    zotero_dir = workspace_tmp_dir / "Zotero"
    zotero_dir.mkdir()
    base = workspace_tmp_dir / "Base"
    (base / "Revue").mkdir(parents=True)
    db_path = _write_attachments_db(
        zotero_dir / "zotero.sqlite",
        ["attachments:Revue/a.pdf", "attachments:b.pdf"],
    )
    prefs = {"extensions.zotero.baseAttachmentPath": str(base)}

    assert zotero_db.find_zotero_linked_pdf_folder(str(db_path), prefs=prefs) == str(base)


def test_storage_stays_the_pdf_folder_when_most_pdfs_are_stored(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    zotero_dir = workspace_tmp_dir / "Zotero"
    (zotero_dir / "storage").mkdir(parents=True)
    linked = workspace_tmp_dir / "Ailleurs"
    linked.mkdir()
    db_path = _write_attachments_db(
        zotero_dir / "zotero.sqlite",
        ["storage:AAAA1111/a.pdf", "storage:BBBB2222/b.pdf", str(linked / "c.pdf")],
    )
    monkeypatch.setattr(zotero_db, "read_zotero_prefs", lambda: {})

    assert zotero_db.find_zotero_linked_pdf_folder(str(db_path)) is None
    assert zotero_db.suggest_zotero_pdf_folder(str(db_path)) == str(zotero_dir / "storage")


def test_home_folder_is_never_suggested(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = workspace_tmp_dir / "home"
    (home / "Documents").mkdir(parents=True)
    (home / "Downloads").mkdir()
    zotero_dir = workspace_tmp_dir / "Zotero"
    zotero_dir.mkdir()
    db_path = _write_attachments_db(
        zotero_dir / "zotero.sqlite",
        [str(home / "Documents" / "a.pdf"), str(home / "Downloads" / "b.pdf")],
    )
    real_expanduser = zotero_db.os.path.expanduser
    monkeypatch.setattr(
        zotero_db.os.path,
        "expanduser",
        lambda value: str(home) if value == "~" else real_expanduser(value),
    )

    assert zotero_db.find_zotero_linked_pdf_folder(str(db_path), prefs={}) is None


def test_loading_zotero_keeps_the_pdf_folder_the_user_chose(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src import config, library

    zotero_dir = workspace_tmp_dir / "Zotero"
    (zotero_dir / "storage").mkdir(parents=True)
    db_path = zotero_dir / "zotero.sqlite"
    db_path.write_bytes(b"")
    chosen = workspace_tmp_dir / "Mes articles"
    chosen.mkdir()
    monkeypatch.setattr(config, "DATA_PATH", str(chosen))
    monkeypatch.setattr(library, "load_zotero_library", _FakeLoader())
    monkeypatch.setattr(
        library, "suggest_zotero_pdf_folder", lambda _path: str(zotero_dir / "storage")
    )

    monkeypatch.setattr(library, "get_app_settings", lambda: {"data_path_configured": True})
    status = library.init_zotero(str(db_path))
    assert status.startswith("Zotero OK")
    assert config.DATA_PATH == str(chosen)

    # A first run with no folder chosen yet still starts from Zotero's PDFs.
    monkeypatch.setattr(library, "get_app_settings", lambda: {"data_path_configured": False})
    library.init_zotero(str(db_path))
    assert config.DATA_PATH == str(zotero_dir / "storage")


class _FakeLoader:
    def __call__(self, *_args: object, **_kwargs: object) -> dict[str, zotero_db.ZoteroItem]:
        return {"a.pdf": zotero_db.ZoteroItem(title="A")}

    def cache_clear(self) -> None:
        return None
