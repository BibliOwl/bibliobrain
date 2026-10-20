
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
APP_JS = read_app_javascript()


def test_replacing_a_hint_does_not_keep_its_hidden_tool_selection() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the chat hint behavior test")
    start = APP_JS.index("function resolveChatHintAction(")
    end = APP_JS.index("function applyChatHintByKey(", start)
    script = APP_JS[start:end] + """
const input = { dataset: { hintAction: 'bibliography', hintText: 'Génère la bibliographie de ce chat' } };
const replaced = resolveChatHintAction(input, 'Rédige une introduction. Plan : discussion et références.');
const original = resolveChatHintAction(input, input.dataset.hintText);
const appended = resolveChatHintAction(input, input.dataset.hintText + ' en APA');
input.dataset = { hintAction: 'article_search', hintText: 'Cherche des articles sur ...' };
const completed = resolveChatHintAction(input, 'Cherche des articles sur les bobines HTS');
const newTask = resolveChatHintAction(input, 'Rédige une introduction pour mon article');
console.log(JSON.stringify([replaced, original, appended, completed, newTask]));
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == ["", "bibliography", "bibliography", "article_search", ""]
    assert "input.dataset.hintText = value" in APP_JS
    assert ": resolveChatHintAction(input, msg)" in APP_JS


def test_chat_ui_applies_guarded_answer_replacement_atomically() -> None:
    branch = "event.type === 'answer_replace'"
    assert branch in APP_JS
    branch_start = APP_JS.index(branch)
    branch_end = APP_JS.index("event.type === 'discovery_session'", branch_start)
    handler = APP_JS[branch_start:branch_end]

    assert "fullText = String(event.text || '')" in handler
    assert "debugTracker?.replace(fullText)" in handler
    assert "renderRichAssistantOutput(bubble, fullText" in handler
    assert "normalizeCitations: false" in handler


def test_chat_debug_metrics_replace_draft_with_final_text() -> None:
    tracker_start = APP_JS.index("function createDebugMetricsTracker")
    tracker_end = APP_JS.index("function withAnswerOrigin", tracker_start)
    tracker = APP_JS[tracker_start:tracker_end]

    assert "replace(text)" in tracker
    assert "outputText = replacement" in tracker
