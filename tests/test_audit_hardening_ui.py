from __future__ import annotations

import re
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
APP_JS = read_app_javascript()
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
KG_JS = (ROOT / "static" / "js" / "knowledge-graph.js").read_text(encoding="utf-8")
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_drafting_citation_actions_do_not_embed_untrusted_javascript() -> None:
    assert "data-drafting-action=\"copy\"" in APP_JS
    assert "data-drafting-action=\"remove\"" in APP_JS
    assert "function _draftingOnDelegatedAction" in APP_JS
    assert "onclick='copyDraftingCitationById(${JSON.stringify(" not in APP_JS
    assert "onclick='removeDraftingSuggestion(${JSON.stringify(" not in APP_JS
    assert ".replace(/\"/g, '&quot;')" in APP_JS
    assert ".replace(/'/g, '&#39;')" in APP_JS


def test_scientific_content_is_excluded_from_automatic_i18n_rewrites() -> None:
    assert '<tbody id="lib-tbody" data-i18n-skip>' in HTML
    assert "panel.setAttribute('data-i18n-skip', '')" in APP_JS
    assert "answer.setAttribute('data-i18n-skip', '')" in APP_JS
    assert 'class="discovery-title" data-i18n-skip' in APP_JS
    assert 'class="discovery-summary" data-i18n-skip' in APP_JS
    assert 'class="kg-node-label" data-i18n-skip' in KG_JS


def test_incremental_i18n_flush_does_not_reconnect_observer_every_frame() -> None:
    flush = I18N_JS.split("function flushMutationTranslations()", 1)[1].split(
        "function scheduleMutationTranslations", 1
    )[0]
    assert "pauseObserver()" not in flush
    assert "resumeObserver()" not in flush
    assert "observer?.takeRecords()" in flush


def test_frontend_contains_no_inline_event_handlers() -> None:
    inline_handler = re.compile(
        r"(?<![\w-])on(?:click|change|input|keydown|keyup|submit|dragstart|"
        r"dragover|drop|contextmenu|dblclick|pointerdown|mousedown|mouseover|"
        r"mouseout|focus|blur)=(['\"]).*?\1",
        re.IGNORECASE | re.DOTALL,
    )
    sources = {
        "index.html": HTML,
        "app.js": APP_JS,
        "knowledge-graph.js": KG_JS,
    }
    offenders = {
        name: sorted(set(inline_handler.findall(source)))
        for name, source in sources.items()
        if inline_handler.search(source)
    }
    assert offenders == {}
    assert 'src="/static/js/event-bindings.js?' in HTML
    assert 'src="/static/js/static-event-bindings.js?' in HTML
