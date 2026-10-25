"""Smoke test on real PDF bytes, read by the real parser.

Every file handed to the indexer must end up in exactly one place: indexed,
read without text (a scan), or reported as unreadable with a reason. A
dependency bump that breaks the reading of a kind of file then fails here
instead of making the file silently vanish from the library.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

from src import config


def _pdf(content: bytes) -> bytes:
    """A one-page PDF whose page draws ``content`` with Helvetica."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def _text_pdf(text: str) -> bytes:
    return _pdf(b"BT /F1 12 Tf 72 720 Td (" + text.encode("latin-1") + b") Tj ET")


def _write_corpus(folder: Path) -> list[tuple[str, str]]:
    folder.mkdir(parents=True, exist_ok=True)
    files: dict[str, bytes] = {
        "text.pdf": _text_pdf("Thermal losses in induction motors " * 20),
        # A scan: a page without any text operator.
        "scan.pdf": _pdf(b"0 0 m 100 100 l S"),
        "corrupt.pdf": b"%PDF-1.4\nthis is not a pdf at all",
        "empty.pdf": b"",
        # One TJ array longer than pypdf's per-array token limit.
        "heavy.pdf": _pdf(b"BT /F1 12 Tf 72 720 Td [" + b"(a) " * 12_000 + b"] TJ ET"),
    }
    for name, data in files.items():
        (folder / name).write_bytes(data)

    reader = PdfReader(str(folder / "text.pdf"))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.encrypt(user_password="secret", owner_password="owner")
    with open(folder / "encrypted.pdf", "wb") as handle:
        writer.write(handle)

    return [(name, str(folder / name)) for name in sorted([*files, "encrypted.pdf"])]


def test_every_file_is_indexed_textless_or_reported(tmp_path: Path, monkeypatch: Any) -> None:
    config.DATA_PATH = str(tmp_path / "data")
    config.STORAGE_DIR = str(tmp_path / "storage")
    config.CHROMA_PATH = str(tmp_path / "chroma")
    document_loader: Any = importlib.reload(importlib.import_module("src.rag.document_loader"))
    document_loader.state._vector_load_pause_event.clear()
    monkeypatch.setattr(document_loader, "resolve_pdf_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(document_loader, "_build_note_documents", lambda *_args: [])
    monkeypatch.setattr(
        document_loader, "visual_analysis_model_settings", lambda: ("off", "", "")
    )

    pdf_list = _write_corpus(tmp_path / "storage")
    textless: set[str] = set()
    failed: dict[str, str] = {}

    _notes, chunks, _pages = document_loader._build_chunks_from_pdfs(
        pdf_list=pdf_list,
        chunk_size=500,
        progress=lambda *_args, **_kwargs: None,
        progress_start=0.0,
        progress_end=1.0,
        textless_paths=textless,
        failed_paths=failed,
    )

    indexed = {str(chunk.metadata.get("rel_path")) for chunk in chunks}
    for rel_path, _full_path in pdf_list:
        outcomes = [rel_path in indexed, rel_path in textless, rel_path in failed]
        assert outcomes.count(True) == 1, (rel_path, outcomes, failed)
    assert "text.pdf" in indexed
    assert all(reason for reason in failed.values())
    for unreadable in ("corrupt.pdf", "empty.pdf"):
        assert unreadable in failed or unreadable in textless
