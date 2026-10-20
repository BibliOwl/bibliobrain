"""App shell: grouped sidebar, recent chats, status pill tones, composer."""

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
CHAT_JS = (ROOT / "static" / "js" / "app" / "chat.js").read_text(encoding="utf-8")
SETTINGS_JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
SHELL_CSS = (ROOT / "static" / "css" / "shell.css").read_text(encoding="utf-8")


def _sidebar() -> str:
    return HTML[HTML.index('<aside id="sidebar">') : HTML.index("</aside>")]


def test_nav_is_grouped_by_what_you_want_to_do() -> None:
    nav = _sidebar()
    order = [
        'data-i18n="nav.groupAsk"', 'data-tab="chat"', 'data-tab="library"',
        'data-i18n="nav.groupExplore"', 'data-tab="discovery"', 'data-tab="connections"',
        'data-i18n="nav.groupWrite"', 'data-tab="notebooks"', 'data-tab="drafting"',
    ]
    positions = [nav.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert 'data-i18n="nav.explorer"' not in HTML


def test_model_section_left_the_sidebar_for_the_composer_chip() -> None:
    nav = _sidebar()
    assert 'id="model-select"' not in nav and "nav-config" not in nav
    composer = HTML[HTML.index('id="chat-composer"') : HTML.index("<!-- LIBRARY -->")]
    # The hidden select keeps the loading / change logic of settings.js.
    state = composer[composer.index('class="model-chip-state" hidden') :]
    assert state.index('id="model-select"') < state.index("</div>")
    assert 'id="model-chip"' in composer and 'data-bb-static-onclick="toggle-model-menu"' in composer
    assert "selectSettingsChatModel(option.value)" in SETTINGS_JS
    # Suggestions, Zotero filters and figure reading move into the Options menu.
    options = composer[composer.index('id="chat-options-menu"') :]
    for element in ('id="chat-command-hints"', 'id="chat-filters-panel"', 'id="chat-force-visual"'):
        assert element in options
    assert 'id="chat-hints-toggle"' not in HTML and 'data-i18n="chat.depthLabel"' not in HTML


def test_chat_history_lives_under_chat_in_the_sidebar() -> None:
    nav = _sidebar()
    assert 'id="sidebar-chat-recent"' in nav and 'id="chat-history-all-btn"' in nav
    assert 'id="chat-history-search"' in nav and 'id="chat-history-list"' in nav
    assert 'id="chat-history-wrap"' not in HTML and 'id="chat-history-resizer"' not in HTML
    assert "const CHAT_RECENT_LIMIT = 5;" in CHAT_JS
    # Only while the Chat tab is active.
    assert ".nav-chat-row:has(.nav-item.active) + .nav-chat-history { display: flex; }" in SHELL_CSS


def test_empty_chat_starters_reach_a_tool_that_can_answer() -> None:
    starters = CHAT_JS[CHAT_JS.index("const CHAT_STARTERS = [") :]
    starters = starters[: starters.index("];")]
    assert starters.count("{ id:") == 4
    # The bibliography action only covers sources already cited in the chat:
    # an empty chat has none, so "Cite" lists the library's articles instead.
    assert "hintAction: 'bibliography'" not in starters
    for starter in ("Find", "Summarize", "Compare", "Cite"):
        prompt = re.search(rf"'chat\.starter{starter}Prompt': \{{ fr: '([^']*)'", I18N)
        assert prompt and "..." in prompt.group(1)


def test_new_shell_strings_exist_in_french_and_english() -> None:
    keys = set(re.findall(r"_ti\('((?:status|nav\.group|chat\.starter)[\w.]*)'", "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "static" / "js").rglob("*.js")
    )))
    keys |= set(re.findall(r'data-i18n="((?:nav\.group|chat\.allConversations|chat\.options|sidebar\.settings)[\w.]*)"', HTML))
    assert keys
    for key in keys:
        entry = re.search(rf"'{re.escape(key)}': \{{\s*fr: [\s\S]*?en: ", I18N)
        assert entry, key
