import time
from pathlib import Path
from typing import Any

import pytest

from src import native_dialogs

# Stand-ins for the helper process: same protocol, no window.
_ANSWERING_WORKER = """
import json, sys
request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
leaked = any(request["title"] in argument for argument in sys.argv)
answer = {"path": "LEAKED ON THE COMMAND LINE" if leaked else request["initial_path"]}
sys.stdout.buffer.write(("SHOWN\\nRESULT " + json.dumps(answer) + "\\n").encode("utf-8"))
"""

_CANCELLED_BEFORE_ANNOUNCING_WORKER = """
import sys
sys.stdin.buffer.read()
sys.stdout.buffer.write(b'RESULT {"path": ""}\\n')
"""

_NEVER_SHOWING_WORKER = """
import time
time.sleep(120)
"""

_CRASHING_WORKER = """
raise OSError("the system dialog failed")
"""


def _use_worker(monkeypatch: Any, directory: Path, source: str) -> None:
    worker = directory / "worker.py"
    worker.write_text(source, encoding="utf-8")
    monkeypatch.setattr(native_dialogs, "_WINDOWS_PICKER_WORKER", worker)


def _capture_requests(monkeypatch: Any, answer: str) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []

    def fake_run(request: dict[str, Any], *, failure: str) -> str:
        requests.append(request)
        return answer

    monkeypatch.setattr(native_dialogs, "_run_windows_picker", fake_run)
    return requests


def test_windows_directory_picker_asks_for_a_folder(monkeypatch: Any) -> None:
    requests = _capture_requests(monkeypatch, r"D:\Recherche\Articles")

    selected = native_dialogs._choose_windows_directory(
        initial_path=r"D:\Point de départ",
        title="Choisir les PDF",
    )

    assert selected == r"D:\Recherche\Articles"
    assert requests == [
        {"kind": "folder", "initial_path": r"D:\Point de départ", "title": "Choisir les PDF"}
    ]


def test_windows_file_picker_offers_metadata_files_then_all_files(monkeypatch: Any) -> None:
    requests = _capture_requests(monkeypatch, r"D:\Recherche\references.bib")

    selected = native_dialogs._choose_windows_file(
        initial_path=r"D:\Recherche",
        title="Choisir les métadonnées",
    )

    assert selected == r"D:\Recherche\references.bib"
    assert requests[0]["kind"] == "file"
    assert requests[0]["file_types"] == (
        ("Métadonnées bibliographiques", ("*.sqlite", "*.bib", "*.bibtex", "*.txt")),
        ("Tous les fichiers", ("*.*",)),
    )


def test_windows_save_picker_passes_name_and_file_types(monkeypatch: Any) -> None:
    requests = _capture_requests(monkeypatch, r"D:\Transfert\index.zip")

    selected = native_dialogs._choose_windows_save_file(
        initial_path=r"D:\Transfert",
        title="Exporter",
        default_name="Bibliobrain-index.zip",
        file_types=(("Index Bibliobrain", ("*.zip",)),),
    )

    assert selected == r"D:\Transfert\index.zip"
    assert requests[0]["kind"] == "save"
    assert requests[0]["default_name"] == "Bibliobrain-index.zip"
    assert requests[0]["file_types"] == (
        ("Index Bibliobrain", ("*.zip",)),
        ("Tous les fichiers", ("*.*",)),
    )


def test_windows_picker_sends_user_values_on_stdin_and_reads_the_answer(
    monkeypatch: Any, workspace_tmp_dir: Path
) -> None:
    _use_worker(monkeypatch, workspace_tmp_dir, _ANSWERING_WORKER)

    selected = native_dialogs._run_windows_picker(
        {"kind": "folder", "initial_path": r"D:\Point de départ", "title": "Choisir les PDF"},
        failure="picker failed",
    )

    assert selected == r"D:\Point de départ"


def test_windows_picker_accepts_an_answer_given_before_the_dialog_is_announced(
    monkeypatch: Any, workspace_tmp_dir: Path
) -> None:
    _use_worker(monkeypatch, workspace_tmp_dir, _CANCELLED_BEFORE_ANNOUNCING_WORKER)

    assert native_dialogs._run_windows_picker({"kind": "folder"}, failure="picker failed") == ""


def test_windows_picker_gives_up_when_no_dialog_appears(
    monkeypatch: Any, workspace_tmp_dir: Path
) -> None:
    """A dialog that never shows must become an error, not an endless wait."""
    _use_worker(monkeypatch, workspace_tmp_dir, _NEVER_SHOWING_WORKER)
    monkeypatch.setattr(native_dialogs, "_WINDOWS_PICKER_APPEAR_TIMEOUT_SECONDS", 1.0)

    started = time.monotonic()
    with pytest.raises(native_dialogs.NativeDialogUnavailable):
        native_dialogs._run_windows_picker({"kind": "folder"}, failure="picker failed")

    assert time.monotonic() - started < 30


def test_windows_picker_reports_a_failing_helper(monkeypatch: Any, workspace_tmp_dir: Path) -> None:
    _use_worker(monkeypatch, workspace_tmp_dir, _CRASHING_WORKER)

    with pytest.raises(native_dialogs.NativeDialogUnavailable):
        native_dialogs._run_windows_picker({"kind": "folder"}, failure="picker failed")


def test_picker_starts_in_an_existing_folder_or_lets_the_system_choose(
    workspace_tmp_dir: Path,
) -> None:
    document = workspace_tmp_dir / "article.pdf"
    document.write_bytes(b"%PDF")

    assert native_dialogs._existing_initial_directory(str(workspace_tmp_dir)) == str(
        workspace_tmp_dir
    )
    assert native_dialogs._existing_initial_directory(str(document)) == str(workspace_tmp_dir)
    assert native_dialogs._existing_initial_directory(str(workspace_tmp_dir / "absent")) == ""
    assert native_dialogs._existing_initial_directory("") == ""
