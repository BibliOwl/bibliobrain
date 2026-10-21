from __future__ import annotations

import contextlib
import json
import shutil
import sqlite3
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import src.config as config
from src.rag import index_transfer, state, vector_store
from src.rag.zotero_integration import _file_signature
from src.zotero_db import ZoteroItem

MODEL = "embeddinggemma:latest"


@pytest.fixture
def env(workspace_tmp_dir: Path, monkeypatch: Any) -> Iterator[SimpleNamespace]:
    settings: dict[str, Any] = {
        "chunk_size": 1000,
        "model": MODEL,
        "provider": "ollama",
        "account": "account-1",
    }
    library: dict[str, ZoteroItem] = {}
    switched: list[str] = []
    installed = {MODEL: "sha256:one"}

    def set_chunk_size(value: Any) -> int:
        settings["chunk_size"] = int(value)
        return int(value)

    def switch_model(name: str, *, provider_mode: str | None = None, persist: bool = True) -> str:
        switched.append(name)
        settings["model"] = name
        return name

    def ollama_entry(name: str) -> tuple[bool, dict[str, Any] | None]:
        return True, ({"name": name, "digest": installed[name]} if name in installed else None)

    def label(name: str | None = None) -> str:
        return name or settings["model"]

    class FakeEmbedder:
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            if settings.get("vectors") is None:
                raise RuntimeError("embedding service offline")
            return [list(vector) for vector in settings["vectors"][: len(texts)]]

    probe = [
        {"text": "passage " * 40, "vector": [1.0, 0.0, 0.0]},
        {"text": "another passage " * 20, "vector": [0.0, 1.0, 0.0]},
    ]
    monkeypatch.setattr(index_transfer, "_index_probe", lambda index_dir: probe)
    monkeypatch.setattr("src.llm_provider.create_embeddings", lambda model: FakeEmbedder())

    monkeypatch.setattr(vector_store, "required_embedding_model_name", lambda: settings["model"])
    monkeypatch.setattr(vector_store, "current_embedding_profile_label", label)
    monkeypatch.setattr(vector_store, "get_persisted_chunk_size", lambda default=None: settings["chunk_size"])
    monkeypatch.setattr(index_transfer, "required_embedding_model_name", lambda: settings["model"])
    monkeypatch.setattr(index_transfer, "current_embedding_profile_label", label)
    monkeypatch.setattr(index_transfer, "get_embedding_provider_mode", lambda: settings["provider"])
    monkeypatch.setattr(index_transfer, "get_persisted_chunk_size", lambda default=None: settings["chunk_size"])
    monkeypatch.setattr(index_transfer, "set_persisted_chunk_size", set_chunk_size)
    monkeypatch.setattr(index_transfer, "set_runtime_embedding_model", switch_model)
    monkeypatch.setattr(index_transfer, "index_visual_signature", lambda: "")
    monkeypatch.setattr(index_transfer, "_metadata_library", lambda: ("zotero", "zotero.sqlite", library))
    monkeypatch.setattr(
        index_transfer, "_zotero_account", lambda source, path: (settings["account"], "synced")
    )
    monkeypatch.setattr(index_transfer, "is_zotero_storage_path", lambda data_path, db_path: False)
    monkeypatch.setattr(index_transfer, "_ollama_model_entry", ollama_entry)
    monkeypatch.setattr("src.llm_provider._ollama_base_url", lambda: "http://localhost:11434")

    namespace = SimpleNamespace(
        root=workspace_tmp_dir,
        settings=settings,
        library=library,
        switched=switched,
        installed=installed,
    )

    def use_computer(name: str) -> None:
        monkeypatch.setattr(vector_store, "CHROMA_PATH", str(workspace_tmp_dir / name / "chroma"))
        monkeypatch.setattr(config, "DATA_PATH", str(workspace_tmp_dir / name / "pdfs"))
        (workspace_tmp_dir / name / "pdfs").mkdir(parents=True, exist_ok=True)
        state.clear_document_scan_cache()

    namespace.use_computer = use_computer
    use_computer("pc1")
    yield namespace
    state.clear_document_scan_cache()


def _profile_dir(chunk_size: int = 1000, model: str = MODEL) -> Path:
    return Path(
        vector_store.build_vector_index_profile(chunk_size=chunk_size, embedding_model=model)["path"]
    )


def _write_pdf(name: str, content: bytes) -> Path:
    path = Path(config.DATA_PATH) / name
    path.write_bytes(b"%PDF-1.4\n" + content)
    return path


def _build_index(env: SimpleNamespace) -> sqlite3.Connection:
    """Index a.pdf, b.pdf, c.pdf, e.pdf; returns an open WAL connection on the lexical db."""
    index_dir = _profile_dir()
    index_dir.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(index_dir / "chroma.sqlite3")) as connection:
        connection.execute("CREATE TABLE embeddings (id TEXT)")
        connection.execute("INSERT INTO embeddings VALUES ('chunk-1')")
        connection.commit()
    lexical = sqlite3.connect(index_dir / "lexical_fts.sqlite3")
    lexical.execute("PRAGMA journal_mode=WAL")
    lexical.execute("CREATE TABLE lexical_chunks (rel_path TEXT)")
    lexical.execute("INSERT INTO lexical_chunks VALUES ('a.pdf')")
    lexical.commit()
    segment = index_dir / "0b7c2e6a-segment"
    segment.mkdir()
    (segment / "data_level0.bin").write_bytes(b"\x01\x02" * 64)

    entries = {}
    for name in ("a.pdf", "b.pdf", "c.pdf", "e.pdf"):
        path = _write_pdf(name, name.encode() * 50)
        entries[name] = {
            "file_sig": _file_signature(str(path)),
            "zotero_sig": "",
            "visual_sig": "",
            "chunk_count": 3,
        }
    (index_dir / state._MANIFEST_FILE).write_text(
        json.dumps(
            {"version": 3, "chunk_size": 1000, "embedding_model": MODEL, "entries": entries}
        ),
        encoding="utf-8",
    )
    env.library["a.pdf"] = ZoteroItem(item_key="AAAA1111", title="Article A", year="2020")
    return lexical


def _export(env: SimpleNamespace) -> Path:
    lexical = _build_index(env)
    try:
        archive = env.root / "transfer" / "index.zip"
        result = index_transfer.export_active_index(archive)
    finally:
        lexical.close()
    assert result["document_count"] == 4
    assert result["chunk_count"] == 12
    return archive


def _move_to_second_computer(env: SimpleNamespace) -> None:
    """a.pdf identical, b.pdf edited, c.pdf absent, e.pdf identical, d.pdf new."""
    source = env.root / "pc1" / "pdfs"
    env.use_computer("pc2")
    target = Path(config.DATA_PATH)
    shutil.copy2(source / "a.pdf", target / "a.pdf")
    shutil.copy2(source / "e.pdf", target / "e.pdf")
    _write_pdf("b.pdf", b"annotated on the other computer")
    _write_pdf("d.pdf", b"only here")
    env.library.clear()
    env.library["a.pdf"] = ZoteroItem(item_key="AAAA1111", title="Article A (corrected)", year="2020")
    env.settings["account"] = "account-2"


def test_export_writes_a_self_described_archive_without_wal_files(env: SimpleNamespace) -> None:
    archive = _export(env)

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        header = json.loads(bundle.read(index_transfer.HEADER_NAME))
        lexical_bytes = bundle.read("index/lexical_fts.sqlite3")

    assert {
        index_transfer.HEADER_NAME,
        "index/chroma.sqlite3",
        "index/lexical_fts.sqlite3",
        "index/index_manifest.json",
        "index/0b7c2e6a-segment/data_level0.bin",
    } <= names
    assert not any(name.endswith(("-wal", "-shm")) for name in names)
    assert header["index"]["embedding_model_name"] == MODEL
    assert header["index"]["embedding_model_digest"] == "sha256:one"
    assert header["documents"]["a.pdf"]["key"] == "AAAA1111"
    assert header["documents"]["b.pdf"] == {"key": "", "sig": "", "title": ""}
    assert "pdfs" not in json.dumps(header)

    # The row still sat in the WAL file when the export ran.
    snapshot = env.root / "lexical-check.sqlite3"
    snapshot.write_bytes(lexical_bytes)
    with contextlib.closing(sqlite3.connect(snapshot)) as connection:
        assert connection.execute("SELECT rel_path FROM lexical_chunks").fetchall() == [("a.pdf",)]


def test_export_refuses_when_no_index_is_ready(env: SimpleNamespace) -> None:
    with pytest.raises(index_transfer.IndexTransferError) as error:
        index_transfer.export_active_index(env.root / "index.zip")
    assert error.value.code == "no_ready_index"


def test_inspect_reports_missing_changed_and_zotero_differences(env: SimpleNamespace) -> None:
    archive = _export(env)
    _move_to_second_computer(env)

    report = index_transfer.inspect_archive(archive)

    documents = report["documents"]
    assert documents["found"] == 3
    assert [row["rel_path"] for row in documents["missing"]] == ["c.pdf"]
    assert [row["rel_path"] for row in documents["content_changed"]] == ["b.pdf"]
    assert [row["title"] for row in documents["metadata_changed"]] == ["Article A"]
    assert documents["reindex_count"] == 2
    assert documents["unchanged_count"] == 1
    assert documents["new_local_count"] == 1
    assert report["library"]["same_account"] is False
    assert report["embedding"]["action"] == "ready"
    assert report["existing_index"]["exists"] is False
    codes = {issue["code"] for issue in report["issues"]}
    assert {
        "documents_missing",
        "documents_changed",
        "metadata_changed",
        "zotero_library_differs",
    } <= codes
    assert report["verdict"] == "attention"
    assert report["can_import"] is True


def test_inspect_offers_download_when_the_model_is_missing(env: SimpleNamespace) -> None:
    archive = _export(env)
    _move_to_second_computer(env)
    env.installed.clear()

    report = index_transfer.inspect_archive(archive)

    embedding = report["embedding"]
    assert embedding["action"] == "download"
    assert embedding["can_download"] is True
    assert embedding["install_command"] == f"ollama pull {MODEL}"
    assert "model_missing" in {issue["code"] for issue in report["issues"]}


def test_inspect_flags_a_model_that_gives_other_vectors(env: SimpleNamespace) -> None:
    archive = _export(env)
    _move_to_second_computer(env)
    env.settings.update(
        provider="lmstudio",
        model="lmstudio:text-embedding-nomic",
        vectors=[[0.0, 0.0, 1.0], [0.0, 0.0, 1.0]],
    )

    report = index_transfer.inspect_archive(archive)

    assert report["embedding"]["action"] == "provider_mismatch"
    assert report["embedding"]["equivalence"]["status"] == "different"
    assert "provider_mismatch" in {issue["code"] for issue in report["issues"]}


def test_same_model_on_another_service_is_recognized_by_its_vectors(
    env: SimpleNamespace,
) -> None:
    archive = _export(env)
    _move_to_second_computer(env)
    # Another quantization of the same weights: close, not identical.
    env.settings.update(
        provider="lmstudio",
        model="lmstudio:text-embedding-embeddinggemma-300m",
        vectors=[[0.99, 0.05, 0.02], [0.04, 0.98, 0.03]],
    )

    report = index_transfer.inspect_archive(archive)
    assert report["embedding"]["action"] == "equivalent"
    assert report["embedding"]["equivalence"]["similarity"] > 0.99
    assert "model_equivalent" in {issue["code"] for issue in report["issues"]}

    result = index_transfer.import_archive(archive)

    target = _profile_dir(model="lmstudio:text-embedding-embeddinggemma-300m")
    manifest = json.loads((target / state._MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["embedding_model"] == "lmstudio:text-embedding-embeddinggemma-300m"
    assert result["active"] is True
    assert env.switched == []
    assert not _profile_dir().exists()


def test_vectors_of_another_size_are_never_equivalent(env: SimpleNamespace) -> None:
    archive = _export(env)
    _move_to_second_computer(env)
    env.settings.update(model="nomic-embed-text:latest", vectors=[[1.0, 0.0], [0.0, 1.0]])
    env.installed["nomic-embed-text:latest"] = "sha256:nomic"

    report = index_transfer.inspect_archive(archive)

    assert report["embedding"]["equivalence"]["status"] == "different"
    assert report["embedding"]["action"] == "switch"


def test_inspect_warns_about_a_different_model_version(env: SimpleNamespace) -> None:
    archive = _export(env)
    _move_to_second_computer(env)
    env.installed[MODEL] = "sha256:two"

    report = index_transfer.inspect_archive(archive)

    assert report["embedding"]["digest_matches"] is False
    assert "model_version_differs" in {issue["code"] for issue in report["issues"]}


def test_import_replaces_the_profile_and_keeps_a_hidden_backup(env: SimpleNamespace) -> None:
    archive = _export(env)
    _move_to_second_computer(env)
    previous = _profile_dir()
    previous.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(previous / "chroma.sqlite3")) as connection:
        connection.execute("CREATE TABLE previous_index (id TEXT)")

    result = index_transfer.import_archive(archive)

    assert result["active"] is True
    assert result["adopted"] == {"chunk_size": False, "embedding_model": False}
    with contextlib.closing(sqlite3.connect(_profile_dir() / "chroma.sqlite3")) as connection:
        assert connection.execute("SELECT id FROM embeddings").fetchall() == [("chunk-1",)]
    backup = Path(result["backup"])
    assert backup.name.startswith(f".backup-{_profile_dir().name}-")
    assert (backup / "chroma.sqlite3").is_file()
    assert [profile["profile_id"] for profile in vector_store.list_vector_index_profiles()] == [
        _profile_dir().name
    ]
    assert not list(_profile_dir().parent.glob(".import-*"))
    active = json.loads((Path(vector_store.CHROMA_PATH) / "active_profile.json").read_text("utf-8"))
    assert active["profile_id"] == _profile_dir().name
    assert active["ready"] is True


def test_import_aligns_chunk_size_and_embedding_model(env: SimpleNamespace) -> None:
    archive = _export(env)
    _move_to_second_computer(env)
    env.settings.update(model="nomic-embed-text:latest", chunk_size=800)
    env.installed["nomic-embed-text:latest"] = "sha256:nomic"

    report = index_transfer.inspect_archive(archive)
    assert report["embedding"]["action"] == "switch"
    assert report["chunk_size"] == {"index": 1000, "current": 800, "same": False}

    result = index_transfer.import_archive(archive)

    assert env.switched == [MODEL]
    assert env.settings["chunk_size"] == 1000
    assert result["adopted"] == {"chunk_size": True, "embedding_model": True}
    assert result["active"] is True


def test_import_without_adoption_leaves_the_index_inactive(env: SimpleNamespace) -> None:
    archive = _export(env)
    _move_to_second_computer(env)
    env.settings.update(model="nomic-embed-text:latest", chunk_size=800)
    env.installed["nomic-embed-text:latest"] = "sha256:nomic"

    result = index_transfer.import_archive(archive, adopt_settings=False)

    assert result["active"] is False
    assert env.switched == []
    assert env.settings["chunk_size"] == 800
    assert (_profile_dir() / "chroma.sqlite3").is_file()


@pytest.mark.parametrize(
    "members",
    [
        {"notes.txt": "hello"},
        {index_transfer.HEADER_NAME: "{}", "index/../escape.txt": "x"},
        {index_transfer.HEADER_NAME: json.dumps({"format": "other"}), "index/chroma.sqlite3": ""},
    ],
)
def test_foreign_or_unsafe_archives_are_rejected(
    env: SimpleNamespace, members: dict[str, str]
) -> None:
    archive = env.root / "foreign.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, content in members.items():
            bundle.writestr(name, content)
    if "index/chroma.sqlite3" in members:
        with zipfile.ZipFile(archive, "a") as bundle:
            bundle.writestr("index/index_manifest.json", json.dumps({"entries": {}}))

    with pytest.raises(index_transfer.IndexTransferError) as error:
        index_transfer.inspect_archive(archive)
    assert error.value.code == "not_an_index"


@pytest.fixture
def pending_storage(workspace_tmp_dir: Path, monkeypatch: Any) -> Path:
    """Keep the pending-analysis file out of the real storage folder."""
    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    return workspace_tmp_dir / "storage"


def test_api_import_needs_the_token_from_inspect_and_a_confirmation(
    monkeypatch: Any, pending_storage: Path
) -> None:
    from app_api import app
    from src.api import application
    from src.api import index_transfer as api

    report = {"archive": {"path": "C:/exports/index.zip"}, "index": {}, "can_import": True}
    imported: list[tuple[str, bool]] = []

    def fake_import(path: str, *, adopt_settings: bool = True) -> dict[str, Any]:
        imported.append((path, adopt_settings))
        return {"active": True, "document_count": 4}

    monkeypatch.setattr(api, "choose_file", lambda **_kwargs: "C:/exports/index.zip")
    monkeypatch.setattr(index_transfer, "inspect_archive", lambda path: report)
    monkeypatch.setattr(index_transfer, "import_archive", fake_import)
    monkeypatch.setattr(application, "get_vector_database_status", lambda: {"status": "present"})
    client = TestClient(app)

    inspected = client.post("/api/index-transfer/inspect").json()
    token = inspected["token"]
    assert inspected["report"] == report

    unconfirmed = client.post("/api/index-transfer/import", json={"token": token})
    assert unconfirmed.status_code == 400
    unknown = client.post(
        "/api/index-transfer/import", json={"token": "unknown-token", "confirm": True}
    )
    assert unknown.status_code == 404

    done = client.post(
        "/api/index-transfer/import",
        json={"token": token, "confirm": True, "adopt_settings": False},
    )
    assert done.status_code == 200
    assert done.json()["active"] is True
    assert imported == [("C:/exports/index.zip", False)]
    replay = client.post("/api/index-transfer/import", json={"token": token, "confirm": True})
    assert replay.status_code == 404


def test_api_analysis_never_expires_and_survives_a_restart(
    monkeypatch: Any, pending_storage: Path
) -> None:
    from app_api import app
    from src.api import application
    from src.api import index_transfer as api

    report = {"archive": {"path": "C:/exports/index.zip"}, "index": {}, "can_import": True}
    monkeypatch.setattr(api, "choose_file", lambda **_kwargs: "C:/exports/index.zip")
    monkeypatch.setattr(index_transfer, "inspect_archive", lambda path: report)
    monkeypatch.setattr(
        index_transfer, "import_archive", lambda path, *, adopt_settings=True: {"active": True}
    )
    monkeypatch.setattr(application, "get_vector_database_status", lambda: {"status": "present"})
    client = TestClient(app)
    token = client.post("/api/index-transfer/inspect").json()["token"]

    # Nothing is held in memory: the analysis lives in a file, here dated a year back.
    pending_file = pending_storage / "index_transfer_pending.json"
    saved = json.loads(pending_file.read_text(encoding="utf-8"))
    saved[token]["at"] -= 365 * 24 * 3600
    pending_file.write_text(json.dumps(saved), encoding="utf-8")

    done = client.post("/api/index-transfer/import", json={"token": token, "confirm": True})
    assert done.status_code == 200
    assert token not in json.loads(pending_file.read_text(encoding="utf-8"))


def test_api_keeps_only_the_latest_analyses(monkeypatch: Any, pending_storage: Path) -> None:
    from app_api import app
    from src.api import index_transfer as api

    report = {"archive": {"path": "C:/exports/index.zip"}, "index": {}, "can_import": True}
    monkeypatch.setattr(api, "choose_file", lambda **_kwargs: "C:/exports/index.zip")
    monkeypatch.setattr(index_transfer, "inspect_archive", lambda path: report)
    client = TestClient(app)

    tokens = [client.post("/api/index-transfer/inspect").json()["token"] for _ in range(12)]

    saved = json.loads((pending_storage / "index_transfer_pending.json").read_text("utf-8"))
    assert len(saved) == 10
    assert tokens[-1] in saved


def test_api_never_downloads_a_model_without_confirmation(
    monkeypatch: Any, pending_storage: Path
) -> None:
    from app_api import app
    from src.api import index_transfer as api

    pulled: list[str] = []
    report = {"archive": {"path": "C:/exports/index.zip"}, "index": {}, "can_import": True}
    monkeypatch.setattr(api, "choose_file", lambda **_kwargs: "C:/exports/index.zip")
    monkeypatch.setattr(index_transfer, "inspect_archive", lambda path: report)
    monkeypatch.setattr(index_transfer, "read_index_info", lambda path: {})
    monkeypatch.setattr(
        index_transfer, "embedding_compatibility", lambda info: {"action": "download", "model_name": MODEL}
    )
    monkeypatch.setattr(index_transfer, "pull_embedding_model", pulled.append)
    monkeypatch.setattr(index_transfer, "finalize_report", lambda value: value)
    client = TestClient(app)
    token = client.post("/api/index-transfer/inspect").json()["token"]

    refused = client.post("/api/index-transfer/pull-model", json={"token": token})
    assert refused.status_code == 400
    assert pulled == []

    accepted = client.post("/api/index-transfer/pull-model", json={"token": token, "confirm": True})
    assert accepted.status_code == 200
    assert pulled == [MODEL]


# ------ Documents in the archive (format 2) ---------------------


def _export_with_documents(env: SimpleNamespace) -> Path:
    lexical = _build_index(env)
    try:
        archive = env.root / "transfer" / "index-with-pdfs.zip"
        result = index_transfer.export_active_index(archive, include_documents=True)
    finally:
        lexical.close()
    assert result["documents_included"] == 4
    return archive


def test_export_with_documents_stores_them_uncompressed_with_their_hash(
    env: SimpleNamespace,
) -> None:
    archive = _export_with_documents(env)

    with zipfile.ZipFile(archive) as bundle:
        header = json.loads(bundle.read(index_transfer.HEADER_NAME))
        info = bundle.getinfo("documents/a.pdf")
        stored = bundle.read("documents/a.pdf")

    original = (env.root / "pc1" / "pdfs" / "a.pdf").read_bytes()
    assert header["format_version"] == 2
    assert stored == original
    assert info.compress_type == zipfile.ZIP_STORED
    assert set(header["files"]) == {"a.pdf", "b.pdf", "c.pdf", "e.pdf"}
    assert header["files"]["a.pdf"]["bytes"] == len(original)
    assert _file_signature(str(env.root / "pc1" / "pdfs" / "a.pdf")).endswith(
        header["files"]["a.pdf"]["sha256"]
    )
    assert str(env.root) not in json.dumps(header)


def test_export_without_documents_stays_readable_by_older_versions(env: SimpleNamespace) -> None:
    lexical = _build_index(env)
    env.library.clear()  # no records either
    try:
        archive = env.root / "transfer" / "index.zip"
        index_transfer.export_active_index(archive)
    finally:
        lexical.close()
    with zipfile.ZipFile(archive) as bundle:
        header = json.loads(bundle.read(index_transfer.HEADER_NAME))
        names = bundle.namelist()
    assert header["format_version"] == 1
    assert "files" not in header
    assert not any(name.startswith("documents/") for name in names)


def test_export_estimate_counts_documents_found_here(env: SimpleNamespace) -> None:
    lexical = _build_index(env)
    lexical.close()
    (env.root / "pc1" / "pdfs" / "e.pdf").unlink()

    estimate = index_transfer.estimate_export()

    assert estimate["document_count"] == 4
    assert estimate["documents_found"] == 3
    expected = sum((env.root / "pc1" / "pdfs" / name).stat().st_size for name in ("a.pdf", "b.pdf", "c.pdf"))
    assert estimate["documents_bytes"] == expected
    assert estimate["index_bytes"] > 0


def test_inspect_counts_archived_documents_as_present(env: SimpleNamespace) -> None:
    archive = _export_with_documents(env)
    _move_to_second_computer(env)

    report = index_transfer.inspect_archive(archive)

    copy = report["documents_copy"]
    assert copy["mode"] == "data_path"
    assert copy["copy_count"] == 1, "only c.pdf is missing here"
    assert "rel_paths" not in copy
    documents = report["documents"]
    assert documents["missing_count"] == 0
    assert documents["copied_count"] == 1
    assert [row["rel_path"] for row in documents["content_changed"]] == ["b.pdf"]
    codes = {issue["code"] for issue in report["issues"]}
    assert "documents_copied" in codes
    assert "documents_missing" not in codes


def test_import_copies_missing_documents_and_never_overwrites(env: SimpleNamespace) -> None:
    archive = _export_with_documents(env)
    _move_to_second_computer(env)
    here = Path(config.DATA_PATH)
    edited = (here / "b.pdf").read_bytes()

    result = index_transfer.import_archive(archive)

    assert result["documents"]["copied"] == 1
    assert result["documents"]["data_path_set"] is False
    assert (here / "c.pdf").read_bytes() == (env.root / "pc1" / "pdfs" / "c.pdf").read_bytes()
    assert (here / "b.pdf").read_bytes() == edited, "the local version wins"
    assert not list(here.rglob("*" + index_transfer._PART_SUFFIX))
    # The copied file is the indexed one: the next reload does not re-embed it.
    manifest = json.loads((_profile_dir() / state._MANIFEST_FILE).read_text("utf-8"))
    assert _file_signature(str(here / "c.pdf")) == manifest["entries"]["c.pdf"]["file_sig"]


def test_import_on_a_computer_without_pdf_folder_creates_one(
    env: SimpleNamespace, monkeypatch: Any
) -> None:
    archive = _export_with_documents(env)
    env.use_computer("pc2")
    folder = env.root / "pc2" / "Documents" / index_transfer.DEFAULT_IMPORT_FOLDER_NAME
    saved: list[str] = []
    monkeypatch.setattr(index_transfer, "_data_path_configured", lambda: False)
    monkeypatch.setattr(index_transfer, "default_import_folder", lambda: str(folder))
    monkeypatch.setattr("src.app_settings.set_app_settings", lambda **kwargs: saved.append(kwargs["data_path"]))

    report = index_transfer.inspect_archive(archive)
    assert report["documents_copy"]["mode"] == "new_folder"
    assert report["documents_copy"]["set_data_path"] is True
    assert report["documents"]["missing_count"] == 0
    assert "data_path_missing" not in {issue["code"] for issue in report["issues"]}

    result = index_transfer.import_archive(archive)

    assert result["documents"] == {
        "copied": 4, "skipped": 0, "target": str(folder), "data_path_set": True,
    }
    assert sorted(path.name for path in folder.iterdir()) == ["a.pdf", "b.pdf", "c.pdf", "e.pdf"]
    assert config.DATA_PATH == str(folder)
    assert saved == [str(folder)]


def test_documents_are_not_copied_into_another_zotero_library(
    env: SimpleNamespace, monkeypatch: Any
) -> None:
    archive = _export_with_documents(env)
    _move_to_second_computer(env)  # another Zotero account
    monkeypatch.setattr(index_transfer, "is_zotero_storage_path", lambda data_path, db_path: True)

    report = index_transfer.inspect_archive(archive)
    assert report["documents_copy"]["mode"] == "zotero_other_library"
    assert report["documents_copy"]["copy_count"] == 0
    assert "documents_not_copied" in {issue["code"] for issue in report["issues"]}

    result = index_transfer.import_archive(archive)
    assert result["documents"]["copied"] == 0
    assert not (Path(config.DATA_PATH) / "c.pdf").exists()


def test_same_zotero_library_receives_the_missing_documents(
    env: SimpleNamespace, monkeypatch: Any
) -> None:
    archive = _export_with_documents(env)
    _move_to_second_computer(env)
    env.settings["account"] = "account-1"
    monkeypatch.setattr(index_transfer, "is_zotero_storage_path", lambda data_path, db_path: True)

    assert index_transfer.inspect_archive(archive)["documents_copy"]["mode"] == "zotero_storage"
    assert index_transfer.import_archive(archive)["documents"]["copied"] == 1
    assert (Path(config.DATA_PATH) / "c.pdf").is_file()


def _rewrite_archive(source: Path, target: Path, change: Any) -> None:
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as copy:
        members = {name: original.read(name) for name in original.namelist()}
        change(members)
        for name, content in members.items():
            copy.writestr(name, content)


def test_a_damaged_document_is_refused_and_leaves_nothing_behind(env: SimpleNamespace) -> None:
    archive = _export_with_documents(env)
    tampered = env.root / "transfer" / "tampered.zip"
    _rewrite_archive(archive, tampered, lambda members: members.update({"documents/c.pdf": b"%PDF-1.4\nother bytes"}))
    _move_to_second_computer(env)

    with pytest.raises(index_transfer.IndexTransferError) as error:
        index_transfer.import_archive(tampered)

    assert error.value.code == "corrupted_archive"
    here = Path(config.DATA_PATH)
    assert not (here / "c.pdf").exists()
    assert not list(here.rglob("*" + index_transfer._PART_SUFFIX))


@pytest.mark.parametrize(
    "change",
    [
        lambda members: members.update({"documents/unlisted.pdf": b"%PDF-1.4"}),
        lambda members: members.update({
            index_transfer.HEADER_NAME: json.dumps({
                **json.loads(members[index_transfer.HEADER_NAME]),
                "files": {"a.pdf": {"path": "../a.pdf", "bytes": 1, "sha256": "0" * 64}},
            }).encode()
        }),
    ],
)
def test_unannounced_or_unsafe_documents_are_rejected(env: SimpleNamespace, change: Any) -> None:
    archive = _export_with_documents(env)
    tampered = env.root / "transfer" / "tampered.zip"
    _rewrite_archive(archive, tampered, change)

    with pytest.raises(index_transfer.IndexTransferError) as error:
        index_transfer.inspect_archive(tampered)
    assert error.value.code == "corrupted_archive"


def test_api_export_includes_documents_unless_asked_not_to(
    monkeypatch: Any, pending_storage: Path
) -> None:
    from app_api import app
    from src.api import application
    from src.api import index_transfer as api

    calls: list[bool] = []

    def fake_export(path: str, *, include_documents: bool) -> dict[str, Any]:
        calls.append(include_documents)
        return {"archive": path, "bytes": 1}

    monkeypatch.setattr(api, "choose_save_file", lambda **_kwargs: "C:/exports/index.zip")
    monkeypatch.setattr(index_transfer, "export_active_index", fake_export)
    monkeypatch.setattr(application, "get_vector_database_status", lambda: {"profile_ready": True})
    client = TestClient(app)

    assert client.post("/api/index-transfer/export").status_code == 200
    assert client.post("/api/index-transfer/export", json={}).status_code == 200
    assert client.post("/api/index-transfer/export", json={"include_documents": False}).status_code == 200
    assert calls == [True, True, False]


# ------ Records snapshot (step 4) -------------------------------


def _no_metadata_here(monkeypatch: Any) -> None:
    monkeypatch.setattr(index_transfer, "_metadata_library", lambda: ("none", "", {}))


def test_export_carries_the_records_of_indexed_documents(env: SimpleNamespace) -> None:
    archive = _export(env)
    with zipfile.ZipFile(archive) as bundle:
        header = json.loads(bundle.read(index_transfer.HEADER_NAME))
        records = json.loads(bundle.read("metadata.json"))
    assert header["format_version"] == 2
    assert header["metadata"] == {"count": 1}
    assert records["format"] == "bibliobrain-metadata"
    assert records["items"]["a.pdf"]["title"] == "Article A"
    assert records["items"]["a.pdf"]["item_key"] == "AAAA1111"


def test_records_replace_a_missing_metadata_source_without_reindexing(
    env: SimpleNamespace, monkeypatch: Any
) -> None:
    archive = _export(env)
    _move_to_second_computer(env)
    _no_metadata_here(monkeypatch)

    report = index_transfer.inspect_archive(archive)

    assert report["records"] == {"count": 1, "use": True}
    assert report["documents"]["metadata_changed_count"] == 0, "a.pdf keeps its record"
    codes = {issue["code"] for issue in report["issues"]}
    assert "records_used" in codes
    assert "metadata_source" not in codes


def test_import_installs_the_records_as_a_file_source(
    env: SimpleNamespace, monkeypatch: Any
) -> None:
    from src.rag.zotero_integration import _zotero_signature
    from src.zotero_db import is_bibtex_source, is_metadata_snapshot, load_zotero_library

    archive = _export(env)
    original = _zotero_signature(env.library["a.pdf"])
    _move_to_second_computer(env)
    _no_metadata_here(monkeypatch)
    monkeypatch.setattr(config, "STORAGE_DIR", str(env.root / "pc2" / "storage"))
    monkeypatch.setattr(config, "ZOTERO_DB_PATH", "")
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr("src.app_settings.set_app_settings", lambda **kwargs: saved.append(kwargs))

    result = index_transfer.import_archive(archive)

    path = result["records"]["path"]
    assert result["records"]["used"] is True
    assert Path(path).parent == env.root / "pc2" / "storage" / "imported_metadata"
    assert saved == [{"metadata_source": "bibtex", "zotero_path": path}]
    assert config.ZOTERO_DB_PATH == path
    assert is_metadata_snapshot(path) and is_bibtex_source(path)
    load_zotero_library.cache_clear()
    library = load_zotero_library(path)
    assert _zotero_signature(library["a.pdf"]) == original


def test_records_never_replace_a_metadata_source_of_this_computer(
    env: SimpleNamespace, monkeypatch: Any
) -> None:
    archive = _export(env)
    _move_to_second_computer(env)  # has its own Zotero library
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr("src.app_settings.set_app_settings", lambda **kwargs: saved.append(kwargs))

    assert index_transfer.inspect_archive(archive)["records"]["use"] is False
    assert index_transfer.import_archive(archive)["records"]["used"] is False
    assert saved == []


def test_unreadable_records_reject_the_archive(env: SimpleNamespace) -> None:
    archive = _export(env)
    tampered = env.root / "transfer" / "tampered.zip"
    _rewrite_archive(archive, tampered, lambda members: members.update({"metadata.json": b'{"format": "other"}'}))
    with pytest.raises(index_transfer.IndexTransferError) as error:
        index_transfer.inspect_archive(tampered)
    assert error.value.code == "corrupted_archive"


def test_infer_metadata_source_treats_records_as_a_file() -> None:
    from src.app_settings import _infer_metadata_source

    assert _infer_metadata_source("C:/x/imported_metadata/notices-1.json") == "bibtex"


# ------ Progress (step 3) ---------------------------------------


def _record_progress(monkeypatch: Any) -> list[tuple[str, int, int]]:
    updates: list[tuple[str, int, int]] = []
    original = index_transfer._progress_update

    def spy(phase: str, done: int, total: int, label: str = "") -> None:
        updates.append((phase, int(done), int(total)))
        original(phase, done, total, label)

    monkeypatch.setattr(index_transfer, "_progress_update", spy)
    return updates


def test_export_and_import_publish_their_progress(env: SimpleNamespace, monkeypatch: Any) -> None:
    updates = _record_progress(monkeypatch)
    archive = _export_with_documents(env)
    export_updates = [update for update in updates if update[0] == "export"]
    assert export_updates[-1][1] == export_updates[-1][2] > 0, "export ends at 100 %"
    assert index_transfer.transfer_progress() == {"active": False}

    _move_to_second_computer(env)
    updates.clear()
    index_transfer.import_archive(archive)
    phases = [phase for phase, _done, _total in updates]
    assert "copy" in phases and "install" in phases
    assert phases.index("copy") < phases.index("install")
    assert index_transfer.transfer_progress() == {"active": False}


def test_model_download_reports_bytes_of_all_layers(monkeypatch: Any) -> None:
    updates = _record_progress(monkeypatch)
    events = [
        {"status": "pulling manifest"},
        {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 40},
        {"status": "pulling b", "digest": "sha256:b", "total": 300, "completed": 0},
        {"status": "pulling a", "digest": "sha256:a", "total": 100, "completed": 100},
        {"status": "pulling b", "digest": "sha256:b", "total": 300, "completed": 300},
        {"status": "success"},
    ]
    monkeypatch.setattr("src.llm_provider._embedding_ollama_base_url", lambda: "http://localhost:11434")
    monkeypatch.setattr(index_transfer, "_pull_events", lambda base, name: iter(events))

    index_transfer.pull_embedding_model(MODEL)

    assert ("download", 40, 100) in updates
    assert ("download", 100, 400) in updates
    assert updates[-1] == ("download", 400, 400)
    assert index_transfer.transfer_progress() == {"active": False}


@pytest.mark.parametrize(
    "events",
    [[{"status": "pulling manifest"}, {"error": "pull model manifest: file does not exist"}], [{"status": "pulling manifest"}]],
)
def test_failed_or_interrupted_downloads_are_errors(monkeypatch: Any, events: list[dict[str, Any]]) -> None:
    monkeypatch.setattr("src.llm_provider._embedding_ollama_base_url", lambda: "http://localhost:11434")
    monkeypatch.setattr(index_transfer, "_pull_events", lambda base, name: iter(events))
    with pytest.raises(index_transfer.IndexTransferError) as error:
        index_transfer.pull_embedding_model(MODEL)
    assert error.value.code == "model_download_failed"
    assert index_transfer.transfer_progress() == {"active": False}


def test_api_progress_is_local_only(monkeypatch: Any) -> None:
    from app_api import app

    client = TestClient(app)
    assert client.get("/api/index-transfer/progress").json() == {"ok": True, "active": False}