from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

import src.config as config


def _load_rag_settings_module(storage_dir: Path) -> Any:
    """Recharge le module avec un STORAGE_DIR local au test pour isoler le fichier JSON."""
    config.STORAGE_DIR = str(storage_dir)
    module = cast(Any, importlib.import_module("src.rag_settings"))
    return cast(Any, importlib.reload(module))


def test_rag_settings_persists_normalized_chunk_size(workspace_tmp_dir: Path) -> None:
    """La taille de chunk doit etre normalisee puis relue depuis le stockage local."""
    rag_settings = _load_rag_settings_module(workspace_tmp_dir)

    assert rag_settings.set_persisted_chunk_size("2048") == 2048
    assert rag_settings.get_persisted_chunk_size() == 2048


def test_rag_settings_rejects_invalid_chunk_size(workspace_tmp_dir: Path) -> None:
    """Une valeur invalide doit retomber sur la valeur par defaut au lieu de casser la lecture."""
    rag_settings = _load_rag_settings_module(workspace_tmp_dir)

    assert rag_settings.set_persisted_chunk_size("oops") == 1000
    assert rag_settings.get_persisted_chunk_size() == 1000


def test_rag_settings_falls_back_to_memory_when_file_write_is_denied(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    """Un refus d'acces Windows sur rag_settings.json ne doit pas casser l'initialisation RAG."""
    rag_settings = _load_rag_settings_module(workspace_tmp_dir)

    def denied_write_text(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "write_text", denied_write_text)

    assert rag_settings.set_persisted_chunk_size("1536") == 1536
    assert rag_settings.get_persisted_chunk_size() == 1536
