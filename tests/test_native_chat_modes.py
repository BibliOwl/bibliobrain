from __future__ import annotations

from typing import Any

from langchain_core.documents import Document


def _doc(content: str, **meta: Any) -> Document:
    return Document(page_content=content, metadata=meta)


def test_compare_fallback_markdown_contains_source_labels() -> None:
    from src.rag.native_chat import _fallback_compare_markdown

    text = _fallback_compare_markdown(
        "HTS AC losses",
        [
            _doc(
                "H formulation details",
                ref_label="S1",
                zotero_title="H Formulation",
                zotero_authors="Doe",
                zotero_year="2025",
            )
        ],
    )

    assert "## Comparaison" in text
    assert "[S1]" in text
    assert "H Formulation" in text


def test_stream_chat_bibliography_renders_deduped_sources() -> None:
    from src.rag.native_chat import stream_chat_bibliography

    messages = [
        {
            "role": "assistant",
            "content": "Reponse sourcee [S1].",
            "meta": {
                "sources": [
                    {
                        "title": "Paper One",
                        "authors": "Doe",
                        "year": "2024",
                        "doi": "10.123/demo",
                        "file": "paper.pdf",
                    },
                    {
                        "title": "Paper One duplicate",
                        "authors": "Doe",
                        "year": "2024",
                        "doi": "10.123/demo",
                        "file": "paper-copy.pdf",
                    },
                ]
            },
        }
    ]

    events = list(stream_chat_bibliography(previous_messages=messages, scope="chat"))
    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")
    sources = next(event for event in events if event.get("type") == "sources")

    assert "## Bibliographie" in text
    assert "Doe (2024)." in text
    assert text.count("10.123/demo") == 1
    assert sources["answer_origin"] == "bibliography"
    assert len(sources["data"]) == 1


def test_stream_notebook_from_chat_creates_notebook(monkeypatch: Any) -> None:
    from src.rag.native_chat import stream_notebook_from_chat

    calls: dict[str, Any] = {"sources": [], "notes": []}

    def fake_create_notebook(title: str) -> dict[str, str]:
        calls["title"] = title
        return {"id": "nb-1", "title": title}

    def fake_add_source(nb_id: str, title: str, content: str, source_path: str = "") -> dict[str, str]:
        calls["sources"].append(
            {
                "nb_id": nb_id,
                "title": title,
                "content": content,
                "source_path": source_path,
            }
        )
        return {"id": "src-1"}

    def fake_update_notes(nb_id: str, notes: str) -> bool:
        calls["notes"].append({"nb_id": nb_id, "notes": notes})
        return True

    monkeypatch.setattr("src.notebooks.create_notebook", fake_create_notebook)
    monkeypatch.setattr("src.notebooks.add_source", fake_add_source)
    monkeypatch.setattr("src.notebooks.update_notes", fake_update_notes)

    messages = [
        {
            "role": "assistant",
            "content": "Analyse recente.",
            "meta": {
                "sources": [
                    {
                        "title": "Useful Paper",
                        "authors": "Curie",
                        "year": "2023",
                        "snippet": "Important local evidence.",
                        "file": "useful.pdf",
                    }
                ]
            },
        }
    ]

    events = list(
        stream_notebook_from_chat(
            user_query="cree un carnet avec les sources",
            previous_messages=messages,
        )
    )
    text = "".join(event.get("text", "") for event in events if event.get("type") == "token")

    assert "Carnet cree" in text
    assert calls["sources"][0]["title"] == "Useful Paper"
    assert "Important local evidence" in calls["sources"][0]["content"]
    assert calls["notes"][0]["notes"] == "Analyse recente."
