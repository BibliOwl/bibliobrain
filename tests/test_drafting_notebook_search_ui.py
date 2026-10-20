
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = read_app_javascript()
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
STATIC_BINDINGS_JS = (
    ROOT / "static" / "js" / "static-event-bindings.js"
).read_text(encoding="utf-8")


def test_writing_assistance_offers_notebook_search_controls() -> None:
    # "Consult a notebook" is a fourth job card, shown only when notebooks exist.
    card = HTML[HTML.index('id="drafting-notebook-card"') : HTML.index('id="drafting-view-notebook"')]

    assert "hidden" in HTML[HTML.index('id="drafting-notebook-card"') - 80 : HTML.index('id="drafting-notebook-card"') + 40]
    assert 'id="drafting-notebook-select"' in card
    assert 'id="drafting-notebook-query"' in card
    assert 'id="drafting-notebook-search-btn"' in card
    assert "runDraftingNotebookSearch()" in STATIC_BINDINGS_JS
    assert "if (card) card.hidden = !list.length;" in APP_JS


def test_one_assistant_panel_keeps_each_action_in_its_own_results_view() -> None:
    start = HTML.index('id="drafting-panel-assistant"')
    panel = HTML[start : HTML.index("</aside>", start)]

    assert "drafting-modebar" not in HTML
    assert "drafting-panel-review" not in HTML
    # No result tabs: each job card holds its button and, below, its results.
    assert "drafting-view-tabs" not in HTML
    jobs = {
        "citations": ("drafting-btn", "drafting-result"),
        "proofread": ("drafting-proofread-btn", "drafting-proofread-result"),
        "arguments": ("drafting-arguments-btn", "drafting-arguments-result"),
        "notebook": ("drafting-notebook-search-btn", "drafting-notebook-result"),
    }
    for view, (button, result) in jobs.items():
        card_start = panel.index(f'data-drafting-job="{view}"')
        next_card = panel.find('data-drafting-job="', card_start + 1)
        card = panel[card_start : next_card if next_card > 0 else len(panel)]
        assert f'id="{button}"' in card
        assert f'id="{result}"' in card
        assert panel.count(f'id="{result}"') == 1
    assert 'data-drafting-show="history"' in panel
    for view in ("proofread", "arguments", "notebook"):
        assert f"_draftingStartReviewView('{view}')" in APP_JS
    assert "is-review-mode" not in APP_JS


def test_notebook_search_is_scoped_and_read_only() -> None:
    assert "async function _draftingLoadNotebookOptions(force = false)" in APP_JS
    assert "async function runDraftingNotebookSearch()" in APP_JS
    assert "persist_history: false" in APP_JS
    assert "read_only: true" in APP_JS
    assert "/api/notebooks/${encodeURIComponent(notebookId)}/chat" in APP_JS
    assert "renderRichAssistantOutput(answerRoot" in APP_JS


def test_notebook_search_controls_have_responsive_layout() -> None:
    assert ".drafting-notebook-search {" in APP_CSS
    assert "grid-template-columns: minmax(170px, 0.8fr)" in APP_CSS
    mobile = APP_CSS[APP_CSS.index("@media (max-width: 760px)") :]
    assert ".drafting-notebook-search {" in mobile
    assert "flex-direction: column" in mobile


def test_drafting_editor_translates_placeholder_without_touching_document_text() -> None:
    editor = HTML[
        HTML.index('id="drafting-editor"') : HTML.index(
            'id="drafting-main-resizer"'
        )
    ]
    translate_element = I18N_JS[
        I18N_JS.index("function translateElement") : I18N_JS.index(
            "function translateNode"
        )
    ]

    assert 'contenteditable="true"' in editor
    assert 'data-i18n-data-placeholder="drafting.placeholder"' in editor
    assert "const editableRoot = el.matches('[contenteditable=\"true\"]')" in translate_element
    assert (
        "if (el.closest('[contenteditable=\"true\"]') && !editableRoot) return;"
        in translate_element
    )
    assert translate_element.index("translateAttributes(el)") < translate_element.index(
        "if (editableRoot) return;"
    )
    assert translate_element.index("if (editableRoot) return;") < translate_element.index(
        "Array.from(el.childNodes)"
    )
