"""Windows file and folder pickers, shown from a short-lived helper process.

``src.native_dialogs`` starts this file as a script. It reads one JSON request
on stdin, shows the system dialog (the one the Explorer uses) and answers on
stdout:

    SHOWN            the dialog window is on screen
    RESULT <json>    {"path": "..."}; the path is empty after a cancel

The dialog lives in its own process so that a misbehaving shell extension
cannot take the server down. Only the standard library is imported, so that it
opens at once.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes
from typing import Any

_CLSID_FILE_OPEN_DIALOG = "{DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7}"
_CLSID_FILE_SAVE_DIALOG = "{C0B4E2F3-BA21-4773-8DBA-335EC946EB8B}"
_IID_FILE_DIALOG = "{42F85136-DB7E-439C-85F1-E4075D135FC8}"
_IID_SHELL_ITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"

# Method slots of IFileDialog (after IUnknown and IModalWindow) and IShellItem.
_RELEASE = 2
_SHOW = 3
_SET_FILE_TYPES = 4
_SET_OPTIONS = 9
_GET_OPTIONS = 10
_SET_FOLDER = 12
_SET_FILE_NAME = 15
_SET_TITLE = 17
_GET_RESULT = 20
_SET_DEFAULT_EXTENSION = 22
_SHELL_ITEM_GET_DISPLAY_NAME = 5

_FOS_PICKFOLDERS = 0x20
_FOS_FORCEFILESYSTEM = 0x40
_FOS_PATHMUSTEXIST = 0x800
_FOS_FILEMUSTEXIST = 0x1000
_SIGDN_FILESYSPATH = 0x80058000
_CANCELLED = ctypes.c_long(0x800704C7).value  # HRESULT_FROM_WIN32(ERROR_CANCELLED)

_COINIT_APARTMENTTHREADED = 0x2
_CLSCTX_INPROC_SERVER = 0x1
_WS_POPUP = 0x80000000
_WS_EX_TOPMOST = 0x8
_WS_EX_TOOLWINDOW = 0x80
_MONITOR_DEFAULTTONEAREST = 2
_SWP_NOSIZE = 0x1
_SWP_NOZORDER = 0x4
_SWP_NOACTIVATE = 0x10
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_DIALOG_WINDOW_CLASS = "#32770"

_answer_lock = threading.Lock()


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _FilterSpec(ctypes.Structure):
    _fields_ = [("name", wintypes.LPCWSTR), ("spec", wintypes.LPCWSTR)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("monitor", wintypes.RECT),
        ("work", wintypes.RECT),
        ("flags", wintypes.DWORD),
    ]


def _answer(line: str) -> None:
    with _answer_lock:
        sys.stdout.buffer.write((line + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


def _guid(text: str) -> _Guid:
    guid = _Guid()
    ctypes.OleDLL("ole32").CLSIDFromString(wintypes.LPCWSTR(text), ctypes.byref(guid))
    return guid


def _method(interface: ctypes.c_void_p, slot: int, *argument_types: Any, checked: bool = True):
    """Bind one method of a COM interface; a checked call raises on failure."""

    vtable = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    prototype = ctypes.WINFUNCTYPE(
        ctypes.HRESULT if checked else ctypes.c_long,
        ctypes.c_void_p,
        *argument_types,
    )
    function = prototype(vtable[slot])
    return lambda *arguments: function(interface, *arguments)


def _release(interface: ctypes.c_void_p) -> None:
    if interface:
        _method(interface, _RELEASE, checked=False)()


def _shell_item(path: str) -> ctypes.c_void_p | None:
    create = ctypes.OleDLL("shell32").SHCreateItemFromParsingName
    create.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        ctypes.POINTER(_Guid),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    item = ctypes.c_void_p()
    try:
        create(
            os.path.normpath(path), None, ctypes.byref(_guid(_IID_SHELL_ITEM)), ctypes.byref(item)
        )
    except OSError:
        return None
    return item


def _owner_window() -> int:
    """Invisible always-on-top owner, on the screen the user is looking at.

    The server runs in the background: without it the dialog could open behind
    the browser window, where nobody would look for it.
    """

    user32 = ctypes.WinDLL("user32")
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
    info = _MonitorInfo()
    info.size = ctypes.sizeof(_MonitorInfo)
    left, top = point.x, point.y
    monitor = user32.MonitorFromPoint(point, _MONITOR_DEFAULTTONEAREST)
    if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        # The dialog opens at its owner's corner: start it near the middle.
        left = info.work.left + (info.work.right - info.work.left) // 4
        top = info.work.top + (info.work.bottom - info.work.top) // 4

    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    return (
        user32.CreateWindowExW(
            _WS_EX_TOPMOST | _WS_EX_TOOLWINDOW,
            "STATIC",
            "",
            _WS_POPUP,
            left,
            top,
            0,
            0,
            None,
            None,
            None,
            None,
        )
        or 0
    )


def _visible_dialog_window() -> int:
    user32 = ctypes.WinDLL("user32")
    process_id = os.getpid()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(window: int, _parameter: int) -> bool:
        owner_process = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(window), ctypes.byref(owner_process))
        if owner_process.value != process_id or not user32.IsWindowVisible(wintypes.HWND(window)):
            return True
        class_name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(wintypes.HWND(window), class_name, 64)
        if class_name.value != _DIALOG_WINDOW_CLASS:
            return True
        found.append(window)
        return False

    user32.EnumWindows(visit, 0)
    return found[0] if found else 0


def _center_on_its_screen(window: int) -> None:
    user32 = ctypes.WinDLL("user32")
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = ctypes.c_void_p
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MonitorInfo)]
    info = _MonitorInfo()
    info.size = ctypes.sizeof(_MonitorInfo)
    frame = wintypes.RECT()
    monitor = user32.MonitorFromWindow(wintypes.HWND(window), _MONITOR_DEFAULTTONEAREST)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return
    if not user32.GetWindowRect(wintypes.HWND(window), ctypes.byref(frame)):
        return
    left = info.work.left + (info.work.right - info.work.left - (frame.right - frame.left)) // 2
    top = info.work.top + (info.work.bottom - info.work.top - (frame.bottom - frame.top)) // 2
    user32.SetWindowPos(
        wintypes.HWND(window),
        None,
        max(left, info.work.left),
        max(top, info.work.top),
        0,
        0,
        _SWP_NOSIZE | _SWP_NOZORDER | _SWP_NOACTIVATE,
    )


def _announce_when_shown() -> None:
    """Tell the server once the dialog is really on screen."""

    user32 = ctypes.WinDLL("user32")
    while True:
        window = _visible_dialog_window()
        if window:
            _center_on_its_screen(window)
            user32.SetForegroundWindow(wintypes.HWND(window))
            _answer("SHOWN")
            return
        time.sleep(0.05)


def _filter_specs(file_types: list[Any]) -> Any:
    entries = []
    for label, patterns in file_types:
        joined = ";".join(str(pattern) for pattern in patterns)
        entries.append(_FilterSpec(f"{label} ({joined})", joined))
    return (_FilterSpec * len(entries))(*entries)


def _show(request: dict[str, Any]) -> str:
    kind = str(request.get("kind") or "folder")
    ole32 = ctypes.OleDLL("ole32")
    ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    ole32.CoCreateInstance.argtypes = [
        ctypes.POINTER(_Guid),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_Guid),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    dialog = ctypes.c_void_p()
    ole32.CoCreateInstance(
        ctypes.byref(_guid(_CLSID_FILE_SAVE_DIALOG if kind == "save" else _CLSID_FILE_OPEN_DIALOG)),
        None,
        _CLSCTX_INPROC_SERVER,
        ctypes.byref(_guid(_IID_FILE_DIALOG)),
        ctypes.byref(dialog),
    )
    try:
        options = wintypes.DWORD()
        _method(dialog, _GET_OPTIONS, ctypes.POINTER(wintypes.DWORD))(ctypes.byref(options))
        wanted = options.value | _FOS_FORCEFILESYSTEM | _FOS_PATHMUSTEXIST
        if kind == "folder":
            wanted |= _FOS_PICKFOLDERS
        elif kind == "file":
            wanted |= _FOS_FILEMUSTEXIST
        _method(dialog, _SET_OPTIONS, wintypes.DWORD)(wanted)

        title = str(request.get("title") or "")
        if title:
            _method(dialog, _SET_TITLE, wintypes.LPCWSTR)(title)

        file_types = list(request.get("file_types") or [])
        if kind != "folder" and file_types:
            specs = _filter_specs(file_types)
            _method(dialog, _SET_FILE_TYPES, wintypes.UINT, ctypes.POINTER(_FilterSpec))(
                len(specs), specs
            )
        if kind == "save":
            default_name = str(request.get("default_name") or "")
            if default_name:
                _method(dialog, _SET_FILE_NAME, wintypes.LPCWSTR)(default_name)
            extension = os.path.splitext(default_name)[1].lstrip(".")
            if extension:
                _method(dialog, _SET_DEFAULT_EXTENSION, wintypes.LPCWSTR)(extension)

        # Without a usable starting folder the dialog opens where the user last
        # was, which beats any folder Bibliobrain could guess.
        initial_path = str(request.get("initial_path") or "")
        folder = _shell_item(initial_path) if initial_path and os.path.isdir(initial_path) else None
        if folder:
            try:
                _method(dialog, _SET_FOLDER, ctypes.c_void_p)(folder)
            finally:
                _release(folder)

        threading.Thread(target=_announce_when_shown, daemon=True).start()
        outcome = _method(dialog, _SHOW, wintypes.HWND, checked=False)(_owner_window())
        if outcome == _CANCELLED:
            return ""
        if outcome < 0:
            raise OSError(f"the system dialog failed (HRESULT {outcome & 0xFFFFFFFF:#010x})")

        selected = ctypes.c_void_p()
        _method(dialog, _GET_RESULT, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(selected))
        try:
            name = ctypes.c_wchar_p()
            _method(
                selected,
                _SHELL_ITEM_GET_DISPLAY_NAME,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.c_wchar_p),
            )(_SIGDN_FILESYSPATH, ctypes.byref(name))
            path = str(name.value or "")
            ctypes.WinDLL("ole32").CoTaskMemFree(ctypes.cast(name, ctypes.c_void_p))
            return path
        finally:
            _release(selected)
    finally:
        _release(dialog)


def main() -> int:
    request = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    try:
        # A sharp dialog on high-density screens; older Windows simply scales it.
        ctypes.WinDLL("user32").SetProcessDpiAwarenessContext(
            ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        )
    except (AttributeError, OSError):
        pass
    _answer("RESULT " + json.dumps({"path": _show(request)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
