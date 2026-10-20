"""Bibliobrain must prepare the documents before any chat model is installed.

Only Ollama and the indexing model block the preparation; a missing chat model
gets a notice, and each other failure its own message.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import read_app_javascript

from src.api_models import InitRequest
from src.rag import state

ROOT = Path(__file__).resolve().parents[1]
APP_JS = read_app_javascript()
SETTINGS_JS = (ROOT / "static/js/settings.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "static/index.html").read_text(encoding="utf-8")
INNO = (ROOT / "packaging/Bibliobrain.iss").read_text(encoding="utf-8-sig")
QUERY_ENGINE = (ROOT / "src/rag/query_engine.py").read_text(encoding="utf-8")


def _function(source: str, signature: str) -> str:
    start = source.index(signature)
    end = source.find("\n}\n", start)
    return source[start:end]


def test_init_request_and_runtime_accept_no_chat_model() -> None:
    assert InitRequest().model == ""
    assert state.INDEX_ONLY_CHAIN is not None
    # Without a model the chain is the index-only marker, never a chat model.
    assert "document_chain = state.INDEX_ONLY_CHAIN" in QUERY_ENGINE
    assert '"ready_without_chat_model"' in QUERY_ENGINE


def test_startup_engine_check_tells_the_three_failures_apart() -> None:
    check = _function(APP_JS, "async function checkStartupEngine()")
    assert "startup.noChatModel'" not in APP_JS
    assert "_ti('startup.modelsCheckFailed')" in check
    assert "_ti('startup.embeddingProbeFailed')" in check
    # A missing chat model shows the notice and lets the preparation run.
    assert (
        "if (!models.models?.length && chatNotice) chatNotice.hidden = false;\n    return true;"
        in check
    )
    assert 'id="startup-chat-model-notice"' in INDEX_HTML
    assert 'id="startup-chat-command"' in INDEX_HTML
    for key in (
        "startup.noChatModelYet",
        "startup.modelsCheckFailed",
        "startup.embeddingProbeFailed",
    ):
        assert f"'{key}':" in I18N_JS


def test_preparation_never_waits_for_a_chat_model() -> None:
    init = _function(APP_JS, "async function initRAG(")
    assert "optionalSelectedModel() || ''" in init
    assert "requireSelectedModel()" not in init
    auto = _function(APP_JS, "async function runStartupAutoInit(")
    assert "optionalSelectedModel" not in auto
    assert "startup.statusModelRequired" not in APP_JS
    load = _function(SETTINGS_JS, "async function loadModels(")
    assert "setInitButtonsAvailability(models.length > 0)" not in load


def test_ready_library_without_chat_model_says_how_to_add_one() -> None:
    assert "return hasChatModel ? 'ready' : 'no-chat-model';" in APP_JS
    assert 'data-rag-state="no-chat-model"' in APP_JS
    for key in ("chat.noChatModelTitle", "chat.noChatModelDesc", "model.noneInstalledToast"):
        assert f"'{key}':" in I18N_JS


def test_simple_installation_warns_below_the_minimum_configuration() -> None:
    assert (
        "and not WizardSilent and (ChosenChatModel = 'none') and (ModelsChatCount = '0') then"
        in INNO
    )
    assert "MsgBox(CustomMessage('EasyBelowMinimum'), mbInformation, MB_OKCANCEL)" in INNO
    for language in ("en", "fr"):
        assert f"{language}.EasyBelowMinimum=" in INNO
    assert "8 GB of memory and 4 processor cores" in INNO
    assert "8 Go de mémoire et 4 cœurs de processeur" in INNO


def test_simple_installation_asks_for_an_ai_when_the_computer_was_not_checked() -> None:
    # The models page is skipped in the simple installation only after a probe.
    assert "((PageID = ModelsPage.ID) and ModelsProbed)" in INNO
    assert "  else\n    AddUncheckedModelChoices;" in INNO.replace("\r\n", "\n")
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8-sig")
    catalog = re.findall(
        r'Model\s+= "([^"]+)"\s+Download\s+= "([\d.]+) GiB"\s+Status\s+= "(\w+)"', installer
    )
    assert catalog
    unchecked = re.findall(
        r"AddModelChoice\('([^']+)', ModelDetails\('model\|[^|]+\|([\d.]+) GB\|(\w+)\|unknown\|0\|0'\), False, (True|False)\);",
        INNO,
    )
    # Same models, order, status and download size (GiB shown as GB) as the catalog.
    assert [(name, status) for name, _, status, _ in unchecked] == [
        (name, status) for name, _, status in catalog
    ]
    for (_, gib, _), (_, gb, _, _) in zip(catalog, unchecked, strict=True):
        assert round(float(gib) * 2**30 / 1e9, 1) == float(gb)
    # Only the fallback model is selected.
    assert [name for name, _, _, checked in unchecked if checked == "True"] == ["qwen3.5:2b"]
