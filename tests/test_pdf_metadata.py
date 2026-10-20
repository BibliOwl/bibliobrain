from pathlib import Path

from pypdf import PdfWriter

from src import config, pdf_metadata
from src.pdf_metadata import (
    clear_pdf_metadata_cache,
    extract_pdf_metadata,
    resolve_pdf_metadata,
)
from src.zotero_db import ZoteroItem


def _write_pdf_with_metadata(path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=400)
    writer.add_metadata(
        {
            "/Title": "Reliable local metadata extraction",
            "/Author": "Ada Lovelace; Alan Turing",
            "/CreationDate": "D:20250302120000+01'00'",
            "/Subject": "A local extraction study. DOI: 10.1234/LOCAL.2025.7",
            "/Keywords": "metadata; PDF; local first",
        }
    )
    with path.open("wb") as handle:
        writer.write(handle)


def test_extract_pdf_metadata_reads_embedded_fields(workspace_tmp_dir: Path) -> None:
    pdf_path = workspace_tmp_dir / "paper.pdf"
    _write_pdf_with_metadata(pdf_path)
    clear_pdf_metadata_cache()

    item = extract_pdf_metadata(str(pdf_path))

    assert item is not None
    assert item.title == "Reliable local metadata extraction"
    assert item.authors == ["Ada Lovelace", "Alan Turing"]
    assert item.year == "2025"
    assert item.doi == "10.1234/LOCAL.2025.7"
    assert item.tags == ["metadata", "PDF", "local first"]
    assert item.item_type == "document"


def test_resolve_pdf_metadata_completes_bibtex_without_overwriting_it(
    monkeypatch,
    workspace_tmp_dir: Path,
) -> None:
    primary = ZoteroItem(
        title="Authoritative BibTeX title",
        authors=[],
        year="2024",
        item_type="article",
        citation_key="demo2024",
    )
    extracted = ZoteroItem(
        title="PDF title",
        authors=["Ada Lovelace"],
        year="2025",
        abstract="Locally extracted abstract.",
        item_type="document",
        tags=["automatic"],
        doi="10.1234/example",
    )
    monkeypatch.setattr("src.pdf_metadata.extract_pdf_metadata", lambda _path: extracted)

    item = resolve_pdf_metadata(
        "demo2024.pdf",
        str(workspace_tmp_dir / "demo2024.pdf"),
        {"demo2024.pdf": primary},
    )

    assert item is not None
    assert item.title == "Authoritative BibTeX title"
    assert item.year == "2024"
    assert item.item_type == "article"
    assert item.citation_key == "demo2024"
    assert item.authors == ["Ada Lovelace"]
    assert item.abstract == "Locally extracted abstract."
    assert item.tags == ["automatic"]
    assert item.doi == "10.1234/example"
    assert primary.authors == []


def test_resolve_pdf_metadata_works_without_zotero_or_bibtex(
    monkeypatch,
    workspace_tmp_dir: Path,
) -> None:
    extracted = ZoteroItem(title="Standalone PDF", authors=["Grace Hopper"], year="1952")
    monkeypatch.setattr("src.pdf_metadata.extract_pdf_metadata", lambda _path: extracted)

    item = resolve_pdf_metadata(
        "standalone.pdf",
        str(workspace_tmp_dir / "standalone.pdf"),
        {},
    )

    assert item == extracted


def test_external_enrichment_requires_explicit_permission(
    monkeypatch,
    workspace_tmp_dir: Path,
) -> None:
    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    clear_pdf_metadata_cache()
    local = ZoteroItem(
        title="Reliable standalone PDF metadata",
        authors=["Ada Lovelace"],
        year="2025",
        doi="10.1234/local.2025.7",
    )
    monkeypatch.setattr(pdf_metadata, "extract_pdf_metadata", lambda _path: local)
    monkeypatch.setattr(
        pdf_metadata,
        "get_app_settings",
        lambda: {
            "allow_external_api_requests": False,
            "auto_enrich_pdf_metadata": True,
            "metadata_source": "none",
        },
    )

    def forbidden_lookup(_item: ZoteroItem):
        raise AssertionError("Crossref must not be called without consent")

    monkeypatch.setattr(pdf_metadata, "_crossref_lookup", forbidden_lookup)

    item = resolve_pdf_metadata(
        "standalone.pdf",
        str(workspace_tmp_dir / "standalone.pdf"),
        {},
        allow_external_enrichment=True,
    )

    assert item == local


def test_external_enrichment_requires_the_dedicated_setting(
    monkeypatch,
    workspace_tmp_dir: Path,
) -> None:
    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    clear_pdf_metadata_cache()
    local = ZoteroItem(title="Standalone metadata source", doi="10.1234/local")
    monkeypatch.setattr(pdf_metadata, "extract_pdf_metadata", lambda _path: local)
    monkeypatch.setattr(
        pdf_metadata,
        "get_app_settings",
        lambda: {
            "allow_external_api_requests": True,
            "auto_enrich_pdf_metadata": False,
            "metadata_source": "none",
        },
    )

    def forbidden_lookup(_item: ZoteroItem):
        raise AssertionError("Crossref must not be called when enrichment is disabled")

    monkeypatch.setattr(pdf_metadata, "_crossref_lookup", forbidden_lookup)

    item = resolve_pdf_metadata(
        "standalone.pdf",
        str(workspace_tmp_dir / "standalone.pdf"),
        {},
        allow_external_enrichment=True,
    )

    assert item == local


def test_external_enrichment_by_doi_is_cached_and_completes_bibtex(
    monkeypatch,
    workspace_tmp_dir: Path,
) -> None:
    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    clear_pdf_metadata_cache()
    local = ZoteroItem(
        title="Reliable standalone PDF metadata",
        authors=[],
        year="2025",
        abstract="Abstract extracted locally.",
        item_type="document",
        tags=["local"],
        doi="10.1234/local.2025.7",
    )
    remote = ZoteroItem(
        title="Reliable standalone PDF metadata",
        authors=["Ada Lovelace", "Alan Turing"],
        year="2025",
        journal="Journal of Reliable Metadata",
        item_type="article",
        doi="10.1234/local.2025.7",
    )
    calls = {"count": 0}

    monkeypatch.setattr(pdf_metadata, "extract_pdf_metadata", lambda _path: local)
    monkeypatch.setattr(
        pdf_metadata,
        "get_app_settings",
        lambda: {
            "allow_external_api_requests": True,
            "auto_enrich_pdf_metadata": True,
            "metadata_source": "none",
        },
    )

    def lookup(_item: ZoteroItem):
        calls["count"] += 1
        return remote, True

    monkeypatch.setattr(pdf_metadata, "_crossref_lookup", lookup)

    first = resolve_pdf_metadata(
        "standalone.pdf",
        str(workspace_tmp_dir / "standalone.pdf"),
        {},
        allow_external_enrichment=True,
    )
    second = resolve_pdf_metadata(
        "standalone.pdf",
        str(workspace_tmp_dir / "standalone.pdf"),
        {},
        allow_external_enrichment=False,
    )

    assert first is not None
    assert first.authors == ["Ada Lovelace", "Alan Turing"]
    assert first.journal == "Journal of Reliable Metadata"
    assert first.item_type == "article"
    assert first.abstract == "Abstract extracted locally."
    assert first.tags == ["local"]
    assert "@article{AdaLovelace2025" in first.bibtex_entry()
    assert "journal = {Journal of Reliable Metadata}" in first.bibtex_entry()
    assert second == first
    assert calls["count"] == 1


def test_external_enrichment_falls_back_to_openalex_and_is_cached(
    monkeypatch,
    workspace_tmp_dir: Path,
) -> None:
    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    clear_pdf_metadata_cache()
    local = ZoteroItem(
        title="Reliable standalone document metadata",
        authors=["Ada Lovelace"],
        year="2025",
        doi="10.1234/openalex-fallback",
    )
    remote = ZoteroItem(
        title=local.title,
        authors=["Ada Lovelace", "Alan Turing"],
        year="2025",
        journal="OpenAlex Journal",
        item_type="article",
        doi=local.doi,
    )
    calls = {"crossref": 0, "openalex": 0}
    monkeypatch.setattr(
        pdf_metadata,
        "get_app_settings",
        lambda: {
            "allow_external_api_requests": True,
            "auto_enrich_pdf_metadata": True,
            "metadata_source": "none",
        },
    )

    def crossref_lookup(_item: ZoteroItem):
        calls["crossref"] += 1
        return None, True

    def openalex_lookup(_item: ZoteroItem):
        calls["openalex"] += 1
        return remote, True

    monkeypatch.setattr(pdf_metadata, "_crossref_lookup", crossref_lookup)
    monkeypatch.setattr(pdf_metadata, "_openalex_lookup", openalex_lookup)

    first = pdf_metadata.enrich_pdf_metadata(local, allow_network=True)
    second = pdf_metadata.enrich_pdf_metadata(local, allow_network=False)

    assert first == remote
    assert second == remote
    assert calls == {"crossref": 1, "openalex": 1}


def test_openalex_work_is_normalized_without_exposing_markup() -> None:
    item = pdf_metadata._openalex_item(
        {
            "display_name": "A reliable OpenAlex record",
            "publication_year": 2026,
            "doi": "https://doi.org/10.1234/openalex",
            "type": "article",
            "authorships": [
                {"author": {"display_name": "Ada Lovelace"}},
                {"author": {"display_name": "Alan Turing"}},
            ],
            "primary_location": {"source": {"display_name": "Journal of Tests"}},
            "abstract_inverted_index": {"Useful": [0], "abstract": [1]},
        }
    )

    assert item is not None
    assert item.authors == ["Ada Lovelace", "Alan Turing"]
    assert item.year == "2026"
    assert item.journal == "Journal of Tests"
    assert item.abstract == "Useful abstract"
    assert item.doi == "10.1234/openalex"


def test_bibtex_fields_remain_authoritative_during_external_enrichment(
    monkeypatch,
    workspace_tmp_dir: Path,
) -> None:
    monkeypatch.setattr(config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    clear_pdf_metadata_cache()
    primary = ZoteroItem(
        title="Authoritative user BibTeX title",
        authors=["User Supplied Author"],
        year="2024",
        item_type="article",
        citation_key="UserKey2024",
    )
    local = ZoteroItem(
        title="Heuristic PDF title",
        authors=[],
        year="2023",
        doi="10.1234/missing-fields",
    )
    remote = ZoteroItem(
        title="Crossref title must not replace BibTeX",
        authors=["Ada Lovelace"],
        year="2025",
        journal="Journal supplied by Crossref",
        item_type="inproceedings",
        doi="10.1234/missing-fields",
    )
    monkeypatch.setattr(pdf_metadata, "extract_pdf_metadata", lambda _path: local)
    monkeypatch.setattr(
        pdf_metadata,
        "get_app_settings",
        lambda: {
            "allow_external_api_requests": True,
            "auto_enrich_pdf_metadata": True,
            "metadata_source": "bibtex",
        },
    )
    monkeypatch.setattr(pdf_metadata, "_crossref_lookup", lambda _item: (remote, True))

    item = resolve_pdf_metadata(
        "UserKey2024.pdf",
        str(workspace_tmp_dir / "UserKey2024.pdf"),
        {"userkey2024.pdf": primary},
        allow_external_enrichment=True,
    )

    assert item is not None
    assert item.title == "Authoritative user BibTeX title"
    assert item.year == "2024"
    assert item.item_type == "article"
    assert item.citation_key == "UserKey2024"
    assert item.authors == ["User Supplied Author"]
    assert item.journal == "Journal supplied by Crossref"
    assert item.doi == "10.1234/missing-fields"


def test_title_search_accepts_only_a_confident_unambiguous_match() -> None:
    local = ZoteroItem(
        title="Numerical modelling of AC losses in superconducting magnets",
        authors=["Alice Martin"],
        year="2024",
    )
    exact = {
        "title": ["Numerical modelling of AC losses in superconducting magnets"],
        "author": [{"given": "Alice", "family": "Martin"}],
        "published": {"date-parts": [[2024]]},
        "container-title": ["Superconductor Science"],
        "type": "journal-article",
        "DOI": "10.1000/right",
    }
    noise = {
        "title": ["Numerical modelling of unrelated thermal systems"],
        "author": [{"given": "Bob", "family": "Smith"}],
        "published": {"date-parts": [[2024]]},
        "type": "journal-article",
        "DOI": "10.1000/wrong",
    }

    match = pdf_metadata._select_crossref_title_match(local, [noise, exact])
    rejected = pdf_metadata._select_crossref_title_match(local, [noise])

    assert match is not None
    assert match.doi == "10.1000/right"
    assert match.journal == "Superconductor Science"
    assert rejected is None


def test_doi_lookup_rejects_a_crossref_title_mismatch(monkeypatch) -> None:
    local = ZoteroItem(
        title="Actual paper about superconducting magnets",
        doi="10.1000/reference-doi",
    )
    monkeypatch.setattr(
        pdf_metadata,
        "_crossref_get_json",
        lambda *_args, **_kwargs: {
            "message": {
                "title": ["Completely unrelated cited biology article"],
                "DOI": "10.1000/reference-doi",
                "type": "journal-article",
            }
        },
    )

    match, definitive = pdf_metadata._crossref_lookup(local)

    assert definitive is True
    assert match is None
