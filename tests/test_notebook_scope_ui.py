from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).parents[1]
APP_JS = read_app_javascript()
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BINDINGS_JS = (ROOT / "static" / "js" / "static-event-bindings.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
CHAT_PY = (ROOT / "src" / "api" / "chat.py").read_text(encoding="utf-8")


def test_chat_composer_can_be_limited_to_a_notebook() -> None:
    assert 'id="chat-notebook-btn"' in INDEX_HTML
    assert 'data-bb-static-onclick="pick-chat-notebook"' in INDEX_HTML
    assert "openChatNotebookPicker()" in BINDINGS_JS
    # Only library PDFs can scope the chat; text sources stay out.
    assert "const path = String(source?.source_path || '').trim();" in APP_JS
    assert "notebook: { id: String(notebook.id || notebookId), title }," in APP_JS
    assert "article_scope_notebook_id: articleScope.notebookId," in APP_JS


def test_notebook_scope_survives_reopening_a_conversation() -> None:
    assert CHAT_PY.count('"article_scope_notebook_id": str(req.article_scope_notebook_id or "").strip(),') == 2
    assert "const notebookId = String(meta.article_scope_notebook_id || '').trim();" in APP_JS
    assert "notebook: scope.notebook" in APP_JS
