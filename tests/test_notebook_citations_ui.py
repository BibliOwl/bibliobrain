"""Notebook chat citations written as [S1] / [S2].

Codex audit BB-06: notebook sources are numbered 1..N, so an answer citing
[S1], [S2] and [3] kept only [3]; the [Sx] citations were deleted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import read_app_javascript
from test_chat_source_labels import _extract_js_function

ROOT = Path(__file__).resolve().parents[1]

NOTEBOOK_SOURCES = [
    {"source_path": "C:/papers/a.pdf", "title": "Dilasser 2017", "content": "a"},
    {"source_path": "C:/papers/b.pdf", "title": "Tsuchiya 2019", "content": "b"},
    {"source_path": "C:/papers/c.pdf", "title": "Uglietti 2020", "content": "c"},
]


def _run(script_body: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the notebook citation test")
    source = read_app_javascript()
    names = [
        "normalizeAssistantCitationLabel",
        "sourceRefLabel",
        "collectSourceLabelAliases",
        "buildAssistantCitationSourceMap",
        "isAssistantCitationLikePart",
        "cleanupAssistantCitationArtifacts",
        "normalizeAssistantCitations",
        "assistantCitationEntries",
    ]
    functions = "\n\n".join(_extract_js_function(source, name) for name in names)
    script = f"""
{functions}
const sources = {json.dumps(NOTEBOOK_SOURCES)};
{script_body}
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    return json.loads(completed.stdout)


def test_notebook_answer_keeps_s_prefixed_citations() -> None:
    result = _run(
        """
const text = normalizeAssistantCitations('Le quench [S1], la marge [S2] et la loi [3].', sources);
const map = buildAssistantCitationSourceMap(sources);
const entries = ['1', '2', '3'].map((label) => assistantCitationEntries(label, map)[0]);
console.log(JSON.stringify({
  text,
  entries: entries.map((entry) => [entry.displayLabel, entry.source.title]),
  grouped: normalizeAssistantCitations('Deux sources [S1, S2].', sources),
}));
"""
    )
    assert result["text"] == "Le quench [1], la marge [2] et la loi [3]."
    assert result["entries"] == [
        ["1", "Dilasser 2017"],
        ["2", "Tsuchiya 2019"],
        ["3", "Uglietti 2020"],
    ]
    assert result["grouped"] == "Deux sources [1, 2]."


def test_unknown_notebook_citation_stays_visible() -> None:
    result = _run(
        """
console.log(JSON.stringify({
  alone: normalizeAssistantCitations('Hors carnet [S9].', sources),
  mixed: normalizeAssistantCitations('Mélange [S1, S9].', sources),
}));
"""
    )
    assert result["alone"] == "Hors carnet [S9]."
    assert result["mixed"] == "Mélange [1] [S9]."
