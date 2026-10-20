import re

import pytest

from src import chat_origin
from src.chat_harness import plan_chat_tools
from src.chat_intents import detect_chat_intent
from src.origin_tracing import FOR_ANOTHER_POINT, FOR_THIS_IDEA


@pytest.mark.parametrize(
    "message",
    [
        "/origine le modèle de l'état critique",
        "/origin the critical state model",
        "Est-ce que cet article est le papier original sur l'état critique ?",
        "Quel est l'article d'origine du modèle de Bean ?",
        "Qui a introduit le modèle de l'état critique ?",
        "Who first proposed the critical state model?",
        "Which paper first introduced flux pumps?",
    ],
)
def test_origin_requests_route_to_the_origin_tool(message):
    intent = detect_chat_intent(message)
    assert intent.mode == "origin_trace"
    assert plan_chat_tools(intent).primary_tool_name == "research.origin_trace"


def test_origin_command_keeps_the_idea_as_query():
    intent = detect_chat_intent("/origine le modèle de l'état critique")
    assert intent.explicit and intent.query == "le modèle de l'état critique"


@pytest.mark.parametrize(
    "message",
    [
        "Comment fonctionne le modèle de l'état critique ?",
        "Vérifie cette affirmation contre les sources.",
        "Quelle est l'origine des pertes AC ?",
    ],
)
def test_other_questions_do_not_route_to_origin(message):
    assert detect_chat_intent(message).mode != "origin_trace"


def _ref(number, text, rel_path="", label="", year=""):
    library = {"rel_path": rel_path, "label": label, "year": year} if rel_path else None
    return {"number": number, "text": text, "library": library}


def _result(verdict, citations, page="Page 2", model_reliability="normal", self_claim=False):
    return {
        "verdict": verdict,
        "citations": citations,
        "passage": {"text": "Passage text with [3] inside.", "page": page},
        "model_reliability": model_reliability,
        "self_claim": self_claim,
        "notes": [],
    }


_TRACES = {
    "A/Citing 2019.pdf": _result(
        "attributes_elsewhere",
        [
            {"markers": ["[3]"], "attribution": FOR_THIS_IDEA,
             "references": [_ref(3, "[3] B. Varnel, J. Fict. 9 (1968) 1308.", "B/Varnel 1968.pdf", "Varnel (1968)", "1968")]},
            {"markers": ["[7]"], "attribution": FOR_ANOTHER_POINT,
             "references": [_ref(7, "[7] Z. Other, J. Fict. 2 (2001) 1.", "Z/Other.pdf", "Other (2001)", "2001")]},
        ],
    ),
    "B/Varnel 1968.pdf": _result(
        "attributes_elsewhere",
        [
            {"markers": ["[1]"], "attribution": FOR_THIS_IDEA,
             "references": [_ref(1, "[1] C. Quillon, Fict. Phys. Rev. 36 (1962) 31.")]},
        ],
    ),
}


@pytest.fixture
def chain(monkeypatch):
    calls = []

    def fake_trace(claim, rel_path, *, model_name=None, evidence_quote=""):
        calls.append(rel_path)
        return _TRACES[rel_path]

    monkeypatch.setattr(chat_origin, "trace_origin", fake_trace)
    monkeypatch.setattr(chat_origin, "library_record", lambda rel_path: {
        "A/Citing 2019.pdf": {"label": "Citing (2019)", "title": "Citing", "year": "2019"},
        "B/Varnel 1968.pdf": {"label": "Varnel (1968)", "title": "Strips", "year": "1968"},
    }.get(rel_path))
    monkeypatch.setattr(chat_origin, "_lang", lambda: "fr")
    return calls


def _run(**kwargs):
    events = list(chat_origin.stream_chat_origin("Les bandes minces.", "qwen3.5:4b", 0.3, **kwargs))
    text = "".join(event["text"] for event in events if event["type"] == "token")
    sources = next(event for event in events if event["type"] == "sources")
    return events, text, sources


def test_chain_follows_credited_library_sources(chain, monkeypatch):
    monkeypatch.setattr(chat_origin, "_starting_articles", lambda *args, **kwargs: (["A/Citing 2019.pdf"], ""))
    events, text, sources = _run()
    # Only the credited library source is followed, not the one cited for another point.
    assert chain == ["A/Citing 2019.pdf", "B/Varnel 1968.pdf"]
    assert [event["msg"] for event in events if event["type"] == "status"][1:] == [
        "Lecture de Citing (2019) et de sa bibliographie…",
        "Lecture de Varnel (1968) et de sa bibliographie…",
    ]
    assert sources["answer_origin"] == "origin_trace"
    assert [source["ref_label"] for source in sources["data"]] == ["S1", "S2"]
    assert sources["data"][1]["file"] == "B/Varnel 1968.pdf"
    assert "Plus ancienne source créditée trouvée : C. Quillon, Fict. Phys. Rev. 36 (1962) 31. (réf. 1), hors bibliothèque" in text
    assert "- **Citing (2019)** [S1], Page 2 : attribue l'idée à Varnel (1968) [S2] (réf. 3)" in text
    assert "- ↳ **Varnel (1968)** [S2]" in text
    # Article reference numbers never appear in brackets: the chat would read them as its sources.
    assert set(re.findall(r"\[[^\]]+\]", text)) == {"[S1]", "[S2]"}
    assert "ajoutez-la à Zotero" in text


def test_attached_article_skips_library_search(chain, monkeypatch):
    monkeypatch.setattr(chat_origin, "_starting_articles", lambda *args, **kwargs: pytest.fail("no search"))
    _events, _text, sources = _run(article_paths=["B/Varnel 1968.pdf"])
    assert chain == ["B/Varnel 1968.pdf"]
    assert len(sources["data"]) == 1


def test_no_index_and_empty_claim(monkeypatch):
    monkeypatch.setattr(chat_origin, "_lang", lambda: "fr")
    monkeypatch.setattr(chat_origin, "_starting_articles", lambda *args, **kwargs: ([], "Index RAG indisponible."))
    _events, text, sources = _run()
    assert text.startswith("Je ne peux pas chercher dans la bibliothèque") and sources["data"] == []

    events = list(chat_origin.stream_chat_origin("  ", None, 0.3))
    assert "Indiquez l'idée" in "".join(event.get("text", "") for event in events)


def test_chain_stops_at_the_trace_budget(chain, monkeypatch):
    def endless(claim, rel_path, *, model_name=None, evidence_quote=""):
        chain.append(rel_path)
        depth = int(rel_path.split("/")[0])
        nxt = f"{depth + 1}/x.pdf"
        return _result("attributes_elsewhere", [
            {"markers": ["[1]"], "attribution": FOR_THIS_IDEA,
             "references": [_ref(1, "[1] X 1990", nxt, f"Step {depth + 1}", str(2000 - depth))]},
        ])

    monkeypatch.setattr(chat_origin, "trace_origin", endless)
    monkeypatch.setattr(chat_origin, "library_record", lambda rel_path: None)
    _events, text, _sources = _run(article_paths=["0/x.pdf"])
    # Depth limit: the start plus three steps.
    assert chain == ["0/x.pdf", "1/x.pdf", "2/x.pdf", "3/x.pdf"]
    assert "↳ ↳ ↳" in text


def test_library_chain_as_data(chain, monkeypatch):
    monkeypatch.setattr(chat_origin, "_starting_articles", lambda *args, **kwargs: (["A/Citing 2019.pdf"], ""))
    payload = chat_origin.trace_library_chain("Les bandes minces.", "qwen3.5:4b", 0.3)
    assert payload["ok"] is True
    first, second = payload["steps"]
    assert first["parent"] is None and second["parent"] == 0
    assert [ref["year"] for ref in first["credited"]] == ["1968"]
    assert payload["earliest"]["year"] == "1962"
    assert payload["earliest"]["text"].startswith("[1] C. Quillon")

    monkeypatch.setattr(chat_origin, "_starting_articles", lambda *args, **kwargs: ([], "Index RAG indisponible."))
    assert chat_origin.trace_library_chain("x y z", None, 0.3)["error"] == "index_unavailable"


def test_weak_model_and_unreadable_article(chain, monkeypatch):
    def flaky(claim, rel_path, *, model_name=None, evidence_quote=""):
        if rel_path == "B/Varnel 1968.pdf":
            raise chat_origin.OriginTraceError("document_not_found", "Article introuvable dans la bibliothèque.")
        return dict(_TRACES[rel_path], model_reliability="low")

    monkeypatch.setattr(chat_origin, "trace_origin", flaky)
    _events, text, sources = _run(article_paths=["A/Citing 2019.pdf"])
    assert "article illisible (Article introuvable dans la bibliothèque.)" in text
    assert "Petit modèle" in text
    assert len(sources["data"]) == 1
