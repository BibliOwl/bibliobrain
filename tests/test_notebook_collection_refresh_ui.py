
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).parents[1]
APP_JS = read_app_javascript()
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BINDINGS_JS = (ROOT / "static" / "js" / "static-event-bindings.js").read_text(
    encoding="utf-8"
)


def test_add_source_form_scrolls_into_view_and_receives_focus() -> None:
    assert "body.scrollTo({top: body.scrollHeight, behavior: preferredScrollBehavior()})" in APP_JS
    assert "form.querySelector('#nb-source-type')?.focus()" in APP_JS


def test_collection_notebook_exposes_a_refresh_action() -> None:
    assert 'id="nb-refresh-collection-btn"' in INDEX_HTML
    assert 'data-bb-static-onclick="refresh-nb-collection"' in INDEX_HTML
    assert "refreshNbCollection()" in BINDINGS_JS
    assert "notebook_id: _nb.id" in APP_JS
    assert "collection: collection.name" in APP_JS
    assert "button.hidden = !collection" in APP_JS


def test_zotero_collection_creation_uses_a_populated_select() -> None:
    assert '<select id="nb-zotero-collection"' in INDEX_HTML
    assert 'list="zotero-collections-list"' not in INDEX_HTML.split(
        'id="nb-zotero-collection"', 1
    )[1].split('>', 1)[0]
    assert "const notebookCollectionSelect = document.getElementById('nb-zotero-collection')" in APP_JS
    assert "...collectionRows.map(c =>" in APP_JS
