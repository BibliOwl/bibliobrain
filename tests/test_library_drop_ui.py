import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
DROP_JS = (ROOT / "static" / "js" / "app" / "library-drop.js").read_text(encoding="utf-8")
STATIC_EVENT_BINDINGS_JS = (ROOT / "static" / "js" / "static-event-bindings.js").read_text(
    encoding="utf-8"
)


def _server_codes_known_to_the_page() -> set[str]:
    codes: set[str] = set()
    for constant in ("REFUSALS", "ZOTERO_STOPS"):
        match = re.search(rf"const {constant} = \[(.*?)\];", DROP_JS, re.DOTALL)
        assert match is not None
        codes |= set(re.findall(r"'(\w+)'", match.group(1)))
    return codes


def test_drop_zone_markup_and_script_are_wired() -> None:
    assert 'id="library-drop-overlay"' in HTML
    assert 'id="library-file-input" multiple hidden' in HTML
    # Uses reloadLibraryRag() and closeLibraryMoreMenu() from library.js.
    assert HTML.index('src="/static/js/app/library.js?') < HTML.index(
        'src="/static/js/app/library-drop.js?'
    )


def test_adding_files_has_a_keyboard_path_in_the_library_menu() -> None:
    menu = HTML[HTML.index('id="lib-more-popover"') : HTML.index('class="lib-search-wrap')]
    assert 'role="menuitem"' in menu
    assert 'data-bb-static-onclick="pick-library-files"' in menu
    assert "['pick-library-files', 'click'" in STATIC_EVENT_BINDINGS_JS
    assert "window.pickLibraryFiles = pickLibraryFiles" in DROP_JS


def test_every_drop_message_is_translated_in_both_languages() -> None:
    keys = set(re.findall(r"'(library\.(?:drop|add)[\w.]*)'", DROP_JS))
    keys |= set(re.findall(r'data-i18n="(library\.(?:drop|add)\w*)"', HTML))
    # Refusal codes are turned into keys at run time.
    keys |= {f"library.dropErr.{code}" for code in _server_codes_known_to_the_page()}

    assert len(keys) > 20
    for key in sorted(keys):
        # Up to the entry's own closing brace: messages hold {placeholders}.
        entry = re.search(rf"'{re.escape(key)}': \{{(.*?)\}},\n", I18N_JS, re.DOTALL)
        assert entry, f"missing i18n entry: {key}"
        assert "fr: '" in entry.group(1) and "en: '" in entry.group(1), key


def test_refusal_codes_known_to_the_page_exist_on_the_server() -> None:
    server = (ROOT / "src" / "library_import.py").read_text(encoding="utf-8")
    codes = _server_codes_known_to_the_page()
    assert {"unsupported_format", "zotero_rejected", "zotero_disabled"} <= codes
    for code in codes:
        assert f'"{code}"' in server, code


def test_page_follows_pdfs_waiting_for_zotero() -> None:
    server = (ROOT / "src" / "library_import.py").read_text(encoding="utf-8")
    routes = (ROOT / "src" / "api" / "library.py").read_text(encoding="utf-8")
    assert '"zotero_queued"' in server and "files('zotero_queued')" in DROP_JS
    assert '"/api/library/zotero-outbox/collect"' in routes
    assert "fetch('/api/library/zotero-outbox/collect', { method: 'POST' })" in DROP_JS
