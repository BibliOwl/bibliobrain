"""Daily update check and the in-app update launcher.

Privacy contract, also stated in the settings card:

- nothing leaves the machine before the user said yes: ``update_check_enabled``
  stays ``None`` until the setup assistant or the one-time notice records an
  answer, and the background loop only runs when it is ``True``;
- a check is one anonymous GET of a static ``latest.json``: no identifier, no
  version in the URL, a generic User-Agent. The comparison happens here;
- installing always needs a click. The download is verified against the
  manifest's SHA-256, then ``scripts/bibliobrain-manager.ps1 -Action update
  -FromApp`` backs up, stops this server, updates and restarts it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from src import config
from src.app_settings import get_app_settings
from src.atomic_io import atomic_write_json
from src.update_manifest import (
    DEFAULT_MANIFEST_URL,
    ZIP_PACKAGE_MARKER,
    ManifestError,
    compare_versions,
    is_valid_version,
    validate_manifest,
)
from src.version import get_app_version

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 24 * 3600
FIRST_CHECK_DELAY_SECONDS = 45.0
LOOP_INTERVAL_SECONDS = 3600.0
REQUEST_TIMEOUT_SECONDS = 10.0
MAX_MANIFEST_BYTES = 64 * 1024
# Upper bound when the manifest does not state a size: a package only
# carries code, Python and models are installed by install.ps1.
MAX_INSTALLER_BYTES = 512 * 1024 * 1024
USER_AGENT = "Bibliobrain-update-check"
INSTALLER_FILENAME = "Bibliobrain-Setup-{version}.exe"
ARCHIVE_FILENAME = "Bibliobrain-Windows-{version}.zip"

REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGER_SCRIPT = REPO_ROOT / "scripts" / "bibliobrain-manager.ps1"

_check_lock = threading.Lock()
_job_lock = threading.Lock()
_job: dict[str, Any] = {"phase": "idle", "percent": None, "error": ""}
_loop_thread: threading.Thread | None = None
_loop_stop = threading.Event()


def manifest_url() -> str:
    """Official manifest, or ``BIBLIOBRAIN_UPDATE_MANIFEST_URL`` (configuration.json)."""
    configured = str(os.getenv("BIBLIOBRAIN_UPDATE_MANIFEST_URL", "") or "").strip()
    return configured or DEFAULT_MANIFEST_URL


def check_enabled() -> bool | None:
    """``None`` means the user has not answered yet: no request may leave."""
    return get_app_settings().get("update_check_enabled")


def _state_path() -> Path:
    return Path(config.STORAGE_DIR) / "update_check.json"


def _updates_dir() -> Path:
    return Path(config.STORAGE_DIR) / "updates"


def _install_result_path() -> Path:
    # Written by bibliobrain-manager.ps1 -FromApp, which only knows the runtime folder.
    return Path(config.RUNTIME_DIR) / "update-result.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_state() -> dict[str, Any]:
    return _read_json(_state_path())


def _save_state(payload: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, payload)


def _iso(timestamp: Any) -> str:
    try:
        return datetime.fromtimestamp(float(timestamp), UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _fetch_manifest(url: str) -> dict[str, Any]:
    with httpx.Client(
        timeout=REQUEST_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    ) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_MANIFEST_BYTES:
                    raise ManifestError("The manifest is too large")
    try:
        payload = json.loads(bytes(body).decode("utf-8-sig"))
    except ValueError as exc:
        raise ManifestError("The manifest is not valid JSON") from exc
    return validate_manifest(payload, manifest_url=url)


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ManifestError):
        return "invalid_manifest"
    if isinstance(exc, httpx.HTTPStatusError):
        # Before the first public release the URL does not exist yet.
        return "not_found" if exc.response.status_code == 404 else "http_error"
    if isinstance(exc, httpx.TransportError):
        return "offline"
    return "error"


def check_is_due(now: float | None = None) -> bool:
    try:
        checked_at = float(_load_state().get("checked_at") or 0)
    except (TypeError, ValueError):
        return True
    elapsed = (time.time() if now is None else now) - checked_at
    return not checked_at or elapsed >= CHECK_INTERVAL_SECONDS or elapsed < 0


def check_now(*, force: bool = False) -> dict[str, Any]:
    """Fetch the manifest when enabled and due, or always when ``force`` (user click)."""
    if not force and (check_enabled() is not True or not check_is_due()):
        return get_status()
    if not _check_lock.acquire(blocking=False):
        return get_status()
    try:
        url = manifest_url()
        state = _load_state()
        state["checked_at"] = time.time()
        try:
            state["manifest"] = _fetch_manifest(url)
            state["error"] = ""
        except Exception as exc:  # network, HTTP or manifest problem: never fatal
            state["error"] = _error_code(exc)
            logger.info("Update check failed (%s): %s", state["error"], exc)
        _save_state(state)
    finally:
        _check_lock.release()
    return get_status()


def install_mode() -> str:
    """``git`` checkout, ``zip`` package, or ``packaged`` (Setup.exe)."""
    if (REPO_ROOT / ".git").exists():
        return "git"
    # The ZIP exists for machines whose antivirus quarantines Setup.exe: such
    # an install must never be updated by downloading the executable.
    if (REPO_ROOT / ZIP_PACKAGE_MARKER).is_file():
        return "zip"
    return "packaged"


def _install_blocker(manifest: dict[str, Any] | None) -> str:
    """Why the one-click update is unavailable here, or ``""``."""
    if os.name != "nt" or not MANAGER_SCRIPT.is_file():
        return "unsupported"
    # A developer server (uvicorn --reload) must never pull or replace itself.
    if os.getenv("BIBLIOBRAIN_MANAGED_SERVER", "").strip().lower() not in {"1", "true", "yes"}:
        return "not_managed"
    if install_mode() == "git":
        return "" if shutil.which("git") else "git_missing"
    if install_mode() == "zip":
        return "" if manifest and manifest.get("archive") else "no_archive"
    if not manifest or not manifest.get("installer"):
        return "no_installer"
    return ""


def _job_snapshot() -> dict[str, Any]:
    with _job_lock:
        return dict(_job)


def _set_job(**changes: Any) -> None:
    with _job_lock:
        _job.update(changes)


def get_status() -> dict[str, Any]:
    state = _load_state()
    current = get_app_version()
    manifest = state.get("manifest") if isinstance(state.get("manifest"), dict) else None
    latest = str((manifest or {}).get("version") or "")
    available = False
    if latest and is_valid_version(latest) and is_valid_version(current):
        available = compare_versions(latest, current) > 0
    result = _read_json(_install_result_path())
    settings = get_app_settings()
    enabled = settings.get("update_check_enabled")
    return {
        "enabled": enabled,
        # New users answer in the setup assistant; installs that predate this
        # feature get a one-time question once their setup is done.
        "ask_consent": enabled is None and bool(settings.get("onboarding_done_v1")),
        "current_version": current,
        "latest_version": latest,
        "update_available": available,
        "published": str((manifest or {}).get("published") or "") if available else "",
        "notes": str((manifest or {}).get("notes") or "") if available else "",
        "notes_url": str((manifest or {}).get("notes_url") or "") if available else "",
        "checked_at": _iso(state.get("checked_at")),
        "last_error": str(state.get("error") or ""),
        "install_mode": install_mode(),
        "install_blocker": _install_blocker(manifest) if available else "",
        "job": _job_snapshot(),
        "last_install": {
            "ok": bool(result.get("ok")),
            "message": str(result.get("message") or "")[:500],
            "finished_at": str(result.get("finished_at") or ""),
        }
        if result
        else None,
    }


def _download_installer(installer: dict[str, Any], version: str) -> Path:
    return _download_asset(installer, INSTALLER_FILENAME.format(version=version))


def _download_archive(archive: dict[str, Any], version: str) -> Path:
    return _download_asset(archive, ARCHIVE_FILENAME.format(version=version))


def _download_asset(asset: dict[str, Any], filename: str) -> Path:
    target_dir = _updates_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    # Keep only the package being prepared: older downloads are dead weight.
    for pattern in ("Bibliobrain-Setup-*", "Bibliobrain-Windows-*"):
        for stale in target_dir.glob(pattern):
            stale.unlink(missing_ok=True)
    target = target_dir / filename
    partial = target.with_name(target.name + ".part")
    expected_size = int(asset.get("size") or 0)
    limit = expected_size or MAX_INSTALLER_BYTES
    digest = hashlib.sha256()
    received = 0
    with httpx.Client(
        timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS, read=60.0),
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        with client.stream("GET", asset["url"]) as response:
            response.raise_for_status()
            with open(partial, "wb") as handle:
                for chunk in response.iter_bytes(256 * 1024):
                    received += len(chunk)
                    if received > limit:
                        raise ManifestError("The download is larger than announced")
                    digest.update(chunk)
                    handle.write(chunk)
                    if expected_size:
                        _set_job(percent=min(99, int(received * 100 / expected_size)))
    _set_job(phase="verifying", percent=None)
    if expected_size and received != expected_size:
        partial.unlink(missing_ok=True)
        raise ManifestError("The download size does not match the manifest")
    if digest.hexdigest() != asset["sha256"]:
        partial.unlink(missing_ok=True)
        raise ManifestError("The download checksum does not match the manifest")
    partial.replace(target)
    return target


def _manager_command(package_path: Path | None) -> list[str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(MANAGER_SCRIPT),
        "-Action",
        "update",
        "-FromApp",
        "-NoBrowser",
        # The SHA-256 above already proved the file is the published one;
        # this only skips the console question about unsigned alpha builds.
        "-Confirm",
    ]
    if package_path is not None:
        flag = "-ArchivePath" if package_path.suffix.lower() == ".zip" else "-InstallerPath"
        command += [flag, str(package_path)]
    return command


def _launch_manager(command: list[str]) -> None:
    environment = dict(os.environ)
    environment.pop("BIBLIOBRAIN_MANAGED_SERVER", None)
    # Its own console: the user sees the backup and update progress, and Git
    # can ask for credentials. Breaking away from our job object keeps the
    # manager alive when it stops this server.
    flags = (
        getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        subprocess.Popen(command, cwd=str(REPO_ROOT), env=environment, creationflags=flags | breakaway)
    except OSError:
        subprocess.Popen(command, cwd=str(REPO_ROOT), env=environment, creationflags=flags)


def _run_update(manifest: dict[str, Any]) -> None:
    try:
        package_path: Path | None = None
        mode = install_mode()
        if mode == "packaged":
            package_path = _download_installer(manifest["installer"], manifest["version"])
        elif mode == "zip":
            package_path = _download_archive(manifest["archive"], manifest["version"])
        _set_job(phase="restarting", percent=None)
        _install_result_path().unlink(missing_ok=True)
        _launch_manager(_manager_command(package_path))
    except Exception as exc:
        logger.warning("Update preparation failed: %s", exc)
        _set_job(phase="failed", percent=None, error=_error_code(exc))


def start_update(*, is_busy: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Start download + launch in the background. Returns ``{"ok", "code"}``."""
    status = get_status()
    if not status["update_available"]:
        return {"ok": False, "code": "no_update"}
    if status["install_blocker"]:
        return {"ok": False, "code": status["install_blocker"]}
    if is_busy():
        return {"ok": False, "code": "busy"}
    manifest = _load_state().get("manifest")
    with _job_lock:
        if _job.get("phase") in {"downloading", "verifying", "restarting"}:
            return {"ok": False, "code": "already_running"}
        _job.update(phase="downloading", percent=0, error="")
    threading.Thread(
        target=_run_update,
        args=(manifest,),
        name="bibliobrain-update-install",
        daemon=True,
    ).start()
    return {"ok": True, "code": ""}


def _loop() -> None:
    if _loop_stop.wait(FIRST_CHECK_DELAY_SECONDS):
        return
    while not _loop_stop.is_set():
        try:
            check_now()
        except Exception:
            logger.debug("Update check loop recovered from an error.", exc_info=True)
        if _loop_stop.wait(LOOP_INTERVAL_SECONDS):
            return


def start_background_checks() -> bool:
    """Idempotent. The loop itself respects the setting at every wake-up."""
    global _loop_thread
    if _loop_thread is not None and _loop_thread.is_alive():
        return False
    _loop_stop.clear()
    _loop_thread = threading.Thread(target=_loop, name="bibliobrain-update-check", daemon=True)
    _loop_thread.start()
    return True
