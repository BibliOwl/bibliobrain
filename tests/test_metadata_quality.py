from __future__ import annotations

from typing import Any

import pytest

from src import metadata_quality
from src.zotero_db import ZoteroItem

THESIS_COVER = [
    "UNIVERSITE DE PROVENCE - AIX-MARSEILLE I",
    "Institut Universitaire des Systèmes Thermiques Industriels",
    "Ecole Polytechnique Universitaire de Marseille",
    "THESE",
    "pour obtenir le grade de",
    "discipline : MECANIQUE ENERGETIQUE",
    "présentée et soutenue publiquement par",
    "Marc COATANEA-GOUACHET",
    "le 15 février 2012",
    "QUENCH DETECTION AND BEHAVIOUR IN CASE OF QUENCH",
    "IN THE ITER MAGNET SYSTEMS",
    "DETECTION DE QUENCH ET COMPORTEMENT EN CAS DE QUENCH",
    "JURY",
]


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        (
            ZoteroItem(title="discipline : MECANIQUE ENERGETIQUE Ecole Doctorale PHYSIQUE", authors=["A"], year="2012"),
            ["le titre ressemble à une page de garde"],
        ),
        (ZoteroItem(title="Quench detection in ITER magnets", authors=["A"]), ["année manquante"]),
        (ZoteroItem(title="Quench detection in ITER magnets", year="2012"), ["auteurs manquants"]),
        (ZoteroItem(title="Quench detection in ITER magnets", authors=["A"], year="2012"), []),
    ],
)
def test_metadata_issues_flag_cover_titles_and_missing_fields(item: ZoteroItem, expected: list[str]) -> None:
    assert metadata_quality.metadata_issues(item, "KEY/file.pdf") == expected


def test_cover_page_title_skips_boilerplate_and_stops_before_the_translation(monkeypatch) -> None:
    monkeypatch.setattr(metadata_quality, "_first_page_lines", lambda _path: THESIS_COVER)

    cover = metadata_quality.cover_page_metadata("thesis.pdf")

    assert cover["title"] == "QUENCH DETECTION AND BEHAVIOUR IN CASE OF QUENCH IN THE ITER MAGNET SYSTEMS"
    assert cover["author"] == "Marc COATANEA-GOUACHET"
    assert cover["year"] == "2012"
    assert cover["is_thesis_cover"] is True


def test_proposal_searches_with_the_cover_title_only_when_the_zotero_title_is_broken(
    monkeypatch, tmp_path
) -> None:
    from src import pdf_metadata

    pdf = tmp_path / "thesis.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(metadata_quality, "_first_page_lines", lambda _path: THESIS_COVER)
    probes: list[str] = []

    def openalex(probe: ZoteroItem) -> tuple[Any, bool]:
        probes.append(probe.title)
        return ZoteroItem(title="Quench detection and behaviour", year="2012", authors=["Marc Coatanea-Gouachet"]), True

    monkeypatch.setattr(pdf_metadata, "_openalex_lookup", openalex)
    monkeypatch.setattr(pdf_metadata, "_crossref_lookup", lambda probe: (None, True))

    broken = ZoteroItem(title="discipline : MECANIQUE ENERGETIQUE Ecole Doctorale")
    proposal = metadata_quality.propose_metadata_fix(broken, str(pdf))
    assert proposal["source"] == "openalex"
    assert proposal["year"] == "2012"
    assert probes[-1].startswith("QUENCH DETECTION")

    sound = ZoteroItem(title="Experimental study of screening currents in REBCO magnets", authors=["Dilasser"])
    metadata_quality.propose_metadata_fix(sound, str(pdf))
    # A correct Zotero title is never replaced by a line read on the page.
    assert probes[-1] == sound.title


def test_page_title_is_the_fallback_when_no_source_confirms(monkeypatch, tmp_path) -> None:
    from src import pdf_metadata

    pdf = tmp_path / "thesis.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(metadata_quality, "_first_page_lines", lambda _path: THESIS_COVER)
    monkeypatch.setattr(pdf_metadata, "_openalex_lookup", lambda probe: (None, True))
    monkeypatch.setattr(pdf_metadata, "_crossref_lookup", lambda probe: (None, True))

    proposal = metadata_quality.propose_metadata_fix(ZoteroItem(title="discipline : MECANIQUE"), str(pdf))

    assert proposal["source"] == "page"
    assert proposal["title"].startswith("QUENCH DETECTION")
    assert proposal["year"] == "2012"


@pytest.mark.parametrize(
    ("title", "flagged"),
    [
        ("Antenna Engineering Handbook", False),
        ("Numerical Modeling of Superconducting Applications", False),
        ("Dilasser - 2017 - Experimental measurement of screening currents", True),
        ("paper_final_v2", True),
        ("Some Title.pdf", True),
    ],
)
def test_only_titles_with_file_name_traits_are_flagged(title: str, flagged: bool) -> None:
    item = ZoteroItem(title=title, authors=["A"], year="2020")
    issues = metadata_quality.metadata_issues(item, f"KEY/{title}.pdf")
    assert ("le titre reprend le nom du fichier" in issues) is flagged


def test_duplicates_tell_extra_attachments_from_separate_records(monkeypatch) -> None:
    from src import library

    records = {
        "A1/paper.pdf": ZoteroItem(item_key="AAA", title="Modelling of bulk superconductor magnetization", year="2015", doi="10.1/x"),
        "A2/paper.pdf": ZoteroItem(item_key="AAA", title="Modelling of bulk superconductor magnetization", year="2015", doi="10.1/x"),
        "B1/y.pdf": ZoteroItem(item_key="BBB", title="Magnetic relaxation in high temperature superconductors", year="1996"),
        "C1/y.pdf": ZoteroItem(item_key="CCC", title="Magnetic Relaxation in High-Temperature Superconductors", year="1996"),
        "D1/z.pdf": ZoteroItem(item_key="DDD", title="Unrelated work on quench detection", year="2012"),
    }
    monkeypatch.setattr(library, "find_pdfs", lambda: [(path, f"X:/{path}") for path in records])
    monkeypatch.setattr(library, "filter_active_zotero_pdf_entries", lambda entries, *a, **k: entries)
    monkeypatch.setattr(library, "_lookup_zotero", lambda rel_path, full_path="", **k: records[rel_path])
    monkeypatch.setattr(library, "_file_fingerprint", lambda full_path: full_path)

    groups = library.list_duplicate_documents()

    by_paths = {tuple(item["rel_path"] for item in group["items"]): group for group in groups}
    attachments = by_paths[("A1/paper.pdf", "A2/paper.pdf")]
    assert attachments["same_record"] is True
    assert "PDF joints à la même notice" in attachments["reason"]
    separate = by_paths[("B1/y.pdf", "C1/y.pdf")]
    assert separate["same_record"] is False
    assert "notices Zotero distinctes" in separate["reason"]
    assert not any("D1/z.pdf" in paths for paths in by_paths)
