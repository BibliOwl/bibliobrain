from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).parents[1]
APP_JS = read_app_javascript()
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")


def test_add_to_notebook_offers_a_new_notebook() -> None:
    assert "_discoveryNewNotebookFormHtml(inLibrary, addTo, { open: !notebooks.length })" in APP_JS
    assert "fetch('/api/notebooks', {\n        method: 'POST'," in APP_JS
    # The new notebook then receives the papers like any other.
    assert "addTo({ id: payload.id, title: payload.title || title, source_count: 0 });" in APP_JS
    for key in ("discovery.noNotebookYet", "discovery.newNotebookHint", "discovery.createAndAdd"):
        assert f"'{key}':" in I18N_JS
