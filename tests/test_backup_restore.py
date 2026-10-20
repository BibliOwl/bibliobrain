from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.backup_restore import BackupError, create_backup, restore_backup


def _write_configuration(path: Path, settings: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "settings": settings}),
        encoding="utf-8",
    )


def test_backup_restore_round_trip_includes_server_draft_and_skips_runtime_pids(
    workspace_tmp_dir: Path,
) -> None:
    local_app_data = workspace_tmp_dir / "local"
    fixed_root = local_app_data / "bibliobrain"
    home = workspace_tmp_dir / "custom-home"
    storage = home / "storage"
    external_documents = workspace_tmp_dir / "documents"
    configuration = fixed_root / "configuration.json"
    _write_configuration(
        configuration,
        {
            "BIBLIOBRAIN_HOME": str(home),
            "STORAGE_DIR": str(storage),
            "DATA_PATH": str(external_documents),
        },
    )
    storage.mkdir(parents=True)
    (storage / "drafting_document.json").write_text('{"html":"brouillon"}', encoding="utf-8")
    (storage / "chat_history.sqlite3").write_bytes(b"chat-history")
    runtime = home / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "server.pid").write_text("123", encoding="ascii")
    external_documents.mkdir()
    (external_documents / "article.pdf").write_bytes(b"pdf")

    archive = workspace_tmp_dir / "backup.zip"
    result = create_backup(archive, local_app_data=local_app_data, environment={})

    assert result["ok"] is True
    assert result["external_documents_included"] is False
    with zipfile.ZipFile(archive) as backup_zip:
        names = backup_zip.namelist()
        manifest = json.loads(backup_zip.read("manifest.json"))
    assert manifest["draft_document_included"] is True
    assert any(name.endswith("/drafting_document.json") for name in names)
    assert not any(name.endswith("/server.pid") for name in names)
    assert not any(name.endswith("/article.pdf") for name in names)

    (storage / "drafting_document.json").write_text("damaged", encoding="utf-8")
    _write_configuration(
        configuration,
        {
            "BIBLIOBRAIN_HOME": str(home),
            "STORAGE_DIR": str(storage),
            "DATA_PATH": str(external_documents),
            "BIBLIOBRAIN_LOG_LEVEL": "DEBUG",
        },
    )
    restored = restore_backup(
        archive,
        confirm=True,
        local_app_data=local_app_data,
        environment={},
    )

    assert restored["restored_files"] >= 3
    assert Path(restored["rollback_archive"]).is_file()
    restored_configuration = json.loads(configuration.read_text(encoding="utf-8"))
    assert restored_configuration["version"] == 1
    assert "BIBLIOBRAIN_LOG_LEVEL" not in restored_configuration["settings"]
    assert json.loads((storage / "drafting_document.json").read_text(encoding="utf-8")) == {
        "html": "brouillon"
    }
    assert (storage / "chat_history.sqlite3").read_bytes() == b"chat-history"

    with pytest.raises(BackupError, match="existe déjà"):
        create_backup(archive, local_app_data=local_app_data, environment={})


def test_backup_can_include_external_documents(workspace_tmp_dir: Path) -> None:
    local_app_data = workspace_tmp_dir / "local"
    fixed_root = local_app_data / "bibliobrain"
    external_documents = workspace_tmp_dir / "documents"
    external_documents.mkdir()
    (external_documents / "article.pdf").write_bytes(b"pdf")
    _write_configuration(
        fixed_root / "configuration.json",
        {"DATA_PATH": str(external_documents)},
    )

    archive = workspace_tmp_dir / "with-documents.zip"
    create_backup(
        archive,
        include_external_documents=True,
        local_app_data=local_app_data,
        environment={},
    )

    with zipfile.ZipFile(archive) as backup_zip:
        assert any(name.endswith("/article.pdf") for name in backup_zip.namelist())


def test_restore_rejects_unconfirmed_or_traversing_archive(workspace_tmp_dir: Path) -> None:
    archive = workspace_tmp_dir / "malicious.zip"
    manifest = {
        "format": "bibliobrain-backup",
        "format_version": 1,
        "sources": [],
    }
    with zipfile.ZipFile(archive, "w") as backup_zip:
        backup_zip.writestr("manifest.json", json.dumps(manifest))
        backup_zip.writestr("../outside.txt", "no")

    with pytest.raises(BackupError, match="confirmation"):
        restore_backup(archive, local_app_data=workspace_tmp_dir / "local", environment={})
    with pytest.raises(BackupError, match="dangereuse"):
        restore_backup(
            archive,
            confirm=True,
            create_rollback=False,
            local_app_data=workspace_tmp_dir / "local",
            environment={},
        )
