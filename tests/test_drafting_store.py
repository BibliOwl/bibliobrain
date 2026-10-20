from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app_api
from src import drafting_store


def test_drafting_document_round_trip_uses_storage_dir(monkeypatch, workspace_tmp_dir) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    monkeypatch.setattr(drafting_store.config, "STORAGE_DIR", str(storage_dir))
    client = TestClient(app_api.app)

    missing = client.get("/api/drafting-document")
    assert missing.status_code == 200
    assert missing.json() == {"ok": True, "exists": False, "document": None}

    saved = client.put(
        "/api/drafting-document",
        json={"html": "<p>Brouillon <strong>persistant</strong></p>", "citation_start_number": 7},
    )
    assert saved.status_code == 200
    assert saved.json()["document"]["citation_start_number"] == 7

    payload = json.loads((storage_dir / "drafting_document.json").read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["html"] == "<p>Brouillon <strong>persistant</strong></p>"

    restored = client.get("/api/drafting-document").json()
    assert restored["exists"] is True
    assert restored["document"]["html"] == payload["html"]

    cleared = client.delete("/api/drafting-document")
    assert cleared.status_code == 200
    assert not (storage_dir / "drafting_document.json").exists()


def test_drafting_frontend_migrates_browser_copy_to_server() -> None:
    source = (Path(__file__).parents[1] / "static/js/app/drafting.js").read_text(
        encoding="utf-8"
    )

    assert "async function _draftingLoadDocument()" in source
    assert "fetch('/api/drafting-document')" in source
    assert "method: 'PUT'" in source
    assert "remoteUpdatedAt >= localUpdatedAt" in source
    assert "if (!restoredFromServer && localHtml)" in source
    assert "_draftingSaveDocument();" in source
