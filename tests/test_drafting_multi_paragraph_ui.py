from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
APP_JS_PATH = ROOT / "static" / "js" / "app.js"


def _extract_js_function(source: str, name: str) -> str:
    marker = f"function {name}("
    start = source.index(marker)
    signature_end = source.index(")", start)
    brace_start = source.index("{", signature_end)
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


def test_drafting_query_extraction_preserves_contenteditable_block_boundaries() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the drafting editor regression test")

    source = read_app_javascript()
    function_names = [
        "_draftingGetCitationTokenCopyLabel",
        "_draftingSerializeCopyNode",
        "_draftingNormalizeSerializedText",
        "_draftingExtractPlainText",
    ]
    functions = "\n\n".join(
        _extract_js_function(source, name) for name in function_names
    )
    script = f"""
global.Node = {{ TEXT_NODE: 3, ELEMENT_NODE: 1, DOCUMENT_FRAGMENT_NODE: 11 }};

{functions}

const text = (value) => ({{
  nodeType: Node.TEXT_NODE,
  nodeValue: value,
}});
const element = (tagName, children = [], classNames = []) => ({{
  nodeType: Node.ELEMENT_NODE,
  tagName,
  childNodes: children,
  classList: {{ contains: (name) => classNames.includes(name) }},
  querySelector: () => null,
}});
const fragment = (children) => ({{
  nodeType: Node.DOCUMENT_FRAGMENT_NODE,
  childNodes: children,
}});

const first = element('DIV', [text('Premier paragraphe')]);
const citation = element('SPAN', [text('[1]')], ['drafting-citation-token']);
const second = element('P', [text('Deuxième paragraphe '), citation]);
const root = element('DIV', [first, second]);
const selected = fragment([first, second]);

console.log(JSON.stringify({{
  fullDraft: _draftingExtractPlainText(root),
  selectedDraft: _draftingExtractPlainText(selected),
}}));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )

    assert json.loads(completed.stdout) == {
        "fullDraft": "Premier paragraphe\n\nDeuxième paragraphe",
        "selectedDraft": "Premier paragraphe\n\nDeuxième paragraphe",
    }


def test_drafting_copy_keeps_inline_citations_inside_their_paragraph() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the drafting editor regression test")

    source = read_app_javascript()
    function_names = [
        "_draftingGetCitationTokenCopyLabel",
        "_draftingSerializeCopyNode",
        "_draftingNormalizeSerializedText",
        "_draftingExtractCopyText",
    ]
    functions = "\n\n".join(
        _extract_js_function(source, name) for name in function_names
    )
    script = f"""
global.Node = {{ TEXT_NODE: 3, ELEMENT_NODE: 1, DOCUMENT_FRAGMENT_NODE: 11 }};

{functions}

const text = (value) => ({{
  nodeType: Node.TEXT_NODE,
  nodeValue: value,
}});
const element = (tagName, children = [], classNames = []) => ({{
  nodeType: Node.ELEMENT_NODE,
  tagName,
  childNodes: children,
  classList: {{ contains: (name) => classNames.includes(name) }},
  querySelector: () => null,
  textContent: children.map((child) => child.nodeValue || child.textContent || '').join(''),
}});

const citation = element('SPAN', [text('[1]')], ['drafting-citation-token']);
citation.querySelector = (selector) => (
  selector === '.drafting-citation-link' ? {{ textContent: '[1]' }} : null
);
const first = element('P', [
  text('Cette affirmation est confirmée '),
  citation,
  text('.'),
]);
const second = element('P', [text('Paragraphe suivant.')]);
const root = element('DIV', [first, second]);

console.log(JSON.stringify(_draftingExtractCopyText(root)));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )

    assert json.loads(completed.stdout) == (
        "Cette affirmation est confirmée [1].\n\nParagraphe suivant."
    )


def test_drafting_selection_paths_use_structured_plain_text_extraction() -> None:
    source = read_app_javascript()

    selection_start = source.index("function _draftingGetSelectionText()")
    selection_end = source.index("function _draftingRangePointsIntoEditor", selection_start)
    selection_handler = source[selection_start:selection_end]

    query_start = source.index("function _draftingGetQueryText(")
    query_end = source.index("function _draftingExtractPlainText", query_start)
    query_handler = source[query_start:query_end]

    assert "_draftingExtractPlainText(selection.getRangeAt(0).cloneContents())" in selection_handler
    assert "_draftingExtractPlainText(storedRange.cloneContents())" in query_handler
