import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import application
from src.api import lifecycle as routes
from src.rag import state


@pytest.fixture(autouse=True)
def _clean_pause_state():
    state._reset_vector_load_pause()
    yield
    state._reset_vector_load_pause()


def client():
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _checkpoint_in_thread():
    outcome = {}

    def run():
        try:
            state._raise_if_vector_load_paused()
            outcome["result"] = "continued"
        except state.VectorLoadPaused:
            outcome["result"] = "stopped"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_suspended_load_waits_at_checkpoint_until_resumed():
    state.suspend_vector_loading()
    thread, outcome = _checkpoint_in_thread()

    thread.join(0.6)
    assert thread.is_alive(), "a paused load must wait, not stop"

    state.resume_vector_loading()
    thread.join(2)
    assert outcome == {"result": "continued"}


def test_stop_ends_a_paused_load():
    state.suspend_vector_loading()
    thread, outcome = _checkpoint_in_thread()
    thread.join(0.3)

    state.pause_vector_loading()
    thread.join(2)
    assert outcome == {"result": "stopped"}
    assert not state.is_vector_loading_suspended()


def test_new_load_starts_unpaused():
    state.suspend_vector_loading()
    state._reset_vector_load_pause()
    assert not state.is_vector_loading_suspended()


def test_pause_endpoint_refuses_when_no_rag_load_runs(monkeypatch):
    # The OCR job holds the shared ingestion lock but is not a RAG load.
    ocr_holds_lock = threading.Lock()
    ocr_holds_lock.acquire()
    monkeypatch.setattr(application, "_rag_init_lock", ocr_holds_lock)
    monkeypatch.setattr(routes, "_rag_load_running", threading.Event())
    body = client().post("/api/pause-init").json()
    assert body["ok"] is False
    assert not state.is_vector_loading_suspended()


def test_pause_and_resume_endpoints_toggle_a_running_load(monkeypatch):
    running = threading.Event()
    running.set()
    monkeypatch.setattr(routes, "_rag_load_running", running)
    api = client()

    assert api.post("/api/pause-init").json() == {"ok": True, "paused": True}
    assert state.is_vector_loading_suspended()

    assert api.post("/api/resume-init").json() == {"ok": True, "paused": False}
    assert not state.is_vector_loading_suspended()
