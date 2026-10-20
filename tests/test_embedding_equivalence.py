from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.rag import embedding_equivalence, state, vector_store

OLLAMA = "embeddinggemma:latest"
LMSTUDIO = "lmstudio:text-embedding-embeddinggemma-300m"
PROBE = [
    {"text": "passage " * 40, "vector": [1.0, 0.0, 0.0]},
    {"text": "another passage " * 20, "vector": [0.0, 1.0, 0.0]},
]


@pytest.fixture
def env(workspace_tmp_dir: Path, monkeypatch: Any) -> SimpleNamespace:
    settings: dict[str, Any] = {"model": OLLAMA, "chunk_size": 1000, "vectors": None, "calls": 0}

    class FakeEmbedder:
        def embed_documents(self, texts: list[str]) -> list[list[float]]:
            settings["calls"] += 1
            if settings["vectors"] is None:
                raise RuntimeError("embedding service offline")
            return [list(vector) for vector in settings["vectors"][: len(texts)]]

    monkeypatch.setattr(vector_store, "CHROMA_PATH", str(workspace_tmp_dir / "chroma"))
    monkeypatch.setattr(vector_store, "required_embedding_model_name", lambda: settings["model"])
    monkeypatch.setattr(
        vector_store, "current_embedding_profile_label", lambda name=None: name or settings["model"]
    )
    monkeypatch.setattr(
        vector_store, "get_persisted_chunk_size", lambda default=None: settings["chunk_size"]
    )
    monkeypatch.setattr(
        embedding_equivalence, "required_embedding_model_name", lambda: settings["model"]
    )
    monkeypatch.setattr("src.llm_provider.create_embeddings", lambda model: FakeEmbedder())
    return SimpleNamespace(settings=settings)


def _build_index(model: str = OLLAMA, chunk_size: int = 1000) -> Path:
    index_dir = Path(
        vector_store.build_vector_index_profile(chunk_size=chunk_size, embedding_model=model)["path"]
    )
    index_dir.mkdir(parents=True)
    (index_dir / "chroma.sqlite3").write_bytes(b"")
    (index_dir / state._MANIFEST_FILE).write_text(
        json.dumps(
            {
                "version": 3,
                "chunk_size": chunk_size,
                "embedding_model": model,
                "entries": {"a.pdf": {"file_sig": "1:sha256:x", "chunk_count": 2}},
            }
        ),
        encoding="utf-8",
    )
    (index_dir / embedding_equivalence.PROBE_FILE).write_text(json.dumps(PROBE), encoding="utf-8")
    return index_dir


def test_same_model_on_another_service_reuses_the_index(env: SimpleNamespace) -> None:
    index_dir = _build_index()
    env.settings.update(model=LMSTUDIO, vectors=[[0.99, 0.05, 0.02], [0.04, 0.98, 0.03]])

    adopted = embedding_equivalence.adopt_equivalent_vector_index()

    assert adopted is not None and adopted["path"] == str(index_dir)
    profile = vector_store.resolve_vector_index_profile(chunk_size=1000, embedding_model=LMSTUDIO)
    assert profile == {**profile, "path": str(index_dir), "exists": True, "source": "equivalent"}
    status = vector_store.get_vector_database_status()
    assert status["status"] == "present"
    assert status["embedding_model_mismatch"] is False
    assert status["profile_ready"] is True


def test_reuse_survives_the_manifest_being_rewritten_by_either_model(
    env: SimpleNamespace,
) -> None:
    index_dir = _build_index()
    env.settings.update(model=LMSTUDIO, vectors=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])
    embedding_equivalence.adopt_equivalent_vector_index()
    # An incremental reload under LM Studio records its own label in the manifest.
    manifest_path = index_dir / state._MANIFEST_FILE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps({**manifest, "embedding_model": LMSTUDIO}), encoding="utf-8")

    assert vector_store.manifest_model_matches(str(index_dir), LMSTUDIO)
    assert vector_store.manifest_model_matches(str(index_dir), OLLAMA)
    env.settings.update(model=OLLAMA)
    assert vector_store.get_vector_database_status()["embedding_model_mismatch"] is False


def test_a_model_giving_other_vectors_still_gets_its_own_index(env: SimpleNamespace) -> None:
    index_dir = _build_index()
    env.settings.update(model="nomic-embed-text:latest", vectors=[[0.0, 0.0, 1.0]] * 2)

    assert embedding_equivalence.adopt_equivalent_vector_index() is None
    assert not (index_dir / vector_store._EQUIVALENT_MODELS_FILE).exists()
    assert vector_store.get_vector_database_status()["status"] == "missing"


def test_an_unreachable_model_stops_after_one_attempt(env: SimpleNamespace) -> None:
    _build_index()
    _build_index(model="other:latest")
    env.settings.update(model=LMSTUDIO, vectors=None)

    assert embedding_equivalence.adopt_equivalent_vector_index() is None
    assert env.settings["calls"] == 1


def test_indexes_with_another_chunk_size_are_not_candidates(env: SimpleNamespace) -> None:
    _build_index(chunk_size=800)
    env.settings.update(model=LMSTUDIO, vectors=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    assert embedding_equivalence.adopt_equivalent_vector_index() is None
    assert env.settings["calls"] == 0


def test_no_check_when_the_model_already_has_its_index(env: SimpleNamespace) -> None:
    _build_index()
    env.settings.update(vectors=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]])

    assert embedding_equivalence.adopt_equivalent_vector_index() is None
    assert env.settings["calls"] == 0
