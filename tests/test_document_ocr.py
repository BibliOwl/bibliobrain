import io
import json
import threading
from pathlib import Path

import pytest

from src import document_ocr as ocr


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(ocr, "root", lambda: tmp_path / "ocr")


def test_quality_preserves_readable_text_even_with_figures():
    assert ocr.native_quality("Figure 1. Measured thermal conductivity along the axis. " * 12) == "native"
    assert ocr.native_quality("I | l | 1 " * 60) == "degraded"
    assert ocr.native_quality("Readable text " * 20 + "\ufffd" * 20) == "degraded"
    assert ocr.native_quality("(cid:12)" * 80) == "degraded"
    assert ocr.native_quality("") == "sparse"


def test_default_model_install_is_explicit_and_pinned():
    with pytest.raises(ocr.OCRInstallError, match="confirmation_required"):
        ocr.install_default_model(ocr.DEFAULT_MODEL, confirmed=False)
    with pytest.raises(ocr.OCRInstallError, match="model_not_allowed"):
        ocr.install_default_model("untrusted/model:latest", confirmed=True)
    assert ocr.DEFAULT_MODEL_SOURCE.endswith(":Q8_0")
    assert all(value.startswith("sha256:") for value in ocr._DEFAULT_MODEL_FILES.values())


def test_default_model_install_creates_alias_from_pinned_blobs(monkeypatch):
    from src import llm_provider

    calls = []
    monkeypatch.setattr(llm_provider, "_ollama_base_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(
        llm_provider,
        "_read_ollama_json",
        lambda *args, **kwargs: {"models": []},
    )
    monkeypatch.setattr(llm_provider, "_ollama_headers", lambda **kwargs: {})
    monkeypatch.setattr(llm_provider, "_ollama_httpx_verify", lambda: True)

    def fake_post(url, *, method, json_payload, headers, timeout, ssl_verify):
        calls.append((url.rsplit("/", 2)[-1], json_payload))
        return {"status": "success"}

    monkeypatch.setattr(llm_provider, "_open_url_json", fake_post)

    result = ocr.install_default_model(ocr.DEFAULT_MODEL, confirmed=True)

    assert result == {
        "ok": True,
        "model": ocr.DEFAULT_MODEL,
        "already_installed": False,
    }
    assert [name for name, _payload in calls] == ["pull", "create"]
    assert calls[1][1]["files"] == ocr._DEFAULT_MODEL_FILES


def test_atomic_cache_write_retries_transient_windows_sharing_lock(monkeypatch, tmp_path):
    replace = Path.replace
    attempts = []
    def flaky_replace(source, target):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError("Windows sharing violation")
        return replace(source, target)
    monkeypatch.setattr(Path, "replace", flaky_replace)
    monkeypatch.setattr(ocr.time, "sleep", lambda delay: None)
    target = tmp_path / "progress.json"
    ocr.write_json(target, {"completed": 225})
    assert ocr.read_json(target) == {"completed": 225}
    assert len(attempts) == 3
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("text,final,issue", [
    ("partiel", {"done": False}, "incomplete_stream"),
    ("partiel", {"done": True, "done_reason": "length"}, "truncated"),
    ("utile\n" + "1\n\n" * 200, {"done": True}, "repetition"),
    ('<img src="images/bbox_1_2_3_4.jpg" />', {"done": True}, "no_readable_text"),
    ("<table><tr><td>5</td></tr>", {"done": True}, "incomplete_table"),
    ("Conductivité : 12 W/m/K.", {"done": True, "done_reason": "stop"}, ""),
])
def test_output_is_not_accepted_without_clean_completion(text, final, issue):
    assert ocr.output_issue(text, final) == issue


def test_retry_and_cache_reuse_then_model_digest_invalidation(monkeypatch):
    calls = []
    monkeypatch.setattr(ocr, "render_page", lambda *a: (b"png", False))

    def request(png, model, penalty, stop):
        calls.append(penalty)
        return ("Texte extrait.", {"done": penalty == 1.15})

    monkeypatch.setattr(ocr, "request_transcription", request)
    args = ("source.pdf", "content-hash", 2, "ovis", "digest1", threading.Event())
    first = ocr.transcribe_page(*args)
    assert first["status"] == "ok"
    assert calls == [1.0, 1.15]
    assert ocr.transcribe_page(*args)["cache_hit"]
    assert len(calls) == 2
    changed = ocr.transcribe_page(*args[:4], "digest2", args[5])
    assert changed["key"] != first["key"]
    assert len(calls) == 4


def test_failures_remain_reviewable_and_do_not_retry_forever(monkeypatch):
    calls = []
    monkeypatch.setattr(ocr, "render_page", lambda *a: (b"png", False))
    def request(*args):
        calls.append(1)
        return "truncated source text", {"done": False}
    monkeypatch.setattr(ocr, "request_transcription", request)
    args = ("source.pdf", "hash", 0, "ovis", "digest", threading.Event())
    assert ocr.transcribe_page(*args)["status"] == "review"
    result = ocr.transcribe_page(*args)
    assert len(calls) == 2
    assert result["text"] == "truncated source text"
    assert len(result["attempts"]) == 2
    ocr.transcribe_page(*args, retry_failed=True)
    assert len(calls) == 4


def test_pause_does_not_save_an_incomplete_page(monkeypatch):
    monkeypatch.setattr(ocr, "render_page", lambda *a: (b"png", False))
    def request(*args):
        raise ocr.OCRPaused()
    monkeypatch.setattr(ocr, "request_transcription", request)
    with pytest.raises(ocr.OCRPaused):
        ocr.transcribe_page("x", "hash", 0, "ovis", "digest", threading.Event())
    assert not list(ocr.root().rglob("*.json"))


def test_blank_page_bypasses_model(monkeypatch):
    monkeypatch.setattr(ocr, "render_page", lambda *a: (b"png", True))
    def forbidden(*args):
        raise AssertionError("No inference on blank pages")
    monkeypatch.setattr(ocr, "request_transcription", forbidden)
    result = ocr.transcribe_page("x", "hash", 0, "ovis", "digest", threading.Event())
    assert result["status"] == "blank"


def test_improved_blank_filter_reclassifies_old_review_without_inference(monkeypatch):
    monkeypatch.setattr(ocr, "render_page", lambda *a: (b"png", False))
    monkeypatch.setattr(ocr, "request_transcription", lambda *a: ("1\n" * 100, {"done": True}))
    args = ("x", "hash", 0, "ovis", "digest", threading.Event())
    assert ocr.transcribe_page(*args)["status"] == "review"
    monkeypatch.setattr(ocr, "BLANK_DETECTOR", ocr.BLANK_DETECTOR + 1)
    monkeypatch.setattr(ocr, "render_page", lambda *a: (b"png", True))
    def forbidden(*a):
        raise AssertionError("No new inference")
    monkeypatch.setattr(ocr, "request_transcription", forbidden)
    result = ocr.transcribe_page(*args)
    assert result["status"] == "blank" and result["cache_hit"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [("local", "http://localhost:11434"), ("chat", "http://192.168.1.20:11434")],
)
def test_pages_go_to_the_chosen_ollama_whatever_answers_the_chat(monkeypatch, source, expected):
    from src import llm_provider

    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(llm_provider, "_ollama_base_url", lambda: "http://192.168.1.20:11434")
    monkeypatch.setattr(llm_provider, "_runtime_document_tools_source", source)
    asked = []

    def read_tags(base, path, **kwargs):
        asked.append(base)
        return {"models": [{"name": "ovis", "digest": "sha256:abc"}]}

    def open_url(url, **kwargs):
        asked.append(url)
        return io.BytesIO(json.dumps({"message": {"content": "ok"}, "done": True}).encode())

    monkeypatch.setattr(llm_provider, "_read_ollama_json", read_tags)
    monkeypatch.setattr(llm_provider, "_open_ollama_url", open_url)

    assert ocr.model_identity("ovis") == "sha256:abc"
    ocr.request_transcription(b"png", "ovis", 1, threading.Event())

    assert asked[0] == expected
    assert asked[1].startswith(expected + "/")


def test_stream_preserves_text_and_checks_pause(monkeypatch):
    from src import llm_provider
    events = [{"message": {"content": "Bonjour "}},
              {"message": {"content": "été"}, "done": True, "done_reason": "stop"}]
    monkeypatch.setattr(llm_provider, "_ollama_base_url", lambda: "http://localhost:11434")
    monkeypatch.setattr(llm_provider, "_open_ollama_url", lambda *a, **kw: io.BytesIO(
        b"\n".join(json.dumps(e).encode() for e in events)))
    text, final = ocr.request_transcription(b"png", "ovis", 1, threading.Event())
    assert text == "Bonjour été"
    assert final["done"]
    stop = threading.Event()
    stop.set()
    with pytest.raises(ocr.OCRPaused):
        ocr.request_transcription(b"png", "ovis", 1, stop)


def test_scan_distinguishes_blank_native_and_image_pdf(tmp_path):
    from PIL import Image, ImageDraw
    from pypdf import PdfWriter
    image = Image.new("RGB", (600, 800), "white")
    ImageDraw.Draw(image).text((70, 100), "A scanned article", fill="black")
    scan = tmp_path / "scan.pdf"
    image.save(scan, "PDF")
    result = ocr.scan_pdf(str(scan), threading.Event())
    assert result["pages"][0]["reason"] == "sparse"
    blank = tmp_path / "blank.pdf"
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    writer.write(blank)
    assert ocr.scan_pdf(str(blank), threading.Event())["pages"][0]["reason"] == "native"
    assert ocr.scan_pdf(str(scan), threading.Event()) == result
    png, is_blank = ocr.render_page(str(blank), 0)
    assert png.startswith(b"\x89PNG") and is_blank
    png, is_blank = ocr.render_page(str(scan), 0)
    assert png.startswith(b"\x89PNG") and not is_blank


def test_page_indexing_is_idempotent_and_preserves_other_pages(tmp_path):
    from langchain_core.documents import Document

    from src.rag import ocr_ingestion as job
    from src.rag.lexical_index import replace_paths, search

    docs = {
        "native0": Document(page_content="oldtext", metadata={"rel_path": "a.pdf", "page": 0, "content_type": "pdf_page"}),
        "native1": Document(page_content="untouched", metadata={"rel_path": "a.pdf", "page": 1, "content_type": "pdf_page"}),
        "note": Document(page_content="annotation", metadata={"rel_path": "a.pdf", "page": 0, "content_type": "zotero_note"}),
    }
    class VectorStore:
        calls = 0
        def get(self, **kw):
            return {"ids": [key for key, doc in docs.items() if doc.metadata["page"] == 0 and doc.metadata["content_type"] == "pdf_page"]}
        def add_documents(self, batch, ids):
            self.calls += 1
            docs.update(zip(ids, batch))
        def delete(self, ids):
            for key in ids:
                docs.pop(key)
    vector = VectorStore()
    replace_paths(str(tmp_path), ["a.pdf"], docs.values())
    result = {"text": "Recovered conductivity measurements. " * 20, "page": 0,
              "model": "ovis", "model_digest": "digest", "key": "cache1"}
    chunks = job.page_chunks(result, {"rel_path": "a.pdf", "source": "a.pdf", "article_id": "a"}, 300)
    count = job.index_page(vector, str(tmp_path), chunks)
    assert count > 0 and vector.calls == 1
    assert chunks[0].metadata["chunk_id"] == 1_000_000
    assert "native1" in docs and "note" in docs and "native0" not in docs
    assert job.index_page(vector, str(tmp_path), chunks) == 0
    assert vector.calls == 1
    assert search(str(tmp_path), "conductivity", limit=10)[0].metadata["page"] == 0
    assert search(str(tmp_path), "untouched", limit=10)
    assert search(str(tmp_path), "annotation", limit=10)
    assert not search(str(tmp_path), "oldtext", limit=10)


def test_failed_embedding_never_deletes_native_text(tmp_path):
    from src.rag import ocr_ingestion as job
    class BrokenVector:
        def get(self, **kw):
            return {"ids": ["native"]}
        def add_documents(self, *a, **kw):
            raise RuntimeError("embedding unavailable")
        def delete(self, **kw):
            raise AssertionError("Must preserve native text")
    result = {"text": "Recovered text", "page": 0, "model": "ovis", "model_digest": "digest", "key": "k"}
    chunks = job.page_chunks(result, {"rel_path": "a.pdf", "article_id": "a"}, 1000)
    with pytest.raises(RuntimeError, match="embedding unavailable"):
        job.index_page(BrokenVector(), str(tmp_path), chunks)
