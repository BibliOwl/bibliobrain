import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
BINDINGS_JS = (ROOT / "static" / "js" / "static-event-bindings.js").read_text(encoding="utf-8")

TABLE_ROW_RE = re.compile(r"^    \['([^']+)', '([a-z]+)', function\(event\) \{", re.M)
HTML_MARKER_RE = re.compile(r'data-bb-static-on([a-z]+)="([^"]+)"')


def _table_entries() -> list[tuple[str, str]]:
    return [(marker, event) for marker, event in TABLE_ROW_RE.findall(BINDINGS_JS)]


def test_static_binding_markers_are_descriptive_and_unique() -> None:
    entries = _table_entries()

    assert entries
    assert len(entries) == len(set(entries))
    for marker, _event in entries:
        assert re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", marker), marker
        assert not re.fullmatch(r"bbs-\d+", marker), marker


def test_every_static_marker_has_exactly_one_binding() -> None:
    table = set(_table_entries())
    used = {(marker, event) for event, marker in HTML_MARKER_RE.findall(HTML)}

    assert used - table == set(), "markers without a handler"
    assert table - used == set(), "handlers no element uses"


def test_index_html_has_no_inline_event_handlers() -> None:
    assert not re.search(r"<[^>]*\son[a-z]+=", HTML)
