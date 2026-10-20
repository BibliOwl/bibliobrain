from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = read_app_javascript()
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")


def test_argument_ideas_offer_a_target_kind_selector() -> None:
    start = HTML.index('id="drafting-argument-kind"')
    selector = HTML[start : HTML.index("</select>", start)]

    for value in (
        "any",
        "mechanism",
        "counter_argument",
        "limitation",
        "context",
        "implication",
    ):
        assert f'value="{value}"' in selector
    assert "argument_kind: argumentKind" in APP_JS


def test_argument_ideas_freeze_and_show_request_scope() -> None:
    assert "const scope = _draftingGetQueryScope();" in APP_JS
    assert "lastArgumentRequest = { text, scope, argumentKind, model }" in APP_JS
    assert "context.text" in APP_JS
    assert "context.argumentKind" in APP_JS
    assert "drafting.argument.scope.selection" in I18N_JS
    assert "drafting.argument.scope.document" in I18N_JS
    assert "_draftingArgumentScopeLabel(state.drafting.lastArgumentRequest?.scope)" in APP_JS


def test_argument_ideas_show_staged_progress_and_regenerate_one_card() -> None:
    for key in (
        "drafting.argument.progress.sources",
        "drafting.argument.progress.generation",
        "drafting.argument.progress.verification",
    ):
        assert key in APP_JS
        assert key in I18N_JS
    assert "async function regenerateDraftingArgumentSuggestion(index)" in APP_JS
    assert "maxSuggestions: 1" in APP_JS
    assert "nextSuggestions[Number(index)] = nextSuggestion" in APP_JS


def test_argument_ideas_explain_working_notes_and_handle_text_errors() -> None:
    assert "Ajouter comme note de travail" in I18N_JS
    assert "Add as working note" in I18N_JS
    assert "drafting.argument.insertNoteHelp" in APP_JS
    assert "await response.text()" in APP_JS
    assert ".slice(0, 500)" in APP_JS
