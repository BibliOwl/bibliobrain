from pathlib import Path

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "css" / "knowledge-graph.css").read_text(encoding="utf-8")
LIBRARY_JS = (ROOT / "static" / "js" / "app" / "library.js").read_text(encoding="utf-8")
STATIC_BINDINGS_JS = (ROOT / "static" / "js" / "static-event-bindings.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")


def test_generate_analysis_is_the_primary_action() -> None:
    assert 'class="btn btn-indigo" id="analysis-btn"' in HTML


def test_prompt_editor_is_a_compact_contextual_action() -> None:
    controls = HTML.split('<div id="analysis-controls">', 1)[1].split(
        '<div id="analysis-result">', 1
    )[0]

    assert "analysis-field-heading" in controls
    assert 'id="analysis-prompt-edit-btn"' in controls
    assert 'class="btn ' not in controls.split(
        'id="analysis-prompt-edit-btn"', 1
    )[0].rsplit("<button", 1)[1]
    assert "#analysis-prompt-edit-btn" in CSS
    assert 'data-i18n="library.editAnalysisPrompt"' in controls


def test_analysis_summary_can_be_expanded_for_reading() -> None:
    button = HTML.split('id="analysis-expand-btn"', 1)[1].split("</button>", 1)[0]
    assert 'aria-controls="analysis-result"' in button
    assert 'aria-expanded="false"' in button
    assert 'data-bb-static-onclick="toggle-analysis-expanded"' in button
    assert "toggleAnalysisExpanded()" in STATIC_BINDINGS_JS
    assert "function toggleAnalysisExpanded(force = null)" in LIBRARY_JS
    assert "document.createComment('analysis-section-placeholder')" in LIBRARY_JS
    assert "document.body.appendChild(section)" in LIBRARY_JS
    assert "analysisSectionPlaceholder.parentNode.insertBefore(section, analysisSectionPlaceholder)" in LIBRARY_JS
    assert "analysisSectionPlaceholder.remove()" in LIBRARY_JS
    assert "analysisSectionPlaceholder = null" in LIBRARY_JS
    assert "section.classList.toggle('is-expanded', expanded)" in LIBRARY_JS
    assert "document.body.classList.toggle('analysis-summary-expanded', expanded)" in LIBRARY_JS
    assert "event.key === 'Escape'" in LIBRARY_JS
    assert "#analysis-section.is-expanded" in CSS
    assert "@media (max-width: 760px)" in CSS
    assert "'library.expandSummary'" in I18N_JS
    assert "'library.collapseSummary'" in I18N_JS
