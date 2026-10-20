from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from src.machine_config import (
    MachineConfigError,
    activate_machine_settings,
    load_machine_settings,
    save_machine_settings,
)


def _migration_module() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts" / "migrate-env-config.py"
    spec = importlib.util.spec_from_file_location("migrate_env_config", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_machine_settings_are_atomic_and_ignore_secrets(
    workspace_tmp_dir: Path,
) -> None:
    path = workspace_tmp_dir / "configuration.json"

    save_machine_settings(
        {
            "OLLAMA_BASE_URL": "http://127.0.0.1:22434",
            "REMOTE_LLM_API_KEY": "must-not-be-written",
        },
        path=path,
    )

    raw = path.read_text(encoding="utf-8")
    assert "must-not-be-written" not in raw
    assert load_machine_settings(path) == {"OLLAMA_BASE_URL": "http://127.0.0.1:22434"}


def test_machine_settings_do_not_override_explicit_process_values(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    path = workspace_tmp_dir / "configuration.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {"OLLAMA_BASE_URL": "http://json.example:11434"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://process.example:11434")

    activate_machine_settings(path)

    assert __import__("os").environ["OLLAMA_BASE_URL"] == "http://process.example:11434"


def test_a_persisted_version_override_is_ignored(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    # Migrated from the .env template of 0.6.x, it froze the reported version.
    path = workspace_tmp_dir / "configuration.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {"BIBLIOBRAIN_VERSION": "0.6.6", "RERANKER_DEVICE": "cpu"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("BIBLIOBRAIN_VERSION", raising=False)

    assert load_machine_settings(path) == {"RERANKER_DEVICE": "cpu"}
    save_machine_settings({"BIBLIOBRAIN_VERSION": "0.6.6"}, path=path)
    assert "BIBLIOBRAIN_VERSION" not in path.read_text(encoding="utf-8")


def test_legacy_migration_skips_the_template_version(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    legacy_file = workspace_tmp_dir / ".env"
    legacy_file.write_text(
        "BIBLIOBRAIN_VERSION=0.6.6\nRERANKER_DEVICE=cpu\n",
        encoding="utf-8",
    )
    migration = _migration_module()
    saved_updates: list[dict[str, str]] = []

    def save_machine_settings(updates: dict[str, str]) -> Path:
        saved_updates.append(dict(updates))
        return workspace_tmp_dir / "configuration.json"

    monkeypatch.setattr(migration, "load_machine_settings", lambda: {})
    monkeypatch.setattr(migration, "save_machine_settings", save_machine_settings)
    monkeypatch.setattr(
        migration,
        "api_config_status",
        lambda storage_dir: {"secrets": {}, "remote_llm_base_url": {"source": None}},
    )

    migration.migrate(legacy_file)

    assert saved_updates == [{"RERANKER_DEVICE": "cpu"}]


def test_machine_settings_reject_unknown_versions(workspace_tmp_dir: Path) -> None:
    path = workspace_tmp_dir / "configuration.json"
    path.write_text('{"version": 999, "settings": {}}', encoding="utf-8")

    with pytest.raises(MachineConfigError):
        load_machine_settings(path)


def test_legacy_migration_parser_separates_secrets(
    workspace_tmp_dir: Path,
) -> None:
    legacy_file = workspace_tmp_dir / ".env"
    legacy_file.write_text(
        "OLLAMA_BASE_URL=http://127.0.0.1:11434\n"
        "OPENALEX_API_KEY=secret-openalex\n"
        "HF_TOKEN='secret-hf'\n",
        encoding="utf-8",
    )
    migration = _migration_module()

    values = migration._parse_legacy_env(legacy_file)
    secrets = migration._secret_updates(values)

    assert values["OLLAMA_BASE_URL"] == "http://127.0.0.1:11434"
    assert secrets == {
        "openalex_api_key": "secret-openalex",
        "huggingface_token": "secret-hf",
    }


def test_legacy_migration_only_fills_values_that_are_not_already_persisted(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    legacy_file = workspace_tmp_dir / ".env"
    legacy_file.write_text(
        "BIBLIOBRAIN_HOME=C:/legacy-home\n"
        "OLLAMA_BASE_URL=http://legacy.example:11434\n"
        "RERANKER_ENABLED=false\n"
        "OPENALEX_API_KEY=legacy-openalex\n"
        "HF_TOKEN=legacy-hf\n"
        "REMOTE_LLM_BASE_URL=https://legacy-llm.example/v1\n",
        encoding="utf-8",
    )
    migration = _migration_module()
    saved_updates: list[dict[str, str]] = []
    secret_updates: list[dict[str, str]] = []

    def save_machine_settings(updates: dict[str, str]) -> Path:
        saved_updates.append(dict(updates))
        return workspace_tmp_dir / "configuration.json"

    monkeypatch.setattr(
        migration,
        "load_machine_settings",
        lambda: {
            "BIBLIOBRAIN_HOME": "C:/current-home",
            "OLLAMA_BASE_URL": "http://current.example:11434",
        },
    )
    monkeypatch.setattr(
        migration,
        "save_machine_settings",
        save_machine_settings,
    )
    monkeypatch.setattr(
        migration,
        "api_config_status",
        lambda storage_dir: {
            "secrets": {
                key: {
                    "configured": key == "openalex_api_key",
                    "source": "secure_store" if key == "openalex_api_key" else None,
                }
                for key in migration.SECRET_DEFINITIONS
            },
            "remote_llm_base_url": {
                "configured": True,
                "source": "secure_store",
                "value": "https://current-llm.example/v1",
            },
        },
    )
    monkeypatch.setattr(
        migration,
        "update_api_config",
        lambda storage_dir, *, updates: secret_updates.append(dict(updates)),
    )

    config_path, storage_dir, public_count, secret_count = migration.migrate(legacy_file)

    assert config_path == workspace_tmp_dir / "configuration.json"
    assert storage_dir == Path("C:/current-home/storage")
    assert saved_updates == [{"RERANKER_ENABLED": "false"}]
    assert secret_updates == [{"huggingface_token": "legacy-hf"}]
    assert public_count == 1
    assert secret_count == 1
