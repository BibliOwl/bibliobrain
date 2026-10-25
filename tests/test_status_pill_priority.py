from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
OCR_JS = (ROOT / "static" / "js" / "app" / "ocr.js").read_text(encoding="utf-8")


def _extract_js_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Unterminated JavaScript function: {name}")


def _shown_task(tasks: dict[str, dict]) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the status pill regression test")
    functions = "\n".join(
        _extract_js_function(APP_JS, name)
        for name in ("_statusToneRank", "_sortedStatusTaskIds", "_shownStatusTaskId")
    )
    script = f"""
const _STATUS_TASK_ORDER = ['ollama', 'rag', 'ocr', 'reranker'];
const _STATUS_TONES = ['blocked', 'needs', 'working', 'ready'];
const _statusTasks = new Map(Object.entries({json.dumps(tasks)}));
{functions}
process.stdout.write(_shownStatusTaskId());
"""
    return subprocess.run(
        [node, "-e", script], check=True, capture_output=True, text=True, cwd=ROOT
    ).stdout


def test_interrupted_ocr_leaves_the_pill_to_a_running_library_update() -> None:
    # Before: the interrupted OCR ("needs", Resume) hid the update behind "+1".
    assert _shown_task({
        "rag": {"tone": "working"},
        "ocr": {"tone": "needs", "yieldsToWork": True},
    }) == "rag"


def test_interrupted_ocr_still_asks_to_resume_when_nothing_else_runs() -> None:
    assert _shown_task({
        "rag": {"tone": "ready"},
        "ocr": {"tone": "needs", "yieldsToWork": True},
    }) == "ocr"


def test_other_tasks_that_need_the_user_keep_priority_over_work() -> None:
    assert _shown_task({
        "rag": {"tone": "needs"},
        "ocr": {"tone": "working"},
    }) == "rag"
    assert _shown_task({
        "rag": {"tone": "working"},
        "ollama": {"tone": "blocked"},
    }) == "ollama"


def test_ocr_registers_its_resume_request_as_yielding_to_work() -> None:
    assert "yieldsToWork: true" in OCR_JS
