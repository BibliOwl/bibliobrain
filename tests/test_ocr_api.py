import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import ocr as routes
from src.rag import ocr_ingestion as job


def client():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def test_interrupted_job_and_settings_survive_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(routes.document_ocr, "root", lambda: tmp_path)
    monkeypatch.setattr(job, "_thread", None)
    routes.document_ocr.write_json(tmp_path / "progress.json", {"status": "running", "completed": 12})
    api = client()
    assert api.get("/api/ocr/status").json()["status"] == "interrupted"
    assert api.get("/api/ocr/status").json()["completed"] == 12
    assert api.put("/api/ocr/settings", json={"enabled": True, "model": "ovis-local"}).status_code == 200
    assert api.get("/api/ocr/status").json()["settings"] == {"enabled": True, "model": "ovis-local"}


def test_active_job_rejects_settings_changes_and_accepts_pause(monkeypatch, tmp_path):
    monkeypatch.setattr(routes.document_ocr, "root", lambda: tmp_path)
    class Active:
        def is_alive(self):
            return True
    monkeypatch.setattr(job, "_thread", Active())
    stop = threading.Event()
    monkeypatch.setattr(job, "_stop", stop)
    api = client()
    assert api.put("/api/ocr/settings", json={"enabled": False, "model": "ovis"}).status_code == 409
    assert api.post("/api/ocr/pause").json()["ok"]
    assert stop.is_set()


def test_start_requires_loaded_index_and_does_not_hold_lock(monkeypatch, tmp_path):
    from src.api import application
    monkeypatch.setattr(routes.document_ocr, "root", lambda: tmp_path)
    routes.document_ocr.save_settings(enabled=True, model="ovis")
    monkeypatch.setattr(job, "_thread", None)
    monkeypatch.setattr(job.state, "vectorstore", None)
    lock = threading.Lock()
    monkeypatch.setattr(application, "_rag_init_lock", lock)
    response = client().post("/api/ocr/start")
    assert response.status_code == 409
    assert not lock.locked()


def test_automatic_start_after_a_load_skips_an_up_to_date_ocr(monkeypatch, tmp_path):
    # Every RAG load used to restart a full pass that held the index lock for
    # minutes even with nothing new, refusing "Update RAG" with a 409.
    from src.api import application
    monkeypatch.setattr(routes.document_ocr, "root", lambda: tmp_path)
    routes.document_ocr.save_settings(enabled=True, model="ovis")
    monkeypatch.setattr(job, "_thread", None)
    monkeypatch.setattr(job.state, "vectorstore", object())
    monkeypatch.setattr(job.state, "runtime_db_path", str(tmp_path / "index"))
    monkeypatch.setattr(job.state, "runtime_chunk_size", 1000)
    monkeypatch.setattr(job, "needs_pass", lambda *args: False)
    lock = threading.Lock()
    monkeypatch.setattr(application, "_rag_init_lock", lock)
    assert job.start(only_if_needed=True)["skipped"]
    assert not lock.locked() and job._thread is None


def test_readiness_is_independent_of_status_poll_and_accepts_unsaved_model(monkeypatch, tmp_path):
    from src import ocr_readiness
    monkeypatch.setattr(routes.document_ocr, "root", lambda: tmp_path)
    seen = []
    def inspect(model):
        seen.append(model)
        return {"model": model, "level": "unknown", "passive": True}
    monkeypatch.setattr(ocr_readiness, "readiness", inspect)
    api = client()
    assert api.get("/api/ocr/status").status_code == 200
    assert not seen
    assert api.get("/api/ocr/readiness?model=my-ocr").json()["model"] == "my-ocr"
    assert seen == ["my-ocr"]
    assert api.get("/api/ocr/status").json()["settings"]["enabled"] is False


def test_ocr_install_requires_explicit_confirmation(monkeypatch):
    def reject(model, *, confirmed):
        assert model == routes.document_ocr.DEFAULT_MODEL
        assert confirmed is False
        raise routes.document_ocr.OCRInstallError("confirmation_required")

    monkeypatch.setattr(routes.document_ocr, "install_default_model", reject)
    response = client().post(
        "/api/ocr/install",
        json={"model": routes.document_ocr.DEFAULT_MODEL, "confirmed": False},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "confirmation_required"


def test_ocr_install_returns_installed_model(monkeypatch):
    monkeypatch.setattr(
        routes.document_ocr,
        "install_default_model",
        lambda model, *, confirmed: {"ok": confirmed, "model": model},
    )
    response = client().post(
        "/api/ocr/install",
        json={"model": routes.document_ocr.DEFAULT_MODEL, "confirmed": True},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "model": routes.document_ocr.DEFAULT_MODEL}


def test_start_failures_carry_codes_for_the_settings_card(monkeypatch, tmp_path):
    from src.api import application
    monkeypatch.setattr(routes.document_ocr, "root", lambda: tmp_path)
    monkeypatch.setattr(job, "_thread", None)
    api = client()
    routes.document_ocr.save_settings(enabled=False, model="ovis")
    assert api.post("/api/ocr/start").json()["code"] == "disabled"
    routes.document_ocr.save_settings(enabled=True, model="ovis")
    monkeypatch.setattr(job.state, "vectorstore", None)
    monkeypatch.setattr(application, "_rag_init_lock", threading.Lock())
    assert api.post("/api/ocr/start").json()["code"] == "library_not_loaded"
    busy = threading.Lock()
    busy.acquire()
    monkeypatch.setattr(application, "_rag_init_lock", busy)
    assert api.post("/api/ocr/start").json()["code"] == "index_busy"


def test_inventory_counts_scanned_pages_and_reuses_unchanged_files(monkeypatch, tmp_path):
    monkeypatch.setattr(routes.document_ocr, "root", lambda: tmp_path)
    library = tmp_path / "library"
    library.mkdir()
    pdfs = []
    for name in ("scan.pdf", "native.pdf", "broken.pdf", "notes.txt"):
        (library / name).write_bytes(b"%PDF")
        pdfs.append((name, str(library / name)))
    pages = {
        "scan.pdf": [{"reason": "sparse"}, {"reason": "degraded"}, {"reason": "native"}],
        "native.pdf": [{"reason": "native"}],
    }
    opened = []

    def scan_pdf(path, stop):
        name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        opened.append(name)
        if name == "broken.pdf":
            raise ValueError("unreadable")
        return {"pages": pages[name]}

    monkeypatch.setattr(job.ocr, "scan_pdf", scan_pdf)
    report = job.scan_inventory(pdfs, threading.Event())
    assert (report["documents_total"], report["documents"], report["pages"]) == (3, 1, 2)
    assert report["unreadable"] == 1
    assert "notes.txt" not in report["files"]
    routes.document_ocr.write_json(job._inventory_path(), report)
    opened.clear()
    job.scan_inventory(pdfs, threading.Event())
    assert opened == []


def test_inventory_route_reports_progress_without_file_details(monkeypatch, tmp_path):
    monkeypatch.setattr(routes.document_ocr, "root", lambda: tmp_path)
    monkeypatch.setattr(job, "_inventory_thread", None)
    routes.document_ocr.write_json(
        tmp_path / "library_inventory.json",
        {"status": "running", "documents": 4, "pages": 9, "files": {"a.pdf": {}}},
    )
    payload = client().get("/api/ocr/inventory").json()
    assert payload["status"] == "interrupted" and payload["pages"] == 9
    assert "files" not in payload and payload["active"] is False


def test_inventory_refuses_while_the_ocr_pass_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(routes.document_ocr, "root", lambda: tmp_path)
    monkeypatch.setattr(job, "_inventory_thread", None)

    class Active:
        def is_alive(self):
            return True

    monkeypatch.setattr(job, "_thread", Active())
    response = client().post("/api/ocr/inventory")
    assert response.status_code == 409 and response.json()["code"] == "ocr_running"
