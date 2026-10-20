from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
SETTINGS_JS = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
APP = ROOT / "static" / "js" / "app"


def test_each_space_has_its_own_model_chip() -> None:
    # Discovery, Writing and the notebook chat: same chip as the Chat composer.
    for scope in ("discovery", "drafting", "notebook"):
        assert f'id="{scope}-model-chip"' in HTML
        assert f'data-model-scope="{scope}"' in HTML
        assert f'id="{scope}-model-menu"' in HTML
    assert "const WORKSPACE_MODEL_SCOPES = ['discovery', 'notebook', 'drafting'];" in SETTINGS_JS


def test_a_space_model_defaults_to_the_chat_model() -> None:
    # No choice stored, or a model gone from the list: the Chat model is used.
    assert "(scope && workspaceModelOverride(scope)) || document.getElementById('model-select')?.value" in SETTINGS_JS
    assert "option.hasAttribute('value') && option.value === value) ? value : ''" in SETTINGS_JS
    assert "renderWorkspaceModelChips();" in SETTINGS_JS[SETTINGS_JS.index("function syncSettingsChatModelUi") :]


def test_spaces_read_their_own_model() -> None:
    discovery = (APP / "discovery.js").read_text(encoding="utf-8")
    drafting = (APP / "drafting.js").read_text(encoding="utf-8")
    notebooks = (APP / "notebooks.js").read_text(encoding="utf-8")
    assert "optionalSelectedModel('discovery')" in discovery
    assert "optionalSelectedModel();" not in drafting
    assert drafting.count("optionalSelectedModel('drafting')") >= 5
    assert "requireSelectedModel('notebook')" in notebooks
