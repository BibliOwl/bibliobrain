from __future__ import annotations

from fastapi.testclient import TestClient

import app_api
from src import drafting_history


def _use_storage(monkeypatch, workspace_tmp_dir):
    storage_dir = workspace_tmp_dir / "storage"
    monkeypatch.setattr(drafting_history.config, "STORAGE_DIR", str(storage_dir))
    return storage_dir


def test_run_update_keeps_date_and_excerpt(monkeypatch, workspace_tmp_dir) -> None:
    _use_storage(monkeypatch, workspace_tmp_dir)
    client = TestClient(app_api.app)

    first = client.put(
        "/api/drafting-history/runs/run-00000001",
        json={
            "kind": "citations",
            "model": "mistral",
            "excerpt": "Les aimants HTS perdent de l'énergie.",
            "summary": "1 source",
            "result_count": 1,
            "payload": {"suggestions": [{"citation_id": "A"}]},
        },
    )
    assert first.status_code == 200
    # Retried sentences update the same analysis, whatever excerpt they send.
    client.put(
        "/api/drafting-history/runs/run-00000001",
        json={
            "kind": "citations",
            "excerpt": "Une seule phrase réessayée.",
            "summary": "2 sources",
            "result_count": 2,
            "payload": {"suggestions": [{"citation_id": "A"}, {"citation_id": "B"}]},
        },
    )

    listing = client.get("/api/drafting-history").json()
    assert len(listing["runs"]) == 1
    run = listing["runs"][0]
    assert run["excerpt"] == "Les aimants HTS perdent de l'énergie."
    assert run["created_at"] == first.json()["run"]["created_at"]
    assert run["result_count"] == 2
    assert "payload" not in run

    full = client.get("/api/drafting-history/runs/run-00000001").json()["run"]
    assert [s["citation_id"] for s in full["payload"]["suggestions"]] == ["A", "B"]

    assert client.delete("/api/drafting-history/runs/run-00000001").json()["deleted"] is True
    assert client.get("/api/drafting-history/runs/run-00000001").status_code == 404


def test_run_rejects_unsafe_id_and_unknown_kind(monkeypatch, workspace_tmp_dir) -> None:
    _use_storage(monkeypatch, workspace_tmp_dir)
    client = TestClient(app_api.app)

    bad_id = client.put("/api/drafting-history/runs/short", json={"kind": "citations"})
    assert bad_id.status_code == 422
    bad_kind = client.put("/api/drafting-history/runs/run-00000002", json={"kind": "chat"})
    assert bad_kind.status_code == 422
    assert client.get("/api/drafting-history").json()["runs"] == []


def test_versions_skip_empty_and_unchanged_drafts(monkeypatch, workspace_tmp_dir) -> None:
    _use_storage(monkeypatch, workspace_tmp_dir)

    assert drafting_history.save_version("<p><br></p>", reason="before_clear") is None
    saved = drafting_history.save_version("<p>Premier&nbsp;jet</p><p>du texte</p>", 3, "before_clear")
    assert saved is not None
    assert saved["word_count"] == 4
    assert saved["preview"] == "Premier jet du texte"
    assert drafting_history.save_version("<p>Premier&nbsp;jet</p><p>du texte</p>", reason="before_restore") is None

    restored = drafting_history.get_version(saved["id"])
    assert restored["html"] == "<p>Premier&nbsp;jet</p><p>du texte</p>"
    assert restored["citation_start_number"] == 3


def test_versions_keep_only_the_most_recent(monkeypatch, workspace_tmp_dir) -> None:
    _use_storage(monkeypatch, workspace_tmp_dir)
    monkeypatch.setattr(drafting_history, "MAX_VERSIONS", 3)

    for index in range(5):
        drafting_history.save_version(f"<p>Version {index}</p>", reason="before_proofread")

    assert [v["preview"] for v in drafting_history.list_versions()] == [
        "Version 4",
        "Version 3",
        "Version 2",
    ]


def test_draft_save_takes_throttled_automatic_versions(monkeypatch, workspace_tmp_dir) -> None:
    _use_storage(monkeypatch, workspace_tmp_dir)
    client = TestClient(app_api.app)

    client.put("/api/drafting-document", json={"html": "<p>Début</p>"})
    client.put("/api/drafting-document", json={"html": "<p>Début et suite</p>"})
    versions = client.get("/api/drafting-history").json()["versions"]
    assert [(v["reason"], v["preview"]) for v in versions] == [("auto", "Début")]

    # A version asked for before a bulk change is never throttled.
    client.post(
        "/api/drafting-history/versions",
        json={"html": "<p>Début et suite</p>", "reason": "before_proofread"},
    )
    monkeypatch.setattr(drafting_history, "AUTO_VERSION_INTERVAL_SECONDS", 0)
    client.put("/api/drafting-document", json={"html": "<p>Tout autre chose</p>"})
    versions = client.get("/api/drafting-history").json()["versions"]
    assert [v["reason"] for v in versions] == ["auto", "before_proofread", "auto"]

    assert client.delete("/api/drafting-history").json() == {"ok": True}
    assert client.get("/api/drafting-history").json() == {"ok": True, "runs": [], "versions": []}


def test_settings_history_reset_clears_drafting_history_but_keeps_draft(
    monkeypatch, workspace_tmp_dir
) -> None:
    _use_storage(monkeypatch, workspace_tmp_dir)
    client = TestClient(app_api.app)
    client.put("/api/drafting-document", json={"html": "<p>Brouillon en cours</p>"})
    client.put("/api/drafting-history/runs/run-00000003", json={"kind": "proofread"})

    reset = client.post("/api/reset-history", json={"confirm": True}).json()

    assert reset["drafting_history"] == {"runs_deleted": 1, "versions_deleted": 1}
    assert client.get("/api/drafting-history").json() == {"ok": True, "runs": [], "versions": []}
    draft = client.get("/api/drafting-document").json()["document"]
    assert draft["html"] == "<p>Brouillon en cours</p>"


def test_version_before_argument_insertion_is_accepted(monkeypatch, workspace_tmp_dir) -> None:
    # Inserting an argument snapshots the draft first, like a correction does.
    _use_storage(monkeypatch, workspace_tmp_dir)
    client = TestClient(app_api.app)

    response = client.post(
        "/api/drafting-history/versions",
        json={"html": "<p>Avant l'argument.</p>", "reason": "before_arguments"},
    )

    assert response.status_code == 200
    assert client.get("/api/drafting-history").json()["versions"][0]["reason"] == "before_arguments"
