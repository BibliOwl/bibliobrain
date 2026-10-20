"""Advisory hardware inventory only: never loads a model or transcribes a page."""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

_logger = logging.getLogger(__name__)

GIB = 1024**3
_lock = threading.Lock()
_cache: dict = {}


def hardware_snapshot() -> dict:
    result: dict[str, int | None] = {
        "ram_bytes": None,
        "gpu_bytes": None,
        "gpu_free_bytes": None,
    }
    if os.name == "nt":

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "physical",
                    "available_physical",
                    "page_file",
                    "available_page_file",
                    "virtual",
                    "available_virtual",
                    "extended_virtual",
                )
            ]

        memory = MemoryStatus()
        memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            result["ram_bytes"] = int(memory.physical)
    else:
        try:
            sysconf = getattr(os, "sysconf")
            result["ram_bytes"] = sysconf("SC_PHYS_PAGES") * sysconf("SC_PAGE_SIZE")
        except (AttributeError, OSError, ValueError):
            pass
    executable = shutil.which("nvidia-smi")
    if executable:
        try:
            process = subprocess.run(
                [
                    executable,
                    "--query-gpu=memory.total,memory.free",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=1,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            rows = []
            if process.returncode == 0:
                for line in process.stdout.splitlines():
                    try:
                        total, free = (int(value.strip()) * 1024**2 for value in line.split(","))
                        rows.append((total, free))
                    except ValueError:
                        continue
            if rows:
                # Separate GPUs don't form a single pool of VRAM.
                result["gpu_bytes"], result["gpu_free_bytes"] = max(rows)
        except (OSError, subprocess.SubprocessError):
            pass
    # Missing nvidia-smi may mean AMD/Intel/Apple, not the absence of acceleration.
    return result


def installed_model(model: str) -> dict:
    from src.llm_provider import _read_ollama_json, document_tools_ollama_base_url

    base = document_tools_ollama_base_url()
    try:
        payload = _read_ollama_json(base, "/api/tags", timeout=1)
    except Exception:
        _logger.debug("Recovered from an error in installed_model.", exc_info=True)
        return {"installed": None, "model_bytes": None, "reason": "ollama_unavailable"}
    for item in payload.get("models", []):
        if model in (item.get("name"), item.get("model")):
            return {"installed": True, "model_bytes": int(item.get("size") or 0) or None}
    return {"installed": False, "model_bytes": None, "reason": "model_missing"}


def assess(hardware: dict, model: dict) -> dict:
    """Conservative memory headroom heuristic, not a throughput benchmark."""
    result = {**hardware, **model, "level": "unknown", "label": "Performances inconnues"}
    if model.get("installed") is not True:
        messages = {
            "model_missing": "Le modèle sélectionné n’est pas installé dans Ollama.",
        }
        reason = str(model.get("reason") or "")
        result["detail"] = messages.get(
            reason,
            "Ollama ne répond pas : présence du modèle non vérifiée.",
        )
        return result
    if model.get("remote"):
        # This computer's memory says nothing about the one that runs the model.
        result["detail"] = (
            "Le modèle tourne sur un autre ordinateur : son matériel n’est pas évalué ici."
        )
        return result
    ram, gpu = hardware.get("ram_bytes"), hardware.get("gpu_bytes")
    size = model.get("model_bytes")
    # Includes a reserve for execution/context, embedding and the rest of the app.
    reserve = max(4 * GIB, 2 * size + GIB) if size else None
    if (ram is not None and ram < 8 * GIB) or (
        gpu is not None and reserve is not None and gpu < reserve
    ):
        result.update(
            level="slow",
            label="Risque d’être lent",
            detail="Mémoire limitée pour ce modèle et les autres tâches. L’activation reste manuelle.",
        )
    elif gpu is not None and reserve is not None and ram is not None and ram >= 16 * GIB:
        result.update(
            level="recommended",
            label="Conseillé",
            detail="La mémoire détectée laisse une marge pour ce modèle. La vitesse sera estimée pendant le traitement réel.",
        )
    else:
        result["detail"] = (
            "Le matériel détecté ne permet pas de conclure. L’activation reste manuelle, sans test préalable."
        )
    return result


def readiness(model: str) -> dict:
    from src.llm_provider import _is_this_computer_url, document_tools_ollama_base_url

    base_url = document_tools_ollama_base_url()
    remote = not _is_this_computer_url(base_url)
    key = (base_url, model)
    # Lazy and cached: the regular status poll and app startup never probe hardware.
    with _lock:
        now = time.monotonic()
        if _cache.get("key") == key and now - _cache.get("at", 0) < 60:
            return dict(_cache["value"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            hardware = pool.submit(dict if remote else hardware_snapshot)
            installed = pool.submit(installed_model, model)
            try:
                snapshot = hardware.result()
            except Exception:
                _logger.debug("Recovered from an error in readiness.", exc_info=True)
                snapshot = {}
            try:
                model_info = installed.result()
            except Exception:
                _logger.debug("Recovered from an error in readiness.", exc_info=True)
                model_info = {"installed": None}
        if remote:
            model_info = {**model_info, "remote": True}
        value = {**assess(snapshot, model_info), "model": model, "passive": True}
        _cache.update(key=key, at=time.monotonic(), value=value)
        return dict(value)
