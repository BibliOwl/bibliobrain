from __future__ import annotations

import importlib
import sys

import pytest

from src import native_runtime

BLOCKED = (
    "[WinError 4551] Une stratégie de contrôle d’application a bloqué ce fichier. "
    'Error loading "X:\\venv\\Lib\\site-packages\\torch\\lib\\c10.dll" or one of its dependencies.'
)


def test_blocked_dll_is_recognised_and_explained() -> None:
    assert native_runtime.is_native_library_blocked(OSError(BLOCKED))
    message = native_runtime.native_library_message(OSError(BLOCKED), "fr")
    assert "torch" in message
    assert "Contrôle intelligent des applications" in message
    assert "inutile de le reconstruire" in message


def test_missing_dll_is_not_reported_as_a_policy_block() -> None:
    error = ImportError("DLL load failed while importing chromadb_rust_bindings: module introuvable")
    assert not native_runtime.is_native_library_blocked(error)
    message = native_runtime.native_library_message(error, "en")
    assert "chromadb" in message
    assert "Visual C++" in message


def test_ordinary_errors_keep_their_own_message() -> None:
    assert native_runtime.native_library_message(ValueError("collection is corrupted")) == ""


def test_guard_turns_a_blocked_native_import_into_import_error(tmp_path, monkeypatch) -> None:
    package = tmp_path / "fake_native_pkg"
    package.mkdir()
    (package / "__init__.py").write_text(f"raise OSError({BLOCKED!r})\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(native_runtime, "_GUARDED_TOP_LEVEL", frozenset({"fake_native_pkg"}))
    native_runtime.install_native_import_guard()
    sys.modules.pop("fake_native_pkg", None)

    with pytest.raises(ImportError) as caught:
        importlib.import_module("fake_native_pkg")

    assert isinstance(caught.value.__cause__, OSError)
    assert "fake_native_pkg" in native_runtime.native_library_failures()
    assert "fake_native_pkg" not in sys.modules


def test_guard_is_installed_once_and_leaves_healthy_imports_alone() -> None:
    native_runtime.install_native_import_guard()
    native_runtime.install_native_import_guard()
    guards = [finder for finder in sys.meta_path if isinstance(finder, native_runtime._NativeImportGuard)]
    assert len(guards) == 1
    assert importlib.import_module("json").dumps({}) == "{}"


def test_pdf_artifact_repair_restores_latex_accents_but_not_exponents() -> None:
    from src.unicode_utils import repair_pdf_text_artifacts

    assert repair_pdf_text_artifacts("c´ eramiques ` a haute temp´ erature") == (
        "céramiques à haute température"
    )
    assert repair_pdf_text_artifacts("fran¸ cais, ˆ etre, na¨ ive") == "français, être, naïve"
    assert repair_pdf_text_artifacts("the ﬁeld and eﬀects") == "the field and effects"
    assert repair_pdf_text_artifacts("E = Ec (J/Jc)^n and `code`") == "E = Ec (J/Jc)^n and `code`"
