"""A page pypdf refuses costs that page only, never the whole document."""

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PageObject, PdfWriter
from pypdf.errors import LimitReachedError

from src.document_formats import PypdfLoader, load_document_units


def _three_page_pdf(path: Path) -> None:
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)


@pytest.fixture
def page_two_over_limit(monkeypatch: pytest.MonkeyPatch):
    """Page 2 trips pypdf's per-stream token budget; the others read 'texte N'."""

    def extract_text(self, *args, **kwargs):
        number = self.page_number
        if number == 1:
            raise LimitReachedError("Reached maximum number of tokens per stream.")
        return f"texte {number + 1}"

    monkeypatch.setattr(PageObject, "extract_text", extract_text)


@pytest.mark.usefixtures("page_two_over_limit")
def test_isolation_loader_skips_only_the_refused_page(workspace_tmp_dir: Path) -> None:
    document = workspace_tmp_dir / "figure-lourde.pdf"
    _three_page_pdf(document)

    units = load_document_units(document, pdf_loader_class=PypdfLoader, isolated=False)

    assert [unit.text for unit in units] == ["texte 1", "texte 3"]
    assert [unit.page for unit in units] == [0, 2]


@pytest.mark.usefixtures("page_two_over_limit")
def test_failing_loader_falls_back_to_page_by_page_reading(workspace_tmp_dir: Path) -> None:
    document = workspace_tmp_dir / "figure-lourde.pdf"
    _three_page_pdf(document)

    class WholeFileLoader:
        """Like langchain's PyPDFLoader: the first refused page aborts the file."""

        def __init__(self, path: str) -> None:
            self.path = path

        def load(self):
            from pypdf import PdfReader

            return [page.extract_text() for page in PdfReader(self.path).pages]

    units = load_document_units(document, pdf_loader_class=WholeFileLoader, isolated=False)

    assert [unit.text for unit in units] == ["texte 1", "texte 3"]


def test_non_pypdf_errors_are_not_hidden(workspace_tmp_dir: Path) -> None:
    document = workspace_tmp_dir / "ok.pdf"
    _three_page_pdf(document)

    class BrokenLoader:
        def __init__(self, path: str) -> None:
            pass

        def load(self):
            raise RuntimeError("bug in the loader")

    with pytest.raises(RuntimeError, match="bug in the loader"):
        load_document_units(document, pdf_loader_class=BrokenLoader, isolated=False)
