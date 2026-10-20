import pytest

from src import origin_tracing as ot
from src.citation_markers import ReferenceEntry

_INTRO = "Field mapping of bulk samples is a routine measurement in many laboratories today. " * 30
_PASSAGE = (
    "Calculations were first performed for long cylinders in parallel fields. "
    "The critical state in a thin strip was first investigated by Varnel [3] and Ostrik [4]. "
    "Later work added the field dependence of the critical current [5,6].\n"
)
_REFERENCES = (
    "\nReferences\n"
    "[1] A. Quillon, Rev. Fict. Phys. 36 (1964) 31.\n"
    "[2] B. Moraine, J. Fict. 7 (1994) 412.\n"
    "[3] C. Varnel, Strip critical states, J. Fict. Math. 9 (1968) 1308.\n"
    "[4] D. Ostrik, J. Fict. D 3 (1970) 489, doi:10.1000/fict.1970.489.\n"
    "[5] E. Taliot, Phys. Fict. B 48 (1993) 12893.\n"
    "[6] F. Brune, Phys. Fict. B 49 (1994) 9802.\n"
)
_LIBRARY = [
    {
        "citation_id": "ostrik1970",
        "label": "Ostrik (1970)",
        "rel_path": "KEY4/Ostrik 1970.pdf",
        "_title_key": "",
        "_doi_key": "10.1000/fict.1970.489",
        "_surname_key": "ostrik",
        "_year_key": "1970",
    },
    {
        "citation_id": "taliot1993",
        "label": "Taliot (1993)",
        "rel_path": "KEY5/Taliot 1993.pdf",
        "_title_key": "",
        "_doi_key": "",
        "_surname_key": "taliot",
        "_year_key": "1993",
    },
]


@pytest.fixture
def article(monkeypatch):
    text = _INTRO + _PASSAGE + _INTRO + _REFERENCES
    pages = [(0, "Page 1"), (len(_INTRO) + 10, "Page 2")]
    monkeypatch.setattr(ot, "_load_document_text", lambda rel_path: (text, pages))
    monkeypatch.setattr(ot, "_library_index", lambda: _LIBRARY)
    return text


def test_passage_crediting_earlier_sources(monkeypatch, article):
    prompts = []

    def fake_model(model_name, prompt, count):
        prompts.append((prompt, count))
        return {"c1": ot.FOR_THIS_IDEA, "c2": ot.FOR_THIS_IDEA, "c3": ot.FOR_ANOTHER_POINT}

    monkeypatch.setattr(ot, "_ask_model", fake_model)
    result = ot.trace_origin(
        "The critical state of a thin strip was first studied in the late 1960s.",
        "KEY1/Citing paper.pdf",
        evidence_quote="The critical state in a thin strip was first investigated by",
        model_name="qwen3.5:4b",
    )
    assert result["verdict"] == ot.VERDICT_ATTRIBUTES_ELSEWHERE
    assert result["passage"]["quote_found"] is True
    assert result["passage"]["page"] == "Page 2"
    assert "Varnel [3] and Ostrik [4]" in result["passage"]["text"]
    prompt, count = prompts[0]
    assert count == 3
    assert "[3] <c1>" in prompt and "[4] <c2>" in prompt and "[5,6] <c3>" in prompt
    assert "first studied in the late 1960s" in prompt
    first, second, third = result["citations"]
    assert first["markers"] == ["[3]"] and first["attribution"] == ot.FOR_THIS_IDEA
    assert first["references"][0]["library"] is None
    assert second["references"][0]["library"]["citation_id"] == "ostrik1970"
    assert third["attribution"] == ot.FOR_ANOTHER_POINT
    # Author + year match for a title-less entry; private keys never leave the module.
    assert third["references"][0]["library"] == {
        "citation_id": "taliot1993",
        "label": "Taliot (1993)",
        "rel_path": "KEY5/Taliot 1993.pdf",
    }


def test_no_credited_citation_gives_no_attribution(monkeypatch, article):
    monkeypatch.setattr(
        ot, "_ask_model", lambda *args: {"c1": ot.FOR_ANOTHER_POINT, "c2": ot.FOR_ANOTHER_POINT, "c3": ot.FOR_ANOTHER_POINT}
    )
    result = ot.trace_origin("Strips.", "KEY1/Citing paper.pdf",
                             evidence_quote="The critical state in a thin strip was first investigated by",
                             model_name="qwen3.5:4b")
    assert result["verdict"] == ot.VERDICT_NO_ATTRIBUTION


def test_without_model_citations_are_listed_unchecked(monkeypatch, article):
    monkeypatch.setattr(ot, "_ask_model", lambda *args: pytest.fail("no model was selected"))
    result = ot.trace_origin("Strips.", "KEY1/Citing paper.pdf",
                             evidence_quote="The critical state in a thin strip was first investigated by")
    assert result["verdict"] == ot.VERDICT_UNCHECKED
    assert {item["attribution"] for item in result["citations"]} == {ot.NOT_CHECKED}


def test_model_failure_keeps_the_citations(monkeypatch, article):
    def broken(*args):
        raise TimeoutError()

    monkeypatch.setattr(ot, "_ask_model", broken)
    result = ot.trace_origin("Strips.", "KEY1/Citing paper.pdf",
                             evidence_quote="The critical state in a thin strip was first investigated by",
                             model_name="qwen3.5:4b")
    assert result["verdict"] == ot.VERDICT_UNCHECKED
    assert "model_failed" in result["notes"]
    assert len(result["citations"]) == 3


def test_passage_without_marker(monkeypatch, article):
    result = ot.trace_origin("Field mapping is routine.", "KEY1/Citing paper.pdf",
                             evidence_quote="Field mapping of bulk samples is a routine measurement",
                             model_name="qwen3.5:4b")
    assert result["verdict"] == ot.VERDICT_NO_ATTRIBUTION
    assert result["notes"] == ["no_marker_in_passage"]


def test_unknown_passage_is_undetermined(monkeypatch, article):
    result = ot.trace_origin("Quantum gravity needs strings.", "KEY1/Citing paper.pdf",
                             evidence_quote="A sentence that this article never contains anywhere at all.",
                             model_name="qwen3.5:4b")
    assert result["verdict"] == ot.VERDICT_UNDETERMINED
    assert result["notes"] == ["passage_not_found"]


def test_missing_input_is_rejected():
    with pytest.raises(ot.OriginTraceError) as error:
        ot.trace_origin("", "KEY1/x.pdf")
    assert error.value.code == "invalid_request"


def test_quote_location_survives_pdf_hyphenation_and_ligatures():
    body = "Intro text. The magnetic ﬂux was ex-\npelled from the super-\nconductor [2]. End."
    located = ot._locate_quote(body, "The magnetic flux was expelled from the superconductor")
    assert located is not None
    assert body[located.start:located.end].startswith("The magnetic ﬂux")
    assert body[located.start:located.end].endswith("conductor")


def test_ambiguous_author_year_is_not_matched():
    twins = [dict(_LIBRARY[1], citation_id="taliot1993a"), dict(_LIBRARY[1], citation_id="taliot1993b")]
    entry = ReferenceEntry("[5] E. Taliot, Phys. Fict. B 48 (1993) 12893.", 5)
    assert ot._match_library(entry, twins, exclude_path="") is None


def test_model_reliability_flags_small_models():
    assert ot.model_reliability("granite4.2:3b") == "low"
    assert ot.model_reliability("qwen3.5:2b") == "low"
    assert ot.model_reliability("qwen3.5:4b") == "normal"


def test_without_quote_the_passage_is_retrieved(monkeypatch, article):
    monkeypatch.setattr(
        ot,
        "_retrieved_quotes",
        lambda claim, rel_path, model: [
            "Field mapping of bulk samples is a routine measurement in many laboratories today.",
            "The critical state in a thin strip was first investigated by",
        ],
    )
    monkeypatch.setattr(
        ot, "_ask_model", lambda *args: {"c1": ot.FOR_THIS_IDEA, "c2": ot.FOR_THIS_IDEA, "c3": ot.FOR_ANOTHER_POINT}
    )
    result = ot.trace_origin("Thin strips.", "KEY4/Ostrik 1970.pdf", model_name="qwen3.5:4b")
    # The best-ranked sentence cites nothing: the next one, which cites its sources, is kept.
    assert result["passage"]["located_by"] == "retrieval"
    assert "Varnel [3]" in result["passage"]["text"]
    assert result["verdict"] == ot.VERDICT_ATTRIBUTES_ELSEWHERE


def test_validated_quote_is_kept_even_without_citation(monkeypatch, article):
    monkeypatch.setattr(ot, "_retrieved_quotes", lambda *args: pytest.fail("a validated quote needs no search"))
    result = ot.trace_origin("Field mapping is routine.", "KEY1/Citing paper.pdf",
                             evidence_quote="Field mapping of bulk samples is a routine measurement",
                             model_name="qwen3.5:4b")
    assert result["passage"]["located_by"] == "quote"
    assert result["verdict"] == ot.VERDICT_NO_ATTRIBUTION


def test_shared_words_fallback_prefers_a_citing_passage(monkeypatch, article):
    monkeypatch.setattr(ot, "_retrieved_quotes", lambda *args: [])
    monkeypatch.setattr(ot, "_ask_model", lambda *args: {"c1": ot.FOR_THIS_IDEA, "c2": ot.FOR_THIS_IDEA, "c3": ot.FOR_ANOTHER_POINT})
    result = ot.trace_origin("The critical state of a thin strip was first investigated.", "KEY4/Ostrik 1970.pdf",
                             model_name="qwen3.5:4b")
    assert result["passage"]["located_by"] == "words"
    assert "Varnel [3]" in result["passage"]["text"]


def test_shared_words_fallback_skips_front_matter_and_prefers_a_self_claim(monkeypatch):
    text = (
        "Keywords: trapped field, bulk superconductor, record field. "
        + _INTRO
        + "A trapped field record was reached in a bulk superconductor stack after many attempts. "
        + "In this work, we report a record trapped field in a bulk superconductor stack. "
        + _INTRO
        + _REFERENCES
    )
    monkeypatch.setattr(ot, "_load_document_text", lambda rel_path: (text, [(0, "Page 1")]))
    monkeypatch.setattr(ot, "_library_index", lambda: [])
    monkeypatch.setattr(ot, "_retrieved_quotes", lambda *args: [])
    result = ot.trace_origin("A record trapped field in a bulk superconductor stack.", "KEY9/Record.pdf",
                             model_name="qwen3.5:4b")
    assert result["passage"]["located_by"] == "words"
    assert "Keywords" not in result["passage"]["text"]
    assert result["self_claim"] is True
    assert result["verdict"] == ot.VERDICT_NO_ATTRIBUTION


def test_retrieved_quote_reranks_the_sentences_of_the_article(monkeypatch):
    import src.drafting_citations as drafting_citations
    import src.rag.query_engine as query_engine
    import src.rag.reranker as reranker

    class Chunk:
        page_content = (
            "Strips were studied. The critical state of a thin strip was first solved by Varnel "
            "in a closed form. End of story here."
        )

    calls = {}
    monkeypatch.setattr(drafting_citations, "ensure_drafting_rag", lambda model: "")

    def fake_retrieve(question, history, threshold, max_chunks, filters):
        calls["filters"] = filters
        return [Chunk()]

    monkeypatch.setattr(query_engine, "retrieve_for_question", fake_retrieve)
    monkeypatch.setattr(reranker, "rerank_texts", lambda query, texts, top_k=None: [(0, 3.2)])
    quotes = ot._retrieved_quotes("Thin strips were first solved in closed form.", "KEY4/x.pdf", "qwen3.5:4b")
    assert quotes == ["The critical state of a thin strip was first solved by Varnel in a closed form."]
    assert calls["filters"]["sources"] == ["KEY4/x.pdf"]


def test_retrieved_quotes_are_empty_without_index(monkeypatch):
    import src.drafting_citations as drafting_citations

    monkeypatch.setattr(drafting_citations, "ensure_drafting_rag", lambda model: "Index RAG indisponible")
    assert ot._retrieved_quotes("Anything at all here.", "KEY4/x.pdf", None) == []


def _crossref_work(family, given, year, title, doi):
    return {
        "title": [title],
        "author": [{"family": family, "given": given}],
        "issued": {"date-parts": [[int(year)]]},
        "container-title": ["Fict. Phys. Rev."],
        "DOI": doi,
        "is-referenced-by-count": 1234,
        "type": "journal-article",
    }


def test_identify_reference_by_bibliographic_search(monkeypatch):
    import src.pdf_metadata as pdf_metadata

    calls = []

    def fake_get(path, params=None):
        calls.append((path, params))
        return {"message": {"items": [
            _crossref_work("Larsen", "T.", "1980", "Unrelated", "10.1000/other"),
            _crossref_work("Varnel", "C.", "1968", "Strip critical states", "10.1000/strip"),
        ]}}

    monkeypatch.setattr(pdf_metadata, "_crossref_get_json", fake_get)
    work = ot.identify_reference("[3] C. Varnel, Strip critical states, J. Fict. Math. 9 (1968) 1308.")
    assert work is not None
    assert work["doi"] == "10.1000/strip"
    assert work["year"] == "1968" and work["cited_by_count"] == 1234
    assert work["url"] == "https://doi.org/10.1000/strip"
    assert calls[0][1]["query.bibliographic"].startswith("C. Varnel")  # list number dropped


def test_identify_reference_rejects_a_weak_match(monkeypatch):
    import src.pdf_metadata as pdf_metadata

    monkeypatch.setattr(pdf_metadata, "_crossref_get_json", lambda path, params=None: {
        "message": {"items": [_crossref_work("Varnel", "C.", "1975", "Later paper", "10.1000/later")]}
    })
    assert ot.identify_reference("[3] C. Varnel, Strip critical states, J. Fict. Math. 9 (1968) 1308.") is None


def test_identify_reference_rejects_a_namesake_of_the_same_year(monkeypatch):
    import src.pdf_metadata as pdf_metadata

    namesake = dict(_crossref_work("Varnel", "C.", "1968", "The semi-infinite cylinder", "10.1000/cyl"), volume="16", page="61-73")
    same = dict(_crossref_work("Varnel", "C.", "1968", "Field of a current strip", "10.1000/strip"), volume="9", page="1308")
    monkeypatch.setattr(pdf_metadata, "_crossref_get_json", lambda path, params=None: {"message": {"items": [namesake]}})
    assert ot.identify_reference("[3] C. Varnel, J. Fict. Math. 9 (1968) 1308.") is None
    monkeypatch.setattr(pdf_metadata, "_crossref_get_json", lambda path, params=None: {"message": {"items": [namesake, same]}})
    work = ot.identify_reference("[3] C. Varnel, J. Fict. Math. 9 (1968) 1308.")
    assert work is not None and work["doi"] == "10.1000/strip"


def test_identify_reference_uses_the_doi_and_reports_failures(monkeypatch):
    import src.pdf_metadata as pdf_metadata

    paths = []

    def fake_get(path, params=None):
        paths.append(path)
        return {"message": _crossref_work("Ostrik", "D.", "1970", "Strips", "10.1000/fict.1970.489")}

    monkeypatch.setattr(pdf_metadata, "_crossref_get_json", fake_get)
    work = ot.identify_reference("[4] D. Ostrik, J. Fict. D 3 (1970) 489, doi:10.1000/fict.1970.489.")
    assert work is not None
    assert paths == ["/works/10.1000%2Ffict.1970.489"]
    assert work["title"] == "Strips"

    monkeypatch.setattr(pdf_metadata, "_crossref_get_json", lambda path, params=None: None)
    with pytest.raises(ot.OriginTraceError) as error:
        ot.identify_reference("[4] D. Ostrik, J. Fict. D 3 (1970) 489.")
    assert error.value.code == "lookup_failed"
