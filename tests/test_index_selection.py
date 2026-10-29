from __future__ import annotations

from src.index_selection import apply_index_selection, normalize_selection
from src.zotero_db import ZoteroItem


def _item(key: str, *collections: str) -> ZoteroItem:
    return ZoteroItem(item_key=key, title=key, collections=list(collections))


def _entries(tmp_path, *names: str, size: int = 10) -> list[tuple[str, str]]:
    entries = []
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        entries.append((name, str(path)))
    return entries


def _library() -> dict[str, ZoteroItem]:
    return {
        "hts/a.pdf": _item("A", "Fusion / HTS"),
        "nb3sn/b.pdf": _item("B", "Nb3Sn"),
        "vrac/c.pdf": _item("C", "Vrac à trier"),
    }


def test_default_selection_keeps_everything(tmp_path):
    entries = _entries(tmp_path, "hts/a.pdf", "nb3sn/b.pdf")
    kept, skipped = apply_index_selection(entries, _library(), normalize_selection({}))
    assert kept == entries
    assert skipped == []


def test_include_keeps_chosen_collections_and_their_children(tmp_path):
    entries = _entries(tmp_path, "hts/a.pdf", "nb3sn/b.pdf", "vrac/c.pdf", "loose.pdf")
    selection = {"collection_mode": "include", "collections": ["fusion"]}
    kept, skipped = apply_index_selection(entries, _library(), selection)
    assert [rel for rel, _ in kept] == ["hts/a.pdf"]
    assert {entry["reason"] for entry in skipped} == {"collection"}
    assert len(skipped) == 3


def test_exclude_drops_one_collection_only(tmp_path):
    entries = _entries(tmp_path, "hts/a.pdf", "nb3sn/b.pdf", "vrac/c.pdf")
    selection = {"collection_mode": "exclude", "collections": ["Vrac à trier"]}
    kept, _ = apply_index_selection(entries, _library(), selection)
    assert [rel for rel, _ in kept] == ["hts/a.pdf", "nb3sn/b.pdf"]


def test_collection_choice_keeps_nothing_without_metadata(tmp_path):
    # Ignoring the choice used to fill an "Nb3Sn only" index with every document.
    entries = _entries(tmp_path, "hts/a.pdf")
    selection = {"collection_mode": "include", "collections": ["Nb3Sn"]}
    kept, skipped = apply_index_selection(entries, {}, selection)
    assert kept == []
    assert skipped == [{"rel_path": "hts/a.pdf", "reason": "metadata_missing"}]


def test_excluded_folder_matches_whole_segments(tmp_path):
    entries = _entries(tmp_path, "vrac/c.pdf", "vrac2/d.pdf", "Vrac/sub/e.pdf")
    kept, skipped = apply_index_selection(
        entries, {}, {"excluded_folders": ["vrac\\"], "max_file_mb": 0}
    )
    assert [rel for rel, _ in kept] == ["vrac2/d.pdf"]
    assert [entry["reason"] for entry in skipped] == ["folder", "folder"]


def test_size_guard_skips_large_files_unless_allowed(tmp_path):
    big = 2 * 1024 * 1024
    entries = _entries(tmp_path, "big.pdf", size=big) + _entries(tmp_path, "small.pdf")
    kept, skipped = apply_index_selection(entries, {}, {"max_file_mb": 1})
    assert [rel for rel, _ in kept] == ["small.pdf"]
    assert skipped == [{"rel_path": "big.pdf", "reason": "too_large", "size": big}]

    kept, skipped = apply_index_selection(
        entries, {}, {"max_file_mb": 1, "allowed_large": ["big.pdf"]}
    )
    assert len(kept) == 2 and skipped == []


def test_normalize_selection_rejects_bad_values():
    selection = normalize_selection(
        {"collection_mode": "weird", "max_file_mb": "x", "collections": ["A", "a", ""]}
    )
    assert selection["collection_mode"] == "all"
    assert selection["max_file_mb"] == 0
    assert selection["collections"] == ["A"]


def test_settings_round_trip_and_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app_api import app
    from src import config
    from src.rag import state

    data = tmp_path / "data"
    (data / "vrac").mkdir(parents=True)
    (data / "vrac" / "c.pdf").write_bytes(b"x")
    (data / "keep.pdf").write_bytes(b"x")
    monkeypatch.setattr(config, "DATA_PATH", str(data))
    monkeypatch.setattr(config, "ZOTERO_DB_PATH", "")
    state.clear_document_scan_cache()

    client = TestClient(app)
    try:
        response = client.put("/api/index-selection", json={"excluded_folders": ["vrac"]})
        assert response.status_code == 200
        payload = response.json()
        assert payload["selection"]["excluded_folders"] == ["vrac"]
        assert payload["kept_count"] == 1
        assert payload["skipped_by_reason"]["folder"] == 1
        assert payload["folders"] == [{"path": "vrac", "count": 1, "depth": 0}]
        assert client.get("/api/index-selection").json()["kept_count"] == 1
    finally:
        client.put("/api/index-selection", json={"excluded_folders": []})
        state.clear_document_scan_cache()


def test_progress_steps_never_go_back():
    from src.rag.progress_stages import StageTracker

    tracker = StageTracker()
    first = tracker.event(0.02, "checks")
    assert (first["stage"], first["stage_key"], first["stage_count"]) == (1, "check", 3)
    reading = tracker.event(0.40, "reading")
    assert (reading["stage"], reading["frac"]) == (2, 0.4)
    # A lower report inside the same load keeps the bar where it was.
    lower = tracker.event(0.12, "incremental")
    assert (lower["stage"], lower["frac"]) == (2, 0.4)
    assert tracker.event(0.95, "model")["stage_key"] == "finish"


def test_library_profiles_keep_their_own_selection_and_index(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app_api import app
    from src.index_selection import get_index_selection
    from src.rag import vector_store

    monkeypatch.setattr(vector_store, "CHROMA_PATH", str(tmp_path / "chroma"))
    client = TestClient(app)
    try:
        main_root = vector_store.get_vector_db_root()
        assert main_root == str(tmp_path / "chroma")

        created = client.post("/api/library-profiles", json={"name": "Fusion HTS"}).json()
        assert created["created"] == "fusion-hts"
        assert [p["id"] for p in created["profiles"]] == ["default", "fusion-hts"]

        assert client.post("/api/library-profiles/fusion-hts/activate").status_code == 200
        assert vector_store.get_vector_db_root() == str(
            tmp_path / "chroma" / "libraries" / "fusion-hts"
        )
        client.put(
            "/api/index-selection", json={"collection_mode": "include", "collections": ["HTS"]}
        )
        assert get_index_selection()["collections"] == ["HTS"]

        # The active profile cannot be deleted; the main library keeps its own choice.
        assert client.delete("/api/library-profiles/fusion-hts").status_code == 409
        client.post("/api/library-profiles/default/activate")
        assert get_index_selection()["collection_mode"] == "all"
        assert vector_store.get_vector_db_root() == main_root

        index_root = tmp_path / "chroma" / "libraries" / "fusion-hts"
        index_root.mkdir(parents=True)
        deleted = client.delete("/api/library-profiles/fusion-hts").json()
        assert deleted["index_removed"] is True and not index_root.exists()
        assert [p["id"] for p in deleted["profiles"]] == ["default"]
    finally:
        client.post("/api/library-profiles/default/activate")


def _keyed(key: str, keys: dict[str, str]) -> ZoteroItem:
    return ZoteroItem(item_key=key, title=key, collections=list(keys), collection_keys=dict(keys))


def test_selection_saved_without_a_size_keeps_large_files(tmp_path):
    # A 0.9.0 install has no size setting: a reload must not drop its big PDFs.
    big = 2 * 1024 * 1024
    entries = _entries(tmp_path, "big.pdf", size=big)
    kept, skipped = apply_index_selection(entries, {}, normalize_selection({}))
    assert kept == entries and skipped == []


def test_new_library_proposes_the_size_guard(tmp_path, monkeypatch):
    from src import library_profiles
    from src.index_selection import SUGGESTED_MAX_FILE_MB

    saved: dict = {}
    monkeypatch.setattr(library_profiles, "_payload", lambda: dict(saved))
    monkeypatch.setattr(library_profiles, "_save", lambda update: saved.update(update))
    created = library_profiles.create_profile("HTS")
    assert created["selection"]["max_file_mb"] == SUGGESTED_MAX_FILE_MB
    copied = library_profiles.create_profile("Copie", {"max_file_mb": 0})
    assert copied["selection"]["max_file_mb"] == 0


def test_renamed_collection_is_followed_by_its_key(tmp_path):
    from src.index_selection import remember_collection_keys, resolve_selection

    before = {
        "hts/a.pdf": _keyed("A", {"Fusion": "1:FUS", "Fusion / HTS": "1:HTS"}),
        "nb3sn/b.pdf": _keyed("B", {"Nb3Sn": "1:NB"}),
    }
    selection = remember_collection_keys(
        normalize_selection({"collection_mode": "include", "collections": ["Fusion / HTS"]}),
        before,
    )
    assert selection["collection_keys"] == {"Fusion / HTS": "1:HTS"}

    # Renamed in Zotero: the saved name matches nothing, the key still does.
    after = {
        "hts/a.pdf": _keyed("A", {"Fusion": "1:FUS", "Fusion / Supra HTS": "1:HTS"}),
        "nb3sn/b.pdf": _keyed("B", {"Nb3Sn": "1:NB"}),
    }
    assert resolve_selection(selection, after)["collections"] == ["Fusion / Supra HTS"]
    entries = _entries(tmp_path, "hts/a.pdf", "nb3sn/b.pdf")
    kept, _ = apply_index_selection(entries, after, selection)
    assert [rel for rel, _ in kept] == ["hts/a.pdf"]


def test_collection_without_a_key_still_matches_by_name(tmp_path):
    # BibTeX records and choices saved before keys existed.
    entries = _entries(tmp_path, "hts/a.pdf", "nb3sn/b.pdf")
    selection = {"collection_mode": "include", "collections": ["Nb3Sn"], "collection_keys": {}}
    kept, _ = apply_index_selection(entries, _library(), selection)
    assert [rel for rel, _ in kept] == ["nb3sn/b.pdf"]


def test_selection_problem_stops_a_reload_that_would_rewrite_the_index():
    from src.index_selection import selection_problem

    include = {"collection_mode": "include", "collections": ["Fusion"]}
    assert selection_problem(include, _library()) is None
    assert selection_problem({"collection_mode": "all"}, {}) is None
    missing = selection_problem(include, {})
    assert missing is not None and missing["code"] == "selection_metadata_missing"
    gone = selection_problem({"collection_mode": "include", "collections": ["Supprimée"]}, _library())
    assert gone is not None and gone["code"] == "selection_collections_missing"
    # Excluding a collection Zotero no longer has removes nothing.
    assert (
        selection_problem({"collection_mode": "exclude", "collections": ["Supprimée"]}, _library())
        is None
    )


def test_switch_waits_for_no_answer_and_refuses_during_one(tmp_path, monkeypatch):
    import pytest

    from src.rag import state, vector_store

    calls: list[str] = []
    monkeypatch.setattr(
        "src.library_profiles.set_active_profile", lambda profile_id: calls.append(profile_id)
    )
    reader = state.begin_vector_reader()
    try:
        with pytest.raises(vector_store.LibraryBusyError):
            vector_store.switch_library_profile("hts")
        assert calls == []
    finally:
        state.end_vector_reader(reader)
    vector_store.switch_library_profile("hts")
    assert calls == ["hts"]


def test_activate_route_reports_an_answer_in_progress(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app_api import app
    from src.rag import state, vector_store

    monkeypatch.setattr(vector_store, "CHROMA_PATH", str(tmp_path / "chroma"))
    client = TestClient(app)
    created = client.post("/api/library-profiles", json={"name": "Occupée"}).json()["created"]
    reader = state.begin_vector_reader()
    try:
        response = client.post(f"/api/library-profiles/{created}/activate")
        assert response.status_code == 409
        assert response.json()["error"] == "answer_in_progress"
    finally:
        state.end_vector_reader(reader)
        client.post("/api/library-profiles/default/activate")
        client.delete(f"/api/library-profiles/{created}")


def test_index_state_is_ready_only_for_the_current_model(tmp_path, monkeypatch):
    from src.rag import vector_store

    monkeypatch.setattr(vector_store, "_current_embedding_profile", lambda: "embed-a")
    monkeypatch.setattr(vector_store, "_current_chunk_size", lambda: 1000)
    root = tmp_path / "libraries" / "hts"
    assert vector_store.library_profile_index_state(str(root)) == "empty"

    profile_id = vector_store.build_vector_index_profile()["profile_id"]
    other = root / "indexes" / "embed-b__chunk1000"
    other.mkdir(parents=True)
    (other / "chroma.sqlite3").write_bytes(b"")
    assert vector_store.library_profile_index_state(str(root)) == "outdated"

    current = root / "indexes" / str(profile_id)
    current.mkdir(parents=True)
    (current / "chroma.sqlite3").write_bytes(b"")
    assert vector_store.library_profile_index_state(str(root)) == "ready"
    assert vector_store.library_profile_has_index(str(root)) is True
