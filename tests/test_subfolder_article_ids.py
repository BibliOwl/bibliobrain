"""Documents without a Zotero record, spread over sub-folders."""

from __future__ import annotations

from src.rag.document_loader import _article_id_from_item
from src.rag.zotero_integration import _index_signature, _path_article_id, _zotero_signature
from src.zotero_db import ZoteroItem


def test_same_file_name_in_two_folders_gives_two_articles() -> None:
    first = _article_id_from_item("Aimants\\article.pdf", None)
    second = _article_id_from_item("Cryogénie & essais/article.pdf", None)

    assert first != second
    assert first.startswith("article-") and second.startswith("article-")


def test_top_level_files_keep_the_id_they_always_had() -> None:
    assert _path_article_id("Li 2017 - Trapped flux.pdf") == "Li 2017 - Trapped flux"
    assert _article_id_from_item("Li 2017.pdf", None) == "Li_2017"


def test_id_does_not_depend_on_the_path_separator() -> None:
    assert _path_article_id("a\\b\\c\\doc.pdf") == _path_article_id("a/b/c/doc.pdf")


def test_zotero_records_keep_their_key_whatever_the_folder() -> None:
    item = ZoteroItem(item_key="ABCD2345", title="Bobine")

    assert _article_id_from_item("sous/dossier/doc.pdf", item) == "ABCD2345"
    assert _index_signature("sous/dossier/doc.pdf", item) == _zotero_signature(item)


def test_only_sub_folder_files_without_record_are_read_again_once() -> None:
    # Indexes built before stored "" for these files: the mark differs once.
    assert _index_signature("doc.pdf", None) == ""
    assert _index_signature("sous/doc.pdf", None) == "path-id-v1"
    keyless = ZoteroItem(title="Entrée BibTeX sans clé")
    assert _index_signature("sous/doc.pdf", keyless) == f"{_zotero_signature(keyless)}|path-id-v1"
    assert _index_signature("doc.pdf", keyless) == _zotero_signature(keyless)
