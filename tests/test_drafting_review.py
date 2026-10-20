from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from src import drafting_citations, drafting_review
from src.rag.evidence import EvidenceChunk


def _chunk(uid: str = "chunk-1", text: str = "Evidence text.") -> EvidenceChunk:
    return EvidenceChunk(
        chunk_uid=uid,
        text=text,
        paper_id="paper-1",
        rel_path="paper.pdf",
        page="3",
        score=0.88,
        _extra={
            "metadata": {
                "zotero_title": "HTS Magnet Argument",
                "zotero_first_author": "Curie",
                "zotero_authors": "Curie, M.",
                "zotero_year": "2025",
                "zotero_doi": "10.1234/argument",
                "rel_path": "paper.pdf",
            }
        },
    )


def test_proofread_draft_normalizes_llm_payload() -> None:
    def proofread_fn(text: str, model_name: str | None) -> dict[str, Any]:
        assert model_name == "local-model"
        assert "il à dit" in text
        return {
            "corrected_text": "Il a dit que ce resultat est robuste.",
            "summary": "Deux corrections.",
            "issues": [
                {
                    "type": "grammaire",
                    "original": "il à dit",
                    "suggestion": "il a dit",
                    "explanation": "Le verbe avoir prend a sans accent.",
                    "severity": "medium",
                }
            ],
        }

    payload = drafting_review.proofread_draft(
        "il à dit que ce resultat est robuste.",
        model_name="local-model",
        proofread_fn=proofread_fn,
    )

    assert payload["ok"] is True
    assert payload["corrected_text"].startswith("Il a dit")
    assert payload["issues"][0]["type"] == "grammaire"
    assert "Deux" in payload["summary"]


def test_stream_proofread_uses_fast_native_ollama_options(monkeypatch) -> None:
    from src import llm_provider

    captured: dict[str, Any] = {}

    def fake_stream(model_name: str, messages: list[dict[str, str]], **kwargs: Any):
        captured.update({"model_name": model_name, "messages": messages, **kwargs})
        yield {"content": '{"corrected_text":"This is robust.",', "done": False}
        yield {
            "content": '"summary":"One correction.","issues":[]}',
            "done": True,
        }

    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(llm_provider, "stream_ollama_chat", fake_stream)

    events = list(
        drafting_review.stream_proofread_draft(
            "This are robust.",
            model_name="fast-local-model",
        )
    )

    assert captured["think"] is False
    assert captured["format_schema"] == "json"
    assert captured["options_override"]["num_predict"] < 5000
    assert any(event["type"] == "progress" for event in events)
    assert events[-1]["type"] == "result"
    assert events[-1]["percent"] == 100
    assert events[-1]["payload"]["corrected_text"] == "This is robust."


def test_long_proofread_is_segmented_and_reassembled(monkeypatch) -> None:
    from src import llm_provider

    prompts: list[str] = []

    def fake_stream(_model_name: str, messages: list[dict[str, str]], **_kwargs: Any):
        prompt = messages[0]["content"]
        prompts.append(prompt)
        section = prompt.rsplit("Draft to edit:\n", 1)[-1]
        yield {
            "content": json.dumps(
                {
                    "corrected_text": section.replace("faute", "correction"),
                    "summary": "Section relue.",
                    "issues": [],
                }
            )
        }

    monkeypatch.setattr(llm_provider, "is_ollama_provider", lambda: True)
    monkeypatch.setattr(llm_provider, "get_ollama_context_length", lambda: 4096)
    monkeypatch.setattr(llm_provider, "stream_ollama_chat", fake_stream)
    text = "\n\n".join(
        f"Paragraphe {index} avec une faute. " + ("Texte scientifique. " * 80)
        for index in range(8)
    )

    events = list(drafting_review.stream_proofread_draft(text, model_name="local-model"))

    result = events[-1]["payload"]
    assert len(prompts) > 1
    assert result["segmented"] is True
    assert result["section_count"] == len(prompts)
    assert "faute" not in result["corrected_text"]
    assert "Paragraphe 0" in result["corrected_text"]
    assert "Paragraphe 7" in result["corrected_text"]
    assert any(event.get("section_index") == 2 for event in events)


def test_proofread_split_prefers_paragraph_boundaries() -> None:
    text = "\n\n".join(("Premier paragraphe. " * 90, "Deuxieme paragraphe. " * 90))

    sections = drafting_review._split_proofread_sections(text, target_tokens=700)

    assert len(sections) >= 2
    assert all(drafting_review._estimate_tokens(section) <= 700 for section in sections)
    assert "Premier paragraphe" in sections[0]
    assert "Deuxieme paragraphe" in sections[-1]


def test_argument_suggestions_are_limited_to_returned_sources(monkeypatch) -> None:
    monkeypatch.setattr(
        drafting_citations,
        "get_item_citations",
        lambda _: {
            "title": "HTS Magnet Argument",
            "apa": "Curie, M. (2025). HTS Magnet Argument.",
            "bibtex": "@article{Curie2025,title={HTS Magnet Argument}}",
        },
    )

    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        assert query
        return [
            _chunk(
                text=(
                    "No-insulation HTS coils improve thermal stability, but they "
                    "introduce charging-delay trade-offs that should be discussed."
                )
            )
        ]

    def argument_fn(
        text: str,
        model_name: str | None,
        sources: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        assert sources[0]["source_id"] == "S1"
        return {
            "summary": "Le brouillon parle de stabilite.",
            "claims": [
                {
                    "claim": "Les bobines no-insulation ameliorent la stabilite.",
                    "support_status": "supported",
                    "source_ids": ["S1"],
                    "note": "Claim central deja present.",
                }
            ],
            "suggestions": [
                {
                    "kind": "limitation",
                    "title": "Nuancer la stabilite",
                    "draft_gap": "Le brouillon ne mentionne pas le compromis de delai de charge.",
                    "suggestion": "Ajouter que le gain de stabilite implique un compromis de delai de charge.",
                    "insertion_hint": "Apres la phrase sur les bobines no-insulation.",
                    "insert_text": {
                        "sentence": "This stability gain comes with charging-delay trade-offs.",
                        "paragraph": (
                            "This stability gain should be discussed together with "
                            "charging-delay trade-offs."
                        ),
                        "note": "Nuancer la stabilite par le delai de charge.",
                    },
                    "source_ids": ["S1", "S999"],
                    "evidence_quote": "improve thermal stability, but they introduce charging-delay trade-offs",
                    "validation_status": "supported",
                }
            ],
        }

    monkeypatch.setattr(drafting_review, "_invoke_argument_json", lambda *args: {
        "suggestions": [{"index": 0, "supported": True}], "claims": [],
    })
    payload = drafting_review.suggest_argument_improvements(
        "Les bobines no-insulation HTS ameliorent la stabilite.",
        model_name="local-model",
        retriever=retriever,
        argument_fn=argument_fn,
    )

    assert payload["ok"] is True
    assert payload["suggestions"][0]["source_ids"] == ["S1"]
    assert payload["suggestions"][0]["kind"] == "limitation"
    assert payload["suggestions"][0]["draft_gap"].startswith("Le brouillon")
    assert payload["suggestions"][0]["insert_text"]["sentence"].startswith("This stability")
    assert payload["suggestions"][0]["validation_status"] == "supported"
    assert payload["suggestions"][0]["evidence_status"] == "verified"
    assert "confidence" not in payload["suggestions"][0]
    assert payload["claims"][0]["source_ids"] == ["S1"]
    assert payload["sources"][0]["title"] == "HTS Magnet Argument"
    assert payload["sources"][0]["apa"].startswith("Curie")


def test_argument_prompt_targets_source_grounded_article_enrichment() -> None:
    prompt = drafting_review._build_argument_prompt(
        "No-insulation coils improve stability.",
        [
            {
                "source_id": "S1",
                "title": "HTS Magnet Argument",
                "author": "Curie",
                "year": "2025",
                "doi": "10.1234/argument",
                "page": 3,
                "quote": "No-insulation coils add thermal stability with charging-delay trade-offs.",
            }
        ],
        4,
    )

    assert "missing facts" in prompt
    assert "background context" in prompt
    assert "limitations" in prompt
    assert '"claims"' in prompt
    assert '"kind"' in prompt
    assert '"draft_gap"' in prompt
    assert '"insert_text"' in prompt
    assert '"validation_status"' in prompt
    assert "Do not invent results" in prompt
    assert "same language as the draft" in prompt
    assert "in English" in prompt
    assert "source-grounded fact, context, nuance, or argument" in prompt

    mechanism_prompt = drafting_review._build_argument_prompt(
        "No-insulation coils improve stability.", [], 1, argument_kind="mechanism"
    )
    assert "All suggestions MUST have kind 'mechanism'" in mechanism_prompt
    assert "Do not return suggestions of another kind" in mechanism_prompt

    unknown_prompt = drafting_review._build_argument_prompt(
        "No-insulation coils improve stability.", [], 1, argument_kind="unknown"
    )
    assert "All suggestions MUST have kind" not in unknown_prompt

    french_prompt = drafting_review._build_argument_prompt(
        "Les bobines sans isolation améliorent la stabilité.", [], 4
    )
    assert "in French" in french_prompt


def test_proofread_prompt_keeps_draft_language() -> None:
    prompt = drafting_review._build_proofread_prompt("Le verbe avoir prend a sans accent.")

    assert "Never translate" in prompt
    assert "same language as the draft" in prompt
    assert "English version" not in prompt
    assert "explanation in English" not in prompt


def test_argument_normalization_does_not_invent_evidence_for_legacy_payload(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", lambda _: {})

    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        return [_chunk(text="Charging-delay trade-offs remain important evidence.")]

    def argument_fn(
        text: str,
        model_name: str | None,
        sources: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "summary": "Legacy payload.",
            "suggestions": [
                {
                    "title": "Add a trade-off",
                    "suggestion": "Mention the charging-delay trade-off.",
                    "source_ids": ["S1"],
                    "confidence": 0.9,
                }
            ],
        }

    payload = drafting_review.suggest_argument_improvements(
        "No-insulation coils improve stability.",
        model_name="local-model",
        retriever=retriever,
        argument_fn=argument_fn,
    )

    assert payload["suggestions"] == []
    assert payload["ok"] is True
    assert payload["status"] == "verification_rejected"
    assert payload["review_candidates"][0]["validation_status"] == "weak_evidence"
    assert payload["review_candidates"][0]["rejection_reason"]


def test_argument_normalization_rejects_unsupported_suggestions(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", lambda _: {})

    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        return [_chunk(text="Grounded evidence.")]

    def argument_fn(
        text: str,
        model_name: str | None,
        sources: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "summary": "Unsupported idea.",
            "suggestions": [
                {
                    "title": "Speculative addition",
                    "suggestion": "Invent a broader claim.",
                    "source_ids": ["S1"],
                    "evidence_quote": "Grounded evidence.",
                    "validation_status": "unsupported",
                    "confidence": 0.8,
                }
            ],
        }

    payload = drafting_review.suggest_argument_improvements(
        "No-insulation coils improve stability.",
        model_name="local-model",
        retriever=retriever,
        argument_fn=argument_fn,
    )

    assert payload["ok"] is True
    assert payload["status"] == "verification_rejected"
    assert payload["suggestions"] == []
    assert payload["review_candidates"][0]["validation_status"] == "unsupported"
    assert payload["review_candidates"][0]["rejection_reason"]


def _argument_fixture():
    source = {"source_id": "S1", "citation_id": "Curie2025", "quote": "A dedicated apparatus was assembled."}
    idea = {
        "suggestion": "The apparatus enables unprecedented continuous-field NMR.",
        "source_ids": ["S1"], "evidence_quote": source["quote"],
        "validation_status": "supported",
        "insert_text": {"sentence": "An apparatus was assembled.",
                        "paragraph": "An apparatus was assembled for unprecedented continuous-field NMR."},
    }
    return source, idea


@pytest.mark.parametrize("change", [
    {"evidence_quote": "NMR at 50 T was demonstrated."},
    {"evidence_quote": ""},
    {"validation_status": ""},
    {"validation_status": "too_general"},
    {"validation_status": "weak_evidence"},
])
def test_argument_normalization_requires_explicit_support_and_real_quote(change):
    source, idea = _argument_fixture()
    result = drafting_review._normalize_argument_payload(
        {"suggestions": [{**idea, **change}]}, [source], 4,
    )
    assert result["suggestions"] == []


def test_argument_requires_evidence_from_every_linked_source():
    source, idea = _argument_fixture()
    other = {"source_id": "S2", "quote": "Another result."}
    idea["source_ids"] = ["S1", "S2"]
    assert not drafting_review._normalize_argument_payload({"suggestions": [idea]}, [source, other], 4)["suggestions"]
    idea["evidence"] = [{"source_id": "S1", "quote": source["quote"]},
                        {"source_id": "S2", "quote": other["quote"]}]
    assert drafting_review._normalize_argument_payload({"suggestions": [idea]}, [source, other], 4)["suggestions"]


@pytest.mark.parametrize("verdict", [
    {}, {"suggestions": [{"index": 0, "supported": False}]},
    {"suggestions": [{"index": 0, "supported": "true"}]},
    {"suggestions": [{"index": 0, "supported": True}, {"index": 0, "supported": False}]},
])
def test_argument_verifier_rejects_unproven_or_ambiguous_decisions(monkeypatch, verdict):
    source, idea = _argument_fixture()
    captured = []

    def verify(model, prompt):
        captured.append(prompt)
        return verdict

    monkeypatch.setattr(drafting_review, "_invoke_argument_json", verify)
    payload = drafting_review._normalize_argument_payload({"suggestions": [idea]}, [source], 4)
    result = drafting_review._verify_argument_payload("NMR context", "mistral", payload)
    assert result["suggestions"] == []
    assert "NMR context" in captured[0]
    assert idea["insert_text"]["paragraph"] in captured[0]
    assert source["quote"] in captured[0]


def test_argument_verifier_preserves_rejection_reason_as_non_insertable_candidate(monkeypatch):
    source, idea = _argument_fixture()
    monkeypatch.setattr(drafting_review, "_invoke_argument_json", lambda *_args: {
        "suggestions": [{
            "index": 0,
            "supported": False,
            "reason": "The paragraph adds a capability absent from the excerpt.",
        }],
        "claims": [],
    })

    payload = drafting_review._normalize_argument_payload({"suggestions": [idea]}, [source], 4)
    result = drafting_review._verify_argument_payload("NMR context", "mistral", payload)

    assert result["suggestions"] == []
    assert result["review_candidates"][0]["validation_status"] == "weak_evidence"
    assert result["review_candidates"][0]["rejection_reason"].startswith("The paragraph")


def test_argument_generation_falls_back_to_available_schema_model(monkeypatch):
    from src import llm_provider

    source, idea = _argument_fixture()
    calls = []

    monkeypatch.setattr(
        llm_provider,
        "list_available_models",
        lambda: (["fragile:1b", "reliable:8b"], "test"),
    )

    def invoke(model, prompt):
        calls.append((model, prompt.startswith("Independently audit")))
        if model == "fragile:1b":
            raise ValueError("invalid JSON after validation")
        if prompt.startswith("Independently audit"):
            return {"suggestions": [{"index": 0, "supported": True}], "claims": []}
        return {"summary": "Fallback worked.", "suggestions": [idea], "claims": []}

    monkeypatch.setattr(drafting_review, "_invoke_argument_json", invoke)
    result = drafting_review.suggest_argument_improvements(
        "Draft about an apparatus.",
        "fragile:1b",
        retriever=lambda _query, _count: [_chunk(text=source["quote"])],
    )

    assert result["ok"] is True
    assert result["status"] == "success"
    assert result["model"]["requested"] == "fragile:1b"
    assert result["model"]["used"] == "reliable:8b"
    assert result["model"]["fallback_used"] is True
    assert calls[:2] == [("fragile:1b", False), ("reliable:8b", False)]


def test_argument_generation_invalid_has_distinct_status(monkeypatch):
    from src import llm_provider

    monkeypatch.setattr(llm_provider, "list_available_models", lambda: ([], "none"))
    monkeypatch.setattr(
        drafting_review,
        "_invoke_argument_json",
        lambda *_args: (_ for _ in ()).throw(ValueError("invalid JSON after repair")),
    )

    result = drafting_review.suggest_argument_improvements(
        "Draft about an apparatus.",
        "fragile:1b",
        retriever=lambda _query, _count: [_chunk(text="A dedicated apparatus was assembled.")],
    )

    assert result["ok"] is False
    assert result["status"] == "generation_invalid"
    assert result["suggestions"] == []


def test_argument_empty_and_no_sources_are_successful_business_results() -> None:
    no_sources = drafting_review.suggest_argument_improvements(
        "Draft text.", "model:8b", retriever=lambda _query, _count: []
    )
    empty = drafting_review.suggest_argument_improvements(
        "Draft text.",
        "model:8b",
        retriever=lambda _query, _count: [_chunk(text="A source passage.")],
        argument_fn=lambda *_args: {"summary": "Nothing new.", "suggestions": [], "claims": []},
    )

    assert (no_sources["ok"], no_sources["status"]) == (True, "no_sources")
    assert (empty["ok"], empty["status"]) == (True, "empty")


def test_argument_verifier_failure_does_not_publish_suggestions(monkeypatch):
    source, idea = _argument_fixture()
    monkeypatch.setattr(drafting_citations, "get_item_citations", lambda _: {})

    def fail(*args):
        raise TimeoutError("test timeout")

    monkeypatch.setattr(drafting_review, "_invoke_argument_json", fail)
    result = drafting_review.suggest_argument_improvements(
        "Draft about an apparatus.", "mistral",
        retriever=lambda q, k: [_chunk(text=source["quote"])],
        argument_fn=lambda *args: {"suggestions": [idea]},
    )
    assert result["ok"] is False
    assert result["suggestions"] == []
    assert "vérification" in result["summary"]


def test_argument_sources_preserve_distinct_passages_and_cover_end_of_draft(monkeypatch):
    monkeypatch.setattr(drafting_citations, "get_item_citations", lambda _: {})
    queries = []
    sentences = [f"Scientific claim number {i} about a different experimental condition." for i in range(10)]

    def retrieve(query, k):
        queries.append(query)
        return [_chunk(uid=query, text=query)]

    sources = drafting_review._collect_argument_sources(" ".join(sentences), max_sources=8, retriever=retrieve)
    assert len(queries) == 4
    assert queries[0] == sentences[0]
    assert queries[-1] == sentences[-1]
    assert len(sources) == 4  # all have the same article DOI
    assert sources[-1]["quote"] == sentences[-1]


def test_argument_retrieval_uses_deep_search_with_reranking(monkeypatch):
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return lambda q, k: []

    monkeypatch.setattr(drafting_review, "build_query_engine_retriever", build)
    drafting_review._collect_argument_sources("A scientific statement.", max_sources=8, retriever=None)
    assert captured == {"threshold": 0.0, "depth": "deep", "use_reranker": True}


def test_argument_retrieval_honours_cancellation_before_and_after_search():
    class Signal:
        def __init__(self, aborted: bool = False) -> None:
            self.aborted = aborted

        def throw_if_aborted(self) -> None:
            if self.aborted:
                raise InterruptedError("cancelled")

    calls: list[tuple[str, int]] = []

    def retriever(query: str, count: int) -> list[Any]:
        calls.append((query, count))
        return []

    cancelled = Signal(aborted=True)
    with pytest.raises(InterruptedError):
        drafting_review._collect_argument_sources(
            "A scientific statement.",
            max_sources=8,
            retriever=retriever,
            abort_signal=cancelled,
        )
    assert calls == []

    during_search = Signal()

    def retrieve_then_cancel(query, count):
        during_search.aborted = True
        return [_chunk(text="A source passage.")]

    with pytest.raises(InterruptedError):
        drafting_review._collect_argument_sources(
            "A scientific statement.",
            max_sources=8,
            retriever=retrieve_then_cancel,
            abort_signal=during_search,
        )


def test_claim_is_not_supported_without_a_verified_linked_excerpt():
    source, _ = _argument_fixture()
    result = drafting_review._normalize_argument_payload({"claims": [
        {"claim": "An unprecedented capability.", "support_status": "supported", "source_ids": ["S99"]},
        {"claim": "An unprecedented capability.", "support_status": "supported", "source_ids": ["S1"]},
    ]}, [source], 4)
    assert all(c["support_status"] == "needs_source" for c in result["claims"])


def test_argument_generation_and_independent_check_use_bounded_llm_calls(monkeypatch):
    from src import llm_provider

    source, idea = _argument_fixture()
    monkeypatch.setattr(drafting_citations, "get_item_citations", lambda _: {})
    options = []
    prompts = []
    replies = iter([
        {"suggestions": [idea]},
        {"suggestions": [{"index": 0, "supported": True}]},
    ])

    def structured(model, messages, response_model, **kwargs):
        assert model == "mistral"
        options.append(kwargs)
        prompts.append(messages[0]["content"])
        return response_model.model_validate(next(replies))

    monkeypatch.setattr(llm_provider, "invoke_structured_chat", structured)
    result = drafting_review.suggest_argument_improvements(
        "Full draft context about the experiment.", "mistral",
        retriever=lambda q, k: [_chunk(text=source["quote"])],
    )
    assert result["ok"] is True
    assert len(options) == 2
    assert all(o["temperature"] == 0.0 and o["top_p"] == 0.2 for o in options)
    # The structured route receives a window hint large enough for source-heavy prompts.
    assert all(o["context_tokens_hint"] > drafting_review._ARGUMENT_MAX_OUTPUT_TOKENS for o in options)
    assert "Independently audit" in prompts[1]
    assert "Full draft context" in prompts[1]
    assert result["model"]["capability"] in {"schema", "read_then_parse"}


def test_proofread_surfaces_edits_missing_from_the_issue_list() -> None:
    original = (
        "Quench modeling studies show that the discharge should occur before "
        "the dissipation exceed a few mV."
    )
    payload = {
        # The model also swapped "discharge" for "quenching" without saying so.
        "corrected_text": (
            "Quench modeling studies show that quenching should occur before "
            "the dissipation exceeds a few mV."
        ),
        "issues": [
            {
                "type": "grammar",
                "original": "the dissipation exceed a few",
                "suggestion": "the dissipation exceeds a few",
                "explanation": "Agreement.",
            }
        ],
    }

    result = drafting_review._normalize_proofread_payload(payload, original)

    unlisted = [issue for issue in result["issues"] if issue.get("unlisted")]
    assert len(unlisted) == 1
    assert "the discharge should" in unlisted[0]["original"]
    assert "quenching should" in unlisted[0]["suggestion"]
    assert unlisted[0]["original"] in original
    assert result["unlisted_change_count"] == 1


def test_proofread_rewrite_falls_back_to_listed_corrections() -> None:
    original = "The coil was tested at 4.2 K and it's current reached 300 A."
    payload = {
        "corrected_text": "Completely different prose about another experiment entirely.",
        "issues": [{"original": "it's current", "suggestion": "its current"}],
    }

    result = drafting_review._normalize_proofread_payload(payload, original)

    assert result["corrected_text"] == "The coil was tested at 4.2 K and its current reached 300 A."
    assert not any(issue.get("unlisted") for issue in result["issues"])


def test_argument_sources_favour_other_papers_before_more_of_the_same() -> None:
    def source(uid: str, paper: str) -> dict[str, Any]:
        return {"chunk_uid": uid, "citation_id": paper, "quote": uid}

    sources = drafting_review._dedupe_sources(
        [source("a1", "A"), source("a2", "A"), source("a3", "A"), source("b1", "B"), source("a4", "A")]
    )

    assert [item["chunk_uid"] for item in sources] == ["a1", "a2", "b1", "a3", "a4"]
    assert [item["source_id"] for item in sources] == ["S1", "S2", "S3", "S4", "S5"]


def test_argument_claim_contradicted_by_a_quoted_source_is_kept() -> None:
    source = {"source_id": "S1", "quote": "No-insulation winding reduces but does not remove screening currents."}
    payload = {
        "claims": [
            {
                "claim": "No-insulation winding eliminates screening currents.",
                "support_status": "contradicted",
                "source_ids": ["S1"],
                "evidence_quote": "reduces but does not remove screening currents",
            },
            {
                "claim": "Unquoted contradiction.",
                "support_status": "contradicted",
                "source_ids": ["S1"],
                "evidence_quote": "",
            },
        ]
    }

    result = drafting_review._normalize_argument_payload(payload, [source], 4)

    assert [claim["support_status"] for claim in result["claims"]] == ["contradicted", "needs_source"]


def test_argument_source_shares_citation_identity_with_citation_search(monkeypatch) -> None:
    # Without a Zotero match, both pipelines used to pick different keys for
    # one paper, so an inserted argument got a second number and bibliography entry.
    monkeypatch.setattr(drafting_citations, "get_item_citations", lambda _: {})
    chunk = _chunk()
    chunk._extra["metadata"].update({"zotero_item_key": "ABCD1234", "zotero_citation_key": "curie2025"})

    source = drafting_review._source_from_chunk(chunk, 1)
    citation = drafting_citations._citation_payload_from_chunk(
        chunk=chunk, sentence_index=0, sentence_text="", source_pipeline="rag", support_score=0.5,
    )

    assert source["source_id"] == "S1"
    assert source["citation_id"] == citation["citation_id"] == "curie2025"
    assert source["label"] == citation["label"]
    assert source["apa"] == citation["apa"]
    assert source["quote"] == "Evidence text."
