from __future__ import annotations

from typing import Any

from langchain_core.documents import Document

from src.rag import lexical_index


def test_lexical_index_replaces_and_deletes_source_rows(tmp_path) -> None:
    index_path = str(tmp_path / "index")
    first = Document(
        page_content="Flux pinning in superconducting tapes",
        metadata={"rel_path": "paper-a.pdf", "zotero_title": "HTS losses"},
    )
    second = Document(
        page_content="Unrelated mechanics",
        metadata={"rel_path": "paper-b.pdf", "zotero_title": "Beam model"},
    )

    lexical_index.replace_paths(index_path, ["paper-a.pdf", "paper-b.pdf"], [first, second])

    matches = lexical_index.search(index_path, "superconducting", limit=10)
    assert [doc.metadata["rel_path"] for doc in matches] == ["paper-a.pdf"]

    replacement = Document(
        page_content="Updated magnetic hysteresis model",
        metadata={"rel_path": "paper-a.pdf", "zotero_title": "Updated HTS losses"},
    )
    lexical_index.replace_paths(index_path, ["paper-a.pdf"], [replacement])
    assert lexical_index.search(index_path, "superconducting", limit=10) == []
    assert lexical_index.search(index_path, "hysteresis", limit=10)[0].metadata["rel_path"] == (
        "paper-a.pdf"
    )

    lexical_index.delete_paths(index_path, ["paper-a.pdf"])
    assert lexical_index.search(index_path, "hysteresis", limit=10) == []


def test_lexical_index_can_rebuild_an_existing_vectorstore(tmp_path) -> None:
    class FakeCollection:
        @staticmethod
        def count() -> int:
            return 2

    class FakeVectorStore:
        _collection = FakeCollection()

        @staticmethod
        def get(**kwargs: Any) -> dict[str, list[Any]]:
            assert kwargs["offset"] == 0
            return {
                "documents": ["AC loss formulation", "Cryogenic cooling"],
                "metadatas": [
                    {"rel_path": "losses.pdf", "zotero_author": "Brandt"},
                    {"rel_path": "cooling.pdf", "zotero_author": "Smith"},
                ],
            }

    index_path = str(tmp_path / "legacy-index")
    assert lexical_index.ensure_from_vectorstore(index_path, FakeVectorStore()) is True
    assert lexical_index.is_ready(index_path) is True
    matches = lexical_index.search(index_path, "Brandt formulation", limit=5)
    assert matches
    assert matches[0].metadata["rel_path"] == "losses.pdf"
