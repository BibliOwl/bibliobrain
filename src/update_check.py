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

Each kind of installation updates its own way:

- ``packaged`` (Setup.exe) and ``zip``: once daily checks are on, the new
  installer or ZIP package is downloaded and verified in the background, so
  the click only installs it. ZIP installs also get the manual steps
  (``Update-Bibliobrain.cmd`` with the new ZIP) in case that fails;
- ``git``: a button runs ``git pull --ff-only``, refused while tracked files
  have local changes so nothing the user wrote can be lost.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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
    ARCHIVE_FILENAME,
    DEFAULT_MANIFEST_URL,
    UPDATE_REPOSITORY,
    ZIP_PACKAGE_MARKER,
    ManifestError,
    compare_versions,
    is_valid_version,
    sha256_file,
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
PACKAGE_FILENAME = "Bibliobrain-Windows-{version}.zip"
# Which manifest asset each kind of installation updates from.
_PACKAGE_ASSETS = {
    "packaged": ("installer", INSTALLER_FILENAME),
    "zip": ("archive", PACKAGE_FILENAME),
}
RELEASES_PAGE_URL = f"https://github.com/{UPDATE_REPOSITORY}/releases/latest"
# A failed background download is retried at most this often.
AUTO_DOWNLOAD_RETRY_SECONDS = 3600.0
GIT_TIMEOUT_SECONDS = 15.0
MAX_LISTED_GIT_CHANGES = 8
INSTALL_MODES = {"git", "zip", "packaged"}

REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGER_SCRIPT = REPO_ROOT / "scripts" / "bibliobrain-manager.ps1"

_check_lock = threading.Lock()
_job_lock = threading.Lock()
# ``background`` is True while the installer is fetched ahead of the click;
# ``install_when_ready`` turns that download into an install when the user
# clicks before it ends.
_job: dict[str, Any] = {
    "phase": "idle",
    "percent": None,
    "error": "",
    "background": False,
    "install_when_ready": False,
}
_last_auto_attempt = 0.0
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
    # Daily checks on: the new installer or ZIP is fetched right away.
    start_background_download()
    return get_status()


def install_mode() -> str:
    """``git`` checkout, ``zip`` package, or ``packaged`` (Setup.exe).

    ``BIBLIOBRAIN_INSTALL_MODE`` forces one, to preview its page while testing.
    """
    forced = str(os.getenv("BIBLIOBRAIN_INSTALL_MODE", "") or "").strip().lower()
    if forced in INSTALL_MODES:
        return forced
    if (REPO_ROOT / ".git").exists():
        return "git"
    # The ZIP exists for machines whose antivirus quarantines Setup.exe: such
    # an install must never be updated by downloading the executable.
    if (REPO_ROOT / ZIP_PACKAGE_MARKER).is_file():
        return "zip"
    return "packaged"


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    root = REPO_ROOT.as_posix()
    return subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_SECONDS,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


_BRANCH_LINE = re.compile(
    r"^## (?P<branch>.+?)(?:\.\.\.(?P<upstream>\S+))?(?: \[(?P<track>[^\]]*)\])?$"
)


def git_state() -> dict[str, Any]:
    """Branch, upstream and the tracked files changed locally (no network).

    Untracked files are left out: ``git pull`` never touches them unless the
    pull brings a file of the same name, and then Git refuses on its own.
    """
    state: dict[str, Any] = {
        "available": bool(shutil.which("git")),
        "branch": "",
        "upstream": "",
        "behind": 0,
        "ahead": 0,
        "changes": [],
        "change_count": 0,
        "error": "",
    }
    if not state["available"]:
        return state
    try:
        result = _git("status", "--porcelain=v1", "--branch", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError) as exc:
        state["error"] = str(exc)[:200]
        return state
    if result.returncode != 0:
        state["error"] = (result.stderr or result.stdout).strip()[:200]
        return state
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if lines and lines[0].startswith("## "):
        match = _BRANCH_LINE.match(lines.pop(0))
        if match:
            branch = match.group("branch")
            # "HEAD (no branch)" when detached, "No commits yet on x" in a new repo.
            if not branch.startswith(("HEAD ", "No commits yet")):
                state["branch"] = branch
            state["upstream"] = match.group("upstream") or ""
            for part in (match.group("track") or "").split(","):
                name, _, count = part.strip().partition(" ")
                if name in {"ahead", "behind"} and count.isdigit():
                    state[name] = int(count)
    changes = [line[3:].strip() for line in lines if len(line) > 3]
    state["change_count"] = len(changes)
    state["changes"] = changes[:MAX_LISTED_GIT_CHANGES]
    return state


def _install_blocker(manifest: dict[str, Any] | None, git: dict[str, Any] | None = None) -> str:
    """Why the one-click update is unavailable here, or ``""``."""
    mode = install_mode()
    if not _is_windows() or not MANAGER_SCRIPT.is_file():
        return "unsupported"
    # A developer server (uvicorn --reload) must never pull or replace itself.
    if not _managed_server():
        return "not_managed"
    if mode == "git":
        git = git if git is not None else git_state()
        if not git["available"]:
            return "git_missing"
        if git["change_count"]:
            return "git_dirty"
        if not git["branch"] or not git["upstream"]:
            return "git_no_upstream"
        return ""
    if mode == "zip":
        # Never fall back to Setup.exe: the ZIP exists for machines whose
        # antivirus quarantines it. The page then shows the manual steps.
        return "" if manifest and manifest.get("archive") else "no_archive"
    if not manifest or not manifest.get("installer"):
        return "no_installer"
    return ""


def _is_windows() -> bool:
    return os.name == "nt"


def _managed_server() -> bool:
    return os.getenv("BIBLIOBRAIN_MANAGED_SERVER", "").strip().lower() in {"1", "true", "yes"}


def open_install_folder() -> bool:
    """Show the installation folder with ``Update-Bibliobrain.cmd`` selected."""
    if not _is_windows():
        return False
    launcher = REPO_ROOT / "Update-Bibliobrain.cmd"
    command = (
        ["explorer.exe", f"/select,{launcher}"]
        if launcher.is_file()
        else ["explorer.exe", str(REPO_ROOT)]
    )
    try:
        subprocess.Popen(command)
    except OSError:
        return False
    return True


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
    mode = install_mode()
    git = git_state() if mode == "git" else None
    # A Git checkout can pull at any time: its remote may be ahead of the
    # published releases, so the button does not wait for a new version.
    can_pull = mode == "git"
    blocker = _install_blocker(manifest, git) if (available or can_pull) else ""
    archive = (manifest or {}).get("archive") or {}
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
        "install_mode": mode,
        "install_blocker": blocker,
        # Installer or ZIP: the verified package is already on disk.
        "downloaded": bool(available and _ready_package(manifest, mode)),
        "git": git,
        "zip_guide": (
            {
                "download_url": str(archive.get("url") or ""),
                "releases_url": str((manifest or {}).get("notes_url") or "") or RELEASES_PAGE_URL,
                "install_dir": str(REPO_ROOT),
                "file_name": ARCHIVE_FILENAME,
            }
            if mode == "zip"
            else None
        ),
        "job": _job_snapshot(),
        "last_install": (
            {
                "ok": bool(result.get("ok")),
                "message": str(result.get("message") or "")[:500],
                "finished_at": str(result.get("finished_at") or ""),
            }
            if result
            else None
        ),
    }


def _download_installer(installer: dict[str, Any], version: str) -> Path:
    return _download_asset(installer, INSTALLER_FILENAME.format(version=version))


def _package_asset(manifest: dict[str, Any] | None, mode: str) -> tuple[dict[str, Any], str] | None:
    """The manifest asset this installation updates from, and its file name."""
    if not manifest or mode not in _PACKAGE_ASSETS:
        return None
    field, pattern = _PACKAGE_ASSETS[mode]
    asset = manifest.get(field)
    version = str(manifest.get("version") or "")
    if not isinstance(asset, dict) or not version:
        return None
    return asset, pattern.format(version=version)


def _download_package(manifest: dict[str, Any], mode: str) -> Path:
    found = _package_asset(manifest, mode)
    if found is None:
        raise ManifestError("This release has no package for this installation")
    path = _download_asset(*found)
    _remember_download(manifest, found[0])
    return path


def _ready_package(manifest: dict[str, Any] | None, mode: str) -> Path | None:
    """The package downloaded ahead of time for ``manifest``, if still intact."""
    found = _package_asset(manifest, mode)
    downloaded = _load_state().get("downloaded")
    if manifest is None or found is None or not isinstance(downloaded, dict):
        return None
    asset, filename = found
    if downloaded.get("version") != manifest.get("version"):
        return None
    if downloaded.get("sha256") != asset.get("sha256"):
        return None
    path = _updates_dir() / filename
    if not path.is_file():
        return None
    try:
        if expected := int(asset.get("size") or 0):
            if path.stat().st_size != expected:
                return None
    except (OSError, ValueError):
        return None
    return path


def _verified_ready_package(manifest: dict[str, Any], mode: str) -> Path | None:
    """Same, after hashing the file again: it waited on disk, maybe for days."""
    path = _ready_package(manifest, mode)
    found = _package_asset(manifest, mode)
    if path is None or found is None:
        return None
    try:
        if sha256_file(path) == found[0]["sha256"]:
            return path
    except OSError:
        pass
    path.unlink(missing_ok=True)
    return None


def _remember_download(manifest: dict[str, Any], asset: dict[str, Any]) -> None:
    state = _load_state()
    state["downloaded"] = {
        "version": manifest["version"],
        "sha256": asset["sha256"],
        "at": time.time(),
    }
    _save_state(state)


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
    # The package, then the update unpacks it and keeps a backup: ask for
    # three times its size so it does not stop half-way on a full disk.
    free_bytes = shutil.disk_usage(target_dir).free
    if free_bytes < limit * 3:
        raise ManifestError(
            f"Not enough free disk space for the update: {free_bytes // 1_000_000} MB free, "
            f"{limit * 3 // 1_000_000} MB needed"
        )
    digest = hashlib.sha256()
    received = 0
    try:
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
    except BaseException:
        # A cut connection must not leave a partial package behind.
        partial.unlink(missing_ok=True)
        raise
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
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        subprocess.Popen(
            command, cwd=str(REPO_ROOT), env=environment, creationflags=flags | breakaway
        )
    except OSError:
        subprocess.Popen(command, cwd=str(REPO_ROOT), env=environment, creationflags=flags)


def _launch_install(package_path: Path | None) -> None:
    _set_job(phase="restarting", percent=None, background=False, install_when_ready=False)
    _install_result_path().unlink(missing_ok=True)
    _launch_manager(_manager_command(package_path))


def _run_update(manifest: dict[str, Any] | None) -> None:
    try:
        package_path: Path | None = None
        mode = install_mode()
        if mode in _PACKAGE_ASSETS and manifest:
            package_path = _verified_ready_package(manifest, mode)
            if package_path is None:
                package_path = _download_package(manifest, mode)
        _launch_install(package_path)
    except Exception as exc:
        logger.warning("Update preparation failed: %s", exc)
        _set_job(
            phase="failed",
            percent=None,
            error=_error_code(exc),
            background=False,
            install_when_ready=False,
        )


def _run_background_download(manifest: dict[str, Any], mode: str) -> None:
    try:
        _download_package(manifest, mode)
    except Exception as exc:
        logger.info("Background update download failed: %s", exc)
        with _job_lock:
            clicked = _job.get("install_when_ready")
            if clicked:
                _job.update(phase="failed", percent=None, error=_error_code(exc))
            else:
                # Nobody is waiting on it: stay quiet, the next wake-up retries.
                _job.update(phase="idle", percent=None, error="")
            _job.update(background=False, install_when_ready=False)
        return
    with _job_lock:
        clicked = bool(_job.get("install_when_ready"))
        if not clicked:
            _job.update(phase="ready", percent=None, error="", background=False)
    if clicked:
        try:
            ready = _ready_package(manifest, mode)
            if ready is None:
                raise ManifestError("The downloaded package is missing")
            _launch_install(ready)
        except Exception as exc:
            logger.warning("Update launch failed: %s", exc)
            _set_job(
                phase="failed",
                percent=None,
                error=_error_code(exc),
                background=False,
                install_when_ready=False,
            )


def start_background_download(*, now: float | None = None) -> bool:
    """Fetch the new installer or ZIP ahead of the click (not for Git checkouts).

    Only when daily checks are on: that answer is the consent to talk to
    GitHub. Installing still waits for the user. Returns True when started.
    """
    global _last_auto_attempt
    mode = install_mode()
    if check_enabled() is not True or mode not in _PACKAGE_ASSETS or not _is_windows():
        return False
    status = get_status()
    if not status["update_available"] or status["downloaded"]:
        return False
    manifest = _load_state().get("manifest")
    if not isinstance(manifest, dict) or _package_asset(manifest, mode) is None:
        return False
    moment = time.monotonic() if now is None else now
    with _job_lock:
        if _job.get("phase") in {"downloading", "verifying", "restarting"}:
            return False
        if _last_auto_attempt and moment - _last_auto_attempt < AUTO_DOWNLOAD_RETRY_SECONDS:
            return False
        _last_auto_attempt = moment
        _job.update(
            phase="downloading", percent=0, error="", background=True, install_when_ready=False
        )
    threading.Thread(
        target=_run_background_download,
        args=(manifest, mode),
        name="bibliobrain-update-download",
        daemon=True,
    ).start()
    return True


def start_update(*, is_busy: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Start download + launch in the background. Returns ``{"ok", "code"}``."""
    status = get_status()
    if not status["update_available"] and status["install_mode"] != "git":
        return {"ok": False, "code": "no_update"}
    if status["install_blocker"]:
        return {"ok": False, "code": status["install_blocker"]}
    if is_busy():
        return {"ok": False, "code": "busy"}
    manifest = _load_state().get("manifest")
    with _job_lock:
        if _job.get("background") and _job.get("phase") in {"downloading", "verifying"}:
            # The installer is already on its way: install as soon as it is verified.
            _job.update(install_when_ready=True, background=False)
            return {"ok": True, "code": ""}
        if _job.get("phase") in {"downloading", "verifying", "restarting"}:
            return {"ok": False, "code": "already_running"}
        _job.update(
            phase="downloading", percent=0, error="", background=False, install_when_ready=False
        )
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
            start_background_download()
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
