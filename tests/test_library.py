from __future__ import annotations

import os

from pypdf import PdfWriter

from src import config, library
from src.pdf_metadata import clear_pdf_metadata_cache
from src.zotero_db import ZoteroItem


def test_get_cached_summary_cleans_legacy_empty_response_note(monkeypatch) -> None:
    monkeypatch.setattr(library.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(library, "_analysis_fingerprint", lambda _path: "fp")
    monkeypatch.setattr(
        library,
        "get_cached_analysis",
        lambda rel_path, analysis_type, fingerprint: (
            "Answer:\nRésumé rapide.\n\nExplanation:\n\nResponse note:"
        ),
    )

    assert library.get_cached_summary("papers/a.pdf", "Résumé rapide") == "Résumé rapide."


def test_list_zotero_collection_pdfs_matches_nested_collection_paths(monkeypatch) -> None:
    original_library = dict(library._zotero_library)
    try:
        item = ZoteroItem(
            item_key="item-1",
            title="HTS paper",
            collections=["Projet HTS / AC losses"],
        )
        library._zotero_library = {"a.pdf": item}
        monkeypatch.setattr(
            library,
            "find_pdfs",
            lambda: [("papers/a.pdf", "X:/library/papers/a.pdf")],
        )

        records = library.list_zotero_collection_pdfs("Projet HTS")

        assert len(records) == 1
        assert records[0]["path"] == "papers/a.pdf"
        assert records[0]["item"] is item
    finally:
        library._zotero_library = original_library


def test_library_filter_metadata_preserves_full_collections_and_path_order() -> None:
    original_library = dict(library._zotero_library)
    try:
        library._zotero_library = {
            "one.pdf": ZoteroItem(
                item_key="ITEM1",
                title="First paper",
                collections=[
                    "Projet supraconducteurs / Aimants / Pertes AC",
                    "À relire",
                ],
            ),
            "two.pdf": ZoteroItem(
                item_key="ITEM2",
                title="Second paper",
                collections=["Projet supraconducteurs / Câbles / REBCO"],
            ),
        }

        metadata = library.get_library_filter_metadata(
            ["nested/two.pdf", "one.pdf", "missing.pdf"]
        )

        assert metadata == [
            {"collections": ["Projet supraconducteurs / Câbles / REBCO"]},
            {
                "collections": [
                    "Projet supraconducteurs / Aimants / Pertes AC",
                    "À relire",
                ]
            },
            {"collections": []},
        ]
    finally:
        library._zotero_library = original_library


def test_find_library_articles_by_authors_matches_any_author_without_rag(monkeypatch) -> None:
    original_library = dict(library._zotero_library)
    try:
        item = ZoteroItem(
            item_key="ITEM1",
            title="Fast metadata lookup",
            authors=["Alice Example", "Claire Moreau"],
            year="2026",
            doi="10.1234/metadata",
        )
        library._zotero_library = {"paper.pdf": item}
        monkeypatch.setattr(
            library,
            "find_pdfs",
            lambda: [("papers/paper.pdf", "X:/library/papers/paper.pdf")],
        )
        monkeypatch.setattr(
            library,
            "filter_active_zotero_pdf_entries",
            lambda entries, *_args, **_kwargs: entries,
        )
        monkeypatch.setattr(library, "get_indexed_rel_paths", lambda: {"papers/paper.pdf"})

        records, metadata_available = library.find_library_articles_by_authors(["moreau"])

        assert metadata_available is True
        assert [record["title"] for record in records] == ["Fast metadata lookup"]
        assert records[0]["authors"] == ["Alice Example", "Claire Moreau"]
        # Zotero stores "Last, First"; users type "First Last".
        records, _ = library.find_library_articles_by_authors(["Claire Moreau"])
        assert [record["title"] for record in records] == ["Fast metadata lookup"]
        records, _ = library.find_library_articles_by_authors(["Claire Durand"])
        assert records == []
    finally:
        library._zotero_library = original_library


def test_find_library_articles_by_authors_matches_initials_of_a_known_name(monkeypatch) -> None:
    """2026-10-21 report: "claire" missed the paper that stores "Moreau, C."."""
    original_library = dict(library._zotero_library)
    try:
        library._zotero_library = {
            "full.pdf": ZoteroItem(item_key="ITEM1", title="Full name", authors=["Moreau, Claire"]),
            "initial.pdf": ZoteroItem(
                item_key="ITEM2", title="Initial only", authors=["Example, Alice", "Moreau, C."]
            ),
            "double.pdf": ZoteroItem(item_key="ITEM3", title="Two initials", authors=["Moreau, C. C."]),
            "other.pdf": ZoteroItem(item_key="ITEM4", title="Other person", authors=["Moreau, B."]),
            "namesake.pdf": ZoteroItem(item_key="ITEM5", title="Namesake", authors=["Martin, C."]),
        }
        paths = [f"papers/{name}" for name in library._zotero_library]
        monkeypatch.setattr(
            library, "find_pdfs", lambda: [(path, f"X:/library/{path}") for path in paths]
        )
        monkeypatch.setattr(
            library,
            "filter_active_zotero_pdf_entries",
            lambda entries, *_args, **_kwargs: entries,
        )
        monkeypatch.setattr(library, "get_indexed_rel_paths", lambda: set(paths))

        for query in ("claire", "Claire Moreau"):
            records, _ = library.find_library_articles_by_authors([query])
            assert [record["title"] for record in records] == [
                "Full name",
                "Initial only",
                "Two initials",
            ]
        # An initial alone, with no full name to anchor it, matches nothing new.
        records, _ = library.find_library_articles_by_authors(["Alice Example"])
        assert [record["title"] for record in records] == ["Initial only"]
    finally:
        library._zotero_library = original_library

def test_list_library_extracts_pdf_metadata_without_external_source(
    workspace_tmp_dir,
    monkeypatch,
) -> None:
    original_library = dict(library._zotero_library)
    pdf_path = workspace_tmp_dir / "standalone.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_metadata(
        {
            "/Title": "Standalone metadata",
            "/Author": "Grace Hopper",
            "/CreationDate": "D:19520101000000",
            "/Keywords": "compiler; computing",
        }
    )
    with pdf_path.open("wb") as handle:
        writer.write(handle)

    try:
        clear_pdf_metadata_cache()
        library._zotero_library = {}
        monkeypatch.setattr(
            library,
            "find_pdfs",
            lambda: [("standalone.pdf", str(pdf_path))],
        )
        monkeypatch.setattr(library, "get_cached_analysis_types_for_paths", lambda _paths: {})

        rows, paths = library.list_library()

        assert paths == ["standalone.pdf"]
        assert rows[0][0] == "Standalone metadata"
        assert rows[0][1] == "Grace Hopper"
        assert rows[0][2] == "1952"
        assert rows[0][3] == "document"
        assert rows[0][4] == "compiler, computing"
    finally:
        library._zotero_library = original_library


def test_list_library_exposes_every_author_for_search(
    workspace_tmp_dir,
    monkeypatch,
) -> None:
    original_library = dict(library._zotero_library)
    pdf_path = workspace_tmp_dir / "coauthored.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_metadata(
        {
            "/Title": "Coauthored paper",
            "/Author": "Grace Hopper; Alan Turing; Ada Lovelace",
        }
    )
    with pdf_path.open("wb") as handle:
        writer.write(handle)

    try:
        clear_pdf_metadata_cache()
        library._zotero_library = {}
        monkeypatch.setattr(
            library,
            "find_pdfs",
            lambda: [("coauthored.pdf", str(pdf_path))],
        )
        monkeypatch.setattr(library, "get_cached_analysis_types_for_paths", lambda _paths: {})

        rows, _paths = library.list_library()

        assert rows[0][1] == "Grace Hopper"
        assert rows[0][9] == "Grace Hopper; Alan Turing; Ada Lovelace"
        fallback = library._fallback_export_item(rows[0], "coauthored.pdf")
        assert fallback.authors == ["Grace Hopper", "Alan Turing", "Ada Lovelace"]
    finally:
        library._zotero_library = original_library


def test_list_library_hides_orphan_pdfs_when_data_path_is_zotero_storage(
    workspace_tmp_dir,
    monkeypatch,
) -> None:
    original_library = dict(library._zotero_library)
    original_data_path = config.DATA_PATH
    original_zotero_path = config.ZOTERO_DB_PATH
    try:
        zotero_root = workspace_tmp_dir / "zotero"
        storage = zotero_root / "storage"
        active_dir = storage / "ACTVKEY1"
        orphan_dir = storage / "ORPHANPDF"
        active_dir.mkdir(parents=True)
        orphan_dir.mkdir(parents=True)
        active_pdf = active_dir / "active.pdf"
        orphan_pdf = orphan_dir / "orphan.pdf"
        active_pdf.write_bytes(b"%PDF-1.4 active")
        orphan_pdf.write_bytes(b"%PDF-1.4 orphan")
        db_path = zotero_root / "zotero.sqlite"
        db_path.write_bytes(b"sqlite placeholder")

        config.DATA_PATH = str(storage)
        config.ZOTERO_DB_PATH = str(db_path)
        library._zotero_library = {
            "actvkey1": ZoteroItem(
                item_key="ITEM1",
                attachment_key="ACTVKEY1",
                title="Active paper",
                date_added="2026-07-14 08:12:00",
            )
        }
        monkeypatch.setattr(
            library,
            "find_pdfs",
            lambda: [
                (os.path.join("ACTVKEY1", "active.pdf"), str(active_pdf)),
                (os.path.join("ORPHANPDF", "orphan.pdf"), str(orphan_pdf)),
            ],
        )
        monkeypatch.setattr(
            library,
            "get_indexed_rel_paths",
            lambda: {
                os.path.join("ACTVKEY1", "active.pdf"),
                os.path.join("ORPHANPDF", "orphan.pdf"),
            },
        )
        monkeypatch.setattr(library, "get_cached_analysis_types_for_paths", lambda _paths: {})

        rows, paths = library.list_library(indexed_only=True)

        assert paths == [os.path.join("ACTVKEY1", "active.pdf")]
        assert rows[0][0] == "Active paper"
        assert rows[0][8] == "2026-07-14"
    finally:
        library._zotero_library = original_library
        config.DATA_PATH = original_data_path
        config.ZOTERO_DB_PATH = original_zotero_path


def test_index_reload_shows_pdf_zotero_gained_since_startup_in_library_tab(
    workspace_tmp_dir,
    monkeypatch,
) -> None:
    from src.rag import state, zotero_integration

    original_library = dict(library._zotero_library)
    original_db_path = library._zotero_db_path
    original_rag_library = dict(state._zotero_library)
    original_data_path = config.DATA_PATH
    original_zotero_path = config.ZOTERO_DB_PATH
    try:
        storage = workspace_tmp_dir / "zotero" / "storage"
        old_pdf = storage / "OLDKEY01" / "old.pdf"
        new_pdf = storage / "NEWKEY01" / "new.pdf"
        for pdf in (old_pdf, new_pdf):
            pdf.parent.mkdir(parents=True)
            pdf.write_bytes(b"%PDF-1.4 paper")
        db_path = workspace_tmp_dir / "zotero" / "zotero.sqlite"
        db_path.write_bytes(b"sqlite placeholder")
        config.DATA_PATH = str(storage)
        config.ZOTERO_DB_PATH = str(db_path)

        old_item = ZoteroItem(item_key="ITEMOLD", attachment_key="OLDKEY01", title="Old paper")
        new_item = ZoteroItem(item_key="ITEMNEW", attachment_key="NEWKEY01", title="New paper")
        # The Library tab loaded Zotero at startup, before the PDF was dropped.
        library._zotero_library = {"oldkey01": old_item}
        library._zotero_db_path = str(db_path)

        monkeypatch.setattr(
            zotero_integration,
            "get_app_settings",
            lambda: {"metadata_source": "zotero", "zotero_path": str(db_path)},
        )
        loaded = {"oldkey01": old_item, "newkey01": new_item}
        load_calls: list[str] = []

        def fake_load(path, _fingerprints):
            load_calls.append(path)
            return loaded

        fake_load.cache_clear = lambda: None  # type: ignore[attr-defined]
        monkeypatch.setattr(zotero_integration, "load_zotero_library", fake_load)
        monkeypatch.setattr(
            library,
            "find_pdfs",
            lambda: [
                (os.path.join("OLDKEY01", "old.pdf"), str(old_pdf)),
                (os.path.join("NEWKEY01", "new.pdf"), str(new_pdf)),
            ],
        )
        monkeypatch.setattr(
            library,
            "get_indexed_rel_paths",
            lambda: {os.path.join("OLDKEY01", "old.pdf"), os.path.join("NEWKEY01", "new.pdf")},
        )
        monkeypatch.setattr(library, "get_cached_analysis_types_for_paths", lambda _paths: {})

        _rows, paths = library.list_library(indexed_only=True)
        assert paths == [os.path.join("OLDKEY01", "old.pdf")]

        # A reload that is not forced (first indexing, chat on a cold index).
        zotero_integration._refresh_zotero_library_cache()

        rows, paths = library.list_library(indexed_only=True)
        assert load_calls == [str(db_path)]
        assert sorted(paths) == sorted(
            [os.path.join("OLDKEY01", "old.pdf"), os.path.join("NEWKEY01", "new.pdf")]
        )
        assert "New paper" in {row[0] for row in rows}
    finally:
        library._zotero_library = original_library
        library._zotero_db_path = original_db_path
        state._zotero_library = original_rag_library
        config.DATA_PATH = original_data_path
        config.ZOTERO_DB_PATH = original_zotero_path


def test_index_reload_from_another_database_leaves_library_tab_alone(monkeypatch) -> None:
    original_library = dict(library._zotero_library)
    original_db_path = library._zotero_db_path
    try:
        current = ZoteroItem(item_key="ITEM1", attachment_key="KEY00001", title="Current")
        library._zotero_library = {"key00001": current}
        library._zotero_db_path = "/zotero/current.sqlite"

        library.adopt_zotero_library(
            "/zotero/other.sqlite",
            {"key00002": ZoteroItem(item_key="ITEM2", title="Other")},
        )

        assert library._zotero_library == {"key00001": current}
        assert library._zotero_db_path == "/zotero/current.sqlite"
    finally:
        library._zotero_library = original_library
        library._zotero_db_path = original_db_path


def test_build_library_export_generates_markdown_and_deduplicated_bibtex(
    monkeypatch,
) -> None:
    original_library = dict(library._zotero_library)
    try:
        shared_item = ZoteroItem(
            item_key="ITEM1",
            citation_key="Alpha2024",
            title="Alpha paper",
            authors=["Alpha, Alice", "Beta, Bob"],
            year="2024",
            journal="Journal of Tests",
            doi="10.1234/example",
            tags=["RAG"],
            collections=["Project A"],
            item_type="journalArticle",
        )
        duplicate_item = ZoteroItem(
            item_key="ITEM2",
            citation_key="Alpha2024Duplicate",
            title=shared_item.title,
            authors=list(shared_item.authors),
            year=shared_item.year,
            journal=shared_item.journal,
            doi=shared_item.doi,
            item_type=shared_item.item_type,
        )
        library._zotero_library = {
            "a.pdf": shared_item,
            "a-supplement.pdf": duplicate_item,
        }
        monkeypatch.setattr(
            library,
            "list_library",
            lambda indexed_only=True: (
                [
                    ["Alpha paper", "Alpha, Alice", "2024", "journalArticle", "RAG", "Project A"],
                    ["Alpha supplement", "Alpha, Alice", "2024", "attachment", "-", "Project A"],
                    ["Local only", "-", "-", "-", "-", "-"],
                ],
                ["papers/a.pdf", "papers/a-supplement.pdf", "papers/local.pdf"],
            ),
        )

        markdown = library.build_library_export("markdown")
        bibtex = library.build_library_export(
            "bibtex",
            ["papers/a.pdf", "papers/a-supplement.pdf", "papers/local.pdf"],
        )

        assert markdown["count"] == 2
        assert "# Bibliobrain - Articles" in markdown["content"]
        assert "https://doi.org/10.1234/example" in markdown["content"]
        assert bibtex["count"] == 2
        assert bibtex["content"].count("@article{Alpha2024") == 1
        assert "@misc{local" in bibtex["content"]
    finally:
        library._zotero_library = original_library
