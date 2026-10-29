"""Answer length setting and neighbor passages of the chat."""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture
def qe(monkeypatch: pytest.MonkeyPatch):
    from src.rag import query_engine, state

    with state._state_lock:
        state.document_chain = None
        state.vectorstore = None
        state.last_sources = []
    monkeypatch.setattr(query_engine, "_current_ui_language", lambda: "fr")
    yield query_engine
    with state._state_lock:
        state.document_chain = None
        state.vectorstore = None
        state.last_sources = []


def _user_prompt(qe, question: str, length: str | None, model_name: str = "demo") -> str:
    doc = qe.Document(page_content="Un passage.", metadata={"source": "a.pdf"})
    messages = qe.build_chat_messages(
        question, [doc], [doc], model_name=model_name, answer_length=length
    )
    return messages[-1]["content"]


@pytest.mark.parametrize(
    ("length", "present", "absent"),
    [
        ("concise", "1 ou 2 courts paragraphes", "2 a 4 paragraphes"),
        ("standard", "2 a 4 paragraphes", "courts paragraphes"),
        ("detailed", "4 a 7 paragraphes", "courts paragraphes"),
    ],
)
def test_prompt_follows_answer_length(qe, length: str, present: str, absent: str) -> None:
    prompt = _user_prompt(qe, "Quels sont les effets du dopage ?", length)

    assert present in prompt
    assert absent not in prompt


def test_length_rules_sit_before_the_style_guide(qe) -> None:
    prompt = _user_prompt(qe, "Quels sont les effets du dopage ?", "detailed")

    assert prompt.index("Longueur de la reponse:") < prompt.index("Style de reponse a respecter:")
    guide = qe._build_response_style_guide("Quels sont les effets du dopage ?", model_name="demo")
    assert "paragraphes" not in guide


def test_prompt_reads_length_from_settings(qe, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "get_app_settings", lambda: {"chat_answer_length": "detailed"})

    assert "4 a 7 paragraphes" in _user_prompt(qe, "Quels sont les effets du dopage ?", None)


def test_unknown_length_falls_back_to_auto(qe, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "get_app_settings", lambda: {"chat_answer_length": "huge"})

    assert qe._chat_answer_length() == "auto"


def test_auto_is_the_default_setting() -> None:
    from src import app_settings

    assert app_settings._DEFAULT_CHAT_ANSWER_LENGTH == "auto"
    assert app_settings._normalize_chat_answer_length(None) == "auto"
    assert app_settings._normalize_chat_answer_length("concise") == "concise"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Quelle est la formule chimique des REBCO ?", "concise"),
        ("Qu'est-ce que le flux creep ?", "concise"),
        ("C'est quoi un ruban 2G ?", "concise"),
        ("Quelle valeur de Jc donne Bean ?", "concise"),
        ("Qui a proposé le modèle de Kim ?", "concise"),
        ("Quand la formulation T-A a-t-elle été publiée ?", "concise"),
        ("Je cherche si une formulation A-V a été utilisée pour une racetrack.", "concise"),
        ("Existe-t-il une mesure à 77 K ?", "concise"),
        ("What is the critical temperature of YBCO?", "concise"),
        ("How many tapes does the cable use?", "concise"),
        ("Is there a measurement at 20 K?", "concise"),
        ("Pourquoi le bobinage sans isolation retarde-t-il la charge ?", "detailed"),
        ("Compare la formulation H et la formulation T-A.", "detailed"),
        ("Explique le modèle de l'état critique.", "detailed"),
        ("Comment fonctionne un limiteur de courant ?", "detailed"),
        ("Résume les résultats de cet article.", "detailed"),
        ("Why do striated tapes reduce AC loss?", "detailed"),
        ("How does the T-A formulation work?", "detailed"),
        ("Quels sont les effets du dopage ?", "standard"),
        ("Quels sont les principaux thèmes de ma bibliothèque ?", "standard"),
        ("Pertes AC dans les câbles Roebel", "standard"),
    ],
)
def test_classifier_picks_a_length(question: str, expected: str) -> None:
    from src.chat_intents import classify_answer_length

    assert classify_answer_length(question) == expected


def test_auto_resolves_from_the_question_and_explicit_settings_force(qe) -> None:
    question = "Quelle est la formule chimique des REBCO ?"

    assert qe.resolve_answer_length(question, "auto") == "concise"
    assert qe.resolve_answer_length("Pourquoi la charge est-elle lente ?", "auto") == "detailed"
    assert qe.resolve_answer_length(question, "detailed") == "detailed"
    assert "1 ou 2 courts paragraphes" in _user_prompt(qe, question, "auto")


@pytest.mark.parametrize(
    "question",
    [
        "Liste les méthodes de modélisation des pertes AC.",
        "Quels sont les principaux thèmes de ma bibliothèque ?",
        "Regroupe ces articles par sujet",
    ],
)
@pytest.mark.parametrize("length", ["concise", "standard", "detailed"])
def test_lists_are_never_capped(qe, question: str, length: str) -> None:
    from src.chat_intents import is_enumeration_request

    assert is_enumeration_request(question)
    prompt = _user_prompt(qe, question, length)

    assert "Liste chaque element pertinent" in prompt
    assert "ne t'arrete pas a 3 ou 4 elements" in prompt
    assert "liste a puces, une puce par element" in prompt
    assert "en texte naturel" not in prompt
    assert "paragraphes" not in prompt.split("Style de reponse a respecter:")[0]
    assert qe._answer_output_token_cap(question, length) is None


def test_list_depth_follows_the_length(qe) -> None:
    question = "Liste les méthodes de modélisation des pertes AC."

    assert "une phrase courte par element" in _user_prompt(qe, question, "concise")
    assert "une ou deux phrases par element" in _user_prompt(qe, question, "standard")
    assert "deux ou trois phrases par element" in _user_prompt(qe, question, "detailed")


def test_english_list_rule(qe, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qe, "_current_ui_language", lambda: "en")

    prompt = _user_prompt(qe, "List the AC loss models used in my papers.", "standard")

    assert "List every relevant item found in the sources" in prompt
    assert "do not stop at 3 or 4 items" in prompt


def test_word_table_does_not_make_a_list() -> None:
    from src.chat_intents import is_enumeration_request

    assert not is_enumeration_request("Le courant est-il stable à 77 K ?")


def test_overview_task_lists_every_theme(qe) -> None:
    task = qe._library_overview_task("Quels sont les principaux thèmes de ma bibliothèque ?", [])

    assert "tous les themes de recherche distincts" in task
    assert "2 a 4" not in task


@pytest.mark.parametrize("model_name", ["qwen3.5:2b", "qwen3.5:9b"])
@pytest.mark.parametrize(
    ("question", "length", "expected"),
    [
        ("Quels sont les effets du dopage ?", "concise", "1 ou 2 courts paragraphes"),
        ("Quels sont les effets du dopage ?", "detailed", "4 a 7 paragraphes"),
        ("Liste les méthodes de modélisation des pertes AC.", "standard", "ne t'arrete pas a 3 ou 4"),
    ],
)
def test_length_survives_the_qwen35_compact_prompt(
    qe, model_name: str, question: str, length: str, expected: str
) -> None:
    from src.llm_provider import _prepare_ollama_chat_messages_for_thinking

    doc = qe.Document(page_content="Un passage.", metadata={"source": "a.pdf"})
    messages = qe.build_chat_messages(
        question, [doc], [doc], model_name=model_name, answer_length=length
    )
    sent = _prepare_ollama_chat_messages_for_thinking(model_name, messages, False)
    user = sent[-1]["content"]

    assert "Style de reponse a respecter:" not in user
    assert "Longueur de la reponse:" in user
    assert expected in user


def test_concise_prose_caps_the_output(qe) -> None:
    assert qe._answer_output_token_cap("Quelle est la formule des REBCO ?", "concise") == 450
    assert qe._answer_output_token_cap("Quelle est la formule des REBCO ?", "standard") is None
    assert qe._answer_output_token_cap("Pourquoi la charge est lente ?", "detailed") is None


class _FakeVectorStore:
    def __init__(self, rows: list[tuple[str, dict[str, Any]]]):
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def get(self, *, where: dict[str, Any], include: list[str]):
        self.calls.append(where)
        rel_path = where["$and"][0]["rel_path"]
        wanted = set(where["$and"][1]["chunk_id"]["$in"])
        hits = [
            (text, meta)
            for text, meta in self.rows
            if meta.get("rel_path") == rel_path and meta.get("chunk_id") in wanted
        ]
        return {"documents": [t for t, _ in hits], "metadatas": [m for _, m in hits]}


def _meta(chunk_id: int, content_type: str = "pdf", rel_path: str = "a.pdf") -> dict[str, Any]:
    return {"rel_path": rel_path, "chunk_id": chunk_id, "content_type": content_type, "page": 2}


def test_neighbor_passages_extend_the_best_passages(qe) -> None:
    from src.rag import state

    store = _FakeVectorStore(
        [
            ("Before text that ends with the shared overlap of chunks", _meta(4)),
            ("Next passage with the result 42 K.", _meta(6)),
            ("A note with the same number.", _meta(6, content_type="zotero_note")),
        ]
    )
    with state._state_lock:
        state.vectorstore = store
    hit = qe.Document(page_content="the shared overlap of chunks and the method.", metadata=_meta(5))
    other = qe.Document(page_content="Another paper.", metadata=_meta(1, rel_path="b.pdf"))

    expanded = qe._expand_with_neighbor_passages([hit, other], 1)

    assert len(expanded) == 2
    assert expanded[1] is other
    text = expanded[0].page_content
    assert text.startswith("Before text")
    assert text.count("the shared overlap") == 1
    assert "result 42 K" in text
    assert "A note" not in text
    assert expanded[0].metadata["page"] == 2
    assert expanded[0].metadata["neighbor_expanded"] is True
    assert len(store.calls) == 1


def test_neighbor_already_retrieved_is_not_repeated(qe) -> None:
    from src.rag import state

    with state._state_lock:
        state.vectorstore = _FakeVectorStore([("Second hit text.", _meta(6))])
    first = qe.Document(page_content="First hit text.", metadata=_meta(5))
    second = qe.Document(page_content="Second hit text.", metadata=_meta(6))

    expanded = qe._expand_with_neighbor_passages([first, second], 2)

    assert expanded[0].page_content == "First hit text."


def test_neighbor_expansion_survives_store_errors(qe) -> None:
    from src.rag import state

    class _Broken:
        def get(self, **kwargs):
            raise RuntimeError("chroma down")

    with state._state_lock:
        state.vectorstore = _Broken()
    doc = qe.Document(page_content="Text.", metadata=_meta(5))

    assert qe._expand_with_neighbor_passages([doc], 2) == [doc]


def test_concise_does_not_expand(qe) -> None:
    doc = qe.Document(page_content="Text.", metadata=_meta(5))

    assert qe._expand_with_neighbor_passages([doc], qe._NEIGHBOR_EXPANDED_DOCS["concise"]) == [doc]


def test_detailed_length_asks_for_more_passages(qe, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()
    monkeypatch.setattr(state, "_max_chunks_default", 8)
    monkeypatch.setattr(state, "_CHAT_MAX_CHUNKS_CAP", 12)
    monkeypatch.setattr(qe, "_chat_answer_length", lambda: "detailed")
    monkeypatch.setattr(qe, "_stream_reranker_warmup_status", lambda *a, **k: iter(()))
    captured: dict[str, Any] = {}

    def _fake_retrieve(query, threshold, max_chunks, filters, **kwargs):
        captured["max_chunks"] = max_chunks
        return []

    monkeypatch.setattr(qe, "_retrieve_documents", _fake_retrieve)

    list(qe.stream_chat_api(message="Quels effets ?", model_name="dummy", threshold=0.3))

    assert captured["max_chunks"] == 10


def test_list_reads_as_many_passages_as_allowed(qe, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.rag import state

    with state._state_lock:
        state.document_chain = object()
        state.vectorstore = object()
    monkeypatch.setattr(state, "_max_chunks_default", 6)
    monkeypatch.setattr(state, "_CHAT_MAX_CHUNKS_CAP", 12)
    monkeypatch.setattr(qe, "_chat_answer_length", lambda: "concise")
    monkeypatch.setattr(qe, "_stream_reranker_warmup_status", lambda *a, **k: iter(()))
    captured: dict[str, Any] = {}

    def _fake_retrieve(query, threshold, max_chunks, filters, **kwargs):
        captured["max_chunks"] = max_chunks
        return []

    monkeypatch.setattr(qe, "_retrieve_documents", _fake_retrieve)

    list(qe.stream_chat_api(message="Liste les modèles de pertes AC", model_name="d", threshold=0.3))

    assert captured["max_chunks"] == 12
