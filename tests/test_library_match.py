from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from src import library


def _library(monkeypatch: Any, items: dict[str, Any]) -> None:
    monkeypatch.setattr(library, "find_pdfs", lambda: list(items))
    monkeypatch.setattr(
        library,
        "filter_active_zotero_pdf_entries",
        lambda entries, *_args, **_kwargs: [(path, f"X:/{path}") for path in entries],
    )
    monkeypatch.setattr(library, "_lookup_zotero", lambda rel_path, full_path="": items[rel_path])


def test_library_match_uses_doi_then_title(monkeypatch: Any) -> None:
    _library(
        monkeypatch,
        {
            "zotero/uchida.pdf": SimpleNamespace(
                title="High temperature superconductivity", doi="10.7566/JPSJ.84.114701"
            ),
            # Outside Zotero: title read from the PDF, no DOI.
            "inbox/Homogeneous_TA.pdf": SimpleNamespace(
                title="A homogeneous T-A formulation for screening currents in REBCO magnets",
                doi="",
            ),
            "inbox/scan.pdf": None,
        },
    )

    matches = library.match_library_papers(
        [
            {"title": "Another title entirely", "doi": "https://doi.org/10.7566/jpsj.84.114701"},
            {"title": "A Homogeneous T–A Formulation for Screening Currents in REBCO Magnets", "doi": ""},
            {"title": "homogeneous T-A formulation for screening currents in REBCO magnets", "doi": ""},
            # Another paper with a close title must not take this PDF.
            {"title": "A homogeneous T-A formulation for AC losses in REBCO magnets", "doi": ""},
            {"title": "Metal-insulated coils", "doi": "10.1/none"},
        ]
    )

    assert matches[0] == {"rel_path": "zotero/uchida.pdf", "matched_by": "doi"}
    assert matches[1] == {"rel_path": "inbox/Homogeneous_TA.pdf", "matched_by": "title"}
    assert matches[2] == {"rel_path": "inbox/Homogeneous_TA.pdf", "matched_by": "similar_title"}
    assert matches[3] == {"rel_path": "", "matched_by": ""}
    assert matches[4] == {"rel_path": "", "matched_by": ""}
