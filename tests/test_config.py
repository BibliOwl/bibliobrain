from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any, cast

import src.config as config


def _reload_config() -> Any:
    return cast(Any, importlib.reload(config))


def test_config_uses_bibliobrain_home_for_default_paths(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_home = workspace_tmp_dir / "bibliobrain-home"
    monkeypatch.setenv("LOCALAPPDATA", str(workspace_tmp_dir / "local-app-data"))
    monkeypatch.setenv("BIBLIOBRAIN_HOME", str(app_home))
    monkeypatch.setenv("DATA_PATH", "")
    monkeypatch.setenv("STORAGE_DIR", "")
    monkeypatch.setenv("CHROMA_PATH", "")

    cfg = _reload_config()

    assert cfg.BIBLIOBRAIN_HOME == str(app_home)
    assert cfg.DATA_PATH == str(app_home / "data")
    assert cfg.STORAGE_DIR == str(app_home / "storage")
    assert cfg.CHROMA_PATH == str(app_home / "chroma")


def test_config_prefers_legacy_chroma_path_when_present(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    app_home = workspace_tmp_dir / "bibliobrain-home"
    legacy_chroma = app_home / "chroma_db_zotero"
    legacy_chroma.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("LOCALAPPDATA", str(workspace_tmp_dir / "local-app-data"))
    monkeypatch.setenv("BIBLIOBRAIN_HOME", str(app_home))
    monkeypatch.setenv("DATA_PATH", "")
    monkeypatch.setenv("STORAGE_DIR", "")
    monkeypatch.setenv("CHROMA_PATH", "")

    cfg = _reload_config()

    assert cfg.CHROMA_PATH == str(legacy_chroma)


def test_config_loads_per_user_json_when_process_values_are_blank(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    configuration_file = workspace_tmp_dir / "bibliobrain" / "configuration.json"
    configuration_file.parent.mkdir(parents=True)
    configuration_file.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {
                    "BIBLIOBRAIN_HOME": str(workspace_tmp_dir / "configured-home"),
                    "OLLAMA_BASE_URL": "http://127.0.0.1:22434",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(workspace_tmp_dir))
    monkeypatch.setenv("BIBLIOBRAIN_HOME", "")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")

    cfg = _reload_config()

    assert cfg._LOADED_CONFIGURATION_PATH == configuration_file.resolve()
    assert cfg.BIBLIOBRAIN_HOME == str(workspace_tmp_dir / "configured-home")
    assert os.environ["OLLAMA_BASE_URL"] == "http://127.0.0.1:22434"


def test_config_does_not_load_legacy_dotenv(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    (workspace_tmp_dir / ".env").write_text(
        "BIBLIOBRAIN_HOME=C:/must-not-be-used\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(workspace_tmp_dir)
    monkeypatch.setenv("LOCALAPPDATA", str(workspace_tmp_dir / "local"))
    monkeypatch.setenv("BIBLIOBRAIN_HOME", "")

    cfg = _reload_config()

    assert cfg.BIBLIOBRAIN_HOME == str(workspace_tmp_dir / "local" / "bibliobrain")
