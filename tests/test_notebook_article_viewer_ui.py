
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]


def test_notebook_sources_offer_embedded_article_reader() -> None:
    app_js = read_app_javascript()

    assert "function openNbArticleViewer(event)" in app_js
    assert "'/api/open-pdf?path=' + encodeURIComponent(path)" in app_js
    assert (
        "${bindBbHandler('click', function(event) { openNbArticleViewer(event); })}"
        in app_js
    )
    assert "Lire ici" in app_js
    assert "function openNbViewerSourceInLibrary()" in app_js


def test_notebook_article_reader_is_an_accessible_modal() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert 'id="nb-article-viewer" role="dialog" aria-modal="true"' in html
    assert 'aria-labelledby="nb-article-viewer-title"' in html
    assert 'id="nb-article-viewer-frame"' in html
    assert "#nb-article-viewer.open" in css
    assert "body.nb-article-viewer-open" in css


def test_library_reader_button_sits_between_overview_and_analysis() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    app_js = read_app_javascript()

    # The reader and "Ask this article" are header actions, next to the ⋯ menu;
    # the tabs row keeps Overview, Summary and Cite.
    reader = html.index('id="library-read-here-btn"')
    tabs = html.index('id="library-detail-tabs"')
    assert reader < tabs
    assert html.index('id="library-ask-article-btn"') < reader
    assert html.index('id="lib-detail-more"') > reader
    static_bindings = (
        ROOT / "static" / "js" / "static-event-bindings.js"
    ).read_text(encoding="utf-8")
    assert "openSelectedLibraryArticleViewer(event)" in static_bindings
    assert "function openSelectedLibraryArticleViewer(event)" in app_js
    assert "if (readHereBtn) readHereBtn.disabled = !relPath" in app_js
