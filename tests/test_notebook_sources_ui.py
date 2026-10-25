from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).parents[1]
APP_JS = read_app_javascript()
WORKSPACES_CSS = (ROOT / "static" / "css" / "workspaces.css").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")


def test_empty_notebook_chat_suggestions_take_clicks() -> None:
    # The empty-chat block ignores the pointer so the conversation area stays
    # usable; its suggestion buttons must take clicks back.
    empty_rule = APP_CSS.split("#nb-empty-chat {", 1)[1].split("}", 1)[0]
    assert "pointer-events: none" in empty_rule
    assert ".nb-suggestions { pointer-events: auto;" in WORKSPACES_CSS


def test_only_library_papers_enter_a_notebook() -> None:
    assert "fetch('/api/library/match'" in APP_JS
    assert "const inLibrary = entries.filter((entry) => entry.path);" in APP_JS
    assert "_addDiscoveryItemsToNotebook(inLibrary, notebook)" in APP_JS
    # The others are listed with their publisher page, not added.
    assert "_discoveryMissingPapersHtml(missing, inLibrary.length)" in APP_JS
    assert "{ title: _discoveryNotebookSourceTitle(item), content: text, source_path: path }" in APP_JS
    assert "_discoveryNotebookSourceText" not in APP_JS
    # The citation graph adds papers the same way.
    assert "await _discoveryLibraryPaths([item])" in APP_JS


def test_each_notebook_source_says_what_the_chat_reads() -> None:
    assert "_ti('notebooks.statusPdf')" in APP_JS
    assert "_ti('notebooks.statusTextOnly')" in APP_JS
    assert "_ti('notebooks.viewPaper')" in APP_JS
