
from pathlib import Path

from conftest import read_app_javascript

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = read_app_javascript()
I18N_JS = (ROOT / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
STATIC_BINDINGS_JS = (
    ROOT / "static" / "js" / "static-event-bindings.js"
).read_text(encoding="utf-8")


def test_bug_report_discloses_exact_payload_categories() -> None:
    assert 'id="bug-report-data-details"' in HTML
    assert 'data-i18n="bugReport.dataAlwaysSdk"' in HTML
    assert 'data-i18n="bugReport.dataDiagnosticsLogs"' in HTML
    assert 'data-i18n="bugReport.dataChatMessages"' in HTML
    assert 'data-i18n="bugReport.dataScreenshotImage"' in HTML
    assert 'data-i18n="bugReport.dataProtectedContent"' in HTML

    assert "server_name" not in I18N_JS
    assert "The computer name is not sent." in I18N_JS
    assert "a report ID, an event ID" in I18N_JS
    assert "technical trace/span IDs" in I18N_JS
    assert "100 recent backend warnings/errors" in I18N_JS
    assert "latest 20 user/assistant messages" in I18N_JS
    assert "redacted text (up to 4,000 characters per message)" in I18N_JS
    assert "metrics are included only when diagnostics are also selected" in I18N_JS
    assert "including the description and attached conversation" in I18N_JS
    assert "automatic text redaction cannot alter the image" in I18N_JS


def test_bug_report_unconfirmed_delivery_is_not_announced_as_sent() -> None:
    submit = APP_JS[APP_JS.index("async function submitBugReport(") :]
    unconfirmed = submit.index("payload.delivery === 'unconfirmed'")
    # Handled before the form is cleared and the "sent" toast is shown.
    assert unconfirmed < submit.index("getElementById('bug-report-form')?.reset()")
    assert unconfirmed < submit.index("_ti('bugReport.sent')")
    assert "_setBugReportStatus(_ti('bugReport.deliveryUnconfirmed'))" in submit
    entry = I18N_JS[I18N_JS.index("'bugReport.deliveryUnconfirmed'") :].split("},", 1)[0]
    assert "fr: 'Envoi non confirmé" in entry
    assert "en: 'Delivery not confirmed" in entry


def test_bug_report_disclosure_reflects_current_opt_in_state() -> None:
    assert "function _updateBugReportDataDisclosure()" in APP_JS
    assert "'bug-report-data-diagnostics'" in APP_JS
    assert "'bug-report-data-chat'" in APP_JS
    assert "'bug-report-data-screenshot'" in APP_JS
    assert "Boolean(state.bugReportScreenshotDataUrl)" in APP_JS
    assert HTML.count('data-bb-static-onchange="update-bug-report-data-disclosure"') == 2
    assert "_updateBugReportDataDisclosure()" in STATIC_BINDINGS_JS
