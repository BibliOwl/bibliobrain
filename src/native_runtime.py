"""Diagnose native libraries (torch, chromadb) that Windows refuses to load.

Smart App Control and WDAC policies block unsigned DLLs with WinError 4551.
The failure surfaces as an ``OSError`` or ``ImportError`` deep inside an
import chain, and used to be reported as a broken index. Recognising it lets
the UI point at the real cause instead of suggesting a pointless rebuild.
"""

from __future__ import annotations

import importlib.abc
import importlib.machinery
import sys
import threading
from typing import Any

_GUARDED_TOP_LEVEL = frozenset({"torch"})
_failures: dict[str, str] = {}
_failures_lock = threading.Lock()

_BLOCKED_MARKERS = (
    "winerror 4551",
    "application control policy",
    "contrôle d’application",
    "contrôle d'application",
    "controle d'application",
)
_DLL_MARKERS = ("dll load failed", "error loading", ".dll")

NATIVE_LIBRARY_BLOCKED_MESSAGE = {
    "fr": (
        "Windows bloque une bibliothèque native nécessaire à l'index ({library}). "
        "Cause la plus fréquente : le Contrôle intelligent des applications ou une "
        "stratégie WDAC. L'index n'est pas abîmé, inutile de le reconstruire : "
        "autorisez Bibliobrain ou désactivez ce contrôle, puis relancez l'application."
    ),
    "en": (
        "Windows is blocking a native library the index needs ({library}). "
        "Most common cause: Smart App Control or a WDAC policy. The index is not "
        "damaged, do not rebuild it: allow Bibliobrain or turn that control off, "
        "then restart the application."
    ),
}
NATIVE_LIBRARY_MISSING_MESSAGE = {
    "fr": (
        "Une bibliothèque native nécessaire à l'index ({library}) ne se charge pas. "
        "L'index n'est pas en cause : réinstallez les dépendances (install.ps1) ou le "
        "runtime Visual C++, puis relancez l'application."
    ),
    "en": (
        "A native library the index needs ({library}) does not load. The index is "
        "not the problem: reinstall the dependencies (install.ps1) or the Visual C++ "
        "runtime, then restart the application."
    ),
}


def _text(error: BaseException | str | None) -> str:
    return str(error or "").casefold()


def is_native_library_blocked(error: BaseException | str | None) -> bool:
    """True when the OS refused to load a DLL because of an execution policy."""
    text = _text(error)
    return any(marker in text for marker in _BLOCKED_MARKERS)


def is_native_library_failure(error: BaseException | str | None) -> bool:
    """True for a blocked or missing native DLL, as opposed to bad data."""
    text = _text(error)
    return is_native_library_blocked(error) or any(marker in text for marker in _DLL_MARKERS)


def _library_name(text: str) -> str:
    for name in ("torch", "chromadb", "onnxruntime", "tokenizers"):
        if name in text:
            return name
    return "DLL"


def native_library_message(error: BaseException | str | None, lang: str = "fr") -> str:
    """User-facing explanation, or "" when ``error`` is not a native DLL failure."""
    if not is_native_library_failure(error):
        return ""
    table = (
        NATIVE_LIBRARY_BLOCKED_MESSAGE
        if is_native_library_blocked(error)
        else NATIVE_LIBRARY_MISSING_MESSAGE
    )
    template = table.get(lang) or table["fr"]
    return template.format(library=_library_name(_text(error)))


def native_library_failures() -> dict[str, str]:
    """Native imports that failed in this process, by top-level module name."""
    with _failures_lock:
        return dict(_failures)


class _GuardedLoader(importlib.abc.Loader):
    def __init__(self, name: str, wrapped: Any) -> None:
        self._name = name
        self._wrapped = wrapped

    def __getattr__(self, name: str) -> Any:
        # Resource readers, get_data, is_package... stay the real loader's.
        return getattr(self._wrapped, name)

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> Any:
        return self._wrapped.create_module(spec)

    def exec_module(self, module: Any) -> None:
        try:
            self._wrapped.exec_module(module)
        except OSError as exc:
            # torch raises OSError when a policy blocks c10.dll. Optional
            # integrations (sentence-transformers, text splitters, rerankers)
            # only expect ImportError, so an OSError took the whole import
            # chain down, including the library listing.
            with _failures_lock:
                _failures[self._name] = str(exc)
            raise ImportError(f"{self._name} unavailable: {exc}", name=self._name) from exc


class _NativeImportGuard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> Any:
        if fullname not in _GUARDED_TOP_LEVEL:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _GuardedLoader(fullname, spec.loader)
        return spec


def install_native_import_guard() -> None:
    """Idempotently register the guard ahead of the default path finder."""
    if not any(isinstance(finder, _NativeImportGuard) for finder in sys.meta_path):
        sys.meta_path.insert(0, _NativeImportGuard())
