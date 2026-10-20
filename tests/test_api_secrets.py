from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import src.api_secrets as api_secrets


def _install_test_protector(monkeypatch: Any) -> None:
    monkeypatch.setattr(api_secrets, "secure_storage_available", lambda: True)
    monkeypatch.setattr(api_secrets, "_protect", lambda value: b"protected:" + value[::-1])

    def unprotect(value: bytes) -> bytes:
        assert value.startswith(b"protected:")
        return value.removeprefix(b"protected:")[::-1]

    monkeypatch.setattr(api_secrets, "_unprotect", unprotect)


def test_api_secret_store_encrypts_values_and_never_returns_them(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    _install_test_protector(monkeypatch)
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    api_secrets._ENV_BASELINES.clear()

    api_secrets.update_api_config(
        workspace_tmp_dir,
        updates={"openalex_api_key": "oa-super-secret"},
    )

    store_path = workspace_tmp_dir / "api_secrets.dpapi.json"
    raw_store = store_path.read_text(encoding="utf-8")
    status = api_secrets.api_config_status(workspace_tmp_dir)

    assert "oa-super-secret" not in raw_store
    assert "oa-super-secret" not in json.dumps(status)
    assert status["secrets"]["openalex_api_key"] == {
        "configured": True,
        "source": "secure_store",
    }
    assert os.environ["OPENALEX_API_KEY"] == "oa-super-secret"


def test_extended_secrets_are_supported_by_the_encrypted_store(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    _install_test_protector(monkeypatch)
    for environment_name in ("OLLAMA_AUTH_HEADER", "HF_TOKEN", "BIBLIOBRAIN_BUGSINK_DSN"):
        monkeypatch.delenv(environment_name, raising=False)
    api_secrets._ENV_BASELINES.clear()

    api_secrets.update_api_config(
        workspace_tmp_dir,
        updates={
            "ollama_auth_header": "Basic opaque-value",
            "huggingface_token": "hf-secret",
            "bugsink_dsn": "https://secret@example.test/1",
        },
    )

    status = api_secrets.api_config_status(workspace_tmp_dir)
    serialized = json.dumps(status)
    assert "opaque-value" not in serialized
    assert "hf-secret" not in serialized
    assert "secret@example" not in serialized
    assert status["secrets"]["ollama_auth_header"]["source"] == "secure_store"
    assert status["secrets"]["huggingface_token"]["source"] == "secure_store"
    assert status["secrets"]["bugsink_dsn"]["source"] == "secure_store"


def test_clearing_secure_value_restores_environment_fallback(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    _install_test_protector(monkeypatch)
    monkeypatch.setenv("OPENALEX_API_KEY", "env-fallback")
    api_secrets._ENV_BASELINES.clear()

    api_secrets.update_api_config(
        workspace_tmp_dir,
        updates={"openalex_api_key": "stored-secret"},
    )
    api_secrets.update_api_config(
        workspace_tmp_dir,
        clear=["openalex_api_key"],
    )

    status = api_secrets.api_config_status(workspace_tmp_dir)
    assert os.environ["OPENALEX_API_KEY"] == "env-fallback"
    assert status["secrets"]["openalex_api_key"] == {
        "configured": True,
        "source": "environment",
    }


def test_remote_url_status_strips_embedded_credentials(
    workspace_tmp_dir: Path,
    monkeypatch: Any,
) -> None:
    _install_test_protector(monkeypatch)
    api_secrets._ENV_BASELINES.clear()

    api_secrets.update_api_config(
        workspace_tmp_dir,
        updates={
            "remote_llm_base_url": "https://user:password@example.com/v1",
        },
    )

    status = api_secrets.api_config_status(workspace_tmp_dir)
    assert status["remote_llm_base_url"]["value"] == "https://example.com/v1"
    assert "password" not in json.dumps(status)
