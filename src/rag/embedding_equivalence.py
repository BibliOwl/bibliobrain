"""Reuse an index built by the same embedding model under another name or service.

One model served by Ollama, LM Studio or an API carries a different name on
each (``embeddinggemma:latest``, ``text-embedding-embeddinggemma-300m``...),
and Bibliobrain files indexes by that name. Rather than guess from names,
a few stored passages are re-embedded with the configured model: if the
vectors come back the same, the existing index is reused instead of rebuilt,
and the new name is recorded next to it (see ``register_equivalent_model``).
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Any

from src.atomic_io import atomic_write_json
from src.llm_provider import required_embedding_model_name

from . import state
from .vector_store import (
    _chroma_client_settings,
    _current_chunk_size,
    _current_embedding_profile,
    _load_manifest_embedding_prompts,
    list_vector_index_profiles,
    register_equivalent_model,
    resolve_vector_index_profile,
)

PROBE_FILE = "embedding_probe.json"
_PROBE_SAMPLES = 3
# Another quantization of the same weights stays close to 1; another model lands near 0.
EQUIVALENT_SIMILARITY = 0.95
_logger = logging.getLogger(__name__)


def sample_index_probe(index_dir: Path) -> list[dict[str, Any]]:
    """A few stored passages with the vectors the index holds for them."""
    import chromadb

    with state._state_lock:
        loaded = state.vectorstore if state.runtime_db_path == str(index_dir) else None
    try:
        if loaded is not None:
            collections = [loaded._collection]
        else:
            # Not closed on purpose: clearing Chroma's shared cache would also
            # close the store the app may have open on another index.
            client = chromadb.PersistentClient(path=str(index_dir), settings=_chroma_client_settings())
            collections = list(client.list_collections())
        for collection in collections:
            batch = collection.get(limit=24, include=["documents", "embeddings"])
            documents = batch.get("documents") or []
            embeddings = batch.get("embeddings")
            embeddings = [] if embeddings is None else list(embeddings)
            samples = [
                {"text": text, "vector": [round(float(value), 6) for value in vector]}
                for text, vector in zip(documents, embeddings, strict=False)
                if isinstance(text, str) and len(text.strip()) >= 200
            ]
            if samples:
                return samples[:_PROBE_SAMPLES]
    except Exception:
        _logger.warning("Unable to sample %s for the embedding probe", index_dir, exc_info=True)
    return []


def profile_probe(index_dir: Path) -> list[dict[str, Any]]:
    """The index's probe, sampled from Chroma once then kept beside it."""
    path = Path(index_dir) / PROBE_FILE
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(cached, list) and cached:
            return cached
    except (OSError, ValueError):
        pass
    probe = sample_index_probe(Path(index_dir))
    if probe:
        try:
            atomic_write_json(path, probe)
        except OSError:
            _logger.debug("Unable to cache the embedding probe in %s", index_dir, exc_info=True)
    return probe


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


def compare_with_current_embedder(
    probe: list[dict[str, Any]],
    *,
    embedding_prompts: str = "",
) -> dict[str, Any]:
    """Re-embed the probe passages with the configured model and compare.

    ``embedding_prompts`` is the prompt scheme the probed index was built
    with: the same model gives other vectors with or without its prompts.
    ``status``: ``same``, ``different``, ``unavailable`` (the model could not
    be reached) or ``missing`` (no probe to compare with).
    """
    from src.llm_provider import create_embeddings, with_embedding_prompts

    if not probe:
        return {"status": "missing", "similarity": None, "detail": ""}
    try:
        embedder = with_embedding_prompts(
            create_embeddings(required_embedding_model_name()), embedding_prompts
        )
        vectors = embedder.embed_documents([str(sample["text"]) for sample in probe])
    except Exception as exc:
        _logger.info("Embedding probe unavailable: %s", exc)
        return {"status": "unavailable", "similarity": None, "detail": str(exc)[:300]}
    similarities: list[float] = []
    for sample, vector in zip(probe, vectors, strict=False):
        stored = [float(value) for value in sample["vector"]]
        if len(stored) != len(vector):
            return {
                "status": "different",
                "similarity": None,
                "detail": f"dimensions {len(stored)} ≠ {len(vector)}",
            }
        similarities.append(_cosine(stored, [float(value) for value in vector]))
    lowest = min(similarities) if similarities else 0.0
    return {
        "status": "same" if lowest >= EQUIVALENT_SIMILARITY else "different",
        "similarity": round(lowest, 4),
        "detail": "",
    }


def adopt_equivalent_vector_index(
    *,
    chunk_size: int | None = None,
    embedding_model: str | None = None,
) -> dict[str, Any] | None:
    """Before building an index from scratch, look for one that gives the same vectors.

    Only runs when the configured model has no index yet, so it costs one
    short embedding call per candidate at the moment a full build was due.
    Returns the reused profile, or None.
    """
    label = str(embedding_model or _current_embedding_profile())
    chunk = int(chunk_size or _current_chunk_size())
    if resolve_vector_index_profile(chunk_size=chunk, embedding_model=label).get("exists"):
        return None
    for candidate in list_vector_index_profiles():
        # The pre-profile index at the Chroma root has no folder to record aliases in.
        if candidate.get("source") != "profile" or not candidate.get("ready"):
            continue
        if candidate.get("chunk_size") != chunk:
            continue
        candidate_path = str(candidate["path"])
        result = compare_with_current_embedder(
            profile_probe(Path(candidate_path)),
            embedding_prompts=_load_manifest_embedding_prompts(db_path=candidate_path),
        )
        if result["status"] == "unavailable":
            # The configured model does not answer: no other candidate can be checked.
            return None
        if result["status"] != "same":
            continue
        register_equivalent_model(
            candidate_path,
            [str(candidate.get("embedding_model") or ""), label],
        )
        _logger.info(
            "Reusing index %s for %s (similarity %s)",
            candidate["profile_id"],
            label,
            result["similarity"],
        )
        return {**candidate, "similarity": result["similarity"]}
    return None
