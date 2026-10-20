from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.atomic_io import atomic_write_json


class ApiSecretStoreError(RuntimeError):
    """Raised when the per-user encrypted secret store cannot be used."""


SECRET_DEFINITIONS: dict[str, dict[str, Any]] = {
    "remote_llm_api_key": {
        "environment": "REMOTE_LLM_API_KEY",
        "aliases": (),
    },
    "openalex_api_key": {
        "environment": "OPENALEX_API_KEY",
        "aliases": (),
    },
    "semantic_scholar_api_key": {
        "environment": "SEMANTIC_SCHOLAR_API_KEY",
        "aliases": ("S2_API_KEY",),
    },
    "ollama_api_key": {
        "environment": "OLLAMA_API_KEY",
        "aliases": (),
    },
    "ollama_auth_header": {
        "environment": "OLLAMA_AUTH_HEADER",
        "aliases": (),
    },
    "huggingface_token": {
        "environment": "HF_TOKEN",
        "aliases": (),
    },
    "bugsink_dsn": {
        "environment": "BIBLIOBRAIN_BUGSINK_DSN",
        "aliases": (),
    },
}
REMOTE_BASE_URL_KEY = "remote_llm_base_url"
_REMOTE_BASE_URL_ENV = "REMOTE_LLM_BASE_URL"
_ALLOWED_KEYS = {*SECRET_DEFINITIONS, REMOTE_BASE_URL_KEY}
_STORE_FILENAME = "api_secrets.dpapi.json"
_STORE_VERSION = 1
_DPAPI_ENTROPY = b"Bibliobrain/api-secrets/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_LOCK = threading.RLock()
_ENV_BASELINES: dict[str, str | None] = {}


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _store_path(storage_dir: str | os.PathLike[str]) -> Path:
    return Path(storage_dir) / _STORE_FILENAME


def secure_storage_available() -> bool:
    return os.name == "nt" and hasattr(ctypes, "WinDLL")


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    value = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return value, buffer


def _dpapi_transform(data: bytes, *, decrypt: bool) -> bytes:
    if not secure_storage_available():
        raise ApiSecretStoreError("Le stockage sécurisé des clés API nécessite Windows DPAPI.")

    crypt32 = ctypes.WinDLL("Crypt32.dll", use_last_error=True)
    kernel32 = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(_DPAPI_ENTROPY)
    output_blob = _DataBlob()
    description: ctypes.c_wchar_p | None = None

    if decrypt:
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        description = ctypes.c_wchar_p()
        success = crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            ctypes.byref(description),
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        success = crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            "Bibliobrain API secrets",
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )

    # Keep the backing buffers alive until the native call has completed.
    _ = input_buffer, entropy_buffer
    if not success:
        error_code = ctypes.get_last_error()
        raise ApiSecretStoreError(f"Windows DPAPI a refusé l'opération (code {error_code}).")

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        kernel32.LocalFree(output_blob.pbData)
        if description:
            kernel32.LocalFree(ctypes.cast(description, ctypes.c_void_p))


def _protect(data: bytes) -> bytes:
    return _dpapi_transform(data, decrypt=False)


def _unprotect(data: bytes) -> bytes:
    return _dpapi_transform(data, decrypt=True)


def _read_store(storage_dir: str | os.PathLike[str]) -> dict[str, str]:
    path = _store_path(storage_dir)
    if not path.exists():
        return {}
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict) or envelope.get("version") != _STORE_VERSION:
            raise ValueError("unsupported envelope")
        encrypted = base64.b64decode(str(envelope["protected_data"]), validate=True)
        payload = json.loads(_unprotect(encrypted).decode("utf-8"))
    except ApiSecretStoreError:
        raise
    except (OSError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ApiSecretStoreError("Le coffre local de clés API est illisible ou corrompu.") from exc
    if not isinstance(payload, dict):
        raise ApiSecretStoreError("Le coffre local de clés API est invalide.")
    return {
        str(key): str(value)
        for key, value in payload.items()
        if key in _ALLOWED_KEYS and isinstance(value, str) and value.strip()
    }


def _write_store(
    storage_dir: str | os.PathLike[str],
    payload: dict[str, str],
) -> None:
    path = _store_path(storage_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    plaintext = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    encrypted = _protect(plaintext)
    atomic_write_json(
        path,
        {
            "version": _STORE_VERSION,
            "protected_data": base64.b64encode(encrypted).decode("ascii"),
        },
    )
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _environment_name(key: str) -> str:
    if key == REMOTE_BASE_URL_KEY:
        return _REMOTE_BASE_URL_ENV
    return str(SECRET_DEFINITIONS[key]["environment"])


def _set_runtime_value(key: str, value: str | None) -> None:
    environment_name = _environment_name(key)
    if environment_name not in _ENV_BASELINES:
        _ENV_BASELINES[environment_name] = os.environ.get(environment_name)
    if value:
        os.environ[environment_name] = value
        return
    baseline = _ENV_BASELINES.get(environment_name)
    if baseline is None:
        os.environ.pop(environment_name, None)
    else:
        os.environ[environment_name] = baseline


def activate_persisted_api_config(storage_dir: str | os.PathLike[str]) -> None:
    """Load encrypted values into this server process without exposing them."""
    if not secure_storage_available():
        return
    with _LOCK:
        payload = _read_store(storage_dir)
        for key, value in payload.items():
            _set_runtime_value(key, value)


def update_api_config(
    storage_dir: str | os.PathLike[str],
    *,
    updates: dict[str, str] | None = None,
    clear: Sequence[str] | None = None,
) -> None:
    """Persist selected values and apply them immediately to the running server."""
    normalized_updates = {
        str(key): str(value).strip()
        for key, value in (updates or {}).items()
        if key in _ALLOWED_KEYS and str(value).strip()
    }
    normalized_clear = {str(key) for key in (clear or ()) if key in _ALLOWED_KEYS}
    if not normalized_updates and not normalized_clear:
        return
    if not secure_storage_available():
        raise ApiSecretStoreError("Le stockage sécurisé des clés API nécessite Windows DPAPI.")

    with _LOCK:
        payload = _read_store(storage_dir)
        for key in normalized_clear:
            payload.pop(key, None)
        payload.update(normalized_updates)
        _write_store(storage_dir, payload)
        for key in normalized_clear - normalized_updates.keys():
            _set_runtime_value(key, None)
        for key, value in normalized_updates.items():
            _set_runtime_value(key, value)


def _environment_value_for_secret(key: str) -> str:
    definition = SECRET_DEFINITIONS[key]
    names = (definition["environment"], *definition.get("aliases", ()))
    return next(
        (
            str(os.getenv(str(name), "")).strip()
            for name in names
            if str(os.getenv(str(name), "")).strip()
        ),
        "",
    )


def _public_remote_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname or ""
        if not hostname:
            return ""
        netloc = f"[{hostname}]" if ":" in hostname else hostname
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        return urlunsplit(
            (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
        ).rstrip("/")
    except (ValueError, UnicodeError):
        return ""


def api_config_status(storage_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Return configuration metadata only; secret values are never included."""
    stored: dict[str, str] = {}
    error = ""
    available = secure_storage_available()
    if available:
        try:
            with _LOCK:
                stored = _read_store(storage_dir)
        except ApiSecretStoreError as exc:
            error = str(exc)

    secrets: dict[str, dict[str, Any]] = {}
    for key in SECRET_DEFINITIONS:
        in_store = bool(stored.get(key))
        in_environment = bool(_environment_value_for_secret(key))
        secrets[key] = {
            "configured": in_store or in_environment,
            "source": "secure_store" if in_store else ("environment" if in_environment else None),
        }

    stored_url = str(stored.get(REMOTE_BASE_URL_KEY) or "").strip()
    environment_url = str(os.getenv(_REMOTE_BASE_URL_ENV, "")).strip()
    effective_url = stored_url or environment_url
    response: dict[str, Any] = {
        "available": available and not error,
        "backend": "windows_dpapi" if available else "unavailable",
        "secrets": secrets,
        "remote_llm_base_url": {
            "configured": bool(effective_url),
            "source": (
                "secure_store" if stored_url else ("environment" if environment_url else None)
            ),
            "value": _public_remote_url(effective_url),
        },
    }
    if error:
        response["error"] = error
    return response
