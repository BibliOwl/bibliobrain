"""Exercise OCR orchestration without an embedding runtime or a running model."""

import importlib.util
import sys
import threading
import types
from pathlib import Path

import pytest

from src import document_ocr as ocr
from src import rag


@pytest.fixture
def job(monkeypatch, tmp_path):
    # The orchestration only needs these metadata helpers. Keep heavyweight
    # embedding imports out of these file-selection/cache regression tests.
    dependencies: dict[str, dict[str, object]] = {
        "state": {"_zotero_library": {}, "_state_lock": threading.Lock()},
        "document_loader": {"_article_id_from_item": lambda rel, item: rel},
        "zotero_integration": {"_build_item_metadata": lambda rel, full, item: {}},
    }
    for name, attributes in dependencies.items():
        module = types.ModuleType(f"src.rag.{name}")
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        monkeypatch.setattr(rag, name, module, raising=False)
    spec = importlib.util.spec_from_file_location(
        "src.rag._ocr_selection_test", Path(__file__).parents[1] / "src/rag/ocr_ingestion.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(ocr, "root", lambda: tmp_path / "ocr-cache")
    return module


@pytest.mark.parametrize("retry_failed", [False, True])
def test_mixed_library_only_scans_pdf_and_preserves_real_pdf_errors(
    job, monkeypatch, tmp_path, retry_failed
):
    documents = []
    for name in ("paper.pdf", "SCAN.PDF", "broken.pdf", "paper.txt", "notes.docx", "table.csv"):
        full = tmp_path / name
        full.write_text("fixture", encoding="utf-8")
        documents.append((name, str(full)))
    scanned, transcribed = [], []

    class PdfStreamError(Exception):
        pass

    def scan(full, stop):
        scanned.append(Path(full).name)
        if Path(full).name == "broken.pdf":
            raise PdfStreamError()
        return {"fingerprint": full, "pages": [{"page": 0, "reason": "sparse"}]}

    def transcribe(full, fingerprint, page, model, digest, stop, *, retry_failed):
        transcribed.append((Path(full).name, retry_failed))
        return {"status": "blank", "cache_hit": True, "attempts": []}

    monkeypatch.setattr(ocr, "scan_pdf", scan)
    monkeypatch.setattr(ocr, "model_identity", lambda model: "fixture-digest")
    monkeypatch.setattr(ocr, "transcribe_page", transcribe)
    vectorstore = types.SimpleNamespace(get=lambda **kwargs: {"metadatas": [{}]})
    report = job.enrich(
        vectorstore,
        str(tmp_path),
        documents,
        chunk_size=500,
        model="fixture",
        stop=threading.Event(),
        retry_failed=retry_failed,
    )
    assert scanned == ["paper.pdf", "SCAN.PDF", "broken.pdf"]
    assert transcribed == [("paper.pdf", retry_failed), ("SCAN.PDF", retry_failed)]
    assert report["documents_total"] == report["documents_scanned"] == 3
    assert report["candidates"] == report["completed"] == report["cache_hits"] == 2
    assert report["status"] == "completed_with_review"
    assert report["errors"] == [{"file": "broken.pdf", "page": None, "issue": "PdfStreamError"}]


def test_status_excludes_legacy_non_pdf_failures_without_changing_saved_report(job):
    errors = [
        {"file": "ABC\\paper.txt", "page": None, "issue": "PdfStreamError"},
        {"file": "notes.DOCX", "page": None, "issue": "PdfReadError"},
        {"file": "scan.PDF", "page": None, "issue": "PdfStreamError"},
        {"file": "paper.pdf", "page": 2, "issue": "truncated"},
    ]
    saved = {"status": "paused", "errors": errors, "completed": 12, "review": 1}
    path = ocr.root() / "progress.json"
    ocr.write_json(path, saved)
    result = job.status()
    assert result["errors"] == errors[2:]
    assert result["ignored_non_pdf_errors"] == 2
    assert result["completed"] == 12
    assert result["status"] == "paused"
    assert ocr.read_json(path) == saved


def test_completed_job_with_only_non_pdf_failures_no_longer_needs_review(job):
    ocr.write_json(
        ocr.root() / "progress.json",
        {
            "status": "completed_with_review",
            "review": 0,
            "errors": [{"file": "article.txt", "page": None, "issue": "PdfStreamError"}],
        },
    )
    result = job.status()
    assert result["status"] == "completed"
    assert result["errors"] == []
    assert result["ignored_non_pdf_errors"] == 1


class _OcrIndex:
    """Vector store fake: remembers which OCR pages were indexed per PDF."""

    def __init__(self):
        self.pages: dict[str, set[int]] = {}

    def get(self, where=None, **kwargs):
        if where == {"extraction_method": "ocr"}:
            return {
                "metadatas": [
                    {"rel_path": rel_path, "page": page, "extraction_method": "ocr"}
                    for rel_path, pages in self.pages.items()
                    for page in pages
                ]
            }
        return {"metadatas": [{}]}


@pytest.fixture
def library(job, monkeypatch, tmp_path):
    documents = []
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        full = tmp_path / name
        full.write_text(name, encoding="utf-8")
        documents.append((name, str(full)))
    calls: dict[str, list] = {"scan": [], "transcribe": []}
    index = _OcrIndex()

    def scan(full, stop):
        calls["scan"].append(Path(full).name)
        # a.pdf: two scanned pages; b.pdf: one unreadable page; c.pdf: native.
        pages = {"a.pdf": [0, 1], "b.pdf": [0], "c.pdf": []}[Path(full).name]
        return {
            "fingerprint": full,
            "pages": [{"page": page, "reason": "sparse"} for page in pages]
            + [{"page": 9, "reason": "native"}],
        }

    def transcribe(full, fingerprint, page, model, digest, stop, *, retry_failed):
        name = Path(full).name
        calls["transcribe"].append((name, page))
        if name == "b.pdf":
            return {"status": "review", "cache_hit": True, "key": "k", "attempts": [{"issue": "truncated"}]}
        return {"status": "ok", "cache_hit": True, "page": page, "key": f"{name}{page}", "attempts": []}

    def index_page(vectorstore, index_path, chunks):
        rel_path, page = chunks[0]
        vectorstore.pages.setdefault(rel_path, set()).add(page)
        return 1

    monkeypatch.setattr(ocr, "scan_pdf", scan)
    monkeypatch.setattr(ocr, "model_identity", lambda model: "digest-1")
    monkeypatch.setattr(ocr, "transcribe_page", transcribe)
    monkeypatch.setattr(job, "page_chunks", lambda result, metadata, size: [(metadata["rel_path"], result["page"])])
    monkeypatch.setattr(job, "index_page", index_page)
    monkeypatch.setattr(job.state, "find_documents", lambda: documents, raising=False)

    def run(**kwargs):
        calls["scan"].clear()
        calls["transcribe"].clear()
        report = job.enrich(
            index, str(tmp_path / "index"), documents,
            chunk_size=500, model="fixture", stop=threading.Event(), **kwargs,
        )
        ocr.write_json(ocr.root() / "progress.json", report)
        return report

    return types.SimpleNamespace(run=run, calls=calls, index=index, documents=documents, tmp_path=tmp_path)


def _counts(report):
    keys = ("pages_total", "candidates", "completed", "indexed_pages", "review", "errors", "status")
    return {key: report[key] for key in keys}


def test_second_pass_skips_unchanged_pdfs_and_reports_the_same_totals(library):
    first = library.run()
    assert library.calls["scan"] == ["a.pdf", "b.pdf", "c.pdf"]
    second = library.run()
    assert library.calls == {"scan": [], "transcribe": []}
    assert second["skipped_documents"] == 3
    assert _counts(second) == _counts(first)
    assert second["review"] == 1 and second["errors"][0]["cache_key"] == "k"


def test_pdf_whose_ocr_pages_left_the_index_is_reprocessed(library):
    library.run()
    # A reload of the document deletes all its chunks, OCR pages included.
    library.index.pages.pop("a.pdf")
    library.run()
    assert library.calls["scan"] == ["a.pdf"]
    assert library.calls["transcribe"] == [("a.pdf", 0), ("a.pdf", 1)]
    assert library.index.pages["a.pdf"] == {0, 1}


def test_modified_pdf_and_new_model_are_reprocessed(library, monkeypatch):
    library.run()
    path = library.tmp_path / "c.pdf"
    path.write_text("c.pdf, edited", encoding="utf-8")
    library.run()
    assert library.calls["scan"] == ["c.pdf"]
    monkeypatch.setattr(ocr, "model_identity", lambda model: "digest-2")
    library.run()
    assert library.calls["scan"] == ["a.pdf", "b.pdf", "c.pdf"]


def test_retry_failed_reopens_only_pdfs_with_failures(library):
    library.run()
    library.run(retry_failed=True)
    assert library.calls["scan"] == ["b.pdf"]
    assert library.calls["transcribe"] == [("b.pdf", 0)]


def test_automatic_pass_runs_only_when_something_is_left(job, library):
    index_path = str(library.tmp_path / "index")
    assert job.needs_pass(library.index, index_path, 500, "fixture")  # never run
    library.run()
    assert not job.needs_pass(library.index, index_path, 500, "fixture")
    assert job.needs_pass(library.index, index_path, 500, "other-model")
    assert job.needs_pass(library.index, str(library.tmp_path / "other-index"), 500, "fixture")
    library.index.pages.pop("a.pdf")
    assert job.needs_pass(library.index, index_path, 500, "fixture")
    library.run()
    report = ocr.read_json(ocr.root() / "progress.json")
    ocr.write_json(ocr.root() / "progress.json", {**report, "status": "paused"})
    assert job.needs_pass(library.index, index_path, 500, "fixture")
