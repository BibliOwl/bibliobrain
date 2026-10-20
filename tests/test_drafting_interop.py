from __future__ import annotations

from src import library
from src.chat_export import _inline_latex, build_text_export_document
from src.drafting_interop import (
    CITE_CLOSE,
    CITE_OPEN,
    extract_citation_markers,
    insert_before_end_document,
    render_latex_bibliography,
    resolve_citation_records,
    restore_latex_citations,
    strip_citation_markers,
)
from src.zotero_db import ZoteroItem


def _cite(key: str) -> str:
    return f"{CITE_OPEN}{key}{CITE_CLOSE}"


def test_adjacent_citation_markers_become_one_cite_with_raw_keys() -> None:
    body = f"# Intro\n\nMagnets **fail** under stress {_cite('smith_2020')}{_cite('doe:2021')}.\n\n- point {_cite('smith_2020')}"

    placeholder_body, groups = extract_citation_markers(body)
    document = build_text_export_document(title="Draft", body=placeholder_body, export_format="latex")
    latex = restore_latex_citations(document.content, groups)

    # Keys go through the Markdown to LaTeX conversion without being escaped.
    assert r"under stress \cite{smith_2020,doe:2021}." in latex
    assert r"\item point \cite{smith_2020}" in latex
    assert r"\section*{Intro}" in latex
    assert r"\textbf{fail}" in latex
    assert CITE_OPEN not in latex and "\ue022" not in latex


def test_markers_are_sanitized_and_stripped_for_markdown() -> None:
    _, groups = extract_citation_markers(f"x {_cite('bad key}{')} {_cite('')}")
    assert groups == [["badkey"]]
    assert strip_citation_markers(f"Claim {_cite('a')}.") == "Claim ."


def test_markdown_bold_and_bold_italic_convert_to_latex() -> None:
    assert _inline_latex("a **b** c") == r"a \textbf{b} c"
    assert _inline_latex("***both***") == r"\textbf{\emph{both}}"
    assert _inline_latex("**a *b* c**") == r"\textbf{a \emph{b} c}"
    assert _inline_latex("*only*") == r"\emph{only}"


def test_thebibliography_keys_match_cite_keys_and_escape_text() -> None:
    section = render_latex_bibliography(
        [
            {"latex_key": "smith_2020", "authors_display": "Smith & Co", "title": "Magnets", "journal": "Phys", "year": "2020", "doi": "10.1/x_y"},
            {"latex_key": "smith_2020", "title": "Duplicate"},
            {"citation_id": "unknown_key"},
        ]
    )
    assert r"\bibitem{smith_2020} Smith \& Co, ``Magnets'', \emph{Phys}, 2020, doi: 10.1/x\_y." in section
    assert "Duplicate" not in section
    assert r"\bibitem{unknown_key} \texttt{unknown\_key}" in section
    latex = insert_before_end_document("\\begin{document}\nBody\n\\end{document}\n", section)
    assert latex.index("thebibliography") < latex.index("\\end{document}")
    assert latex.rstrip().endswith("\\end{document}")


def test_resolve_citation_records_matches_citation_and_item_keys(monkeypatch) -> None:
    item = ZoteroItem(
        item_key="ABCD1234",
        title="Quench studies",
        authors=["Curie, Marie"],
        year="2024",
        journal="Supercond. Sci.",
        citation_key="curie2024",
        item_type="journalArticle",
    )
    monkeypatch.setattr(library, "_zotero_library", {"paper.pdf": item})

    records = resolve_citation_records(["CURIE2024", "ABCD1234", "missing"])

    assert set(records) == {"CURIE2024", "ABCD1234"}
    record = records["CURIE2024"]
    assert record["citation_id"] == "curie2024"
    assert record["bibtex"].startswith("@article{curie2024,")
    assert record["title"] == "Quench studies"
    assert record["label"] == "Curie, Marie (2024)"


def test_word_citation_items_link_user_and_group_items(monkeypatch) -> None:
    from src.drafting_interop import resolve_word_citation_items

    mine = ZoteroItem(
        item_key="ABCD1234",
        title="Quench studies",
        authors=["Curie, Marie", "CERN"],
        year="2024",
        journal="Supercond. Sci.",
        doi="10.1/qs",
        citation_key="curie2024",
        item_type="journalArticle",
    )
    shared = ZoteroItem(item_key="WXYZ9876", library_type="group", group_id=99, title="Magnets", item_type="book")
    monkeypatch.setattr(library, "_zotero_library", {"a.pdf": mine, "b.pdf": shared})
    monkeypatch.setattr(library, "zotero_user_library_uri", lambda: "http://zotero.org/users/local/AbCdEf")

    items = resolve_word_citation_items(["curie2024", "WXYZ9876", "missing"])

    assert set(items) == {"curie2024", "WXYZ9876"}
    assert items["curie2024"]["uris"] == ["http://zotero.org/users/local/AbCdEf/items/ABCD1234"]
    assert items["curie2024"]["itemData"] == {
        "type": "article-journal",
        "title": "Quench studies",
        "author": [{"family": "Curie", "given": "Marie"}, {"literal": "CERN"}],
        "container-title": "Supercond. Sci.",
        "issued": {"date-parts": [[2024]]},
        "DOI": "10.1/qs",
    }
    assert items["WXYZ9876"]["uris"] == ["http://zotero.org/groups/99/items/WXYZ9876"]
    assert items["WXYZ9876"]["itemData"]["type"] == "book"


def test_word_citation_items_without_zotero_keep_item_data_only(monkeypatch) -> None:
    from src.drafting_interop import resolve_word_citation_items

    bibtex_item = ZoteroItem(title="From BibTeX", authors=["Doe, John"], year="2020", citation_key="doe2020")
    monkeypatch.setattr(library, "_zotero_library", {"a.pdf": bibtex_item})
    monkeypatch.setattr(library, "zotero_user_library_uri", lambda: "")

    items = resolve_word_citation_items(["doe2020"])

    assert items["doe2020"]["uris"] == []
    assert items["doe2020"]["itemData"]["title"] == "From BibTeX"


def test_zotero_user_library_uri_prefers_synced_user_id(workspace_tmp_dir) -> None:
    import sqlite3

    from src.zotero_db import load_zotero_user_library_uri

    db_path = workspace_tmp_dir / "zotero.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE settings (setting TEXT, key TEXT, value)")
    conn.execute("INSERT INTO settings VALUES ('account', 'localUserKey', 'AbCdEf12')")
    conn.commit()
    conn.close()
    assert load_zotero_user_library_uri(str(db_path)) == "http://zotero.org/users/local/AbCdEf12"

    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO settings VALUES ('account', 'userID', 123456)")
    conn.commit()
    conn.close()
    assert load_zotero_user_library_uri(str(db_path)) == "http://zotero.org/users/123456"
    assert load_zotero_user_library_uri(str(workspace_tmp_dir / "missing.sqlite")) == ""
