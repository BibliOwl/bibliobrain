"""Client for the local Zotero connector server, the API Zotero's browser extension uses.

Zotero stays the owner of its library: a file reaches it over HTTP, never by
writing into its storage folder or its database.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx

_logger = logging.getLogger(__name__)

_DEFAULT_SAVE_ITEMS_URL = "http://127.0.0.1:23119/connector/saveItems"


def connector_url(endpoint: str) -> str:
    """URL of a connector endpoint, or "" when the connector is disabled.

    ``ZOTERO_CONNECTOR_URL`` has always named the saveItems endpoint; the other
    endpoints are its siblings.
    """
    configured = (os.getenv("ZOTERO_CONNECTOR_URL", _DEFAULT_SAVE_ITEMS_URL) or "").strip()
    base = configured.rsplit("/", 1)[0] if "/" in configured else ""
    return f"{base}/{endpoint}" if base else ""


def zotero_is_running() -> bool:
    ping_url = connector_url("ping")
    if not ping_url:
        return False
    try:
        return httpx.get(ping_url, timeout=1.0, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


def find_zotero_executable() -> str | None:
    """Find the Zotero application without invoking a shell."""
    from_path = shutil.which("zotero")
    if from_path:
        return from_path

    candidates: list[Path] = []
    if os.name == "nt":
        registered = _windows_app_path("zotero.exe")
        if registered:
            candidates.append(Path(registered))
        for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            root = os.getenv(env_name, "").strip()
            if root:
                candidates.append(Path(root) / "Zotero" / "zotero.exe")
    else:
        candidates.append(Path("/Applications/Zotero.app/Contents/MacOS/zotero"))

    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _windows_app_path(executable: str) -> str:
    """Where the installer registered ``executable`` ("App Paths"), or ""."""
    import winreg

    key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable}"
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        if value:
            return str(value).strip().strip('"')
    return ""


def _launch_zotero(executable: str) -> None:
    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name != "nt":
        subprocess.Popen([executable], start_new_session=True, **popen_kwargs)
        return
    # Outside our job object, so Zotero stays open when this server stops.
    # Zotero opens in the foreground whatever window state is requested.
    try:
        subprocess.Popen(
            [executable], creationflags=subprocess.CREATE_BREAKAWAY_FROM_JOB, **popen_kwargs
        )
    except OSError:
        subprocess.Popen([executable], **popen_kwargs)


# One caller launches and waits; the others then find Zotero running.
_start_lock = threading.Lock()


def ensure_zotero_running(wait_seconds: float = 45.0) -> str:
    """Start Zotero when it is closed and wait for its connector to answer.

    Returns ``running``, ``started``, ``not_found`` (no local Zotero to start)
    or ``timeout`` (launched, but its connector is not answering yet: Zotero
    asks before installing an update and starts only once answered). Launching
    a Zotero that is already loading only raises its window, so a retry after a
    timeout simply launches again.
    """
    with _start_lock:
        if zotero_is_running():
            return "running"
        ping_url = connector_url("ping")
        if not ping_url or urlsplit(ping_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
            return "not_found"
        executable = find_zotero_executable()
        if not executable:
            return "not_found"
        try:
            _launch_zotero(executable)
        except OSError:
            _logger.warning("Unable to start Zotero", exc_info=True)
            return "not_found"

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            time.sleep(0.5)
            if zotero_is_running():
                return "started"
        return "timeout"


@dataclass(frozen=True)
class ZoteroSaveResult:
    saved: bool
    # Zotero found the bibliographic record and filed the PDF under it.
    recognized: bool = False
    title: str = ""
    error: str = ""


def save_pdf_to_zotero(
    pdf_path: Path,
    *,
    filename: str,
    recognition_timeout: float = 30.0,
) -> ZoteroSaveResult:
    """Hand a PDF to the running Zotero, as dropping it on Zotero's window would.

    Zotero imports it as a standalone attachment, then looks up its metadata
    ("Retrieve Metadata") and, on a match, creates the parent item. Without a
    match the PDF stays standalone, which Zotero reports with a 204.
    """
    save_url = connector_url("saveStandaloneAttachment")
    if not save_url:
        return ZoteroSaveResult(saved=False, error="not_configured")

    session_id = secrets.token_hex(8)
    # Zotero names the stored file after the URL's last segment. The header is
    # ASCII-only: json.dumps escapes the accents.
    metadata = json.dumps(
        {"sessionID": session_id, "url": f"file:///{quote(filename)}", "title": filename}
    )
    timeout = httpx.Timeout(connect=3.0, read=60.0, write=60.0, pool=5.0)
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            with pdf_path.open("rb") as handle:
                response = client.post(
                    save_url,
                    content=handle,
                    headers={"Content-Type": "application/pdf", "X-Metadata": metadata},
                )
            if response.status_code != 201:
                _logger.warning("Zotero refused the attachment (HTTP %s)", response.status_code)
                return ZoteroSaveResult(saved=False, error="rejected")
            if not _json_object(response).get("canRecognize"):
                return ZoteroSaveResult(saved=True)
            try:
                recognized = client.post(
                    connector_url("getRecognizedItem"),
                    json={"sessionID": session_id},
                    timeout=httpx.Timeout(recognition_timeout, connect=3.0),
                )
            except httpx.HTTPError:
                # Still looking it up (slow network, offline): the PDF is saved.
                return ZoteroSaveResult(saved=True)
            if recognized.status_code != 200:
                return ZoteroSaveResult(saved=True)
            return ZoteroSaveResult(
                saved=True,
                recognized=True,
                title=str(_json_object(recognized).get("title") or ""),
            )
    except (httpx.HTTPError, OSError):
        _logger.debug("Zotero connector unreachable", exc_info=True)
        return ZoteroSaveResult(saved=False, error="unreachable")


def _json_object(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}
