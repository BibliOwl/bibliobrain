import re
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = read_app_javascript()
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
APP_CSS = (ROOT / "static" / "css" / "app.css").read_text(encoding="utf-8")
STATIC_BINDINGS_JS = (
    ROOT / "static" / "js" / "static-event-bindings.js"
).read_text(encoding="utf-8")
TOUR_JS = (ROOT / "static" / "js" / "app" / "feature-tour.js").read_text(encoding="utf-8")
STARTUP_JS = (ROOT / "static" / "js" / "app" / "startup.js").read_text(encoding="utf-8")

TOUR_TABS = ["chat", "library", "discovery", "drafting", "connections", "notebooks"]
TOUR_TARGET_IDS = [
    "msg-input",
    "lib-search",
    "discovery-search-form",
    "drafting-actions",
    "cartography-header",
    "nb-new-btn",
]
TIP_TABS = ["library", "discovery", "drafting", "connections", "notebooks"]
CHECKLIST_ITEMS = [
    "prepared",
    "question-asked",
    "source-opened",
    "citation-inserted",
    "notebook-created",
]


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start : source.index("\n}", start)]


def _assert_translated(key: str) -> None:
    marker = f"'{key}':"
    assert marker in I18N_JS, key
    start = I18N_JS.index(marker)
    entry = I18N_JS[start : I18N_JS.index("},", start) + 1]
    assert "fr:" in entry, key
    assert "en:" in entry, key


def test_feature_tour_script_is_referenced_after_startup() -> None:
    startup_pos = HTML.index('/static/js/app/startup.js?v=')
    tour_pos = HTML.index('/static/js/app/feature-tour.js?v=')
    settings_pos = HTML.index('/static/js/settings.js?v=')
    assert startup_pos < tour_pos < settings_pos


def test_finishing_setup_neither_starts_nor_offers_a_guide() -> None:
    # The tutorial and the visit are opt-in ("Premiers pas", Settings).
    assert "startChatTutorial" not in STARTUP_JS
    assert "startFeatureTour" not in STARTUP_JS

    controls = _function_body(STARTUP_JS, "function updateStartupGuideControls()")
    assert "utorial" not in controls
    assert "_ti('startup.start')" in controls

    # Closing the assistant once preparation is done still completes setup.
    later = _function_body(STARTUP_JS, "function requestHideStartupHelp()")
    assert "finishStartupGuide()" in later

    checklist = TOUR_JS[TOUR_JS.index("const ONBOARDING_CHECKLIST = [") :]
    checklist = checklist[: checklist.index("\n];")]
    assert "startChatTutorial()" in checklist


def test_feature_tour_steps_cover_the_six_tabs_in_order() -> None:
    start = APP_JS.index("const FEATURE_TOUR_STEPS = [")
    end = APP_JS.index("\n];", start)
    steps_block = APP_JS[start:end]

    assert re.findall(r"tab:\s*'([a-z]+)'", steps_block) == TOUR_TABS


def test_feature_tour_target_ids_exist_in_html() -> None:
    for target_id in TOUR_TARGET_IDS:
        assert f'id="{target_id}"' in HTML


def test_tutorial_waits_for_real_actions_from_the_app() -> None:
    steps = TOUR_JS[TOUR_JS.index("const TUTORIAL_STEPS = [") : TOUR_JS.index("\n];", TOUR_JS.index("const TUTORIAL_STEPS = ["))]
    for milestone in ("question-asked", "answer-ready", "source-opened"):
        assert f"'{milestone}'" in steps

    chat_js = (ROOT / "static" / "js" / "app" / "chat.js").read_text(encoding="utf-8")
    drafting_js = (ROOT / "static" / "js" / "app" / "drafting.js").read_text(encoding="utf-8")
    notebooks_js = (ROOT / "static" / "js" / "app" / "notebooks.js").read_text(encoding="utf-8")
    settings_js = (ROOT / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    for source, name in (
        (chat_js, "question-asked"),
        (chat_js, "answer-ready"),
        (chat_js, "source-opened"),
        (drafting_js, "citation-inserted"),
        (notebooks_js, "notebook-created"),
    ):
        assert f"new CustomEvent('bibliobrain:milestone', {{ detail: {{ name: '{name}'" in source, name
    assert "new CustomEvent('bibliobrain:tabchange'" in settings_js


def test_hands_on_steps_do_not_capture_the_keyboard_or_clicks() -> None:
    keydown = _function_body(TOUR_JS, "function _ftHandleKeydown(event)")
    assert "step?.interactive && !_ftEls.card.contains(document.activeElement)" in keydown

    assert "#feature-tour-root.ft-interactive {" in APP_CSS
    rule = APP_CSS.split("#feature-tour-root.ft-interactive {", maxsplit=1)[1].split("}", maxsplit=1)[0]
    assert "pointer-events: none;" in rule


def test_tab_tips_and_checklist_are_wired() -> None:
    tips_block = TOUR_JS[TOUR_JS.index("const ONBOARDING_TIPS = {") :]
    tips_block = tips_block[: tips_block.index("\n};")]
    for tab in TIP_TABS:
        match = re.search(rf"{tab}: '([a-z-]+)'", tips_block)
        assert match, tab
        assert f'id="{match.group(1)}"' in HTML, tab

    checklist_block = TOUR_JS[TOUR_JS.index("const ONBOARDING_CHECKLIST = [") :]
    checklist_block = checklist_block[: checklist_block.index("\n];")]
    assert re.findall(r"id: '([a-z-]+)'", checklist_block) == CHECKLIST_ITEMS

    for element_id in ("onboarding-checklist-btn", "tutorial-start-btn", "onboarding-reset-btn"):
        assert f'id="{element_id}"' in HTML


def test_feature_tour_i18n_keys_are_translated_in_both_languages() -> None:
    keys = [
        "tour.kicker",
        "tour.skip",
        "tour.prev",
        "tour.next",
        "tour.finish",
        "tour.settingsButton",
        "tour.settingsHelp",
        "tour.done.title",
        "tour.done.body",
        "tutorial.kicker",
        "tutorial.quit",
        "tutorial.question",
        "tutorial.skipStep",
        "tutorial.done.title",
        "tutorial.done.body",
        "tutorial.failed.title",
        "tutorial.failed.body",
        "tutorial.finish",
        "tutorial.needsSetup",
        "tutorial.busy",
        "tutorial.settingsButton",
        "tip.kicker",
        "tip.gotIt",
        "onboarding.checklistTitle",
        "onboarding.show",
        "onboarding.hide",
        "onboarding.allDone",
        "onboarding.resetButton",
    ]
    for tab in TOUR_TABS:
        keys.append(f"tour.step.{tab}.title")
        keys.append(f"tour.step.{tab}.body")
    for step in ("ask", "wait", "cite", "depth"):
        keys.append(f"tutorial.{step}.title")
        keys.append(f"tutorial.{step}.body")
    for tab in TIP_TABS:
        keys.append(f"tip.{tab}.title")
        keys.append(f"tip.{tab}.body")
    for item in CHECKLIST_ITEMS:
        keys.append(f"onboarding.item.{item}")

    for key in keys:
        _assert_translated(key)


def test_app_css_defines_pulse_animation_and_reduced_motion_override() -> None:
    assert "@keyframes ft-pulse" in APP_CSS
    start = APP_CSS.index("@keyframes ft-pulse")
    reduced_start = APP_CSS.index("@media (prefers-reduced-motion: reduce)", start)
    block = APP_CSS[reduced_start : reduced_start + 400]
    assert ".ft-" in block
    assert "transition: none" in block
    assert "animation: none" in block


def test_settings_button_reopens_the_tour() -> None:
    assert "reopenFeatureTour()" in STATIC_BINDINGS_JS

    reopen_fn = _function_body(APP_JS, "function reopenFeatureTour()")
    assert "_closeSettingsIfOpen()" in reopen_fn
    assert "startFeatureTour()" in reopen_fn
