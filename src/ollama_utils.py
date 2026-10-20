import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from src.llm_provider import (
    _format_ollama_access_error,
    _open_ollama_url,
    _read_ollama_json,
)
from src.llm_provider import (
    fetch_gpu_status as _fetch_gpu_status,
)
from src.llm_provider import (
    list_available_models as _list_available_models,
)

_logger = logging.getLogger(__name__)

OLLAMA_DEFAULT_URL = "http://localhost:11434"


def _has_ollama_model_manifests(models_directory: Path) -> bool:
    manifest_root = models_directory / "manifests"
    if not manifest_root.is_dir():
        return False
    try:
        return any(path.is_file() for path in manifest_root.rglob("*"))
    except OSError:
        return False


def _find_ollama_models_directory_from_logs(log_directory: Path) -> str | None:
    """Recover Ollama's last custom model directory from its Windows logs."""
    try:
        log_paths = sorted(
            log_directory.glob("server*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    for log_path in log_paths:
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                header = handle.read(65536)
        except OSError:
            continue
        match = re.search(r"OLLAMA_MODELS:(.*?)\s+OLLAMA_NOHISTORY:", header)
        if not match:
            continue
        candidate = Path(match.group(1).strip().strip('"'))
        if _has_ollama_model_manifests(candidate):
            return str(candidate)
    return None


def find_ollama_models_directory() -> str | None:
    """Return the configured or last known populated Ollama model directory."""
    configured = os.getenv("OLLAMA_MODELS", "").strip()
    if configured:
        return configured

    default_directory = Path.home() / ".ollama" / "models"
    if _has_ollama_model_manifests(default_directory):
        return str(default_directory)

    if os.name == "nt":
        local_app_data = os.getenv("LOCALAPPDATA", "").strip()
        if local_app_data:
            recovered = _find_ollama_models_directory_from_logs(
                Path(local_app_data) / "Ollama"
            )
            if recovered:
                return recovered
    return None


def is_local_ollama_url(ollama_url: str = OLLAMA_DEFAULT_URL) -> bool:
    """Return True only for an HTTP(S) Ollama endpoint on this machine."""
    try:
        parsed = urlsplit((ollama_url or OLLAMA_DEFAULT_URL).strip())
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
    )


def find_ollama_executable() -> str | None:
    """Find the Ollama CLI without invoking a shell."""
    from_path = shutil.which("ollama")
    if from_path:
        return from_path

    candidates: list[Path] = []
    local_app_data = os.getenv("LOCALAPPDATA", "").strip()
    if local_app_data:
        base = Path(local_app_data)
        candidates.extend(
            (
                base / "Programs" / "Ollama" / "ollama.exe",
                base / "Ollama" / "ollama.exe",
            )
        )
    for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.getenv(env_name, "").strip()
        if root:
            candidates.append(Path(root) / "Ollama" / "ollama.exe")

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def start_ollama_server() -> dict:
    """Start ``ollama serve`` in the background and return a UI-safe result."""
    executable = find_ollama_executable()
    if not executable:
        return {
            "ok": False,
            "started": False,
            "code": "not_installed",
            "msg": "Ollama n'est pas installé ou son exécutable est introuvable.",
        }

    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        # CREATE_NO_WINDOW prevents Ollama and its console children from
        # briefly flashing terminal windows during hardware discovery.  Do
        # not combine it with DETACHED_PROCESS: Windows documents that
        # CREATE_NO_WINDOW is ignored for detached processes.
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        popen_kwargs["startupinfo"] = startupinfo
    else:
        popen_kwargs["start_new_session"] = True

    models_directory = find_ollama_models_directory()
    if models_directory:
        process_env = os.environ.copy()
        process_env["OLLAMA_MODELS"] = models_directory
        popen_kwargs["env"] = process_env

    try:
        process = subprocess.Popen([executable, "serve"], **popen_kwargs)
    except OSError as exc:
        return {
            "ok": False,
            "started": False,
            "code": "start_failed",
            "msg": f"Impossible de démarrer Ollama : {exc}",
        }

    return {
        "ok": True,
        "started": True,
        "code": "started",
        "pid": process.pid,
        "msg": "Démarrage d'Ollama demandé.",
    }


def fetch_ollama_models(
    ollama_url: str | None = None,
    *,
    force_refresh: bool = False,
) -> tuple[list[str], str]:
    """
    Compat layer historique:
    - mode local: renvoie les modeles Ollama
    - mode distant: renvoie les modeles exposes par l'API distante
    """
    return _list_available_models(ollama_url, force_refresh=force_refresh)


def fetch_gpu_status(ollama_url: str = OLLAMA_DEFAULT_URL) -> str:
    """
    Compat layer historique:
    - mode local: statut GPU Ollama
    - mode distant: message explicite "non disponible"
    """
    return _fetch_gpu_status(ollama_url)


def fetch_ollama_runtime_status(
    ollama_url: str = OLLAMA_DEFAULT_URL,
    model_name: str = "",
) -> dict:
    """
    Returns runtime status from Ollama /api/ps.
    Useful to indicate whether a model is already loaded in memory.
    """
    target = (ollama_url or OLLAMA_DEFAULT_URL).rstrip("/")
    wanted = (model_name or "").strip()

    def _normalize(name: str) -> str:
        return str(name or "").split(":", 1)[0].strip().lower()

    try:
        data = _read_ollama_json(target, "/api/ps", timeout=4)
    except Exception as exc:
        _logger.debug("Recovered from an error in fetch_ollama_runtime_status.", exc_info=True)
        return {
            "ok": False,
            "model_loaded": False,
            "running_models": [],
            "msg": _format_ollama_access_error(exc, path="/api/ps").replace(
                "Ollama inaccessible",
                "Statut Ollama indisponible",
                1,
            ),
        }

    raw = data.get("models", [])
    running = [
        str(model.get("name", "")).strip()
        for model in raw
        if isinstance(model, dict) and str(model.get("name", "")).strip()
    ]
    running = list(dict.fromkeys(running))

    wanted_norm = _normalize(wanted)
    loaded = bool(wanted_norm and any(_normalize(name) == wanted_norm for name in running))
    active_context_length = None
    for model in raw:
        if not isinstance(model, dict):
            continue
        if wanted_norm and _normalize(model.get("name", "")) != wanted_norm:
            continue
        try:
            active_context_length = int(model.get("context_length") or 0) or None
        except Exception:
            _logger.debug("Recovered from an error in fetch_ollama_runtime_status.", exc_info=True)
            active_context_length = None
        if active_context_length:
            break

    model_context_length = None
    if wanted:
        try:
            with _open_ollama_url(
                target + "/api/show",
                method="POST",
                json_payload={"model": wanted},
                timeout=4,
            ) as response:
                show_payload = json.loads(response.read() or b"{}")
            model_info = show_payload.get("model_info", {})
            context_values = []
            if isinstance(model_info, dict):
                for key, value in model_info.items():
                    if not str(key).endswith(".context_length"):
                        continue
                    try:
                        parsed = int(value)
                    except (TypeError, ValueError):
                        continue
                    if parsed > 0:
                        context_values.append(parsed)
            if context_values:
                model_context_length = max(context_values)
        except Exception:
            _logger.debug("Recovered from an error in fetch_ollama_runtime_status.", exc_info=True)
            model_context_length = None

    if not running:
        if wanted:
            msg = f"Ollama en ligne. Chargement du modele {wanted}..."
        else:
            msg = "Ollama en ligne. Aucun modele charge en memoire."
    elif loaded:
        msg = f"Ollama: modele {wanted} deja charge en memoire."
    elif wanted:
        msg = (
            f"Ollama actif ({running[0]} en memoire). "
            f"Chargement de {wanted} en cours si necessaire..."
        )
    else:
        msg = f"Ollama actif: {', '.join(running[:3])}"

    return {
        "ok": True,
        "model_loaded": loaded,
        "running_models": running,
        "active_context_length": active_context_length,
        "model_context_length": model_context_length,
        "msg": msg,
    }


def check_ollama_health(
    ollama_url: str = OLLAMA_DEFAULT_URL,
    required_embedding_model: str = "embeddinggemma",
    required: bool = True,
) -> dict:
    if not required:
        return {
            "required": False,
            "ok": True,
            "ollama_ok": True,
            "embedding_ok": True,
            "embedding_model": required_embedding_model,
            "msg": "Verification Ollama non requise pour la configuration actuelle.",
        }

    target = (ollama_url or OLLAMA_DEFAULT_URL).rstrip("/")
    try:
        data = _read_ollama_json(target, "/api/tags", timeout=4)
    except Exception as exc:
        _logger.debug("Recovered from an error in check_ollama_health.", exc_info=True)
        return {
            "required": True,
            "ok": False,
            "ollama_ok": False,
            "embedding_ok": False,
            "embedding_model": required_embedding_model,
            "msg": _format_ollama_access_error(exc, path="/api/tags"),
        }

    raw = data.get("models", [])
    names = [str(model.get("name", "")).strip() for model in raw if isinstance(model, dict)]

    def _normalize(name: str) -> str:
        return name.split(":", 1)[0].strip().lower()

    wanted = _normalize(required_embedding_model or "embeddinggemma")
    embedding_ok = any(_normalize(name) == wanted for name in names)
    if not embedding_ok:
        return {
            "required": True,
            "ok": False,
            "ollama_ok": True,
            "embedding_ok": False,
            "embedding_model": required_embedding_model,
            "msg": (
                f"Modele d'embedding manquant: {required_embedding_model}. "
                f"Installez-le avec: ollama pull {required_embedding_model}"
            ),
        }

    return {
        "required": True,
        "ok": True,
        "ollama_ok": True,
        "embedding_ok": True,
        "embedding_model": required_embedding_model,
        "msg": f"Ollama OK + embedding {required_embedding_model} detecte.",
    }
