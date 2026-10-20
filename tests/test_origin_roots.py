import asyncio
from collections import Counter

import pytest

from src import origin_roots


def _corpus(years_and_refs):
    return [{"id": f"https://openalex.org/W{index}", "publication_year": year, "referenced_works": refs}
            for index, (year, refs) in enumerate(years_and_refs)]


def test_select_roots_keeps_older_widely_cited_references():
    corpus = _corpus([(2000 + index, []) for index in range(20)])
    references = {
        "W1": {"openalex_id": "W1", "year": 1962, "title": "Root"},
        "W2": {"openalex_id": "W2", "year": 2015, "title": "Too recent"},
        "W3": {"openalex_id": "W3", "year": 1970, "title": "Cited by too few"},
        "W4": {"openalex_id": "W4", "year": None, "title": "No year"},
    }
    counts = Counter({"W1": 12, "W2": 15, "W3": 1, "W4": 9})
    roots, meta = origin_roots.select_roots(corpus, references, counts)
    assert [root["openalex_id"] for root in roots] == ["W1"]
    assert roots[0]["citing_share"] == 0.6
    assert meta["corpus_size"] == 20 and meta["corpus_median_year"] == 2009
    assert {point["year"] for point in meta["spectrum"]} >= {1962, 2015}


def test_spectrum_peaks_compare_each_year_with_its_neighbours():
    deviations = origin_roots.spectrum_peaks({1960: 1, 1961: 1, 1962: 20, 1963: 2, 1964: 1})
    assert deviations[1962] == 19  # 20 minus the median of 1, 1, 20, 2, 1
    assert max(deviations, key=deviations.get) == 1962


def test_find_topic_roots_counts_each_citing_paper_once(monkeypatch):
    calls = []

    async def fake_get(client, url, *, params=None, api_name="", retries=2):
        calls.append(dict(params or {}))
        if "search" in (params or {}):
            if params["page"] > 1:
                return {"results": []}
            return {"results": [
                # A paper listing the same reference twice still counts once.
                {"id": "W10", "publication_year": 2010, "referenced_works": ["https://openalex.org/W1", "https://openalex.org/W1"]},
                {"id": "W11", "publication_year": 2012, "referenced_works": ["https://openalex.org/W1", "https://openalex.org/W2"]},
                {"id": "W12", "publication_year": 2014, "referenced_works": ["https://openalex.org/W1"]},
            ]}
        return {"results": [
            {"id": "https://openalex.org/W1", "display_name": "Magnetization of hard superconductors",
             "publication_year": 1962, "doi": "https://doi.org/10.1103/X", "cited_by_count": 4000,
             "authorships": [{"author": {"display_name": "C. P. Bean"}}],
             "primary_location": {"source": {"display_name": "Phys. Rev. Lett."}}},
            {"id": "https://openalex.org/W2", "display_name": "Other", "publication_year": 1999},
        ]}

    monkeypatch.setattr(origin_roots, "_get_json_with_retry", fake_get)
    monkeypatch.setattr(origin_roots, "_openalex_api_key", lambda: "key")
    monkeypatch.setattr(origin_roots, "get_cached_response", lambda key: None)
    stored = {}
    monkeypatch.setattr(origin_roots, "set_cached_response", lambda key, api, payload, ttl_seconds: stored.update(payload=payload))
    monkeypatch.setattr(origin_roots, "_mark_library", lambda roots: [root.update(library=None) for root in roots])

    result = asyncio.run(origin_roots.find_topic_roots("critical state"))
    root = result["roots"][0]
    assert root["openalex_id"] == "W1" and root["citing_count"] == 3 and root["citing_share"] == 1.0
    assert root["authors"] == ["C. P. Bean"] and root["url"] == "https://doi.org/10.1103/x"
    assert calls[0]["search"] == "critical state"
    assert calls[-1]["filter"].startswith("openalex_id:W1|W2")
    # The cached copy carries no library marks: they are recomputed on every read.
    assert "library" not in stored["payload"]["roots"][0]


def test_find_topic_roots_needs_an_openalex_key(monkeypatch):
    monkeypatch.setattr(origin_roots, "_openalex_api_key", lambda: "")
    with pytest.raises(origin_roots.OriginRootsConfigurationError):
        asyncio.run(origin_roots.find_topic_roots("critical state"))
