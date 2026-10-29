
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


@pytest.mark.parametrize("navigate_before_meta", [False, True])
@pytest.mark.parametrize("existing_session", [False, True])
def test_conversation_is_in_sidebar_before_generation_finishes(
    navigate_before_meta: bool, existing_session: bool
) -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the chat streaming behavior test")
    start = APP_JS.index("async function sendMessage(")
    end = APP_JS.index("async function _autotitleChat(", start)
    script = r"""
const assert = require('node:assert/strict');
const navigateBeforeMeta = NAVIGATE_BEFORE_META;
const existingSession = EXISTING_SESSION;
const state = {
  chatSessions: [{ id: 'old', title: 'Old chat' }],
  activeChatId: existingSession ? 'new' : '',
  chatViewRevision: 0, chatArticleScopeRevision: 0,
};
if (existingSession) state.chatSessions.push({ id: 'new', title: 'Previous title' });
const input = { value: 'My question', dataset: {}, style: {} };
const bubble = { classList: { contains: () => false } };
const document = { getElementById: () => input };
const localStorage = { setItem() {} };
const LS_PREFIX = '';
const window = { dispatchEvent() {} };
class CustomEvent {}
const noop = () => {};
const syncOllamaBaseUrl = async () => {};
const resolveChatHintAction = () => '';
const requireSelectedModel = () => 'model';
const parseFilterInput = () => [];
const _chatArticleScopePaths = () => [];
const _setSendButtonStop = noop;
const _setSendButtonIdle = noop;
const updateChatVisualForceUi = noop;
const _appendUserBubble = noop;
const _appendAssistantPlaceholder = () => bubble;
const isDebugMetricsEnabled = () => false;
const isLocalOllamaProvider = () => false;
const isThinkingEnabled = () => false;
const isForceThinkingSmallModelsEnabled = () => false;
const getChatDepth = () => 'standard';
const renderChatHistory = () => { renderedSessions = [...state.chatSessions]; };
let renderedSessions = [];
let historyReloaded = false;
const loadChatHistory = async () => { historyReloaded = true; };
const refreshActiveChatIfStillOpen = async () => {};
let releaseStream;
const blocked = new Promise(resolve => { releaseStream = resolve; });
let metaHandled;
const handled = new Promise(resolve => { metaHandled = resolve; });
const fetch = async () => {
  if (navigateBeforeMeta) {
    state.chatViewRevision += 1;
    state.activeChatId = 'old';
    state.activeChatTitle = 'Old chat';
  }
  return { ok: true };
};
async function* readSSE() {
  yield { type: 'chat_meta', chat: { id: 'new', title: 'My question' }, created: !existingSession };
  // Exercise repeated metadata too: the sidebar must not duplicate the chat.
  yield { type: 'chat_meta', chat: { id: 'new', title: 'My question' }, created: !existingSession };
  metaHandled();
  await blocked;
}
"""
    script = script.replace("NAVIGATE_BEFORE_META", json.dumps(navigate_before_meta))
    script = script.replace("EXISTING_SESSION", json.dumps(existing_session))
    script += APP_JS[start:end] + r"""
(async () => {
  const sending = sendMessage();
  await handled;
  assert.equal(state.streaming, true);
  assert.equal(historyReloaded, false);
  assert.deepEqual(renderedSessions.map(chat => chat.id), ['new', 'old']);
  assert.equal(renderedSessions[0].title, 'My question');
  assert.equal(state.activeStreamChatId, 'new');
  assert.equal(state.activeChatId, navigateBeforeMeta ? 'old' : 'new');
  if (navigateBeforeMeta) assert.equal(state.activeChatTitle, 'Old chat');
  // Switching away after metadata must keep the new conversation reachable.
  state.activeChatId = 'old';
  renderChatHistory();
  assert.equal(renderedSessions[0].id, 'new');
  releaseStream();
  await sending;
  assert.equal(state.streaming, false);
  assert.equal(historyReloaded, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run([node, "-e", script], capture_output=True, text=True, check=True, timeout=15)
