"""Cartographies › "Depuis des articles" with Windows paths.

Codex audit BB-01: the paths went into data-article-graph-* attributes
through escapeAttr(), which doubles backslashes for inline JS strings. The
dataset value then no longer matched the library key, so clicking a search
result said "none of these articles is in the library" and the remove
button of a chosen article did nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS_PATH = r"C:\Users\x\Zotero\storage\AB\a.pdf"


def _slice_js(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


def _run(script_body: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the article graph path test")
    rendering = (ROOT / "static/js/app/assistant-rendering.js").read_text(encoding="utf-8")
    helpers = _slice_js(rendering, "function escapeHtml(", "function escapeAttr(") + _slice_js(
        rendering, "function escapeAttr(", "\n}\n"
    ) + "\n}\n"
    article_graph = (ROOT / "static/js/app/article-graph.js").read_text(encoding="utf-8")
    script = f"""
const elements = {{}};
function el(id) {{
  if (!elements[id]) {{
    elements[id] = {{
      id, innerHTML: '', textContent: '', value: '', hidden: false, disabled: false,
      dataset: {{}}, focus() {{}}, select() {{}},
      querySelector() {{ return {{ textContent: '' }}; }},
    }};
  }}
  return elements[id];
}}
const document = {{ getElementById: el, querySelector: () => null }};
const _ti = (key) => key;
const state = {{}};
// What the browser hands back through element.dataset: entity-decoded text.
function datasetValue(html, attr) {{
  const re = new RegExp(attr + '="([^"]*)"');
  const match = html.match(re);
  if (!match) return null;
  return match[1]
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
}}
{helpers}
{article_graph}
const LIB_PATH = {json.dumps(WINDOWS_PATH)};
state.libPaths = [LIB_PATH];
state.libRows = [['Quench in REBCO tapes', 'Dilasser', '2017']];
state.libPathIndex = new Map([[LIB_PATH, 0]]);
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


def test_search_result_with_windows_path_adds_and_removes_article() -> None:
    result = _run(
        """
el('article-graph-search').value = 'dilasser';
_articleGraphRenderResults();
const resultPath = datasetValue(el('article-graph-results').innerHTML, 'data-article-graph-path');
_articleGraphAdd([resultPath]);
const afterAdd = [..._articleGraph.paths];
const statusAfterAdd = el('article-graph-status').textContent;
const removePath = datasetValue(el('article-graph-selected').innerHTML, 'data-article-graph-remove');
_articleGraphRemove(removePath);
console.log(JSON.stringify({
  resultPath, afterAdd, statusAfterAdd, removePath, afterRemove: _articleGraph.paths,
}));
"""
    )
    assert result["resultPath"] == WINDOWS_PATH
    assert result["afterAdd"] == [WINDOWS_PATH]
    assert result["statusAfterAdd"] == ""
    assert result["removePath"] == WINDOWS_PATH
    assert result["afterRemove"] == []


def test_clicking_a_chosen_result_again_removes_it() -> None:
    result = _run(
        """
_articleGraphAdd([LIB_PATH]);
el('article-graph-search').value = 'quench';
_articleGraphRenderResults();
const resultPath = datasetValue(el('article-graph-results').innerHTML, 'data-article-graph-path');
const chosen = _articleGraph.paths.includes(resultPath);
if (chosen) _articleGraphRemove(resultPath);
console.log(JSON.stringify({ chosen, after: _articleGraph.paths }));
"""
    )
    assert result == {"chosen": True, "after": []}
