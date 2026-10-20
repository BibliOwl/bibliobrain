from __future__ import annotations

from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).parents[1]
APP_JS = read_app_javascript()
SETTINGS_JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_zotero_controls_are_hidden_until_zotero_is_enabled() -> None:
    assert 'id="chat-filters-panel" class="chat-filters-panel" hidden' in INDEX_HTML
    assert 'id="library-open-zotero-btn"' in INDEX_HTML
    assert 'tabindex="-1" hidden>Afficher dans Zotero</a>' in INDEX_HTML
    assert (
        'option value="zotero_collection" data-i18n="notebooks.zoteroCollection" '
        "hidden disabled"
    ) in INDEX_HTML

    assert "function isZoteroInUse()" in APP_JS
    assert "filters.hidden = !active" in APP_JS
    assert "libraryButton.hidden = !active" in APP_JS
    assert "notebookOption.hidden = !active" in APP_JS
    assert "if (isZoteroInUse()) {" in APP_JS


def test_metadata_source_is_persisted_and_none_skips_zotero_bootstrap() -> None:
    assert "metadata_source: state.metadataSource" in SETTINGS_JS
    assert "if (source === 'none')" in APP_JS
    assert "Zotero non utilisé." in APP_JS
