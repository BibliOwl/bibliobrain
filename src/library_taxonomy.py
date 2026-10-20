"""Article taxonomy fallback for the knowledge graph UI.

This module deliberately does not depend on the structured Wiki KB. It
classifies already-indexed RAG articles into a broad-to-narrow category
tree and returns the same payload shape consumed by the graph UI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from src import config
from src.atomic_io import atomic_write_text
from src.rel_paths import candidate_rel_paths as _candidate_rel_paths
from src.rel_paths import normalize_rel_path as _normalize_rel_path
from src.unicode_utils import json_dumps_utf8
from src.zotero_db import ZoteroItem, casefold_sort_key

_logger = logging.getLogger(__name__)


def find_pdfs():
    from src.rag.state import find_pdfs as _find_pdfs

    return _find_pdfs()


def get_indexed_rel_paths() -> set[str]:
    from src.rag.vector_store import get_indexed_rel_paths as _get_indexed_rel_paths

    return _get_indexed_rel_paths()


_ALGORITHM_VERSION = "2026-07-17-article-taxonomy-v5-semantic-auto"
_CACHE_FILE = "taxonomy_payload.json"
_GENERIC_COLLECTIONS = {
    "articles",
    "bibliography",
    "biblio",
    "library",
    "papers",
    "pdf",
    "references",
    "zotero",
}
_GENERIC_TAGS = {
    "article",
    "paper",
    "review",
    "todo",
    "read",
    "important",
    "pdf",
}
_SEMANTIC_STOPWORDS = {
    "about",
    "after",
    "also",
    "among",
    "article",
    "articles",
    "avec",
    "based",
    "between",
    "chez",
    "des",
    "dans",
    "depuis",
    "during",
    "effect",
    "effects",
    "entre",
    "etude",
    "étude",
    "from",
    "general",
    "into",
    "leurs",
    "model",
    "models",
    "nouveau",
    "nouvelle",
    "paper",
    "papers",
    "pour",
    "results",
    "resultats",
    "résultats",
    "review",
    "study",
    "system",
    "systems",
    "that",
    "this",
    "through",
    "using",
    "with",
    "without",
}
_SEMANTIC_ACRONYMS = {"ac", "ai", "dc", "dna", "hts", "llm", "mri", "nlp", "rag", "rna"}
_GENERIC_PATH_ROOTS = {
    "engineering",
    "research",
    "research areas",
    "science",
    "sciences",
    "scientific literature",
}
_DOMAIN_HINTS: list[tuple[tuple[str, ...], list[str]]] = [
    (
        ("superconduct", "hts", "rebco", "ybco", "quench", "ac loss", "critical current"),
        ["Physical sciences", "Electrical engineering", "Applied superconductivity"],
    ),
    (
        ("retrieval", "embedding", "rag", "llm", "language model", "nlp", "transformer"),
        ["Computer science", "Artificial intelligence", "Information retrieval"],
    ),
    (
        ("electrical", "power system", "motor", "generator", "transformer", "magnet", "coil", "comsol", "finite element"),
        ["Physical sciences", "Electrical engineering", "Applied electromagnetics"],
    ),
    (
        ("quantum", "particle", "accelerator", "photon", "laser", "optical", "physics"),
        ["Physical sciences", "Physics", "Applied physics"],
    ),
    (
        ("clinical", "patient", "diagnosis", "therapy", "biomedical", "disease"),
        ["Life sciences", "Medicine", "Biomedical research"],
    ),
    (
        ("protein", "gene", "genome", "cell", "enzyme", "molecular"),
        ["Life sciences", "Biology", "Molecular biology"],
    ),
    (
        ("catalyst", "reaction", "molecule", "polymer", "spectroscopy", "chemical"),
        ["Physical sciences", "Chemistry", "Chemical sciences"],
    ),
    (
        ("material", "graphene", "semiconductor", "alloy", "thin film", "crystal"),
        ["Physical sciences", "Materials science", "Functional materials"],
    ),
    (
        ("climate", "ecosystem", "soil", "hydrology", "environment", "geology"),
        ["Earth and environmental sciences", "Environmental science", "Earth systems"],
    ),
    (
        ("optimization", "bayesian", "statistical", "regression", "theorem", "probability"),
        ["Mathematics and statistics", "Applied mathematics", "Quantitative methods"],
    ),
    (
        ("economics", "economic", "policy", "society", "social", "education", "survey"),
        ["Social sciences", "Applied social sciences", "Empirical studies"],
    ),
]

ArticleClassifier = Callable[[dict[str, Any]], dict[str, Any] | None]


def _taxonomy_dir() -> Path:
    path = Path(config.STORAGE_DIR) / "article_taxonomy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_path() -> Path:
    return _taxonomy_dir() / _CACHE_FILE


def clear_article_taxonomy_cache() -> None:
    try:
        _cache_path().unlink()
    except FileNotFoundError:
        pass


def _file_stat_signature(full_path: str) -> str:
    try:
        stat = os.stat(full_path)
    except OSError:
        return ""
    return f"{int(stat.st_size)}:{int(getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1_000_000_000)))}"


def _zotero_signature(item: ZoteroItem | None) -> str:
    if item is None:
        return ""
    payload = {
        "title": item.title,
        "authors": item.authors,
        "year": item.year,
        "journal": item.journal,
        "abstract": item.abstract,
        "item_type": item.item_type,
        "tags": sorted(item.tags, key=casefold_sort_key),
        "collections": sorted(item.collections, key=casefold_sort_key),
        "doi": item.doi,
        "notes": item.notes[:8],
    }
    raw = json_dumps_utf8(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _slug(value: str, fallback: str = "node") -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return cleaned[:80] or fallback


def _clean_label(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = text.replace("Ã©", "é").replace("Ã¨", "è").replace("Ã ", "à")
    return text


def _split_collection_path(value: str) -> list[str]:
    parts = [
        _clean_label(part)
        for part in re.split(r"\s*(?:/|>|\\\\)\s*", str(value or ""))
        if _clean_label(part)
    ]
    return [part for part in parts if part.lower() not in _GENERIC_COLLECTIONS]


def _dedupe_adjacent(labels: list[str]) -> list[str]:
    out: list[str] = []
    for label in labels:
        clean = _clean_label(label)
        if not clean:
            continue
        if out and out[-1].lower() == clean.lower():
            continue
        out.append(clean)
    return out


def _label_tokens(label: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(label or "").lower())
        if len(token) >= 3 and token not in {"and", "the", "for", "with"}
    }


def _is_redundant_parent(parent: str, child: str) -> bool:
    parent_tokens = _label_tokens(parent)
    child_tokens = _label_tokens(child)
    if not parent_tokens or not child_tokens:
        return False
    if parent_tokens <= child_tokens:
        return True
    parent_norm = " ".join(sorted(parent_tokens))
    child_norm = " ".join(sorted(child_tokens))
    return parent_norm in child_norm


def _normalize_taxonomy_path(labels: list[str]) -> list[str]:
    cleaned = _dedupe_adjacent(labels)
    if len(cleaned) > 1 and cleaned[0].lower() in _GENERIC_PATH_ROOTS:
        cleaned = cleaned[1:]
    out: list[str] = []
    for label in cleaned:
        while out and _is_redundant_parent(out[-1], label):
            out.pop()
        if any(existing.lower() == label.lower() for existing in out):
            continue
        out.append(label)
    return out


def _lookup_zotero(rel_path: str) -> ZoteroItem | None:
    try:
        from src.library import _lookup_zotero as lookup

        return lookup(rel_path)
    except Exception:
        _logger.debug("Recovered from an error in _lookup_zotero.", exc_info=True)
        return None


def _article_id(rel_path: str, item: ZoteroItem | None) -> str:
    if item is not None:
        for value in (item.item_key, item.citation_key):
            clean = str(value or "").strip()
            if clean:
                return clean
    digest = hashlib.sha1(_normalize_rel_path(rel_path).encode("utf-8")).hexdigest()[:12]
    return f"article-{digest}"


def _collect_indexed_articles() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    indexed_raw = get_indexed_rel_paths()
    indexed_norm = {candidate for path in indexed_raw for candidate in _candidate_rel_paths(path)}
    rows: list[dict[str, Any]] = []
    fingerprint_rows: list[dict[str, Any]] = []
    for rel_path, full_path in find_pdfs():
        rel_candidates = _candidate_rel_paths(rel_path)
        if indexed_norm and not rel_candidates.intersection(indexed_norm):
            continue
        if not indexed_norm:
            continue
        item = _lookup_zotero(rel_path)
        article_id = _article_id(rel_path, item)
        note_texts = item.all_note_texts() if item is not None else []
        article = {
            "article_id": article_id,
            "path": _normalize_rel_path(rel_path),
            "title": _clean_label(item.title if item and item.title else Path(rel_path).stem),
            "author": item.first_author() if item else "",
            "authors": list(item.authors[:8]) if item else [],
            "year": item.year if item else "",
            "journal": item.journal if item else "",
            "abstract": item.abstract if item else "",
            "item_type": item.item_type if item else "",
            "tags": list(item.tags[:12]) if item else [],
            "collections": list(item.collections[:8]) if item else [],
            "notes": note_texts[:3],
            "citation_key": item.citation_key_or_fallback() if item else article_id,
        }
        rows.append(article)
        fingerprint_rows.append(
            {
                "article_id": article_id,
                "rel_path": _normalize_rel_path(rel_path),
                "file_sig": _file_stat_signature(full_path),
                "zotero_sig": _zotero_signature(item),
            }
        )
    rows.sort(key=lambda row: (str(row.get("title") or "").lower(), str(row.get("path") or "")))
    fingerprint_rows.sort(key=lambda row: str(row.get("rel_path") or ""))
    return rows, fingerprint_rows


def _fingerprint(fingerprint_rows: list[dict[str, Any]], model_name: str) -> str:
    payload = {
        "algorithm": _ALGORITHM_VERSION,
        "model_name": str(model_name or "").strip(),
        "rows": fingerprint_rows,
    }
    raw = json_dumps_utf8(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _article_text(article: dict[str, Any]) -> str:
    return " ".join(
        [
            str(article.get("title") or ""),
            str(article.get("abstract") or ""),
            " ".join(str(t) for t in article.get("tags") or []),
            " ".join(str(c) for c in article.get("collections") or []),
            " ".join(str(n) for n in article.get("notes") or []),
        ]
    ).lower()


def _domain_hint_path(article: dict[str, Any]) -> list[str]:
    text = _article_text(article)
    for tokens, path in _DOMAIN_HINTS:
        if any(token in text for token in tokens):
            return list(path)
    return ["Scientific literature", "Unclassified research areas"]


def _best_collection_path(article: dict[str, Any]) -> list[str]:
    paths = [
        _normalize_taxonomy_path(_split_collection_path(collection))
        for collection in article.get("collections") or []
    ]
    usable = [path for path in paths if path]
    if not usable:
        return []
    return max(usable, key=lambda path: (len(path), sum(len(label) for label in path)))[:5]


def _metadata_seed_path(article: dict[str, Any]) -> tuple[list[str], bool]:
    collection_path = _best_collection_path(article)
    if collection_path:
        return collection_path, False
    tags = [_clean_label(tag) for tag in (article.get("tags") or []) if _clean_label(tag)]
    useful_tags = [tag for tag in tags if tag.lower() not in _GENERIC_TAGS]
    if useful_tags:
        return [*_domain_hint_path(article)[:3], useful_tags[0]], False
    return [*_domain_hint_path(article), "General studies"], True


def _collection_hierarchy_quality(articles: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(articles)
    paths = [_best_collection_path(article) for article in articles]
    hierarchical = [path for path in paths if len(path) >= 2]
    coverage = len(hierarchical) / max(1, total)
    root_counts = Counter(path[0].casefold() for path in hierarchical if path)
    shared_articles = sum(count for count in root_counts.values() if count >= 2)
    shared_coverage = shared_articles / max(1, total)

    if total <= 3:
        usable = bool(hierarchical)
    else:
        root_limit = max(2, round(math.sqrt(total)))
        usable = coverage >= 0.55 and (
            shared_coverage >= 0.45 or len(root_counts) <= root_limit
        )
    return {
        "usable": usable,
        "coverage": round(coverage, 4),
        "shared_coverage": round(shared_coverage, 4),
        "root_count": len(root_counts),
    }


def _semantic_article_tokens(article: dict[str, Any]) -> list[str]:
    title = str(article.get("title") or "")
    text = " ".join(
        [
            title,
            title,
            str(article.get("abstract") or "")[:4000],
            str(article.get("journal") or ""),
            " ".join(str(tag) for tag in article.get("tags") or []),
        ]
    ).casefold()
    tokens = [
        token
        for token in re.findall(r"[^\W_]+", text, flags=re.UNICODE)
        if (len(token) >= 3 or token in _SEMANTIC_ACRONYMS)
        and not token.isdigit()
        and token not in _SEMANTIC_STOPWORDS
    ]
    bigrams = [
        f"{left}_{right}"
        for left, right in zip(tokens, tokens[1:], strict=False)
        if left != right
    ]
    domain_path = _domain_hint_path(article)
    domain_features = [
        f"domain_{_slug(label)}"
        for label in domain_path[:2]
        if label != "Unclassified research areas"
    ]
    return [*tokens, *bigrams, *domain_features]


def _normalize_sparse_vector(vector: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(value * value for value in vector.values()))
    if norm <= 0:
        return {}
    return {key: value / norm for key, value in vector.items() if value}


def _sparse_similarity(first: dict[str, float], second: dict[str, float]) -> float:
    if len(first) > len(second):
        first, second = second, first
    return sum(value * second.get(key, 0.0) for key, value in first.items())


def _semantic_vectors(articles: list[dict[str, Any]]) -> list[dict[str, float]]:
    token_counts = [Counter(_semantic_article_tokens(article)) for article in articles]
    document_frequency: Counter[str] = Counter()
    total_frequency: Counter[str] = Counter()
    for counts in token_counts:
        document_frequency.update(counts.keys())
        total_frequency.update(counts)

    total = len(articles)
    max_document_frequency = max(2, math.ceil(total * 0.88))
    candidates = [
        term
        for term, frequency in document_frequency.items()
        if frequency >= 2 and frequency <= max_document_frequency
    ]
    candidates.sort(
        key=lambda term: (
            math.log((1 + total) / (1 + document_frequency[term]))
            * math.log1p(total_frequency[term]),
            document_frequency[term],
            term,
        ),
        reverse=True,
    )
    vocabulary = set(candidates[:600])

    vectors: list[dict[str, float]] = []
    for counts in token_counts:
        kept_total = sum(count for term, count in counts.items() if term in vocabulary)
        if kept_total <= 0:
            vectors.append({})
            continue
        vector = {
            term: (count / kept_total)
            * (math.log((1 + total) / (1 + document_frequency[term])) + 1.0)
            for term, count in counts.items()
            if term in vocabulary
        }
        vectors.append(_normalize_sparse_vector(vector))
    return vectors


def _semantic_centroid(
    vectors: list[dict[str, float]], member_indexes: list[int]
) -> dict[str, float]:
    aggregate: dict[str, float] = {}
    for index in member_indexes:
        for term, value in vectors[index].items():
            aggregate[term] = aggregate.get(term, 0.0) + value
    if member_indexes:
        aggregate = {term: value / len(member_indexes) for term, value in aggregate.items()}
    return _normalize_sparse_vector(aggregate)


def _semantic_kmeans(
    vectors: list[dict[str, float]], cluster_count: int
) -> tuple[list[int], list[dict[str, float]]]:
    non_empty = [index for index, vector in enumerate(vectors) if vector]
    if not non_empty:
        return [0] * len(vectors), [{}]
    cluster_count = max(1, min(cluster_count, len(non_empty)))
    seed_indexes = [max(non_empty, key=lambda index: (len(vectors[index]), -index))]
    while len(seed_indexes) < cluster_count:
        remaining = [index for index in non_empty if index not in seed_indexes]
        next_index = min(
            remaining,
            key=lambda index: (
                max(
                    _sparse_similarity(vectors[index], vectors[seed])
                    for seed in seed_indexes
                ),
                index,
            ),
        )
        seed_indexes.append(next_index)

    centroids = [dict(vectors[index]) for index in seed_indexes]
    assignments = [0] * len(vectors)
    for _ in range(10):
        updated = [
            max(
                range(len(centroids)),
                key=lambda cluster: _sparse_similarity(vector, centroids[cluster]),
            )
            if vector
            else 0
            for vector in vectors
        ]
        members = [
            [index for index, assigned in enumerate(updated) if assigned == cluster]
            for cluster in range(len(centroids))
        ]
        new_centroids = [
            _semantic_centroid(vectors, indexes) if indexes else centroids[cluster]
            for cluster, indexes in enumerate(members)
        ]
        if updated == assignments:
            assignments = updated
            centroids = new_centroids
            break
        assignments = updated
        centroids = new_centroids
    return assignments, centroids


def _semantic_term_label(term: str) -> str:
    words = [word for word in term.split("_") if word and not word.startswith("domain-")]
    if not words:
        return ""
    return " ".join(word.upper() if word in _SEMANTIC_ACRONYMS else word.title() for word in words)


def _semantic_cluster_label(
    centroid: dict[str, float], cluster_number: int
) -> str:
    ranked = [
        term
        for term, _value in sorted(centroid.items(), key=lambda item: item[1], reverse=True)
        if not term.startswith("domain_")
    ]
    selected: list[str] = []
    selected_words: set[str] = set()
    for term in ranked:
        words = set(term.split("_"))
        if selected_words and words <= selected_words:
            continue
        label = _semantic_term_label(term)
        if not label:
            continue
        selected.append(label)
        selected_words.update(words)
        if len(selected) >= 2:
            break
    return " · ".join(selected) if selected else f"Research theme {cluster_number + 1}"


def _semantic_cluster_seed_paths(
    articles: list[dict[str, Any]],
) -> tuple[dict[str, tuple[list[str], float]], int]:
    if not articles:
        return {}, 0
    vectors = _semantic_vectors(articles)
    useful_vector_count = sum(bool(vector) for vector in vectors)
    if useful_vector_count < 2:
        fallback_result: dict[str, tuple[list[str], float]] = {}
        for article in articles:
            domain = _domain_hint_path(article)
            base = domain[:2] if domain[-1] != "Unclassified research areas" else ["Semantic themes"]
            fallback_result[str(article.get("article_id") or "")] = (
                [*base, "General research"],
                0.42,
            )
        return fallback_result, 1

    requested_clusters = min(
        12,
        max(2, round(math.sqrt(len(articles)))),
        max(2, len(articles) // 2),
    )
    assignments, centroids = _semantic_kmeans(vectors, requested_clusters)
    members_by_cluster: dict[int, list[int]] = {}
    for index, cluster in enumerate(assignments):
        members_by_cluster.setdefault(cluster, []).append(index)

    result: dict[str, tuple[list[str], float]] = {}
    used_labels: Counter[str] = Counter()
    for cluster, indexes in sorted(members_by_cluster.items()):
        centroid = centroids[cluster] if cluster < len(centroids) else {}
        label = _semantic_cluster_label(centroid, cluster)
        used_labels[label] += 1
        if used_labels[label] > 1:
            label = f"{label} {used_labels[label]}"

        hinted_paths = [
            tuple(_domain_hint_path(articles[index])[:2])
            for index in indexes
            if _domain_hint_path(articles[index])[-1] != "Unclassified research areas"
        ]
        hint_counts = Counter(hinted_paths)
        if hint_counts:
            domain_path, domain_count = hint_counts.most_common(1)[0]
            base_path = list(domain_path) if domain_count / len(indexes) >= 0.34 else ["Semantic themes"]
        else:
            base_path = ["Semantic themes"]

        for index in indexes:
            article_id = str(articles[index].get("article_id") or "")
            similarity = _sparse_similarity(vectors[index], centroid) if vectors[index] else 0.0
            confidence = max(0.42, min(0.86, 0.46 + 0.4 * max(0.0, similarity)))
            result[article_id] = (_normalize_taxonomy_path([*base_path, label]), confidence)
    return result, len(members_by_cluster)


def _parse_llm_json(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _build_llm_prompt(article: dict[str, Any], seed_path: list[str]) -> str:
    payload = {
        "article_id": article.get("article_id"),
        "title": article.get("title"),
        "year": article.get("year"),
        "journal": article.get("journal"),
        "tags": article.get("tags"),
        "collections": article.get("collections"),
        "abstract": str(article.get("abstract") or "")[:1500],
        "notes": [str(note)[:500] for note in (article.get("notes") or [])[:2]],
        "seed_path": seed_path,
    }
    return (
        "Classify one scientific article into one broad-to-narrow taxonomy path.\n"
        "Return strict JSON only with keys: article_id, path, confidence, rationale_short.\n"
        "Rules:\n"
        "- path must contain 2 to 5 category labels, broad to narrow.\n"
        "- Do not include the article title as the final path label.\n"
        "- Prefer scientific domains over generic labels such as Papers or Articles.\n"
        "- Keep labels concise.\n\n"
        f"INPUT:\n{json_dumps_utf8(payload, ensure_ascii=False, indent=2)}\n\n"
        "Output JSON now."
    )


def _make_llm_classifier(model_name: str) -> ArticleClassifier | None:
    model = str(model_name or "").strip()
    if not model:
        return None
    try:
        from src.llm_provider import create_chat_llm, invoke_text

        llm = create_chat_llm(model)
    except Exception:
        _logger.debug("Recovered from an error in _make_llm_classifier.", exc_info=True)
        return None

    def classify(article: dict[str, Any]) -> dict[str, Any] | None:
        seed_path = list(article.get("_seed_path") or [])
        raw = invoke_text(llm, _build_llm_prompt(article, seed_path)) or ""
        return _parse_llm_json(raw)

    return classify


def _classify_article(
    article: dict[str, Any],
    *,
    classifier: ArticleClassifier | None,
) -> tuple[list[str], float, str, str]:
    seed_path, needs_llm = _metadata_seed_path(article)
    article["_seed_path"] = seed_path
    if needs_llm and classifier is not None:
        try:
            result = classifier(article)
        except Exception:
            _logger.debug("Recovered from an error in _classify_article.", exc_info=True)
            result = None
        if isinstance(result, dict):
            path = [_clean_label(item) for item in (result.get("path") or []) if _clean_label(item)]
            if 2 <= len(path) <= 6:
                confidence = max(0.0, min(1.0, float(result.get("confidence") or 0.62)))
                return _normalize_taxonomy_path(path[:6]), confidence, "llm", str(result.get("rationale_short") or "")
    confidence = 0.72 if not needs_llm else 0.38
    return _normalize_taxonomy_path(seed_path[:6]), confidence, "metadata" if not needs_llm else "metadata_fallback", ""


def _node_id(prefix: list[str]) -> str:
    digest = hashlib.sha1("::".join(prefix).lower().encode("utf-8")).hexdigest()[:10]
    return f"article-tax-{_slug(prefix[-1], fallback='category')}-{digest}"


def _article_payload(article: dict[str, Any], *, match_count: int = 1) -> dict[str, Any]:
    authors = article.get("authors") or []
    author = ", ".join(str(a) for a in authors[:3] if str(a).strip())
    return {
        "article_id": article.get("article_id"),
        "title": article.get("title"),
        "path": article.get("path"),
        "year": article.get("year"),
        "author": author or article.get("author") or "",
        "match_count": match_count,
        "citation_key": article.get("citation_key") or "",
    }


def _merge_duplicate_label_nodes(
    nodes: dict[str, dict[str, Any]],
    children_by_parent: dict[str, list[str]],
    root_ids: list[str],
    article_ids_by_node: dict[str, set[str]],
    article_payloads_by_node: dict[str, dict[str, dict[str, Any]]],
) -> None:
    """Fold nodes that share the same label into a single canonical node.

    Different article paths can produce different node IDs for the same
    concept (e.g. ``["Electrical engineering"]`` vs
    ``["Engineering", "Electrical engineering"]`` or ``["Library
    collections", "Electrical engineering"]`` vs ``["Computer science",
    "Electrical engineering"]``) because IDs are hashed on the full path
    prefix. We keep the most informative instance — deepest path, most
    articles attached — and reparent the others' articles and children
    under it.
    """
    label_groups: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        key = str(node.get("label") or "").strip().lower()
        if not key:
            continue
        label_groups.setdefault(key, []).append(node_id)

    for candidate_ids in label_groups.values():
        if len(candidate_ids) < 2:
            continue
        canonical = max(
            candidate_ids,
            key=lambda nid: (
                nodes[nid].get("depth") or 0,
                len(article_ids_by_node.get(nid, set())),
                -(nodes[nid].get("sort_order") or 0),
            ),
        )
        for other in candidate_ids:
            if other == canonical:
                continue
            for aid in article_ids_by_node.get(other, set()):
                article_ids_by_node.setdefault(canonical, set()).add(aid)
            for aid, payload in article_payloads_by_node.get(other, {}).items():
                article_payloads_by_node.setdefault(canonical, {})[aid] = payload
            for child_id in children_by_parent.pop(other, []):
                children_by_parent.setdefault(canonical, [])
                if child_id not in children_by_parent[canonical]:
                    children_by_parent[canonical].append(child_id)
                if child_id in nodes:
                    nodes[child_id]["parent_id"] = canonical
            other_parent = nodes[other].get("parent_id")
            if other_parent and other_parent in children_by_parent:
                children_by_parent[other_parent] = [
                    cid for cid in children_by_parent[other_parent] if cid != other
                ]
            article_ids_by_node.pop(other, None)
            article_payloads_by_node.pop(other, None)
            nodes.pop(other, None)
            if other in root_ids:
                root_ids.remove(other)


def _build_tree(classified: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]], dict[str, list[str]], dict[str, list[dict[str, Any]]]]:
    nodes: dict[str, dict[str, Any]] = {}
    children_by_parent: dict[str, list[str]] = {}
    root_ids: list[str] = []
    article_ids_by_node: dict[str, set[str]] = {}
    article_payloads_by_node: dict[str, dict[str, dict[str, Any]]] = {}

    for row in classified:
        labels = _normalize_taxonomy_path(row.get("path") or [])
        if not labels:
            continue
        parent_id = ""
        prefix: list[str] = []
        for depth, label in enumerate(labels):
            prefix.append(label)
            node_id = _node_id(prefix)
            if node_id not in nodes:
                nodes[node_id] = {
                    "id": node_id,
                    "node_id": node_id,
                    "label": label,
                    "summary": "",
                    "description": "",
                    "type": "category",
                    "display_level": "taxonomy",
                    "parent_id": parent_id or None,
                    "depth": depth,
                    "sort_order": len(nodes),
                    "group": "article_taxonomy",
                    "degree": 0,
                    "score": row.get("confidence", 0.0),
                    "navigation_score": row.get("confidence", 0.0),
                    "article_count": 0,
                    "claim_count": 0,
                    "child_count": 0,
                    "topic_keys": [],
                    "is_taxonomy": True,
                    "taxonomy_source": row.get("source", ""),
                    "taxonomy_confidence": row.get("confidence", 0.0),
                    "taxonomy_provider": "local",
                    "provider_id": "",
                    "candidate_count": 0,
                    "taxonomy_path": list(prefix),
                }
            if parent_id:
                children_by_parent.setdefault(parent_id, [])
                if node_id not in children_by_parent[parent_id]:
                    children_by_parent[parent_id].append(node_id)
            elif node_id not in root_ids:
                root_ids.append(node_id)
            article = row.get("article") or {}
            aid = str(article.get("article_id") or "").strip()
            if aid:
                article_ids_by_node.setdefault(node_id, set()).add(aid)
                article_payloads_by_node.setdefault(node_id, {})[aid] = _article_payload(article)
            parent_id = node_id

    _merge_duplicate_label_nodes(
        nodes,
        children_by_parent,
        root_ids,
        article_ids_by_node,
        article_payloads_by_node,
    )

    for parent_id, child_ids in children_by_parent.items():
        child_ids.sort(key=lambda cid: str(nodes.get(cid, {}).get("label") or cid).lower())
        if parent_id in nodes:
            nodes[parent_id]["child_count"] = len(child_ids)
            nodes[parent_id]["degree"] = len(child_ids)

    for node_id, node in nodes.items():
        node["article_count"] = len(article_ids_by_node.get(node_id, set()))

    root_ids.sort(key=lambda cid: str(nodes.get(cid, {}).get("label") or cid).lower())
    articles_by_concept = {
        node_id: sorted(
            payloads.values(),
            key=lambda item: (str(item.get("year") or ""), str(item.get("title") or "").lower()),
            reverse=True,
        )
        for node_id, payloads in article_payloads_by_node.items()
    }
    return root_ids, list(nodes.values()), children_by_parent, articles_by_concept


def _load_cached_payload(fingerprint: str) -> dict[str, Any] | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("fingerprint") != fingerprint:
        return None
    result = payload.get("payload")
    if not isinstance(result, dict):
        return None
    stats = result.setdefault("stats", {})
    if isinstance(stats, dict):
        stats["cache_hit"] = True
        stats["build_ms"] = 0
    return result


def _save_cached_payload(fingerprint: str, payload: dict[str, Any]) -> None:
    cache_payload = {
        "fingerprint": fingerprint,
        "algorithm": _ALGORITHM_VERSION,
        "saved_at": time.time(),
        "payload": payload,
    }
    target = _cache_path()
    atomic_write_text(
        target,
        json_dumps_utf8(cache_payload, ensure_ascii=False, indent=2),
    )


def build_article_taxonomy_payload(
    *,
    force_rebuild: bool = False,
    model_name: str | None = None,
    classifier: ArticleClassifier | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    articles, fingerprint_rows = _collect_indexed_articles()
    model = str(model_name or "").strip()
    fp = _fingerprint(fingerprint_rows, model)
    if not force_rebuild:
        cached = _load_cached_payload(fp)
        if cached is not None:
            return cached

    if not articles:
        payload = {
            "ok": True,
            "engine": "rag_article_taxonomy",
            "graph_type": "article_taxonomy",
            "root_ids": [],
            "nodes": [],
            "edges": [],
            "children_by_parent": {},
            "articles_by_concept": {},
            "stats": {
                "concepts": 0,
                "categories": 0,
                "roots": 0,
                "articles": 0,
                "build_ms": 0,
                "concept_graph_strategy": "article_taxonomy_empty",
                "taxonomy_strategy": "empty",
                "cache_hit": False,
                "semantic_clustered": 0,
                "semantic_clusters": 0,
                "zotero_hierarchy_coverage": 0.0,
            },
            "warning": "Aucun PDF indexé RAG disponible pour construire la taxonomie d'articles.",
            "hidden_node_count": 0,
        }
        _save_cached_payload(fp, payload)
        return payload

    effective_classifier = classifier or _make_llm_classifier(model)
    hierarchy_quality = _collection_hierarchy_quality(articles)
    semantic_seeds: dict[str, tuple[list[str], float]] = {}
    semantic_cluster_count = 0
    if not hierarchy_quality["usable"] and effective_classifier is None:
        semantic_seeds, semantic_cluster_count = _semantic_cluster_seed_paths(articles)

    classified: list[dict[str, Any]] = []
    llm_count = 0
    fallback_count = 0
    semantic_count = 0
    for article in articles:
        article_id = str(article.get("article_id") or "")
        semantic_seed = semantic_seeds.get(article_id)
        if semantic_seed is not None:
            path, confidence = semantic_seed
            source = "semantic_cluster"
            rationale = "Local TF-IDF grouping from title, abstract, tags, and journal."
            semantic_count += 1
        else:
            path, confidence, source, rationale = _classify_article(
                dict(article),
                classifier=effective_classifier,
            )
        if source == "llm":
            llm_count += 1
        if source.endswith("fallback"):
            fallback_count += 1
        classified.append(
            {
                "article": article,
                "path": path,
                "confidence": confidence,
                "source": source,
                "rationale_short": rationale,
            }
        )

    root_ids, nodes, children_by_parent, articles_by_concept = _build_tree(classified)
    build_ms = int((time.perf_counter() - started) * 1000)
    payload = {
        "ok": True,
        "engine": "rag_article_taxonomy",
        "graph_type": "article_taxonomy",
        "root_ids": root_ids,
        "nodes": nodes,
        "edges": [],
        "children_by_parent": children_by_parent,
        "articles_by_concept": articles_by_concept,
        "stats": {
            "concepts": len(nodes),
            "categories": len(nodes),
            "roots": len(root_ids),
            "articles": len(articles),
            "build_ms": build_ms,
            "concept_graph_strategy": (
                "article_taxonomy_semantic"
                if semantic_count
                else "article_taxonomy_metadata"
            ),
            "taxonomy_strategy": (
                "semantic_content_clusters"
                if semantic_count
                else "llm_classification" if llm_count else "zotero_hierarchy"
            ),
            "taxonomy_provider": "local",
            "taxonomy_degraded": False,
            "cache_hit": False,
            "llm_classified": llm_count,
            "metadata_fallback": fallback_count,
            "semantic_clustered": semantic_count,
            "semantic_clusters": semantic_cluster_count,
            "zotero_hierarchy_coverage": hierarchy_quality["coverage"],
            "zotero_shared_root_coverage": hierarchy_quality["shared_coverage"],
            "algorithm": _ALGORITHM_VERSION,
        },
        "warning": "",
        "hidden_node_count": 0,
    }
    _save_cached_payload(fp, payload)
    return payload


__all__ = [
    "build_article_taxonomy_payload",
    "clear_article_taxonomy_cache",
]
