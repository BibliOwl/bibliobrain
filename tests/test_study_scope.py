from types import SimpleNamespace

from langchain_core.documents import Document

from src.rag.study_scope import (
    named_studies,
    overview_passages,
    overview_share,
    resolve_study_sources,
)

_TITLE = "High temperature superconductivity: the road to higher critical temperature"


def _paper(author: str, year: str, path: str, title: str = _TITLE) -> SimpleNamespace:
    return SimpleNamespace(
        metadata={
            "zotero_first_author": author,
            "zotero_year": year,
            "zotero_title": title,
            "rel_path": path,
        }
    )


def test_names_and_years_are_found_with_or_without_parentheses() -> None:
    assert named_studies("Que dit Uchida 2015 sur la supraconductivité ?") == [("uchida", "2015")]
    assert named_studies("Compare Kim et al. (2019) et Bean, 1962") == [
        ("kim", "2019"),
        ("bean", "1962"),
    ]


def test_a_named_study_resolves_to_its_library_paper() -> None:
    papers = [
        _paper("Uchida, Shin-ichi", "2015", "L6EDW2A4/Uchida - 2015.pdf"),
        _paper("Uchida, Shin-ichi", "2011", "AAAA/Uchida - 2011.pdf", title="Other"),
        _paper("Larbalestier, David", "2015", "BBBB/Larbalestier - 2015.pdf", title="Other"),
    ]
    records = resolve_study_sources("Que dit Uchida 2015 sur la supraconductivité ?", papers)
    assert [record["rel_path"] for record in records] == ["L6EDW2A4/Uchida - 2015.pdf"]


def test_a_name_that_is_not_a_library_first_author_is_left_to_the_search() -> None:
    papers = [_paper("Larbalestier, David", "2015", "BBBB/Larbalestier - 2015.pdf")]
    assert resolve_study_sources("Le modèle de Bean 1962 ?", papers) == []
    assert resolve_study_sources("Que dit Uchida 2015 ?", papers) == []


def test_too_many_papers_for_one_name_and_year_is_not_a_reference() -> None:
    papers = [
        _paper("Wang, Li", "2020", f"P{index}/Wang - 2020.pdf", title=f"Paper {index}")
        for index in range(4)
    ]
    assert resolve_study_sources("Que dit Wang 2020 ?", papers) == []


def test_a_question_on_the_paper_subject_is_mostly_an_overview() -> None:
    studies = [("uchida", "2015")]
    question = "Que dit Uchida 2015 sur la supraconductivité à haute température ?"
    assert overview_share(question, _TITLE, studies=studies) == 2 / 3
    assert overview_share("Résume Uchida 2015", _TITLE, studies=studies) == 2 / 3


def test_a_question_on_one_point_keeps_most_passages_from_the_search() -> None:
    studies = [("uchida", "2015")]
    assert overview_share("Que dit Uchida 2015 sur le dopage ?", _TITLE, studies=studies) == 1 / 3
    assert overview_share("Quelle est la Tc de LaFeAsO ?", _TITLE) == 0.0


def _chunk(page: int, index: int, text: str, uid_prefix: str = "3FHX") -> Document:
    filler = " The discussion continues with more details about the materials." * 4
    return Document(
        page_content=text + filler,
        metadata={
            "page": page,
            "chunk_uid": f"{uid_prefix}:{index}:abc",
            "content_type": "pdf_page",
        },
    )


def test_the_overview_spreads_over_the_whole_book_in_document_order() -> None:
    chunks = [
        _chunk(1, 0, "ISBN 978-4-431 All rights reserved."),
        _chunk(5, 1, "Preface\nHigh-Tc superconductivity in the cuprates was discovered in 1986."),
    ]
    index = 2
    for chapter, page in enumerate((20, 60, 100, 140, 180), start=1):
        chunks.append(_chunk(page, index, f"Chapter {chapter}\nAbstract Chapter {chapter} opens."))
        index += 1
        for offset in range(1, 6):
            chunks.append(_chunk(page + offset, index, f"Body text of chapter {chapter}."))
            index += 1
    chunks.append(_chunk(200, index, "References\nKim (2001) ... Lee (2003) ..."))
    chunks.append(_chunk(195, index + 1, "6 Conclusions\nThe road to higher Tc goes on."))
    picked = overview_passages(list(reversed(chunks)), 6)
    pages = [doc.metadata["page"] for doc in picked]
    assert pages == sorted(pages)
    assert pages[0] == 5  # the preface, not the copyright page
    assert 195 in pages  # the conclusion
    assert 200 not in pages and 1 not in pages
    assert len({doc.page_content[:20] for doc in picked}) == 6
    assert sum(1 for doc in picked if doc.page_content.startswith("Chapter")) == 4


def test_ocr_twins_of_text_pages_are_skipped() -> None:
    chunks = [
        _chunk(1, 0, "Abstract The paper studies AC losses."),
        _chunk(1, 1, "Abstract The paper studies AC losses (OCR).", uid_prefix="ocr-x"),
        _chunk(2, 2, "Introduction\nCoils are used."),
    ]
    chunks[1].metadata["chunk_uid"] = "ocr-123"
    picked = overview_passages(chunks, 3)
    assert all(not doc.metadata["chunk_uid"].startswith("ocr-") for doc in picked)
