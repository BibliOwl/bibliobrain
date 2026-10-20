"""Small, lazy wrappers around native desktop dialogs."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


class NativeDialogUnavailable(RuntimeError):
    """Raised when the current runtime cannot display a desktop dialog."""


class NativeDialogBusy(RuntimeError):
    """Raised when another native picker is already open."""


_native_picker_lock = threading.Lock()

_WINDOWS_PICKER_WORKER = Path(__file__).with_name("native_dialog_worker.py")

# The dialog normally shows within a second. Past this delay it will not come:
# an error the interface can explain beats a button that spins forever.
_WINDOWS_PICKER_APPEAR_TIMEOUT_SECONDS = 20.0

# (label, patterns) pairs; "All files" is always appended.
FileTypes = tuple[tuple[str, tuple[str, ...]], ...]

METADATA_FILE_TYPES: FileTypes = (
    ("Métadonnées bibliographiques", ("*.sqlite", "*.bib", "*.bibtex", "*.txt")),
)


def _with_all_files(file_types: FileTypes) -> FileTypes:
    return (*file_types, ("Tous les fichiers", ("*.*",)))


def _tk_filetypes(file_types: FileTypes) -> tuple[tuple[str, str], ...]:
    return tuple((label, " ".join(patterns)) for label, patterns in _with_all_files(file_types))


def _existing_initial_directory(value: str) -> str:
    """Folder to open the picker in, or ``""`` to let the system choose."""

    text = str(value or "").strip()
    if not text:
        return ""
    candidate = Path(text).expanduser()
    if candidate.is_file():
        candidate = candidate.parent
    return str(candidate) if candidate.is_dir() else ""


def _run_windows_picker(request: dict[str, Any], *, failure: str) -> str:
    """Show a system dialog in a helper process and return the chosen path.

    User values travel on stdin, never on the command line.
    """

    process = subprocess.Popen(
        [sys.executable, "-I", str(_WINDOWS_PICKER_WORKER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        encoding="utf-8",
        errors="replace",
    )
    shown = threading.Event()
    lines: list[str] = []

    def read_answers() -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line == "SHOWN":
                shown.set()
            elif line:
                lines.append(line)
        # The helper is gone: nothing left to wait for.
        shown.set()

    reader = threading.Thread(target=read_answers, daemon=True)
    reader.start()
    try:
        assert process.stdin is not None
        process.stdin.write(json.dumps(request))
        process.stdin.close()
    except OSError:
        pass

    if not shown.wait(_WINDOWS_PICKER_APPEAR_TIMEOUT_SECONDS):
        process.kill()
        process.wait()
        reader.join(timeout=5)
        _logger.warning(
            "%s: no dialog after %.0f s. %s",
            failure,
            _WINDOWS_PICKER_APPEAR_TIMEOUT_SECONDS,
            " | ".join(lines)[-800:],
        )
        raise NativeDialogUnavailable(failure)

    return_code = process.wait()
    reader.join(timeout=5)
    for line in lines:
        if line.startswith("RESULT "):
            return str(json.loads(line[len("RESULT ") :]).get("path") or "")
    _logger.warning("%s (exit code %s). %s", failure, return_code, " | ".join(lines)[-800:])
    raise NativeDialogUnavailable(failure)


def _choose_windows_directory(*, initial_path: str, title: str) -> str:
    return _run_windows_picker(
        {"kind": "folder", "initial_path": initial_path, "title": title},
        failure="Windows directory picker failed",
    )


def _choose_windows_file(
    *,
    initial_path: str,
    title: str,
    file_types: FileTypes = METADATA_FILE_TYPES,
) -> str:
    return _run_windows_picker(
        {
            "kind": "file",
            "initial_path": initial_path,
            "title": title,
            "file_types": _with_all_files(file_types),
        },
        failure="Windows file picker failed",
    )


def _choose_windows_save_file(
    *,
    initial_path: str,
    title: str,
    default_name: str,
    file_types: FileTypes,
) -> str:
    return _run_windows_picker(
        {
            "kind": "save",
            "initial_path": initial_path,
            "title": title,
            "default_name": default_name,
            "file_types": _with_all_files(file_types),
        },
        failure="Windows save picker failed",
    )


def _choose_tk_directory(*, initial_path: str, title: str) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        root.update_idletasks()
        return str(
            filedialog.askdirectory(
                parent=root,
                initialdir=initial_path,
                mustexist=True,
                title=title,
            )
            or ""
        )
    finally:
        root.destroy()


def _choose_tk_file(
    *,
    initial_path: str,
    title: str,
    file_types: FileTypes = METADATA_FILE_TYPES,
) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        root.update_idletasks()
        return str(
            filedialog.askopenfilename(
                parent=root,
                initialdir=initial_path,
                title=title,
                filetypes=_tk_filetypes(file_types),
            )
            or ""
        )
    finally:
        root.destroy()


def _choose_tk_save_file(
    *,
    initial_path: str,
    title: str,
    default_name: str,
    file_types: FileTypes,
) -> str:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except tk.TclError:
            pass
        root.update_idletasks()
        return str(
            filedialog.asksaveasfilename(
                parent=root,
                initialdir=initial_path,
                initialfile=default_name,
                defaultextension=Path(default_name).suffix,
                confirmoverwrite=True,
                title=title,
                filetypes=_tk_filetypes(file_types),
            )
            or ""
        )
    finally:
        root.destroy()


def choose_directory(*, initial_path: str = "", title: str = "") -> str:
    """Open the platform folder picker and return an absolute path or ``""``.

    Windows shows the system dialog from a helper process (see
    ``native_dialog_worker``) because Bibliobrain deliberately installs Python
    without Tcl/Tk. Tk is loaded lazily as a cross-platform development
    fallback, so headless server starts remain unaffected.
    """

    if not _native_picker_lock.acquire(blocking=False):
        raise NativeDialogBusy("a directory picker is already open")

    try:
        try:
            initial_directory = _existing_initial_directory(initial_path)
            dialog_title = title or "Choisir le dossier contenant les PDF"
            if os.name == "nt":
                selected = _choose_windows_directory(
                    initial_path=initial_directory,
                    title=dialog_title,
                )
            else:
                selected = _choose_tk_directory(
                    initial_path=initial_directory,
                    title=dialog_title,
                )
        except Exception as exc:
            if isinstance(exc, NativeDialogUnavailable):
                raise
            raise NativeDialogUnavailable("could not open the directory picker") from exc

        if not selected:
            return ""
        return os.path.abspath(os.path.normpath(str(selected)))
    finally:
        _native_picker_lock.release()


def choose_file(
    *,
    initial_path: str = "",
    title: str = "",
    file_types: FileTypes = METADATA_FILE_TYPES,
) -> str:
    """Open a file picker (metadata files by default); absolute path or ``""``."""

    if not _native_picker_lock.acquire(blocking=False):
        raise NativeDialogBusy("a native picker is already open")

    try:
        try:
            initial_directory = _existing_initial_directory(initial_path)
            dialog_title = title or "Choisir un fichier de métadonnées"
            if os.name == "nt":
                selected = _choose_windows_file(
                    initial_path=initial_directory,
                    title=dialog_title,
                    file_types=file_types,
                )
            else:
                selected = _choose_tk_file(
                    initial_path=initial_directory,
                    title=dialog_title,
                    file_types=file_types,
                )
        except Exception as exc:
            if isinstance(exc, NativeDialogUnavailable):
                raise
            raise NativeDialogUnavailable("could not open the file picker") from exc

        if not selected:
            return ""
        return os.path.abspath(os.path.normpath(str(selected)))
    finally:
        _native_picker_lock.release()


def choose_save_file(
    *,
    default_name: str,
    file_types: FileTypes,
    initial_path: str = "",
    title: str = "",
) -> str:
    """Open a "save as" picker and return an absolute path or ``""``.

    The picker itself asks before overwriting an existing file.
    """

    if not _native_picker_lock.acquire(blocking=False):
        raise NativeDialogBusy("a native picker is already open")

    try:
        try:
            initial_directory = _existing_initial_directory(initial_path)
            dialog_title = title or "Enregistrer sous"
            if os.name == "nt":
                selected = _choose_windows_save_file(
                    initial_path=initial_directory,
                    title=dialog_title,
                    default_name=default_name,
                    file_types=file_types,
                )
            else:
                selected = _choose_tk_save_file(
                    initial_path=initial_directory,
                    title=dialog_title,
                    default_name=default_name,
                    file_types=file_types,
                )
        except Exception as exc:
            if isinstance(exc, NativeDialogUnavailable):
                raise
            raise NativeDialogUnavailable("could not open the save picker") from exc

        if not selected:
            return ""
        return os.path.abspath(os.path.normpath(str(selected)))
    finally:
        _native_picker_lock.release()
