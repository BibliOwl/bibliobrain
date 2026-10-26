from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "static" / "js" / "app.js"
APP_CSS = ROOT / "static" / "css" / "app.css"
INDEX_HTML = ROOT / "static" / "index.html"


def _extract_js_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    for index in range(brace_start, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Unterminated JavaScript function: {name}")


def test_chat_source_labels_are_stable_across_the_conversation() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend citation-label regression test")

    source = read_app_javascript()
    function_names = [
        "normalizeAssistantCitationLabel",
        "sourceRefLabel",
        "resetChatSourceRegistry",
        "_normalizeChatSourceIdentityPart",
        "chatSourceIdentityKey",
        "ensureChatSourceRefLabel",
        "normalizeChatSourcesForRender",
        "collectSourceLabelAliases",
        "buildAssistantCitationSourceMap",
        "isAssistantCitationLikePart",
        "cleanupAssistantCitationArtifacts",
        "normalizeAssistantCitations",
    ]
    functions = "\n\n".join(_extract_js_function(source, name) for name in function_names)
    script = f"""
{functions}

const state = {{}};
resetChatSourceRegistry();

const firstSources = normalizeChatSourcesForRender([
  {{ index: 1, ref_label: 'S1', chunk_uid: 'paper-a:chunk-1', title: 'Aubert' }},
]);
const secondSources = normalizeChatSourcesForRender([
  {{ index: 1, ref_label: 'S1', chunk_uid: 'paper-b:chunk-1', title: 'Tsuchiya' }},
  {{ index: 2, ref_label: 'S2', chunk_uid: 'paper-c:chunk-1', title: 'Uglietti' }},
]);
const repeatedSources = normalizeChatSourcesForRender([
  {{ index: 3, ref_label: 'S3', chunk_uid: 'paper-a:chunk-1', title: 'Aubert' }},
]);

const secondSourceMap = buildAssistantCitationSourceMap(secondSources);
const normalizedSecondAnswer = normalizeAssistantCitations(
  'Second answer [S1] and [S2].',
  secondSources,
);

resetChatSourceRegistry();
const replayFirstSources = normalizeChatSourcesForRender([
  {{ index: 1, ref_label: 'S1', chunk_uid: 'paper-a:chunk-1', title: 'Aubert' }},
]);
const replaySecondSources = normalizeChatSourcesForRender([
  {{ index: 1, ref_label: 'S1', chunk_uid: 'paper-b:chunk-1', title: 'Tsuchiya' }},
]);

console.log(JSON.stringify({{
  labels: [
    firstSources[0].ref_label,
    secondSources[0].ref_label,
    secondSources[1].ref_label,
    repeatedSources[0].ref_label,
  ],
  rawLabels: [
    firstSources[0].raw_ref_label,
    secondSources[0].raw_ref_label,
    secondSources[1].raw_ref_label,
    repeatedSources[0].raw_ref_label,
  ],
  secondAliases: [
    secondSourceMap.get('S2')?.title || '',
    secondSourceMap.get('S3')?.title || '',
  ],
  normalizedSecondAnswer,
  replayLabels: [replayFirstSources[0].ref_label, replaySecondSources[0].ref_label],
}}));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    payload = json.loads(completed.stdout)

    assert payload == {
        "labels": ["S1", "S2", "S3", "S1"],
        "rawLabels": ["S1", "S1", "S2", "S3"],
        "secondAliases": ["Tsuchiya", "Uglietti"],
        "normalizedSecondAnswer": "Second answer [S2] and [S3].",
        "replayLabels": ["S1", "S2"],
    }


def test_chat_source_summary_label_has_no_vertical_offset() -> None:
    css = APP_CSS.read_text(encoding="utf-8")

    assert ".assistant-sources-summary .assistant-section-label {" in css
    scoped_rule = css.split(
        ".assistant-sources-summary .assistant-section-label {",
        maxsplit=1,
    )[
        1
    ].split("}", maxsplit=1)[0]
    assert "margin-bottom: 0;" in scoped_rule


def test_pdf_source_opens_the_page_it_displays() -> None:
    # Codex audit BB-03 (2026-10-25): the card said "Page 126" while its
    # button opened #page=125, because PDF pages are indexed from 0.
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend page-number regression test")

    source = read_app_javascript()
    functions = "\n\n".join(
        _extract_js_function(source, name)
        for name in ("sourcePageNumber", "sourcePageLabel", "sourceOpenDocumentUrl")
    )
    samples = {
        "labelled": {"page": 125, "content_type": "pdf_page", "location_label": "Page 126"},
        "first": {"page": 0, "content_type": "pdf_page", "location_label": "Page 1"},
        "old_index": {"page": "125"},
        "no_page": {"page": "?"},
        "slide": {"page": 4, "content_type": "presentation_slide", "location_label": "Diapositive 4"},
    }
    script = f"""
{functions}
const samples = {json.dumps(samples)};
const result = {{}};
for (const [name, source] of Object.entries(samples)) {{
  result[name] = [sourcePageLabel(source), sourceOpenDocumentUrl(source, 'a.pdf')];
}}
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        [node, "-e", script], check=True, capture_output=True, text=True, encoding="utf-8", cwd=ROOT
    )
    result = json.loads(completed.stdout)

    assert result["labelled"] == ["Page 126", "/api/open-pdf?path=a.pdf#page=126"]
    assert result["first"] == ["Page 1", "/api/open-pdf?path=a.pdf#page=1"]
    assert result["old_index"] == ["p.126", "/api/open-pdf?path=a.pdf#page=126"]
    assert result["no_page"] == ["", "/api/open-pdf?path=a.pdf"]
    assert result["slide"] == ["Diapositive 4", "/api/open-pdf?path=a.pdf"]
