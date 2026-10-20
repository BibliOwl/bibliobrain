from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import config, library_import, zotero_connector, zotero_drops, zotero_outbox
from src.api import library as routes
from src.http_security import RequestBodyLimitMiddleware
from src.rag import state
from src.zotero_connector import ZoteroSaveResult

PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


@pytest.fixture(autouse=True)
def never_the_real_zotero(monkeypatch):
    """No test may start, or depend on, the Zotero installed on this machine."""
    monkeypatch.setattr(
        zotero_connector,
        "_launch_zotero",
        lambda _executable: pytest.fail("a test tried to start the real Zotero"),
    )
    monkeypatch.setattr(library_import, "zotero_is_running", lambda: False)
    monkeypatch.setattr(library_import, "ensure_zotero_running", lambda: "not_found")
    monkeypatch.setattr(zotero_outbox, "zotero_is_running", lambda: False)
    monkeypatch.setattr(
        zotero_outbox,
        "save_pdf_to_zotero",
        lambda *_args, **_kwargs: pytest.fail("the outbox reached Zotero unexpectedly"),
    )
    monkeypatch.setattr(zotero_outbox, "_POLL_SECONDS", 0.02)
    monkeypatch.setattr(zotero_outbox, "_delivered", [])
    yield
    # Let the delivery thread end before the patches above are undone.
    for leftover in zotero_outbox._outbox().glob("*"):
        leftover.unlink(missing_ok=True)
    worker = zotero_outbox._worker
    if worker is not None:
        worker.join(timeout=5)
        assert not worker.is_alive()


def wait_until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


@pytest.fixture
def folder_library(workspace_tmp_dir, monkeypatch) -> Iterator[Path]:
    data_dir = workspace_tmp_dir / "data"
    data_dir.mkdir()
    monkeypatch.setattr(config, "DATA_PATH", str(data_dir))
    monkeypatch.setattr(config, "ZOTERO_DB_PATH", "")
    monkeypatch.setattr(config, "BIBLIOBRAIN_HOME", str(workspace_tmp_dir / "home"))
    monkeypatch.setattr(state, "_zotero_library", {})
    state.clear_document_scan_cache()
    yield data_dir
    state.clear_document_scan_cache()


@pytest.fixture
def zotero_library(folder_library, workspace_tmp_dir, monkeypatch) -> Path:
    zotero_root = workspace_tmp_dir / "zotero"
    storage = zotero_root / "storage"
    storage.mkdir(parents=True)
    (zotero_root / "zotero.sqlite").write_bytes(b"sqlite placeholder")
    monkeypatch.setattr(config, "DATA_PATH", str(storage))
    monkeypatch.setattr(config, "ZOTERO_DB_PATH", str(zotero_root / "zotero.sqlite"))
    state.clear_document_scan_cache()
    return storage


def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def upload(api: TestClient, filename: str, body: bytes):
    return api.post("/api/library/upload", params={"filename": filename}, content=body)


def test_dropped_pdf_lands_in_the_imports_folder_where_the_scan_finds_it(folder_library) -> None:
    response = upload(client(), "Étude énergie.pdf", PDF_BYTES)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "added",
        "filename": "Étude énergie.pdf",
        "rel_path": "imports/Étude énergie.pdf",
    }
    stored = folder_library / "imports" / "Étude énergie.pdf"
    assert stored.read_bytes() == PDF_BYTES
    assert [Path(rel).as_posix() for rel, _full in state.find_documents()] == [
        "imports/Étude énergie.pdf"
    ]
    assert list((folder_library / "imports").glob("*.part")) == []


def test_same_file_dropped_twice_is_reported_instead_of_stored_again(folder_library) -> None:
    api = client()
    upload(api, "paper.pdf", PDF_BYTES)

    response = upload(api, "renamed copy.pdf", PDF_BYTES)

    assert response.json()["status"] == "duplicate"
    assert response.json()["rel_path"] == "imports/paper.pdf"
    assert [path.name for path in (folder_library / "imports").iterdir()] == ["paper.pdf"]


def test_same_name_with_other_content_gets_a_numbered_name(folder_library) -> None:
    api = client()
    upload(api, "paper.pdf", PDF_BYTES)

    response = upload(api, "paper.pdf", PDF_BYTES + b"% second edition\n")

    assert response.json()["rel_path"] == "imports/paper_2.pdf"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("..\\..\\Windows\\evil.pdf", "evil.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\me\\a<b>:c?.PDF", "a_b__c_.pdf"),
        ("con.pdf", "_con.pdf"),
        (".pdf.pdf", "pdf.pdf"),
        ("x" * 300 + ".md", "x" * 120 + ".md"),
    ],
)
def test_client_file_names_are_reduced_to_a_bare_safe_name(filename, expected) -> None:
    assert library_import.safe_document_name(filename) == expected


@pytest.mark.parametrize("filename", ["script.exe", "archive.zip", "noextension", ".pdf", ""])
def test_unsupported_formats_are_refused_before_anything_is_written(
    folder_library, filename
) -> None:
    response = upload(client(), filename, PDF_BYTES)

    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_format"
    assert not (folder_library / "imports").exists()


@pytest.mark.parametrize(
    ("filename", "body", "status_code", "error"),
    [
        ("fake.pdf", b"MZ\x90\x00 not a pdf", 415, "content_mismatch"),
        ("fake.docx", b"plain text", 415, "content_mismatch"),
        ("empty.txt", b"", 400, "empty_file"),
    ],
)
def test_bad_content_is_refused_and_leaves_no_file(
    folder_library, filename, body, status_code, error
) -> None:
    response = upload(client(), filename, body)

    assert response.status_code == status_code
    assert response.json()["error"] == error
    assert list((folder_library / "imports").iterdir()) == []


def test_text_documents_are_accepted_whatever_their_encoding(folder_library) -> None:
    response = upload(client(), "notes.txt", "résumé".encode("utf-16"))

    assert response.json()["status"] == "added"


def test_file_over_the_upload_limit_is_refused_and_removed(folder_library, monkeypatch) -> None:
    monkeypatch.setattr(library_import, "max_upload_bytes", lambda: 16)

    response = upload(client(), "big.pdf", PDF_BYTES)

    assert response.status_code == 413
    assert response.json()["error"] == "file_too_large"
    assert list((folder_library / "imports").iterdir()) == []


def test_upload_route_alone_gets_the_larger_body_limit() -> None:
    app = FastAPI()

    @app.post("/api/library/upload")
    @app.post("/api/other")
    async def echo() -> dict:
        return {"ok": True}

    app.add_middleware(
        RequestBodyLimitMiddleware, max_bytes=8, path_limits={"/api/library/upload": 64}
    )
    api = TestClient(app)

    assert api.post("/api/library/upload", content=b"x" * 32).status_code == 200
    assert api.post("/api/library/upload", content=b"x" * 65).status_code == 413
    assert api.post("/api/other", content=b"x" * 32).status_code == 413


def test_upload_target_describes_a_folder_library(folder_library) -> None:
    target = client().get("/api/library/upload-target").json()

    assert target["target"] == "folder"
    assert ".pdf" in target["extensions"] and ".docx" in target["extensions"]
    assert target["max_bytes"] > 10 * 1024 * 1024


def test_zotero_library_sends_the_pdf_to_zotero_and_keeps_its_storage_untouched(
    zotero_library, workspace_tmp_dir, monkeypatch
) -> None:
    received: dict = {}

    def fake_save(pdf_path: Path, *, filename: str) -> ZoteroSaveResult:
        received["bytes"] = pdf_path.read_bytes()
        received["filename"] = filename
        received["staged_in"] = pdf_path.parent
        return ZoteroSaveResult(saved=True, recognized=True, title="A recognized paper")

    monkeypatch.setattr(library_import, "save_pdf_to_zotero", fake_save)
    api = client()

    assert api.get("/api/library/upload-target").json()["extensions"] == [".pdf"]
    response = upload(api, "paper.pdf", PDF_BYTES)

    assert response.json() == {
        "ok": True,
        "status": "zotero_added",
        "filename": "paper.pdf",
        "title": "A recognized paper",
    }
    assert received["bytes"] == PDF_BYTES
    assert received["filename"] == "paper.pdf"
    assert list(zotero_library.iterdir()) == []
    assert list(received["staged_in"].iterdir()) == []


def fingerprint(body: bytes) -> tuple[int, str]:
    return len(body), hashlib.sha256(body).hexdigest()


def test_pdf_zotero_could_not_recognize_is_remembered_for_the_index(
    zotero_library, monkeypatch
) -> None:
    replies = {
        "scan.pdf": ZoteroSaveResult(saved=True),
        "paper.pdf": ZoteroSaveResult(saved=True, recognized=True),
    }
    monkeypatch.setattr(
        library_import, "save_pdf_to_zotero", lambda _path, *, filename: replies[filename]
    )
    api = client()

    assert upload(api, "scan.pdf", PDF_BYTES).json()["status"] == "zotero_unrecognized"
    assert upload(api, "paper.pdf", PDF_BYTES + b"% paper\n").json()["status"] == "zotero_added"

    # Only the PDF left without a record: the other one has a parent item.
    assert zotero_drops.fingerprints() == {fingerprint(PDF_BYTES)}


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [("rejected", 502, "zotero_rejected"), ("not_configured", 503, "zotero_disabled")],
)
def test_zotero_library_explains_why_zotero_did_not_take_the_file(
    zotero_library, monkeypatch, error, status_code, code
) -> None:
    monkeypatch.setattr(
        library_import,
        "save_pdf_to_zotero",
        lambda _path, *, filename: ZoteroSaveResult(saved=False, error=error),
    )
    monkeypatch.setattr(
        library_import,
        "ensure_zotero_running",
        lambda: pytest.fail("only an unreachable Zotero is started"),
    )

    response = upload(client(), "paper.pdf", PDF_BYTES)

    assert response.status_code == status_code
    assert response.json()["error"] == code
    assert zotero_outbox.pending_count() == 0


@pytest.mark.parametrize("start_outcome", ["timeout", "not_found"])
def test_pdf_waits_for_a_zotero_that_does_not_answer_then_is_sent_without_a_new_drop(
    zotero_library, monkeypatch, start_outcome
) -> None:
    starts: list[str] = []
    monkeypatch.setattr(
        library_import,
        "save_pdf_to_zotero",
        lambda _path, *, filename: ZoteroSaveResult(saved=False, error="unreachable"),
    )

    def ensure_zotero_running():
        starts.append("start")
        return start_outcome

    monkeypatch.setattr(library_import, "ensure_zotero_running", ensure_zotero_running)
    api = client()

    first = upload(api, "Étude énergie.pdf", PDF_BYTES)
    # Zotero is known not to answer: no second 45 s wait, no second launch.
    second = upload(api, "scan.pdf", PDF_BYTES + b"% other\n")
    same_again = upload(api, "Étude énergie.pdf", PDF_BYTES)

    for response in (first, second, same_again):
        assert response.json() == {
            "ok": True,
            "status": "zotero_queued",
            "filename": response.json()["filename"],
        }
    assert starts == ["start"]
    collected = api.post("/api/library/zotero-outbox/collect").json()
    assert collected == {"ok": True, "pending": 2, "delivered": []}
    assert list(zotero_library.iterdir()) == [], "Zotero's storage is never written to"
    staging = Path(config.BIBLIOBRAIN_HOME) / "uploads"
    assert [path.name for path in staging.iterdir()] == ["zotero-outbox"]

    # The user answers Zotero's prompt: its connector comes up.
    sent: list[tuple[str, bytes]] = []

    def fake_save(pdf_path: Path, *, filename: str) -> ZoteroSaveResult:
        sent.append((filename, pdf_path.read_bytes()))
        return ZoteroSaveResult(saved=True, recognized=filename != "scan.pdf")

    monkeypatch.setattr(zotero_outbox, "save_pdf_to_zotero", fake_save)
    monkeypatch.setattr(zotero_outbox, "zotero_is_running", lambda: True)
    wait_until(lambda: zotero_outbox.pending_count() == 0)

    assert sent == [("Étude énergie.pdf", PDF_BYTES), ("scan.pdf", PDF_BYTES + b"% other\n")]
    collected = api.post("/api/library/zotero-outbox/collect").json()
    assert collected["pending"] == 0
    assert collected["delivered"] == [
        {"filename": "Étude énergie.pdf", "status": "zotero_added"},
        {"filename": "scan.pdf", "status": "zotero_unrecognized"},
    ]
    assert api.post("/api/library/zotero-outbox/collect").json()["delivered"] == []
    assert list((staging / "zotero-outbox").iterdir()) == []
    assert zotero_drops.fingerprints() == {fingerprint(PDF_BYTES + b"% other\n")}


def test_outbox_keeps_what_zotero_could_not_take(
    folder_library, workspace_tmp_dir, monkeypatch
) -> None:
    for name in ("a", "b", "c"):
        staged = workspace_tmp_dir / f"{name}.part"
        staged.write_bytes(PDF_BYTES + name.encode())
        zotero_outbox.enqueue(staged, f"{name}.pdf", sha256=name * 8)

    # Zotero takes one PDF, refuses the next, then goes away.
    replies = {
        "a.pdf": ZoteroSaveResult(saved=True, recognized=True),
        "b.pdf": ZoteroSaveResult(saved=False, error="rejected"),
        "c.pdf": ZoteroSaveResult(saved=False, error="unreachable"),
    }
    monkeypatch.setattr(
        zotero_outbox, "save_pdf_to_zotero", lambda _path, *, filename: replies[filename]
    )
    monkeypatch.setattr(zotero_outbox, "zotero_is_running", lambda: True)
    wait_until(lambda: zotero_outbox.pending_count() == 1)

    assert zotero_outbox.collect() == {
        "pending": 1,
        "delivered": [
            {"filename": "a.pdf", "status": "zotero_added"},
            {"filename": "b.pdf", "status": "zotero_rejected"},
        ],
    }
    assert [path.name for path in zotero_outbox._entries()] == ["cccccccc.json"]


def test_pdfs_left_by_a_previous_run_are_sent_when_the_server_starts(
    folder_library, monkeypatch
) -> None:
    outbox = zotero_outbox._outbox()
    outbox.mkdir(parents=True)
    (outbox / "abc.pdf").write_bytes(PDF_BYTES)
    (outbox / "abc.json").write_text(
        json.dumps({"filename": "resté.pdf", "order": 1}), encoding="utf-8"
    )
    sent: list[str] = []

    def fake_save(pdf_path: Path, *, filename: str) -> ZoteroSaveResult:
        sent.append(filename)
        return ZoteroSaveResult(saved=True, recognized=True)

    monkeypatch.setattr(zotero_outbox, "save_pdf_to_zotero", fake_save)
    monkeypatch.setattr(zotero_outbox, "zotero_is_running", lambda: True)
    assert zotero_outbox._worker is None

    zotero_outbox.resume()

    wait_until(lambda: zotero_outbox.pending_count() == 0)
    assert sent == ["resté.pdf"]
    assert zotero_outbox.collect()["delivered"] == [
        {"filename": "resté.pdf", "status": "zotero_added"}
    ]


def test_an_empty_outbox_starts_no_thread(folder_library) -> None:
    zotero_outbox.resume()

    assert zotero_outbox._worker is None


def test_closed_zotero_is_started_then_given_the_pdf(zotero_library, monkeypatch) -> None:
    events: list[str] = []

    def fake_save(pdf_path: Path, *, filename: str) -> ZoteroSaveResult:
        events.append("save")
        if "start" not in events:
            return ZoteroSaveResult(saved=False, error="unreachable")
        assert pdf_path.read_bytes() == PDF_BYTES
        return ZoteroSaveResult(saved=True, recognized=True, title="A recognized paper")

    monkeypatch.setattr(library_import, "save_pdf_to_zotero", fake_save)

    def ensure_zotero_running():
        events.append("start")
        return "started"

    monkeypatch.setattr(library_import, "ensure_zotero_running", ensure_zotero_running)
    monkeypatch.setattr(library_import, "zotero_is_running", lambda: False)
    api = client()

    assert api.get("/api/library/upload-target").json()["zotero_running"] is False
    response = upload(api, "paper.pdf", PDF_BYTES)

    assert response.json()["status"] == "zotero_added"
    assert events == ["save", "start", "save"]


def test_zotero_library_takes_pdfs_only(zotero_library, monkeypatch) -> None:
    monkeypatch.setattr(
        library_import,
        "save_pdf_to_zotero",
        lambda *_args, **_kwargs: pytest.fail("a non-PDF must not reach Zotero"),
    )

    response = upload(client(), "notes.docx", b"PK\x03\x04 docx")

    assert response.status_code == 415
    assert response.json()["error"] == "zotero_pdf_only"


class _FakeZotero(BaseHTTPRequestHandler):
    """The two connector endpoints, as Zotero 7+ implements them."""

    requests: list[dict] = []
    recognized_status = 200
    # Zotero's connector only answers once the application has loaded.
    loaded = True

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/connector/ping" and type(self).loaded:
            self._reply(200, {})
        else:
            self._reply(503, None)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if self.path == "/connector/saveStandaloneAttachment":
            self._reply(201, {"canRecognize": True})
        elif self.path == "/connector/getRecognizedItem":
            if type(self).recognized_status == 200:
                self._reply(200, {"title": "Recognized title", "itemType": "journalArticle"})
            else:
                self._reply(204, None)
        else:
            self._reply(404, None)

    def _reply(self, status: int, payload: dict | None) -> None:
        data = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def fake_zotero(monkeypatch):
    _FakeZotero.requests = []
    _FakeZotero.recognized_status = 200
    _FakeZotero.loaded = True
    server = HTTPServer(("127.0.0.1", 0), _FakeZotero)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "ZOTERO_CONNECTOR_URL", f"http://127.0.0.1:{server.server_port}/connector/saveItems"
    )
    yield _FakeZotero
    server.shutdown()
    server.server_close()


def test_connector_sends_the_pdf_then_waits_for_the_recognized_record(
    fake_zotero, workspace_tmp_dir
) -> None:
    pdf = workspace_tmp_dir / "staged.part"
    pdf.write_bytes(PDF_BYTES)

    result = zotero_connector.save_pdf_to_zotero(pdf, filename="Étude énergie.pdf")

    assert result == ZoteroSaveResult(saved=True, recognized=True, title="Recognized title")
    save, recognize = fake_zotero.requests
    assert save["path"] == "/connector/saveStandaloneAttachment"
    assert save["body"] == PDF_BYTES
    assert save["headers"]["Content-Type"] == "application/pdf"
    assert save["headers"]["Content-Length"] == str(len(PDF_BYTES))
    # A browser-like request is dropped by Zotero unless it carries a connector header.
    assert not save["headers"]["User-Agent"].startswith("Mozilla/")
    assert "Origin" not in save["headers"]
    metadata = json.loads(save["headers"]["X-Metadata"])
    assert metadata["title"] == "Étude énergie.pdf"
    assert metadata["url"] == "file:///%C3%89tude%20%C3%A9nergie.pdf"
    assert recognize["path"] == "/connector/getRecognizedItem"
    assert json.loads(recognize["body"]) == {"sessionID": metadata["sessionID"]}


def test_connector_reports_a_saved_but_unrecognized_pdf(fake_zotero, workspace_tmp_dir) -> None:
    fake_zotero.recognized_status = 204
    pdf = workspace_tmp_dir / "staged.part"
    pdf.write_bytes(PDF_BYTES)

    result = zotero_connector.save_pdf_to_zotero(pdf, filename="scan.pdf")

    assert result == ZoteroSaveResult(saved=True)


def test_connector_reports_zotero_closed(workspace_tmp_dir, monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_CONNECTOR_URL", "http://127.0.0.1:9/connector/saveItems")
    pdf = workspace_tmp_dir / "staged.part"
    pdf.write_bytes(PDF_BYTES)

    result = zotero_connector.save_pdf_to_zotero(pdf, filename="paper.pdf")

    assert result == ZoteroSaveResult(saved=False, error="unreachable")


def test_connector_can_be_disabled(workspace_tmp_dir, monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_CONNECTOR_URL", "")

    result = zotero_connector.save_pdf_to_zotero(workspace_tmp_dir / "x", filename="paper.pdf")

    assert result == ZoteroSaveResult(saved=False, error="not_configured")


def test_open_zotero_is_not_started_again(fake_zotero, monkeypatch) -> None:
    monkeypatch.setattr(
        zotero_connector,
        "find_zotero_executable",
        lambda: pytest.fail("an open Zotero needs no executable"),
    )

    assert zotero_connector.zotero_is_running() is True
    assert zotero_connector.ensure_zotero_running() == "running"


def test_closed_zotero_is_launched_once_and_awaited(fake_zotero, monkeypatch) -> None:
    fake_zotero.loaded = False
    launched: list[str] = []

    def fake_launch(executable: str) -> None:
        launched.append(executable)
        threading.Timer(0.3, lambda: setattr(fake_zotero, "loaded", True)).start()

    monkeypatch.setattr(zotero_connector, "find_zotero_executable", lambda: "zotero-test.exe")
    monkeypatch.setattr(zotero_connector, "_launch_zotero", fake_launch)

    assert zotero_connector.zotero_is_running() is False
    assert zotero_connector.ensure_zotero_running(wait_seconds=10) == "started"
    assert launched == ["zotero-test.exe"]


def test_zotero_that_did_not_come_up_is_launched_again_on_the_next_try(
    fake_zotero, monkeypatch
) -> None:
    fake_zotero.loaded = False
    launched: list[str] = []
    monkeypatch.setattr(zotero_connector, "find_zotero_executable", lambda: "zotero-test.exe")
    monkeypatch.setattr(zotero_connector, "_launch_zotero", launched.append)

    assert zotero_connector.ensure_zotero_running(wait_seconds=0.6) == "timeout"
    assert zotero_connector.ensure_zotero_running(wait_seconds=0.6) == "timeout"
    assert launched == ["zotero-test.exe", "zotero-test.exe"]


def test_zotero_that_is_not_installed_cannot_be_started(fake_zotero, monkeypatch) -> None:
    fake_zotero.loaded = False
    monkeypatch.setattr(zotero_connector, "find_zotero_executable", lambda: None)

    assert zotero_connector.ensure_zotero_running(wait_seconds=0.6) == "not_found"


def test_zotero_on_another_machine_is_never_launched_here(monkeypatch) -> None:
    monkeypatch.setenv("ZOTERO_CONNECTOR_URL", "http://192.0.2.10:23119/connector/saveItems")
    monkeypatch.setattr(zotero_connector, "zotero_is_running", lambda: False)
    monkeypatch.setattr(
        zotero_connector,
        "find_zotero_executable",
        lambda: pytest.fail("a remote connector must not start a local Zotero"),
    )

    assert zotero_connector.ensure_zotero_running(wait_seconds=0.6) == "not_found"


def test_zotero_executable_comes_from_the_path_first(monkeypatch) -> None:
    monkeypatch.setattr(zotero_connector.shutil, "which", lambda name: f"/opt/{name}/{name}")

    assert zotero_connector.find_zotero_executable() == "/opt/zotero/zotero"


def test_dropped_pdf_registry_survives_a_damaged_file(folder_library, workspace_tmp_dir) -> None:
    pdf = workspace_tmp_dir / "scan.pdf"
    pdf.write_bytes(PDF_BYTES)
    registry = Path(config.BIBLIOBRAIN_HOME) / "zotero-dropped-pdfs.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{not json", encoding="utf-8")

    assert zotero_drops.fingerprints() == frozenset()
    zotero_drops.remember(pdf, "scan.pdf", "abc")
    zotero_drops.remember(pdf, "scan again.pdf", "abc")

    assert zotero_drops.fingerprints() == {(len(PDF_BYTES), "abc")}
