from __future__ import annotations

import json
from typing import Any

import pytest

from src import drafting_citations
from src.rag.evidence import EvidenceChunk


def _chunk(
    *,
    uid: str = "chunk-1",
    text: str,
    item_key: str = "Alpha2024",
    doi: str = "10.1234/example",
) -> EvidenceChunk:
    return EvidenceChunk(
        chunk_uid=uid,
        text=text,
        paper_id="paper-1",
        rel_path="paper.pdf",
        score=0.9,
        _extra={
            "metadata": {
                "zotero_title": "HTS Magnet Losses",
                "zotero_first_author": "Alpha",
                "zotero_authors": "Alpha, A.",
                "zotero_year": "2024",
                "zotero_item_key": item_key,
                "zotero_doi": doi,
                "rel_path": "paper.pdf",
            }
        },
    )


def _citations(_: str) -> dict[str, Any]:
    return {
        "found": True,
        "title": "HTS Magnet Losses",
        "citation_key": "Alpha2024",
        "apa": "Alpha, A. (2024). HTS Magnet Losses.",
        "bibtex": "@article{Alpha2024,title={HTS Magnet Losses}}",
    }


def _highly_cited(_: str) -> dict[str, Any]:
    payload = _citations(_)
    payload["citation_count"] = 1200
    payload["influential_citation_count"] = 80
    return payload


def _accept_all(candidates: list[dict[str, Any]], model_name: str | None) -> list[dict[str, Any]]:
    assert model_name is None
    return [
        {
            "candidate_id": candidate["candidate_id"],
            "support_status": "supported",
            "confidence": 0.91,
            "evidence_quote": candidate["evidence_quote"],
            "support_explanation": "L'extrait original soutient directement la phrase.",
        }
        for candidate in candidates
    ]


def _validation_candidates(prompt: str) -> list[dict[str, Any]]:
    """Read back the candidate records the validator prompt carries."""

    payload = json.loads(str(prompt).split("Candidates:\n", 1)[1])
    return list(payload)


def _patch_structured(
    monkeypatch,
    decide,
    *,
    captured: dict[str, Any] | None = None,
):
    """Route ``invoke_structured_chat`` to a deterministic schema-mode stub."""

    from src import llm_provider

    def fake_invoke(model_name, messages, response_model=None, **kwargs):
        prompt = str(messages[-1]["content"])
        if captured is not None:
            captured.update(
                {
                    "model_name": model_name,
                    "response_model": response_model,
                    "messages": messages,
                    "kwargs": kwargs,
                    "prompt": prompt,
                }
            )
        return response_model(decisions=decide(_validation_candidates(prompt)))

    monkeypatch.setattr(llm_provider, "invoke_structured_chat", fake_invoke)
    return captured


def _patch_structured_failure(monkeypatch, message: str) -> None:
    from src import llm_provider

    def fake_invoke(*args, **kwargs):
        raise RuntimeError(message)

    monkeypatch.setattr(llm_provider, "invoke_structured_chat", fake_invoke)


def _patch_text_fallback(monkeypatch, response: Any) -> None:
    from src import llm_provider

    monkeypatch.setattr(llm_provider, "create_chat_llm", lambda *a, **k: object())

    def invoke(*args):
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(llm_provider, "invoke_text", invoke)


def test_split_into_sentences_keeps_unpunctuated_paragraphs() -> None:
    text = (
        "AC losses increase with ripple field amplitude\n\n"
        "Thermal cycling changes the conductor response\n\n"
        "Mechanical loading also affects stability"
    )

    assert drafting_citations._split_into_sentences(text) == [
        "AC losses increase with ripple field amplitude",
        "Thermal cycling changes the conductor response",
        "Mechanical loading also affects stability",
    ]


@pytest.mark.parametrize("line_break", ["\n", "\n\n"])
def test_citations_keep_wrapped_nmr_claim_intact(line_break) -> None:
    claim = (
        "Une expérience RMN autour de 50 T en champ continu constituerait"
        f"{line_break}donc une capacité expérimentale inédite."
    )
    assert drafting_citations._split_into_sentences(claim) == [claim.replace(line_break, " ")]
    assert drafting_citations._split_into_sentences("Le champ atteint 3.5 T. Suite.") == [
        "Le champ atteint 3.5 T.", "Suite.",
    ]


def test_citations_send_cross_language_evidence_and_context_to_validator(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    source = "Measurements were performed in a steady magnetic field of fifty tesla."
    paragraph = (
        "Une expérience RMN autour de 50 T en champ continu constituerait\n"
        "donc une capacité expérimentale inédite. Elle permettrait des acquisitions longues."
    )
    seen = []

    def validate(candidates, model):
        seen.extend(candidates)
        return []  # This source does not establish novelty.

    events = list(drafting_citations.stream_citations_for_draft(
        paragraph, retriever=lambda q, k: [_chunk(text=source)], validator_fn=validate,
    ))
    assert seen
    assert seen[0]["sentence_text"].startswith("Une expérience RMN autour de 50 T")
    assert "acquisitions longues" in seen[0]["context_after"]
    prompt = drafting_citations._build_validation_prompt(seen[:1])
    assert source in prompt
    assert "novelty or priority" in prompt
    assert "draft_context" in prompt
    assert "inédite. Elle permettrait des acquisitions longues." in prompt
    assert events[-1]["accepted_suggestions"] == 0


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (TimeoutError("unavailable"), drafting_citations.VALIDATION_STATUS_ERROR),
        ("not json", drafting_citations.VALIDATION_STATUS_ERROR),
        ("", drafting_citations.VALIDATION_STATUS_ERROR),
        ('{"decisions": []}', drafting_citations.VALIDATION_STATUS_NO_DECISIONS),
    ],
)
def test_llm_failure_never_accepts_keyword_overlap(
    monkeypatch, response, expected_status
) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    _patch_structured_failure(monkeypatch, "schema mode unavailable")
    _patch_text_fallback(monkeypatch, response)

    claim = "AC losses in HTS magnets increase with ripple field amplitude."
    result = drafting_citations.suggest_citations_for_draft(
        claim, model_name="mistral", retriever=lambda q, k: [_chunk(text=claim)],
    )

    assert result["suggestions"] == []
    assert result["validation_status"] == expected_status


def test_structured_validation_accepts_grounded_candidate(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    chunk_text = (
        "AC losses in HTS magnets increase with ripple field amplitude "
        "during dynamic operation."
    )
    sentence = "AC losses in HTS magnets increase with ripple field amplitude."
    captured: dict[str, Any] = {}

    def decide(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": candidate["candidate_id"],
                "support_status": "supported",
                "confidence": 0.9,
                "evidence_quote": candidate["original_evidence_chunk"],
                "support_explanation": "L'extrait original soutient la phrase.",
            }
            for candidate in candidates
        ]

    _patch_structured(monkeypatch, decide, captured=captured)
    payload = drafting_citations.suggest_citations_for_draft(
        sentence,
        model_name="mistral",
        retriever=lambda q, k: [_chunk(text=chunk_text)],
    )

    assert len(payload["suggestions"]) == 1
    assert payload["validation_status"] == drafting_citations.VALIDATION_STATUS_OK
    suggestion = payload["suggestions"][0]
    assert suggestion["support_status"] == "supported"
    assert suggestion["evidence_chunk_uid"] == "chunk-1"
    assert suggestion["sentence_text"] == sentence
    # The drafting panel opens the cited article from these two fields.
    assert suggestion["rel_path"] == "paper.pdf"
    assert "page" in suggestion
    # The validator runs through the Pydantic schema and an explicit context budget.
    assert captured["response_model"] is drafting_citations.CitationValidationResult
    assert captured["messages"][0]["role"] == "system"
    assert chunk_text in captured["prompt"]
    assert captured["kwargs"]["temperature"] == 0.0
    assert captured["kwargs"]["context_tokens_hint"] >= len(captured["prompt"]) / 4


def test_structured_validation_rejects_fabricated_evidence_quote(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    chunk_text = "Un dispositif experimental dedie a ete assemble."

    def decide(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": candidate["candidate_id"],
                "support_status": "supported",
                "confidence": 0.9,
                "evidence_quote": "The first continuous 50 T NMR experiment.",
                "support_explanation": "Citation inventee.",
            }
            for candidate in candidates
        ]

    _patch_structured(monkeypatch, decide)
    payload = drafting_citations.suggest_citations_for_draft(
        "Une capacite experimentale inedite en RMN a 50 T.",
        model_name="mistral",
        retriever=lambda q, k: [_chunk(text=chunk_text)],
    )

    assert payload["suggestions"] == []
    assert payload["validation_status"] == drafting_citations.VALIDATION_STATUS_REJECTED


def test_structured_validation_rejects_unknown_status(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)

    def decide(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": candidate["candidate_id"],
                "support_status": "probably_fine",
                "confidence": 0.99,
                "evidence_quote": candidate["original_evidence_chunk"],
                "support_explanation": "Statut non reconnu.",
            }
            for candidate in candidates
        ]

    _patch_structured(monkeypatch, decide)
    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        model_name="mistral",
        retriever=lambda q, k: [_chunk(text="AC losses increase with ripple field.")],
    )

    assert payload["suggestions"] == []
    assert payload["validation_status"] == drafting_citations.VALIDATION_STATUS_REJECTED


def test_structured_validation_provider_failure_reports_technical_error(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    _patch_structured_failure(
        monkeypatch, "Echec structured output via Ollama natif: timeout"
    )
    _patch_text_fallback(monkeypatch, RuntimeError("ollama unreachable"))

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        model_name="ornith-1.5:9b",
        retriever=lambda q, k: [_chunk(text="AC losses increase with ripple field.")],
    )

    assert payload["suggestions"] == []
    assert payload["validation_status"] == drafting_citations.VALIDATION_STATUS_ERROR
    assert "structured:" in payload["validation_detail"]
    assert "text:" in payload["validation_detail"]
    assert "Validation impossible" in payload["assistant_text"]


def test_structured_validation_malformed_prose_is_a_technical_error(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    _patch_structured_failure(monkeypatch, "schema mode unavailable")
    # The audit failure mode: 1720 chars of incomplete prose instead of JSON.
    _patch_text_fallback(
        monkeypatch,
        "Let me analyze each candidate carefully against its evidence chunk.\n\n"
        "**cand-0:** This directly supports all claims, this is fully supp",
    )

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        model_name="ornith-1.5:9b",
        retriever=lambda q, k: [_chunk(text="AC losses increase with ripple field.")],
    )

    assert payload["suggestions"] == []
    assert payload["validation_status"] == drafting_citations.VALIDATION_STATUS_ERROR
    assert "Validation impossible" in payload["assistant_text"]


def test_structured_validation_empty_payload_is_not_a_failure(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    _patch_structured_failure(
        monkeypatch,
        "Echec structured output via Ollama natif: all structured fields were empty",
    )

    fallback_calls: list[str] = []

    def unusable_text_fallback(*args, **kwargs):
        fallback_calls.append("called")
        raise ValueError("no JSON")

    from src import llm_provider

    monkeypatch.setattr(llm_provider, "create_chat_llm", unusable_text_fallback)

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        model_name="ornith-1.5:9b",
        retriever=lambda q, k: [_chunk(text="AC losses increase with ripple field.")],
    )

    # An empty schema payload gets one text-fallback attempt; when that is
    # unusable too, the outcome stays "no decisions", not a provider error.
    assert fallback_calls == ["called"]
    assert payload["suggestions"] == []
    assert payload["validation_status"] == drafting_citations.VALIDATION_STATUS_NO_DECISIONS
    assert "aucune décision exploitable" in payload["assistant_text"]


def test_text_fallback_accepts_grounded_decision(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    chunk_text = (
        "AC losses in HTS magnets increase with ripple field amplitude "
        "during dynamic operation."
    )
    _patch_structured_failure(monkeypatch, "provider has no schema mode")

    def invoke_text(llm, prompt):
        candidate = _validation_candidates(prompt)[0]
        return json.dumps(
            {
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "support_status": "SUPPORTED",
                        "confidence": 0.8,
                        "evidence_quote": candidate["original_evidence_chunk"],
                        "support_explanation": "L'extrait soutient la phrase.",
                    }
                ]
            }
        )

    from src import llm_provider

    monkeypatch.setattr(llm_provider, "create_chat_llm", lambda *a, **k: object())
    monkeypatch.setattr(llm_provider, "invoke_text", invoke_text)

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        model_name="mistral",
        retriever=lambda q, k: [_chunk(text=chunk_text)],
    )

    assert len(payload["suggestions"]) == 1
    assert payload["suggestions"][0]["support_status"] == "supported"
    assert payload["validation_status"] == drafting_citations.VALIDATION_STATUS_OK


def test_stream_reports_technical_validation_error(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    _patch_structured_failure(monkeypatch, "schema mode unavailable")
    _patch_text_fallback(monkeypatch, RuntimeError("ollama unreachable"))

    events = list(
        drafting_citations.stream_citations_for_draft(
            "AC losses in HTS magnets increase with ripple field amplitude.",
            model_name="ornith-1.5:9b",
            retriever=lambda q, k: [_chunk(text="AC losses increase with ripple field.")],
        )
    )

    sentence_results = [event for event in events if event.get("type") == "sentence_result"]
    assert sentence_results
    assert sentence_results[0]["validation_status"] == drafting_citations.VALIDATION_STATUS_ERROR
    assert events[-1]["type"] == "done"
    assert events[-1]["validation_status"] == drafting_citations.VALIDATION_STATUS_ERROR
    assert events[-1]["accepted_suggestions"] == 0


def test_validation_prompt_grows_ollama_context_past_default(monkeypatch) -> None:
    """A six-candidate validator prompt must not stay inside Ollama's 4096 default."""

    from src import llm_provider

    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    monkeypatch.setattr(llm_provider, "get_ollama_context_length", lambda: 0)
    monkeypatch.setattr(llm_provider, "_vram_context_cap", lambda: 32768)
    monkeypatch.setattr(
        llm_provider, "_model_advertised_context_length", lambda name=None: 131072
    )

    chunks = [
        _chunk(
            uid=f"chunk-{index}",
            text=(
                "At 60 K, negative feedback control of the pulsed magnetization reduces "
                "the temperature rise by 10 K compared to a passive PFM and traps 2.17 T "
                "in the centre of the bulk sample. "
            )
            * 12,
        )
        for index in range(6)
    ]
    candidates = drafting_citations._collect_candidates_for_sentence(
        sentence=(
            "A 60 K, le controle par retroaction negative de la magnetisation pulsee "
            "d'un bulk GdBaCuO reduit la hausse de temperature de 10 K et piet 2,17 T."
        ),
        sentence_index=0,
        retriever=lambda query, k: chunks,
    )
    assert len(candidates) == 6

    prompt = drafting_citations._build_validation_prompt(candidates)
    hint = drafting_citations._estimate_validation_context_tokens(prompt)
    required = max(
        hint,
        llm_provider._estimate_structured_prompt_tokens(
            [
                {"role": "system", "content": drafting_citations._VALIDATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            drafting_citations.CitationValidationResult,
        ),
    ) + min(llm_provider._ollama_max_output_tokens(), 2048)

    assert hint > llm_provider._AUTO_CONTEXT_FLOOR
    resolved = llm_provider.resolve_ollama_context_length(
        "ornith-1.5:9b", required_tokens=required
    )
    assert resolved >= 8192


def test_audit_harness_hooks_keep_their_signatures(monkeypatch) -> None:
    """The release audit wraps these two readers; their call shape is a contract."""

    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    real_collect = drafting_citations._collect_candidates_for_sentence
    real_validate = drafting_citations._validate_candidates
    calls: dict[str, Any] = {}

    def collecting(*args, **kwargs):
        calls["collect_kwargs"] = kwargs
        result = real_collect(*args, **kwargs)
        calls["candidates"] = [candidate["candidate_id"] for candidate in result]
        return result

    def validating(candidates, model_name, *, validator_fn=None):
        calls["validate"] = {"count": len(candidates), "model": model_name}
        return real_validate(candidates, model_name, validator_fn=validator_fn)

    monkeypatch.setattr(drafting_citations, "_collect_candidates_for_sentence", collecting)
    monkeypatch.setattr(drafting_citations, "_validate_candidates", validating)

    chunk_text = (
        "AC losses in HTS magnets increase with ripple field amplitude "
        "during dynamic operation."
    )

    def accept(candidates, model_name):
        return [
            {
                "candidate_id": candidate["candidate_id"],
                "support_status": "supported",
                "confidence": 0.9,
                "evidence_quote": candidate["evidence_quote"],
                "support_explanation": "L'extrait soutient la phrase.",
            }
            for candidate in candidates
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        model_name="mistral",
        retriever=lambda q, k: [_chunk(text=chunk_text)],
        validator_fn=accept,
    )

    assert len(payload["suggestions"]) == 1
    assert payload["validation_status"] == drafting_citations.VALIDATION_STATUS_OK
    assert calls["collect_kwargs"]["sentence_index"] == 0
    assert calls["collect_kwargs"]["user_hint"] == ""
    assert calls["validate"] == {"count": 1, "model": "mistral"}
    assert len(calls["candidates"]) == 1
    assert payload["suggestions"][0]["evidence_chunk_uid"] == "chunk-1"


def test_citations_reject_fabricated_evidence_quote(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)

    def validate(candidates, model):
        return [{**decision, "evidence_quote": "The first continuous 50 T NMR experiment."}
                for decision in _accept_all(candidates, model)]

    result = drafting_citations.suggest_citations_for_draft(
        "Une capacité expérimentale inédite en RMN à 50 T.",
        retriever=lambda q, k: [_chunk(text="Un dispositif expérimental dédié a été assemblé.")],
        validator_fn=validate,
    )
    assert result["suggestions"] == []


def test_citations_keep_later_chunks_from_same_source_for_validation(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    chunks = [
        _chunk(uid="generic", text="An experimental apparatus was assembled."),
        _chunk(uid="specific", text="NMR spectra were acquired in a continuous magnetic field."),
    ]
    seen = []

    def validate(candidates, model):
        seen.extend(candidates)
        return _accept_all([c for c in candidates if c["evidence_chunk_uid"] == "specific"], model)

    result = drafting_citations.suggest_citations_for_draft(
        "Des spectres RMN ont été acquis en champ continu.",
        retriever=lambda q, k: chunks, validator_fn=validate,
    )
    assert len(seen) == 2
    assert result["suggestions"][0]["evidence_chunk_uid"] == "specific"


def test_citations_count_one_source_once_across_evidence_chunks(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    chunks = [
        _chunk(
            uid="chunk-introduction",
            text="AC losses in HTS magnets increase with ripple field amplitude.",
        ),
        _chunk(
            uid="chunk-results",
            text="Measurements confirm that AC losses increase with ripple field amplitude.",
        ),
    ]
    sentence = "AC losses in HTS magnets increase with ripple field amplitude."

    payload = drafting_citations.suggest_citations_for_draft(
        sentence,
        retriever=lambda query, k: chunks,
        validator_fn=_accept_all,
    )
    events = list(
        drafting_citations.stream_citations_for_draft(
            sentence,
            retriever=lambda query, k: chunks,
            validator_fn=_accept_all,
        )
    )

    assert len(payload["suggestions"]) == 1
    sentence_result = next(event for event in events if event["type"] == "sentence_result")
    assert len(sentence_result["suggestions"]) == 1
    assert sentence_result["accepted_suggestions"] == 1
    assert events[-1]["accepted_suggestions"] == 1
    assert "1 citation(s) validée(s)" in events[-1]["assistant_text"]


def test_citation_retrieval_bypasses_expensive_local_reranking(monkeypatch) -> None:
    from src.rag import query_engine

    monkeypatch.setattr(drafting_citations, "_rag_ready", lambda: True)
    seen = []

    def retrieve(query, **kwargs):
        seen.append(kwargs)
        return []

    monkeypatch.setattr(query_engine, "_retrieve_documents", retrieve)
    drafting_citations.suggest_citations_for_draft("Une expérience de RMN en champ continu.")
    assert seen[0]["use_reranker"] is False
    assert seen[0]["depth"] == "quick"


def test_drafting_citations_uses_original_evidence(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)

    sentence = "AC losses in HTS magnets increase with ripple field amplitude."


    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        return [
            _chunk(
                text=(
                    "AC losses in HTS magnets increase with ripple field amplitude "
                    "during dynamic operation."
                )
            )
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        sentence,
        retriever=retriever,
        validator_fn=_accept_all,
    )

    assert len(payload["suggestions"]) == 1
    suggestion = payload["suggestions"][0]
    assert suggestion["sentence_index"] == 0
    assert suggestion["sentence_text"] == sentence
    assert suggestion["source_pipeline"] == "rag"
    assert suggestion["evidence_chunk_uid"] == "chunk-1"
    assert suggestion["support_status"] == "supported"
    assert suggestion["support_explanation"]


def test_drafting_citations_uses_plain_rag(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)


    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        assert "wiki expansion" not in query
        return [
            _chunk(
                text=(
                    "The screening current induced field is reduced by field shaking "
                    "in no-insulation HTS coils."
                )
            )
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        "Field shaking reduces the screening current induced field in HTS coils.",
        retriever=retriever,
        validator_fn=_accept_all,
    )

    assert len(payload["suggestions"]) == 1
    assert payload["suggestions"][0]["source_pipeline"] == "rag"


def test_drafting_citations_rejects_unsupported_candidates(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)


    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        return [
            _chunk(
                text=(
                    "This paper discusses superconducting magnets and measurement "
                    "apparatus in broad terms."
                )
            )
        ]

    def reject_all(candidates: list[dict[str, Any]], model_name: str | None) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": candidate["candidate_id"],
                "support_status": "too_general",
                "confidence": 0.2,
                "evidence_quote": candidate["evidence_quote"],
                "support_explanation": "Trop general.",
            }
            for candidate in candidates
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        retriever=retriever,
        validator_fn=reject_all,
    )

    assert payload["suggestions"] == []
    assert "rejetées" in payload["assistant_text"]


def test_drafting_citations_honors_excluded_citation_keys(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)


    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        return [
            _chunk(
                text=(
                    "AC losses in HTS magnets increase with ripple field amplitude "
                    "during dynamic operation."
                )
            )
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        retriever=retriever,
        validator_fn=_accept_all,
        exclude_citation_keys=["Alpha2024"],
    )

    assert payload["suggestions"] == []


def test_stream_drafting_citations_remaps_global_sentence_index(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)


    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        return [
            _chunk(
                text=(
                    "AC losses in HTS magnets increase with ripple field amplitude "
                    "during dynamic operation."
                )
            )
        ]

    events = list(
        drafting_citations.stream_citations_for_draft(
            "Bref. AC losses in HTS magnets increase with ripple field amplitude.",
            retriever=retriever,
            validator_fn=_accept_all,
        )
    )

    assert events[0]["type"] == "start"
    assert events[-1]["type"] == "done"
    suggestion_events = [
        event
        for event in events
        if event.get("type") == "sentence_result" and event.get("suggestions")
    ]
    assert len(suggestion_events) == 1
    suggestion = suggestion_events[0]["suggestions"][0]
    assert suggestion["sentence_index"] == 1
    assert suggestion["sentence_text"].startswith("AC losses")


def test_drafting_citations_uses_citation_count_as_small_confidence_boost(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _highly_cited)


    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        return [
            _chunk(
                text=(
                    "AC losses in HTS magnets increase with ripple field amplitude "
                    "during dynamic operation."
                )
            )
        ]

    def borderline_accept(
        candidates: list[dict[str, Any]],
        model_name: str | None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": candidate["candidate_id"],
                "support_status": "supported",
                "confidence": 0.7,
                "evidence_quote": candidate["evidence_quote"],
                "support_explanation": "L'extrait original soutient la phrase.",
            }
            for candidate in candidates
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        retriever=retriever,
        validator_fn=borderline_accept,
    )

    assert len(payload["suggestions"]) == 1
    suggestion = payload["suggestions"][0]
    assert suggestion["citation_count"] == 1200
    assert suggestion["influential_citation_count"] == 80
    assert suggestion["base_confidence"] == 0.7
    assert 0.0 < suggestion["authority_bonus"] <= 0.08
    assert 0.7 < suggestion["confidence"] <= 0.78


def test_drafting_citations_citation_count_cannot_rescue_weak_validation(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _highly_cited)


    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        return [
            _chunk(
                text=(
                    "AC losses in HTS magnets increase with ripple field amplitude "
                    "during dynamic operation."
                )
            )
        ]

    def weak_accept(
        candidates: list[dict[str, Any]],
        model_name: str | None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "candidate_id": candidate["candidate_id"],
                "support_status": "supported",
                "confidence": 0.43,
                "evidence_quote": candidate["evidence_quote"],
                "support_explanation": "Signal trop faible.",
            }
            for candidate in candidates
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        retriever=retriever,
        validator_fn=weak_accept,
    )

    assert payload["suggestions"] == []


def test_drafting_citations_user_hint_enriches_retrieval_query(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)

    seen_queries: list[str] = []


    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        seen_queries.append(query)
        return [
            _chunk(
                text=(
                    "AC losses in HTS magnets increase with ripple field amplitude "
                    "during dynamic operation."
                )
            )
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        retriever=retriever,
        validator_fn=_accept_all,
        user_hint="Smith 2020, screening currents",
    )

    assert payload["suggestions"], "expected at least one suggestion"
    # The hint must reach the retriever query.
    joined = " | ".join(seen_queries)
    assert "Smith 2020" in joined
    assert "screening currents" in joined


@pytest.mark.parametrize("accept", [True, False])
def test_partial_batch_failure_remains_visible(monkeypatch, accept) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    monkeypatch.setattr(drafting_citations, "VALIDATION_BATCH_SIZE", 1)
    chunks = [
        _chunk(uid=f"chunk-{i}", text=f"AC losses increase with ripple field amplitude during operation {i}.")
        for i in range(2)
    ]
    calls = 0

    def validate(candidates, model_name):
        nonlocal calls
        calls += 1
        if calls % 2 == 0:
            raise RuntimeError("test validator unavailable")
        decisions = _accept_all(candidates, model_name)
        if not accept:
            for decision in decisions:
                decision["support_status"] = "unsupported"
        return decisions

    events = list(drafting_citations.stream_citations_for_draft(
        "AC losses increase with ripple field amplitude.",
        retriever=lambda query, k: chunks,
        validator_fn=validate,
    ))
    result = next(event for event in events if event["type"] == "sentence_result")
    assert calls == 2
    assert len(result["suggestions"]) == int(accept)
    assert result["validation_status"] == drafting_citations.VALIDATION_STATUS_ERROR
    assert "unavailable" in result["validation_detail"]
    assert events[-1]["validation_status"] == drafting_citations.VALIDATION_STATUS_ERROR
    assert "incomplète" in events[-1]["assistant_text"]


def test_drafting_citations_empty_hint_keeps_original_query(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)

    seen_queries: list[str] = []


    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        seen_queries.append(query)
        return [
            _chunk(
                text=(
                    "AC losses in HTS magnets increase with ripple field amplitude "
                    "during dynamic operation."
                )
            )
        ]

    sentence = "AC losses in HTS magnets increase with ripple field amplitude."
    drafting_citations.suggest_citations_for_draft(
        sentence,
        retriever=retriever,
        validator_fn=_accept_all,
        user_hint="   ",
    )

    assert seen_queries
    for query in seen_queries:
        assert query.strip() == sentence.strip() or sentence in query


_LATEX_CHUNK = (
    "Les REBCO sont une famille de c´ eramiques supraconductrices ` a haute temp´ erature "
    "cri- tique dont les membres poss´ edent des compositions chimiques similaires "
    ":REBa 2Cu3O7−x. The phenomenon has problematic consequences on the ﬁeld generated "
    "by magnets designed with such tapes. One can isolate three main eﬀects."
)


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        # The validator "repairs" LaTeX accents, hyphenation and subscripts.
        (
            "Les REBCO sont une famille de céramiques supraconductrices à haute température "
            "critique dont les membres possèdent des compositions chimiques similaires : REBa2Cu3O7",
            True,
        ),
        # Ellipsis-joined excerpts must each come from the chunk, in order.
        ("The phenomenon has problematic consequences on the field generated by magnets... "
         "One can isolate three main effects", True),
        ("One can isolate three main effects ... The phenomenon has problematic consequences", False),
        # Near-verbatim is tolerated, a meaning change is not.
        ("The phenomenon has consequences on the field generated by magnets designed with such tapes", True),
        ("The phenomenon has no consequences on the field generated by magnets designed with such tapes", False),
        ("One can isolate four main effects", False),
        ("Screening currents reduce the central field by more than 50 % in all magnets", False),
        ("magnets ... tapes", False),
        ("", False),
    ],
)
def test_quote_check_tolerates_extraction_artifacts_not_meaning_changes(quote, expected) -> None:
    assert drafting_citations._quote_matches_evidence(quote, _LATEX_CHUNK) is expected


def test_repaired_latex_quote_becomes_a_suggestion(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)

    def accept(candidates, model_name):
        return [
            {
                "candidate_id": candidate["candidate_id"],
                "support_status": "supported",
                "confidence": 0.9,
                "evidence_quote": "Les REBCO sont une famille de céramiques supraconductrices "
                "à haute température critique",
                "support_explanation": "Définition explicite.",
            }
            for candidate in candidates
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        "Les REBCO sont des céramiques supraconductrices à haute température critique.",
        retriever=lambda q, k: [_chunk(text=_LATEX_CHUNK)],
        validator_fn=accept,
    )

    assert len(payload["suggestions"]) == 1
    assert "céramiques" in payload["suggestions"][0]["evidence_quote"]


def test_drafting_rag_loads_the_index_on_first_use(monkeypatch) -> None:
    from src.rag import native_chat

    calls = []
    state = {"ready": False}
    monkeypatch.setattr(drafting_citations, "_rag_ready", lambda: state["ready"])

    def fake_ensure(model_name, threshold):
        calls.append(model_name)
        state["ready"] = True

    monkeypatch.setattr(native_chat, "ensure_rag_ready", fake_ensure)
    assert drafting_citations.ensure_drafting_rag("ornith-1.5:9b") == ""
    assert calls == ["ornith-1.5:9b"]


def test_drafting_rag_reports_a_blocked_dll_instead_of_a_missing_index(monkeypatch) -> None:
    from src.rag import native_chat

    monkeypatch.setattr(drafting_citations, "_rag_ready", lambda: False)

    def blocked(model_name, threshold):
        raise OSError(r'[WinError 4551] application control policy blocked "torch\lib\c10.dll"')

    monkeypatch.setattr(native_chat, "ensure_rag_ready", blocked)
    message = drafting_citations.ensure_drafting_rag("ornith-1.5:9b")
    assert "torch" in message
    assert "inutile de le reconstruire" in message


def test_candidate_slots_go_to_several_papers_before_repeats(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)

    def chunk(uid: str, paper: str, score: float) -> EvidenceChunk:
        item = _chunk(uid=uid, text=f"AC losses evidence {uid} for ripple field amplitude.")
        item.paper_id = paper
        item.score = score
        return item

    retrieved = [chunk(f"a{i}", "A", 1.0 - i / 100) for i in range(5)] + [
        chunk("b1", "B", 0.5),
        chunk("c1", "C", 0.4),
    ]
    candidates = drafting_citations._collect_candidates_for_sentence(
        sentence="AC losses increase with ripple field amplitude in HTS magnets.",
        sentence_index=0,
        retriever=lambda q, k: retrieved,
    )

    assert [c["evidence_chunk_uid"] for c in candidates] == ["a0", "a1", "b1", "c1", "a2", "a3"]


def test_library_retrieval_pool_is_reranked_before_the_validation_slots(monkeypatch) -> None:
    from src.rag import reranker

    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    seen_k: list[int] = []

    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        seen_k.append(k)
        chunks = []
        for index in range(10):
            item = _chunk(uid=f"c{index}", text=f"Passage {index} about AC losses and ripple fields.")
            item.paper_id = f"P{index}"
            item.score = 1.0 - index / 100
            chunks.append(item)
        return chunks

    # The cross-encoder prefers the last retrieved passages.
    monkeypatch.setattr(
        reranker, "rerank_texts",
        lambda query, texts, top_k=None: [(i, i / 10) for i in reversed(range(len(texts)))],
    )
    candidates = drafting_citations._collect_candidates_for_sentence(
        sentence="AC losses increase with ripple field amplitude in HTS magnets.",
        sentence_index=0, retriever=retriever, rerank=True,
    )

    assert seen_k == [drafting_citations._RERANK_POOL_SIZE]
    assert [c["evidence_chunk_uid"] for c in candidates] == ["c9", "c8", "c7", "c6", "c5", "c4"]


def test_candidates_far_below_the_best_rerank_score_skip_validation(monkeypatch) -> None:
    from src.rag import reranker

    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    chunks = []
    for index in range(4):
        item = _chunk(uid=f"c{index}", text=f"Passage {index} about AC losses and ripple fields.")
        item.paper_id = f"P{index}"
        chunks.append(item)
    scores = [9.0, 8.2, 7.6, 3.0]
    monkeypatch.setattr(
        reranker, "rerank_texts",
        lambda query, texts, top_k=None: [(i, scores[i]) for i in range(len(texts))],
    )

    candidates = drafting_citations._collect_candidates_for_sentence(
        sentence="AC losses increase with ripple field amplitude in HTS magnets.",
        sentence_index=0, retriever=lambda q, k: chunks, rerank=True,
    )

    assert [c["evidence_chunk_uid"] for c in candidates] == ["c0", "c1", "c2"]


def test_disabled_reranker_keeps_retrieval_order(monkeypatch) -> None:
    from src.rag import reranker

    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    monkeypatch.setattr(
        reranker, "rerank_texts",
        lambda query, texts, top_k=None: [(i, 0.0) for i in range(len(texts))],
    )
    chunks = [_chunk(uid=f"c{i}", text=f"Passage {i} on AC losses.") for i in range(3)]
    for index, item in enumerate(chunks):
        item.paper_id = f"P{index}"
        item.score = 1.0 - index / 10

    assert drafting_citations._rerank_pool("AC losses", list(reversed(chunks))) == (chunks, {})


def test_cited_markers_mark_their_sentence_including_after_the_full_stop() -> None:
    marker = drafting_citations.CITED_MARKER
    sentences = drafting_citations._split_into_sentences(
        f"AC losses rise with ripple amplitude {marker}. Coupling currents flow in the stabilizer. "
        f"Quench detection needs a low threshold. {marker} Delays can reach seconds."
    )

    clean, cited = drafting_citations._mark_cited_sentences(sentences)

    assert clean == [
        "AC losses rise with ripple amplitude.",
        "Coupling currents flow in the stabilizer.",
        "Quench detection needs a low threshold.",
        "Delays can reach seconds.",
    ]
    assert cited == {0, 2}


def test_stream_skips_cited_sentences_and_prefetches_the_others(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    marker = drafting_citations.CITED_MARKER
    queried: list[str] = []

    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        queried.append(query)
        return [_chunk(text=f"Evidence that {query}")]

    def reject(candidates, model_name):
        return []

    events = list(drafting_citations.stream_citations_for_draft(
        f"AC losses rise with ripple field amplitude {marker}. "
        "Coupling currents flow through the copper stabilizer layer. "
        "Quench detection thresholds must stay below a few millivolts.",
        retriever=retriever,
        validator_fn=reject,
    ))

    results = [event for event in events if event["type"] == "sentence_result"]
    assert [bool(event.get("skipped_cited")) for event in results] == [True, False, False]
    assert "déjà citée" in results[0]["assistant_text"]
    # The cited sentence never reaches retrieval; the markers never leak.
    assert queried == [
        "Coupling currents flow through the copper stabilizer layer.",
        "Quench detection thresholds must stay below a few millivolts.",
    ]
    assert events[-1]["processed_sentences"] == 3


def test_validator_sees_two_sentences_before_and_one_after_cited_ones_included(
    monkeypatch,
) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    marker = drafting_citations.CITED_MARKER
    seen: list[dict[str, Any]] = []

    def validate(candidates, model_name):
        seen.extend(candidates)
        return []

    list(drafting_citations.stream_citations_for_draft(
        f"REBCO tapes carry large screening currents {marker}. "
        "Screening currents distort the central magnetic field. "
        "Field errors reach several hundred parts per million. "
        "Shimming coils compensate part of the field error.",
        retriever=lambda query, k: [_chunk(text="Screening currents distort the field.")],
        validator_fn=validate,
    ))

    by_sentence = {candidate["sentence_text"]: candidate for candidate in seen}
    third = by_sentence["Field errors reach several hundred parts per million."]
    assert third["context_before"] == (
        "REBCO tapes carry large screening currents. "
        "Screening currents distort the central magnetic field."
    )
    assert third["context_after"] == "Shimming coils compensate part of the field error."
    assert by_sentence["Shimming coils compensate part of the field error."]["context_after"] == ""


def test_selection_context_reaches_its_sentences_and_anaphoric_retrieval(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    queries: list[str] = []
    seen: list[dict[str, Any]] = []

    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        queries.append(query)
        return [_chunk(text="Screening currents distort the field.")]

    def validate(candidates, model_name):
        seen.extend(candidates)
        return []

    list(drafting_citations.stream_citations_for_draft(
        "These currents distort the central magnetic field.",
        retriever=retriever,
        validator_fn=validate,
        context_before=(
            "Far away opening sentence. Introduction of the coil design. "
            "REBCO tapes carry large screening currents."
        ),
        context_after="Shimming coils compensate part of the error. Unrelated closing sentence.",
    ))

    assert seen[0]["context_before"] == (
        "Introduction of the coil design. REBCO tapes carry large screening currents."
    )
    assert seen[0]["context_after"] == "Shimming coils compensate part of the error."
    # "These currents" is searched with the sentence naming them.
    assert queries == [
        "REBCO tapes carry large screening currents. "
        "These currents distort the central magnetic field.",
    ]


def test_anaphoric_sentence_is_reranked_on_its_own_claim(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    calls: dict[str, str] = {}

    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        calls["retrieval"] = query
        return [_chunk(uid=f"chunk-{index}", text=f"Evidence {index}.") for index in range(3)]

    def rerank(query: str, chunks: list[EvidenceChunk]):
        calls["rerank"] = query
        return chunks, {}

    monkeypatch.setattr(drafting_citations, "_rerank_pool", rerank)
    drafting_citations._collect_candidates_for_sentence(
        sentence="It completely eliminates screening currents in these coils.",
        sentence_index=0,
        retriever=retriever,
        rerank=True,
        context_before="No-insulation winding has been proposed to protect REBCO coils.",
    )

    assert calls["retrieval"].startswith("No-insulation winding has been proposed")
    # Ranked on the context too, passages supporting only the previous
    # sentence ("protects the coil") were validated for this one.
    assert calls["rerank"] == "It completely eliminates screening currents in these coils."


def test_self_contained_sentence_is_searched_alone(monkeypatch) -> None:
    queries: list[str] = []

    def retriever(query: str, k: int) -> list[EvidenceChunk]:
        queries.append(query)
        return []

    drafting_citations._collect_candidates_for_sentence(
        sentence="Screening currents distort the central magnetic field.",
        sentence_index=0,
        retriever=retriever,
        user_hint="Smith 2020",
        context_before="REBCO tapes carry large screening currents.",
    )

    assert queries == ["Screening currents distort the central magnetic field. Smith 2020"]


def test_validation_prompt_frames_each_sentence_with_its_context() -> None:
    candidate = {
        "candidate_id": "cand-0",
        "sentence_index": 0,
        "sentence_text": "These currents distort the central magnetic field.",
        "context_before": "REBCO tapes carry large screening currents. They decay slowly.",
        "context_after": "",
        "chunk_text": "Screening currents distort the field.",
    }

    prompt = drafting_citations._build_validation_prompt([candidate])

    assert (
        '"draft_context": "REBCO tapes carry large screening currents. They decay slowly. '
        'These currents distort the central magnetic field."'
    ) in prompt


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("This winding technique offers a higher current density.", True),
        ("In this magnet, a shield reduces the AC losses.", True),
        ("Ces courants dégradent l'homogénéité du champ.", True),
        ("L'effet de ces courants reste mal connu.", True),
        ("Elles permettent des champs plus élevés.", True),
        ("The latter reaches 32 T in a 50 mm bore.", True),
        ("REBCO tapes carry large screening currents.", False),
        ("Cependant, les rubans REBCO restent coûteux.", False),
        ("Thermal runaway starts at a critical current defect.", False),
    ],
)
def test_anaphoric_openings_are_detected(sentence: str, expected: bool) -> None:
    assert drafting_citations._is_anaphoric(sentence) is expected


def test_citation_bonus_cannot_lift_a_partial_verdict_over_the_threshold(monkeypatch) -> None:
    monkeypatch.setattr(drafting_citations, "get_item_citations", _highly_cited)
    text = "AC losses in HTS magnets increase with ripple field amplitude during operation."

    def partial(candidates, model_name):
        return [
            {
                "candidate_id": candidate["candidate_id"],
                "support_status": "supported",
                "confidence": 0.62,
                "evidence_quote": candidate["evidence_quote"],
                "support_explanation": "Partiel.",
            }
            for candidate in candidates
        ]

    payload = drafting_citations.suggest_citations_for_draft(
        "AC losses in HTS magnets increase with ripple field amplitude.",
        retriever=lambda q, k: [_chunk(text=text)],
        validator_fn=partial,
    )

    assert payload["suggestions"] == []


@pytest.mark.parametrize(
    ("sentence", "missing"),
    [
        ("REBCO tape will cost less than 5 $/kA·m by 2030.", ["5", "2030"]),
        ("A trapped field of 17.6 T was reached in 2014.", []),
        ("REBa2Cu3O7 tapes and Nb3Sn coils in 2D models.", []),
        ("Dipoles are limited to 14–16 T.", ["14", "16"]),
    ],
)
def test_figures_absent_from_the_source_are_detected(sentence: str, missing: list[str]) -> None:
    candidate = {"chunk_text": "A trapped field of 17,6 T was reached.", "year": "2014"}
    assert drafting_citations._missing_numbers(sentence, candidate) == missing


def test_a_significant_figure_stated_elsewhere_in_the_paper_counts() -> None:
    candidate = {
        "chunk_text": "A conducting shield reduces the AC losses in the superconducting coil.",
        "paper_numbers": ["43", "2", "8.5"],
    }
    assert drafting_citations._missing_numbers("In the 43 T LNCMI hybrid magnet, a shield helps.", candidate) == []
    # Small figures are too common to be vouched for by another passage.
    assert drafting_citations._missing_numbers("It lowers losses by 2 orders.", candidate) == ["2"]


def test_remote_stream_validates_sentences_in_parallel_and_emits_each_source_once(
    monkeypatch,
) -> None:
    import threading

    monkeypatch.setattr(drafting_citations, "get_item_citations", _citations)
    monkeypatch.setattr(drafting_citations, "_validation_concurrency", lambda: 3)
    # Fails unless the three sentences are being validated at the same time.
    all_in_flight = threading.Barrier(3, timeout=5)

    def accept_together(candidates, model_name):
        all_in_flight.wait()
        return _accept_all(candidates, model_name)

    sentences = [
        "AC losses in HTS magnets increase with ripple field amplitude.",
        "Coupling currents in HTS magnets increase with ripple field amplitude.",
        "Hysteresis losses in HTS magnets increase with ripple field amplitude.",
    ]
    events = list(drafting_citations.stream_citations_for_draft(
        " ".join(sentences),
        retriever=lambda q, k: [_chunk(uid=f"chunk-{q[:4]}", text=q)],
        validator_fn=accept_together,
    ))

    results = [event for event in events if event["type"] == "sentence_result"]
    assert [event["sentence_index"] for event in results] == [0, 1, 2]
    # The three sentences cite the same paper: only the first keeps it.
    assert [len(event["suggestions"]) for event in results] == [1, 0, 0]
    assert events[-1]["accepted_suggestions"] == 1
