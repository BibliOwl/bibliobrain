from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest

from src.document_formats import (
    DocumentFormatError,
    extract_document_metadata,
    load_document_units,
)
from src.file_access import resolve_document_path, resolve_pdf_path


def _write_archive(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)


def test_text_document_is_extracted_and_resolved_safely(workspace_tmp_dir: Path) -> None:
    document = workspace_tmp_dir / "notes.md"
    document.write_text("# Mes notes\n\nRésultat expérimental important.", encoding="utf-8")

    units = load_document_units(document)

    assert len(units) == 1
    assert units[0].content_type == "text_document"
    assert "Résultat expérimental" in units[0].text
    metadata = extract_document_metadata(document)
    assert metadata is not None
    assert metadata.title == "Mes notes"
    assert resolve_document_path(str(workspace_tmp_dir), "notes.md") == document.resolve()
    assert resolve_pdf_path(str(workspace_tmp_dir), "notes.md") is None


def test_docx_keeps_heading_sections_and_core_metadata(workspace_tmp_dir: Path) -> None:
    document = workspace_tmp_dir / "article.docx"
    _write_archive(
        document,
        {
            "word/document.xml": """
                <w:document xmlns:w="urn:w"><w:body>
                  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Méthodes</w:t></w:r></w:p>
                  <w:p><w:r><w:t>Le protocole emploie un capteur optique.</w:t></w:r></w:p>
                  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Résultats</w:t></w:r></w:p>
                  <w:p><w:r><w:t>La précision atteint 98 pour cent.</w:t></w:r></w:p>
                </w:body></w:document>
            """,
            "docProps/core.xml": """
                <cp:coreProperties xmlns:cp="urn:cp" xmlns:dc="urn:dc" xmlns:dcterms="urn:dcterms">
                  <dc:title>Article DOCX</dc:title><dc:creator>Ada Lovelace</dc:creator>
                  <dcterms:created>2025-03-01T00:00:00Z</dcterms:created>
                </cp:coreProperties>
            """,
        },
    )

    units = load_document_units(document)
    metadata = extract_document_metadata(document)

    assert [unit.content_type for unit in units] == ["document_section", "document_section"]
    assert units[0].location_label == "Section 1 — Méthodes"
    assert "capteur optique" in units[0].text
    assert metadata is not None
    assert (metadata.title, metadata.authors, metadata.year) == (
        "Article DOCX",
        ["Ada Lovelace"],
        "2025",
    )


def test_odt_keeps_sections_and_metadata(workspace_tmp_dir: Path) -> None:
    document = workspace_tmp_dir / "rapport.odt"
    _write_archive(
        document,
        {
            "content.xml": """
                <office:document-content xmlns:office="urn:office" xmlns:text="urn:text">
                  <office:body><office:text>
                    <text:h>Introduction</text:h><text:p>Texte ODT indexable.</text:p>
                  </office:text></office:body>
                </office:document-content>
            """,
            "meta.xml": """
                <office:document-meta xmlns:office="urn:office" xmlns:dc="urn:dc" xmlns:meta="urn:meta">
                  <office:meta><dc:title>Rapport ODT</dc:title><meta:initial-creator>Grace Hopper</meta:initial-creator>
                  <dc:date>2024-07-02</dc:date></office:meta>
                </office:document-meta>
            """,
        },
    )

    units = load_document_units(document)
    metadata = extract_document_metadata(document)

    assert len(units) == 1
    assert units[0].location_label == "Section 1 — Introduction"
    assert "Texte ODT indexable" in units[0].text
    assert metadata is not None
    assert metadata.title == "Rapport ODT"
    assert metadata.authors == ["Grace Hopper"]
    assert metadata.year == "2024"


def test_epub_follows_spine_and_extracts_bibliographic_metadata(
    workspace_tmp_dir: Path,
) -> None:
    document = workspace_tmp_dir / "livre.epub"
    _write_archive(
        document,
        {
            "META-INF/container.xml": """
                <container xmlns="urn:container"><rootfiles>
                  <rootfile full-path="OEBPS/content.opf"/>
                </rootfiles></container>
            """,
            "OEBPS/content.opf": """
                <package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">
                  <metadata><dc:title>Livre EPUB</dc:title><dc:creator>Ursula Auteur</dc:creator>
                    <dc:date>2023</dc:date><dc:identifier>doi:10.1234/example.epub</dc:identifier></metadata>
                  <manifest><item id="c2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>
                    <item id="c1" href="chapter1.xhtml" media-type="application/xhtml+xml"/></manifest>
                  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
                </package>
            """,
            "OEBPS/chapter1.xhtml": "<html><body><h1>Premier</h1><p>Début du livre.</p></body></html>",
            "OEBPS/chapter2.xhtml": "<html><body><h1>Second</h1><p>Fin du livre.</p></body></html>",
        },
    )

    units = load_document_units(document)
    metadata = extract_document_metadata(document)

    assert [unit.page for unit in units] == [1, 2]
    assert "Premier" in units[0].text
    assert "Second" in units[1].text
    assert all(unit.content_type == "ebook_chapter" for unit in units)
    assert metadata is not None
    assert metadata.title == "Livre EPUB"
    assert metadata.authors == ["Ursula Auteur"]
    assert metadata.doi == "10.1234/example.epub"


def test_pptx_keeps_one_rag_unit_per_slide(workspace_tmp_dir: Path) -> None:
    document = workspace_tmp_dir / "présentation.pptx"
    _write_archive(
        document,
        {
            "ppt/slides/slide2.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>Conclusion</a:t></a:r></a:p></p:sld>',
            "ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>Hypothèse</a:t></a:r></a:p></p:sld>',
            "docProps/core.xml": '<cp:coreProperties xmlns:cp="urn:cp" xmlns:dc="urn:dc"><dc:title>Présentation</dc:title></cp:coreProperties>',
        },
    )

    units = load_document_units(document)
    metadata = extract_document_metadata(document)

    assert [(unit.page, unit.text) for unit in units] == [(1, "Hypothèse"), (2, "Conclusion")]
    assert all(unit.content_type == "presentation_slide" for unit in units)
    assert metadata is not None
    assert metadata.item_type == "presentation"


def test_archive_member_traversal_is_rejected(workspace_tmp_dir: Path) -> None:
    document = workspace_tmp_dir / "hostile.docx"
    _write_archive(
        document,
        {
            "word/document.xml": '<w:document xmlns:w="urn:w"><w:body/></w:document>',
            "../outside.xml": "blocked",
        },
    )

    with pytest.raises(DocumentFormatError, match="Chemin interne"):
        load_document_units(document)


def _write_deck_with_member(path: Path, member: str, size: int) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:p><a:r><a:t>Bobine testée</a:t></a:r></a:p></p:sld>',
        )
        archive.writestr(
            "docProps/core.xml",
            '<cp:coreProperties xmlns:cp="urn:cp" xmlns:dc="urn:dc"><dc:title>Essais</dc:title></cp:coreProperties>',
        )
        archive.writestr(member, bytes(size))


@pytest.mark.parametrize(
    "member",
    [
        "ppt/media/media4.mp4",
        # From a tester's log: a large spreadsheet pasted into a slide.
        "ppt/embeddings/oleObject26.bin",
        "ppt/embeddings/Microsoft_Excel_Worksheet3.xlsx",
        "ppt/fonts/font1.fntdata",
    ],
)
def test_pptx_with_large_embedded_payload_is_still_indexed(
    workspace_tmp_dir: Path, member: str
) -> None:
    # A tester's decks held a 30 MB video and a large embedded object: the
    # whole library scan stopped on them.
    document = workspace_tmp_dir / "essais.pptx"
    _write_deck_with_member(document, member, 30 * 1024 * 1024)

    units = load_document_units(document)
    metadata = extract_document_metadata(document)

    assert [unit.text for unit in units] == ["Bobine testée"]
    assert metadata is not None
    assert metadata.title == "Essais"


@pytest.mark.parametrize(
    "member",
    ["ppt/slides/slide2.xml", "ppt/media/hidden.xml", "ppt/embeddings/_rels/x.xml.rels"],
)
def test_oversized_text_part_still_rejected_but_metadata_never_raises(
    workspace_tmp_dir: Path, member: str
) -> None:
    # A text part counts wherever it sits: the binary folders are no way round.
    document = workspace_tmp_dir / "bombe.pptx"
    _write_deck_with_member(document, member, 30 * 1024 * 1024)

    with pytest.raises(DocumentFormatError, match="trop volumineux"):
        load_document_units(document)
    assert extract_document_metadata(document) is None


def test_document_over_size_limit_does_not_stop_metadata_resolution(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.document_formats import resolve_document_metadata

    monkeypatch.setenv("BIBLIOBRAIN_MAX_DOCUMENT_MB", "1")
    document = workspace_tmp_dir / "enorme.pptx"
    with zipfile.ZipFile(document, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("ppt/media/video.mp4", bytes(2 * 1024 * 1024))

    with pytest.raises(DocumentFormatError, match="trop volumineux"):
        load_document_units(document)
    assert resolve_document_metadata("enorme.pptx", str(document), {}) is None


def test_legacy_file_routes_support_non_pdf_documents(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import config
    from src.api import files

    document = workspace_tmp_dir / "readme.txt"
    document.write_text("Texte extrait par la route générique.", encoding="utf-8")
    monkeypatch.setattr(config, "DATA_PATH", str(workspace_tmp_dir))

    extracted = asyncio.run(files.api_extract_pdf_text("readme.txt"))
    opened = asyncio.run(files.api_open_pdf("readme.txt"))

    assert "Texte extrait" in extracted["text"]
    assert extracted["units"] == 1
    assert opened.media_type == "text/plain"
    assert "inline" in opened.headers["content-disposition"]


def test_document_discovery_includes_supported_formats_only(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src import config
    from src.rag import state

    (workspace_tmp_dir / "paper.PDF").write_bytes(b"pdf")
    (workspace_tmp_dir / "notes.txt").write_text("text", encoding="utf-8")
    (workspace_tmp_dir / "slides.pptx").write_bytes(b"zip")
    (workspace_tmp_dir / "ignored.html").write_text("html", encoding="utf-8")
    hidden = workspace_tmp_dir / ".hidden"
    hidden.mkdir()
    (hidden / "secret.docx").write_bytes(b"zip")
    monkeypatch.setattr(config, "DATA_PATH", str(workspace_tmp_dir))
    state.clear_pdf_scan_cache()

    paths = {rel.replace("\\", "/") for rel, _full in state.find_documents()}

    assert paths == {"paper.PDF", "notes.txt", "slides.pptx"}
    assert state.find_pdfs() == state.find_documents()


def test_standalone_rag_refresh_never_auto_detects_zotero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.rag import state, zotero_integration
    from src.zotero_db import ZoteroItem

    state._zotero_library = {"old.pdf": ZoteroItem(title="Old Zotero item")}
    monkeypatch.setattr(
        zotero_integration,
        "get_app_settings",
        lambda: {"metadata_source": "none", "zotero_path": ""},
    )
    monkeypatch.setattr(
        zotero_integration,
        "find_zotero_db",
        lambda: pytest.fail("Zotero auto-detection must not run in standalone mode"),
    )

    zotero_integration._refresh_zotero_library_cache()

    assert state._zotero_library == {}


def test_force_reload_without_metadata_source_clears_library_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import application, lifecycle
    from src.api_models import InitRequest

    cleared: list[bool] = []
    monkeypatch.setattr(application, "init_rag", lambda *_args, **_kwargs: (False, "stopped"))
    monkeypatch.setattr(
        lifecycle,
        "get_app_settings",
        lambda: {"metadata_source": "none", "zotero_path": ""},
    )
    monkeypatch.setattr(lifecycle, "clear_metadata_library", lambda: cleared.append(True))
    monkeypatch.setattr(
        lifecycle,
        "init_zotero",
        lambda _path: pytest.fail("init_zotero must not run in standalone mode"),
    )

    async def consume() -> str:
        response = await lifecycle.api_init(
            InitRequest(model="test", force_reload=True),
        )
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else str(chunk))
        return "".join(chunks)

    body = asyncio.run(consume())

    assert cleared == [True]
    assert json.loads(body.removeprefix("data: ").strip())["msg"] == "stopped"
