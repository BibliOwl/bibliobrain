from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
SHELL_CSS = (ROOT / "static" / "css" / "shell.css").read_text(encoding="utf-8")
RAG_JS = (ROOT / "static" / "js" / "app" / "rag.js").read_text(encoding="utf-8")


def test_library_no_longer_carries_its_own_rag_reload_button() -> None:
    # The sidebar status pill ("{n} new PDFs" · Add them) owns the refresh action.
    assert 'id="library-rag-reload-btn"' not in HTML
    assert 'id="rag-refresh-notice"' not in HTML
    assert "_ragPendingChanges" in RAG_JS


def test_rag_reload_controls_are_found_after_static_binding() -> None:
    # static-event-bindings.js replaces data-bb-static-onclick, so selecting
    # the buttons by that attribute finds nothing once the page has booted.
    assert "querySelectorAll('.btn-rag-reload')" in RAG_JS
    assert '[data-bb-static-onclick="reload-library-rag"]' not in RAG_JS


def test_rag_refresh_check_is_not_polled_on_a_timer() -> None:
    assert "setInterval(checkRagRefreshNeeded" not in RAG_JS


def test_library_exports_live_in_the_more_menu() -> None:
    menu = HTML[HTML.index('id="lib-more-menu"') :]
    menu = menu[: menu.index('class="lib-search-wrap')]
    assert 'aria-haspopup="menu"' in menu and 'role="menu"' in menu
    assert 'id="library-export-md-btn"' in menu and 'id="library-export-bibtex-btn"' in menu
    title_row = HTML[HTML.index('class="lib-title-row"') : HTML.index('id="lib-more-popover"')]
    assert title_row.index('id="lib-count"') < title_row.index('id="lib-more-toggle"')
    # The shared "⋯" button gives it a 44px touch target on phones.
    assert 'class="btn-icon-more lib-more-toggle"' in title_row
    assert ".btn-icon-more { width: 44px; min-width: 44px; height: 44px; min-height: 44px; }" in SHELL_CSS


def test_init_conflict_is_not_reported_as_a_chat_error() -> None:
    # The OCR job holds the same lock as /api/init: a 409 must keep the loaded
    # index ready and say why, not go through readSSE's "Chat impossible".
    conflict = RAG_JS.index("if (response.status === 409)")
    assert conflict < RAG_JS.index("for await (const event of readSSE(response))")
    assert "_ti('library.ragInitLocked')" in RAG_JS[conflict:conflict + 600]


def test_reload_button_pauses_and_resumes_running_ocr() -> None:
    library_js = (ROOT / "static" / "js" / "app" / "library.js").read_text(encoding="utf-8")
    assert "await pauseOcrForRagReload()" in library_js
    assert "if (resumeOcr) await resumeOcrAfterRagReload();" in library_js
    assert "fetch('/api/ocr/pause', { method: 'POST' })" in RAG_JS


def test_background_work_shares_one_sidebar_status_pill() -> None:
    # The AI engine, the RAG load, the OCR pass and the reranker each register
    # a task with a tone; one pill shows the most urgent and one card lists all.
    ocr_js = (ROOT / "static" / "js" / "app" / "ocr.js").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
    settings_js = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    shell_css = (ROOT / "static" / "css" / "shell.css").read_text(encoding="utf-8")
    bindings = (ROOT / "static" / "js" / "static-event-bindings.js").read_text(encoding="utf-8")
    assert HTML.count('class="sidebar-status-row"') == 1
    assert 'id="status-more-badge"' in HTML
    assert "const _STATUS_TASK_ORDER = ['ollama', 'rag', 'ocr', 'reranker'];" in app_js
    assert "const _STATUS_TONES = ['blocked', 'needs', 'working', 'ready'];" in app_js
    assert "setStatusTask('rag', _ragStatusTask(" in app_js
    assert "setStatusTask('ollama', task);" in settings_js
    assert "setStatusTask('ocr', {" in ocr_js and "setStatusTask('ocr', null)" in ocr_js
    assert "setStatusTask('reranker', {" in settings_js and "setStatusTask('reranker', null)" in settings_js
    for tone in ("working", "needs", "blocked"):
        assert f'#rag-status-row[data-tone="{tone}"]' in shell_css
    # The inline action (Start, Add them, Prepare) only for tasks that need the user;
    # pause and resume live in the card.
    assert "(tone === 'needs' || tone === 'blocked') && task?.action?.label" in app_js
    assert 'id="rag-status-action"' in HTML
    for removed in ('id="rag-pause-btn"', 'id="ocr-pause-btn"', 'id="first-init-btn"',
                    'id="sidebar-rag-reload-btn"', 'id="ollama-runtime-alert"'):
        assert removed not in HTML
    assert "toggleStatusPanel()" in bindings
    assert "{ label: tr('pause'), onClick: toggleOcrPause }" in ocr_js
    # The collapsed sidebar shrinks the pill to its dot: the activity rows stay clickable.
    assert "activityClear('ocr')" in ocr_js and "onClick: toggleStatusPanel" in ocr_js
    assert "row.addEventListener('click', onClick);" in app_js
    for task in ("rag", "ocr", "reranker"):
        assert f'.activity-item[data-activity="{task}"]' in CSS
    assert "window.refreshOcrStatus?.();" in RAG_JS


def test_status_pill_sits_above_the_action_buttons() -> None:
    row_start = HTML.index('<div class="sidebar-status-row" id="rag-status-row" data-tone="idle">')
    bottom_row = HTML.index('<div id="sidebar-bottom-row">')
    assert row_start < bottom_row
    # The summary opens the card; the action button beside it runs the action.
    row = HTML[row_start:bottom_row]
    assert 'id="rag-status-summary"' in row and 'id="rag-status-action"' in row
    assert 'id="rag-status-bar"' in row
