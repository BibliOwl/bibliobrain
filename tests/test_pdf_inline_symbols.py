from __future__ import annotations

from pathlib import Path
from typing import Any

from src.pdf_inline_symbols import page_has_image_symbols


def _write_pdf(path: Path, pages: list[list[tuple[int, int, bool]]]) -> None:
    """Write a PDF whose pages hold image XObjects (width, height, is_mask)."""
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman >>")
    page_ids: list[int] = []
    pages_id = len(objects) + 1 + sum(1 + 1 + len(images) for images in pages)
    for images in pages:
        names = []
        drawing = b"BT /F1 10 Tf 72 720 Td (With a 1) Tj ET\n"
        for index, (width, height, is_mask) in enumerate(images, start=1):
            data = bytes(((width + 7) // 8) * height)
            kind = b"/ImageMask true" if is_mask else b"/ColorSpace /DeviceGray /BitsPerComponent 8"
            if not is_mask:
                data = bytes(width * height)
            image = add(
                b"<< /Type /XObject /Subtype /Image /Width %d /Height %d %s /Length %d >>\nstream\n"
                % (width, height, kind, len(data))
                + data
                + b"\nendstream"
            )
            names.append(b"/Im%d %d 0 R" % (index, image))
            drawing += b"q 5.4 0 0 6.4 120 720 cm /Im%d Do Q\n" % index
        content = add(b"<< /Length %d >>\nstream\n" % len(drawing) + drawing + b"endstream")
        page_ids.append(
            add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] /Contents %d 0 R "
                b"/Resources << /Font << /F1 %d 0 R >> /XObject << %s >> >> >>"
                % (pages_id, content, font, b" ".join(names))
            )
        )
    kids = b" ".join(b"%d 0 R" % page_id for page_id in page_ids)
    assert add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))) == pages_id
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        catalog,
        xref,
    )
    path.write_bytes(bytes(out))


def test_a_page_drawing_a_symbol_as_a_small_mask_is_detected(tmp_path: Path) -> None:
    pdf = tmp_path / "hahn.pdf"
    # Page 1: the µ of Hahn et al. 2011 (56x66 px mask). Page 2: a figure
    # (large mask) and a small grey picture. Page 3: no image at all. Page 4:
    # 1x1 px masks, which publishers use for rules and fills.
    _write_pdf(
        pdf,
        [[(56, 66, True)], [(900, 600, True), (40, 40, False)], [], [(1, 1, True), (1, 1, True)]],
    )

    assert page_has_image_symbols(pdf, 0) is True
    assert page_has_image_symbols(pdf, 1) is False
    assert page_has_image_symbols(pdf, 2) is False
    assert page_has_image_symbols(pdf, 3) is False
    assert page_has_image_symbols(pdf, 7) is False


def test_unreadable_or_missing_files_never_warn(tmp_path: Path) -> None:
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"%PDF-1.4 truncated")

    assert page_has_image_symbols(broken, 0) is False
    assert page_has_image_symbols(tmp_path / "missing.pdf", 0) is False


def test_cited_pages_with_symbol_images_are_named(tmp_path: Path, monkeypatch: Any) -> None:
    from langchain_core.documents import Document

    from src.rag import query_engine

    (tmp_path / "papers").mkdir()
    _write_pdf(tmp_path / "papers" / "hahn.pdf", [[], [(56, 66, True)]])
    _write_pdf(tmp_path / "papers" / "plain.pdf", [[]])
    monkeypatch.setattr(query_engine.config, "DATA_PATH", str(tmp_path))
    docs = [
        Document(
            page_content="With a 1 V/cm criterion",
            metadata={"rel_path": "papers/hahn.pdf", "page": 1, "zotero_title": "HTS pancake coils"},
        ),
        # The same page cited twice is named once.
        Document(page_content="Ic", metadata={"rel_path": "papers/hahn.pdf", "page": 1}),
        Document(page_content="text", metadata={"rel_path": "papers/hahn.pdf", "page": 0}),
        Document(page_content="text", metadata={"rel_path": "papers/plain.pdf", "page": 0}),
        Document(
            page_content="note",
            metadata={"rel_path": "papers/hahn.pdf", "page": "note", "content_type": "note"},
        ),
    ]

    assert query_engine._pages_with_image_symbols(docs) == "« HTS pancake coils », p. 2"
    assert query_engine._pages_with_image_symbols(docs[2:]) == ""
