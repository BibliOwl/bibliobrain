"""Admission checks and child-process isolation for document parsing."""

from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

import pytest

import src.document_isolation as isolation
from src.document_formats import (
    DocumentFormatError,
    extract_units_in_process,
    load_document_units,
)
from src.document_guard import DocumentRejected, check_document, content_matches

_DOCX_BODY = (
    '<w:document xmlns:w="urn:w"><w:body>'
    "<w:p><w:r><w:t>Le protocole emploie un capteur optique.</w:t></w:r></w:p>"
    "</w:body></w:document>"
)


def _docx(path: Path, body: str = _DOCX_BODY) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", body)
    return path


def _pdf(path: Path, text: str = "Resultat experimental important") -> Path:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    body = b"%PDF-1.4\n"
    offsets = []
    for number, content in enumerate(objects, start=1):
        offsets.append(len(body))
        body += b"%d 0 obj\n" % number + content + b"\nendobj\n"
    xref = len(body)
    body += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        body += b"%010d 00000 n \n" % offset
    body += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path.write_bytes(body)
    return path


@pytest.fixture
def isolated_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_DOCUMENT_ISOLATION", "1")


# --- admission checks -------------------------------------------------------


def test_signature_must_match_extension() -> None:
    assert content_matches(b"junk\n%PDF-1.7", ".pdf")
    assert not content_matches(b"<html>", ".pdf")
    assert content_matches(b"PK\x03\x04rest", ".docx")
    assert not content_matches(b"%PDF-1.7", ".docx")
    assert not content_matches(b"PK\x03\x04", ".md")
    assert not content_matches(b"%PDF-1.7", ".txt")
    assert content_matches(b"MZ report, first quarter", ".txt")
    assert not content_matches(b"MZ\x90\x00PE\x00\x00", ".txt")
    assert content_matches(b"", ".txt")


def test_oversized_document_is_rejected(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BIBLIOBRAIN_MAX_DOCUMENT_MB", "1")
    big = workspace_tmp_dir / "big.pdf"
    big.write_bytes(b"%PDF-1.4\n" + b"0" * (2 * 1024 * 1024))

    with pytest.raises(DocumentRejected, match="trop volumineux"):
        check_document(big)
    with pytest.raises(DocumentFormatError, match="trop volumineux"):
        load_document_units(big)


def test_disguised_files_never_reach_a_parser(workspace_tmp_dir: Path) -> None:
    fake_docx = workspace_tmp_dir / "fake.docx"
    fake_docx.write_bytes(b"MZ\x90\x00 not an archive")
    fake_pdf = workspace_tmp_dir / "fake.pdf"
    fake_pdf.write_bytes(b"<html>not a pdf</html>")
    pdf_as_text = workspace_tmp_dir / "notes.txt"
    pdf_as_text.write_bytes(b"%PDF-1.4 hidden")

    for path in (fake_docx, fake_pdf, pdf_as_text):
        with pytest.raises(DocumentFormatError, match="ne correspond pas"):
            load_document_units(path)


def test_directories_and_missing_files_are_rejected(workspace_tmp_dir: Path) -> None:
    folder = workspace_tmp_dir / "folder.pdf"
    folder.mkdir()
    with pytest.raises(DocumentRejected):
        check_document(folder)
    with pytest.raises(DocumentRejected):
        check_document(workspace_tmp_dir / "missing.pdf")


def test_entity_declarations_are_refused(workspace_tmp_dir: Path) -> None:
    bomb = _docx(
        workspace_tmp_dir / "bomb.docx",
        '<!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
        '<w:document xmlns:w="urn:w"><w:body><w:p><w:r><w:t>&b;</w:t></w:r></w:p>'
        "</w:body></w:document>",
    )
    with pytest.raises(DocumentFormatError, match="entite"):
        load_document_units(bomb, isolated=False)


def test_archive_with_too_many_members_is_refused(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.document_formats as formats

    monkeypatch.setattr(formats, "_MAX_ARCHIVE_MEMBERS", 3)
    path = workspace_tmp_dir / "many.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", _DOCX_BODY)
        for index in range(5):
            archive.writestr(f"extra/{index}.xml", "<a/>")
    with pytest.raises(DocumentFormatError, match="trop de composants"):
        load_document_units(path, isolated=False)


# --- child process ----------------------------------------------------------


@pytest.mark.usefixtures("isolated_parsing")
def test_isolated_parse_matches_in_process_parse(workspace_tmp_dir: Path) -> None:
    docx = _docx(workspace_tmp_dir / "article.docx")
    notes = workspace_tmp_dir / "notes.md"
    notes.write_text("# Titre\n\nUn résultat accentué : é, ü, 漢字.", encoding="utf-8")
    pdf = _pdf(workspace_tmp_dir / "article.pdf")

    for path in (docx, notes, pdf):
        assert load_document_units(path) == extract_units_in_process(path)
    assert "Resultat experimental" in load_document_units(pdf)[0].text


@pytest.mark.usefixtures("isolated_parsing")
def test_isolated_parse_honours_the_unit_cap(workspace_tmp_dir: Path) -> None:
    body = "".join(
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Section {n}</w:t></w:r></w:p>'
        f"<w:p><w:r><w:t>Contenu {n}</w:t></w:r></w:p>"
        for n in range(4)
    )
    docx = _docx(
        workspace_tmp_dir / "long.docx",
        f'<w:document xmlns:w="urn:w"><w:body>{body}</w:body></w:document>',
    )
    assert len(load_document_units(docx)) == 4
    assert len(load_document_units(docx, max_units=2)) == 2


@pytest.mark.usefixtures("isolated_parsing")
def test_isolated_parse_reports_a_corrupt_archive(workspace_tmp_dir: Path) -> None:
    broken = workspace_tmp_dir / "broken.docx"
    broken.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    with pytest.raises(DocumentFormatError):
        load_document_units(broken)


def _worker_script(directory: Path, source: str) -> Path:
    script = directory / "worker.py"
    script.write_text(source, encoding="utf-8")
    return script


@pytest.mark.usefixtures("isolated_parsing")
def test_hanging_parser_is_killed_at_the_deadline(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = workspace_tmp_dir / "alive.txt"
    script = _worker_script(
        workspace_tmp_dir,
        "import sys, time, pathlib\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "while True:\n"
        "    marker.write_text(str(time.time()))\n"
        "    time.sleep(0.2)\n",
    )
    monkeypatch.setattr(isolation, "_WORKER", script)
    monkeypatch.setenv("BIBLIOBRAIN_PARSER_TIMEOUT_SECONDS", "2")
    docx = _docx(workspace_tmp_dir / "slow.docx")

    started = time.monotonic()
    with pytest.raises(DocumentFormatError, match="interrompue"):
        load_document_units(docx)
    assert time.monotonic() - started < 20

    time.sleep(1.0)
    stamp = marker.read_text()
    time.sleep(1.0)
    assert marker.read_text() == stamp, "the child kept running after the timeout"


@pytest.mark.skipif(sys.platform != "win32", reason="memory cap is a Windows job object here")
@pytest.mark.usefixtures("isolated_parsing")
def test_runaway_memory_use_is_contained(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _worker_script(
        workspace_tmp_dir,
        "blocks = []\nwhile True:\n    blocks.append(bytearray(64 * 1024 * 1024))\n",
    )
    monkeypatch.setattr(isolation, "_WORKER", script)
    monkeypatch.setenv("BIBLIOBRAIN_PARSER_MEMORY_MB", "512")
    docx = _docx(workspace_tmp_dir / "greedy.docx")

    with pytest.raises(DocumentFormatError):
        load_document_units(docx)


@pytest.mark.usefixtures("isolated_parsing")
def test_a_worker_that_prints_garbage_is_an_error_not_a_crash(
    workspace_tmp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _worker_script(workspace_tmp_dir, "print('not json')\n")
    monkeypatch.setattr(isolation, "_WORKER", script)
    docx = _docx(workspace_tmp_dir / "odd.docx")

    with pytest.raises(DocumentFormatError, match="interrompue"):
        load_document_units(docx)


def test_isolation_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("0", "false", "off", "no"):
        monkeypatch.setenv("BIBLIOBRAIN_DOCUMENT_ISOLATION", value)
        assert not isolation.isolation_enabled()
    monkeypatch.delenv("BIBLIOBRAIN_DOCUMENT_ISOLATION")
    assert isolation.isolation_enabled()
