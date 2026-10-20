import io
import json
import os
from pathlib import Path
from types import SimpleNamespace

from src import ollama_utils


def test_find_ollama_models_directory_recovers_custom_path_from_logs(
    workspace_tmp_dir: Path,
) -> None:
    models_directory = workspace_tmp_dir / "custom models"
    manifest = (
        models_directory
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / "qwen3.5"
        / "4b"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    log_directory = workspace_tmp_dir / "logs"
    log_directory.mkdir()
    (log_directory / "server.log").write_text(
        "server config env=\"map[OLLAMA_MODELS:"
        f"{models_directory} OLLAMA_NOHISTORY:false]\"\n",
        encoding="utf-8",
    )

    recovered = ollama_utils._find_ollama_models_directory_from_logs(log_directory)

    assert recovered == str(models_directory)


def test_is_local_ollama_url_accepts_only_loopback_hosts() -> None:
    assert ollama_utils.is_local_ollama_url("http://localhost:11434") is True
    assert ollama_utils.is_local_ollama_url("http://127.0.0.1:11434") is True
    assert ollama_utils.is_local_ollama_url("http://[::1]:11434") is True
    assert ollama_utils.is_local_ollama_url("https://ollama.example.org") is False
    assert ollama_utils.is_local_ollama_url("http://user@localhost:11434") is False


def test_start_ollama_server_uses_argument_list_without_shell(monkeypatch) -> None:
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return SimpleNamespace(pid=456)

    monkeypatch.setattr(ollama_utils, "find_ollama_executable", lambda: "C:/Ollama/ollama.exe")
    monkeypatch.setattr(
        ollama_utils,
        "find_ollama_models_directory",
        lambda: "X:/Documents/.ollama/models",
    )
    monkeypatch.setattr(ollama_utils.subprocess, "Popen", fake_popen)

    result = ollama_utils.start_ollama_server()

    assert result["ok"] is True
    assert result["started"] is True
    assert result["pid"] == 456
    assert captured["args"] == ["C:/Ollama/ollama.exe", "serve"]
    assert "shell" not in captured["kwargs"]
    assert (
        captured["kwargs"]["env"]["OLLAMA_MODELS"]
        == "X:/Documents/.ollama/models"
    )
    if os.name == "nt":
        assert captured["kwargs"]["creationflags"] & ollama_utils.subprocess.CREATE_NO_WINDOW
        assert "startupinfo" in captured["kwargs"]
        assert (
            captured["kwargs"]["startupinfo"].dwFlags
            & ollama_utils.subprocess.STARTF_USESHOWWINDOW
        )
        assert (
            captured["kwargs"]["startupinfo"].wShowWindow
            == ollama_utils.subprocess.SW_HIDE
        )


def test_start_ollama_server_reports_missing_installation(monkeypatch) -> None:
    monkeypatch.setattr(ollama_utils, "find_ollama_executable", lambda: None)

    result = ollama_utils.start_ollama_server()

    assert result["ok"] is False
    assert result["code"] == "not_installed"


def test_runtime_status_reports_active_and_model_context(monkeypatch) -> None:
    monkeypatch.setattr(
        ollama_utils,
        "_read_ollama_json",
        lambda *_args, **_kwargs: {
            "models": [{"name": "demo:latest", "context_length": 32768}]
        },
    )

    def fake_open(*_args, **_kwargs):
        return io.BytesIO(
            json.dumps({"model_info": {"demo.context_length": 131072}}).encode()
        )

    monkeypatch.setattr(ollama_utils, "_open_ollama_url", fake_open)

    payload = ollama_utils.fetch_ollama_runtime_status(
        "http://localhost:11434",
        "demo:latest",
    )

    assert payload["model_loaded"] is True
    assert payload["active_context_length"] == 32768
    assert payload["model_context_length"] == 131072
