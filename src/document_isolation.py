"""Run document parsing in a throw-away child process.

PDF, OOXML, ODF and EPUB parsers handle untrusted bytes. A malformed file can
hang a parser, exhaust memory or trip a bug in native code; none of that must
reach the server process. The child gets a wall-clock timeout, a memory cap
(a Windows job object, or ``RLIMIT_AS`` elsewhere) and is killed with all its
descendants when the deadline passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from src.document_formats import DocumentFormatError, DocumentUnit

_WORKER = Path(__file__).with_name("document_worker.py")
_MIB = 1024 * 1024
_DEFAULT_MEMORY_MB = 4096
_BASE_TIMEOUT_SECONDS = 240
_MAX_EXTRACTED_CHARS = 64_000_000
_MAX_RESULT_BYTES = 4 * _MAX_EXTRACTED_CHARS
_KILL_ON_CLOSE = 0x2000
_PROCESS_MEMORY_LIMIT = 0x100


def isolation_enabled() -> bool:
    """On unless ``BIBLIOBRAIN_DOCUMENT_ISOLATION`` is explicitly turned off."""
    flag = os.environ.get("BIBLIOBRAIN_DOCUMENT_ISOLATION", "").strip().lower()
    return flag not in {"0", "false", "off", "no"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def parse_timeout_seconds(file_size: int) -> int:
    """Base allowance plus one second per MiB, overridable for slow machines."""
    explicit = os.environ.get("BIBLIOBRAIN_PARSER_TIMEOUT_SECONDS", "").strip()
    if explicit:
        return _env_int("BIBLIOBRAIN_PARSER_TIMEOUT_SECONDS", _BASE_TIMEOUT_SECONDS)
    return _BASE_TIMEOUT_SECONDS + file_size // _MIB


class _WindowsJob:
    """A job object that caps the child's memory and kills its whole tree."""

    def __init__(self, memory_bytes: int) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in ("a", "b", "c", "d", "e", "f")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32 = kernel32
        self._handle = kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _KILL_ON_CLOSE | _PROCESS_MEMORY_LIMIT
        limits.ProcessMemoryLimit = memory_bytes
        # JobObjectExtendedLimitInformation == 9
        if not kernel32.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def adopt(self, process: subprocess.Popen) -> None:
        if not self._kernel32.AssignProcessToJobObject(self._handle, int(process._handle)):  # type: ignore[attr-defined]
            import ctypes

            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")

    def kill(self) -> None:
        self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _posix_limits(memory_bytes: int):
    def apply() -> None:
        if sys.platform != "win32":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))

    return apply


def _spawn(command: list[str], memory_bytes: int) -> tuple[subprocess.Popen, _WindowsJob | None]:
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
        job = _WindowsJob(memory_bytes)
        process = subprocess.Popen(command, **options)
        try:
            job.adopt(process)
        except OSError:
            process.kill()
            process.communicate()
            job.close()
            raise
        return process, job
    options["start_new_session"] = True
    options["preexec_fn"] = _posix_limits(memory_bytes)
    return subprocess.Popen(command, **options), None


def _terminate(process: subprocess.Popen, job: _WindowsJob | None) -> None:
    if job is not None:
        job.kill()
    elif sys.platform != "win32":
        import signal

        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
    process.kill()


def parse_in_subprocess(
    path: str | os.PathLike[str], *, max_units: int | None = None
) -> list[DocumentUnit]:
    """Extract the units of one document in a sandboxed child process."""
    full_path = Path(path)
    try:
        size = full_path.stat().st_size
    except OSError as exc:
        raise DocumentFormatError(f"Fichier illisible: {exc}") from exc
    timeout = parse_timeout_seconds(size)
    memory_bytes = _env_int("BIBLIOBRAIN_PARSER_MEMORY_MB", _DEFAULT_MEMORY_MB) * _MIB
    command = [
        sys.executable,
        "-I",
        str(_WORKER),
        str(full_path),
        str(int(max_units or 0)),
        str(_MAX_EXTRACTED_CHARS),
    ]
    try:
        process, job = _spawn(command, memory_bytes)
    except OSError as exc:
        raise DocumentFormatError(f"Analyse isolée impossible: {exc}") from exc
    try:
        try:
            stdout, _stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _terminate(process, job)
            process.communicate()
            raise DocumentFormatError(
                f"Analyse interrompue après {timeout} s: {full_path.name}"
            ) from exc
        except BaseException:
            _terminate(process, job)
            raise
    finally:
        if job is not None:
            job.close()

    if len(stdout) > _MAX_RESULT_BYTES:
        raise DocumentFormatError("Résultat d'analyse trop volumineux")
    try:
        payload = json.loads(stdout.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise DocumentFormatError(
            f"Analyse interrompue (le processus s'est arrêté avec le code {process.returncode})"
        ) from exc
    if not isinstance(payload, dict) or "units" not in payload:
        message = payload.get("error") if isinstance(payload, dict) else ""
        raise DocumentFormatError(str(message or "Analyse impossible"))
    return [
        DocumentUnit(text=str(text), page=page, content_type=str(kind), location_label=str(label))
        for text, page, kind, label in payload["units"]
    ]
