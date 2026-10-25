"""Persistent FTS5 sidecar for fast lexical retrieval over Chroma chunks."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from langchain_core.documents import Document

_LOGGER = logging.getLogger(__name__)
_DB_FILENAME = "lexical_fts.sqlite3"
_SCHEMA_VERSION = "1"
_QUERY_TOKEN_RE = re.compile(r"[\wÀ-ÿ][\wÀ-ÿ.+#-]{1,63}", re.UNICODE)
_SEARCHABLE_METADATA_FIELDS = (
    "title",
    "zotero_title",
    "author",
    "authors",
    "zotero_authors",
    "zotero_first_author",
    "zotero_year",
    "year",
    "doi",
    "zotero_doi",
    "tags",
    "zotero_tags",
    "collections",
    "zotero_collections",
    "source",
    "rel_path",
)


def lexical_db_path(index_path: str) -> str:
    return os.path.join(str(index_path), _DB_FILENAME)


def _connect(index_path: str, *, db_filename: str = _DB_FILENAME) -> sqlite3.Connection:
    Path(index_path).mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(os.path.join(str(index_path), db_filename), timeout=15.0)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lexical_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS lexical_chunks USING fts5(
            search_text,
            content UNINDEXED,
            metadata_json UNINDEXED,
            rel_path UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    connection.execute(
        "INSERT OR REPLACE INTO lexical_meta(key, value) VALUES ('schema_version', ?)",
        (_SCHEMA_VERSION,),
    )
    return connection


@contextmanager
def _open(index_path: str, *, db_filename: str = _DB_FILENAME) -> Iterator[sqlite3.Connection]:
    connection = _connect(index_path, db_filename=db_filename)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _metadata_text(metadata: dict[str, Any]) -> str:
    values: list[str] = []
    for field in _SEARCHABLE_METADATA_FIELDS:
        value = metadata.get(field)
        if isinstance(value, (list, tuple, set)):
            values.extend(str(item) for item in value if item)
        elif value:
            values.append(str(value))
    return "\n".join(values)


def _document_row(document: Document) -> tuple[str, str, str, str]:
    metadata = dict(document.metadata or {})
    content = str(document.page_content or "")
    rel_path = str(metadata.get("rel_path") or metadata.get("source") or "")
    search_text = "\n".join(part for part in (_metadata_text(metadata), content) if part)
    return (
        search_text,
        content,
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":"), default=str),
        rel_path,
    )


def replace_paths(index_path: str, rel_paths: Iterable[str], documents: Iterable[Document]) -> None:
    """Atomically replace all lexical rows belonging to ``rel_paths``."""

    paths = sorted({str(path or "") for path in rel_paths if str(path or "")})
    rows = [_document_row(document) for document in documents]
    with _open(index_path) as connection:
        if paths:
            connection.executemany(
                "DELETE FROM lexical_chunks WHERE rel_path = ?",
                ((path,) for path in paths),
            )
        if rows:
            connection.executemany(
                """
                INSERT INTO lexical_chunks(search_text, content, metadata_json, rel_path)
                VALUES (?, ?, ?, ?)
                """,
                rows,
            )


def replace_page(index_path: str, rel_path: str, page: int, documents: Iterable[Document]) -> None:
    """Atomically refresh one PDF page after incremental OCR, preserving other content."""
    rows = [_document_row(document) for document in documents]
    with _open(index_path) as connection:
        connection.execute(
            "DELETE FROM lexical_chunks WHERE rel_path = ? "
            "AND json_extract(metadata_json, '$.page') = ? "
            "AND json_extract(metadata_json, '$.content_type') = 'pdf_page'",
            (rel_path, page),
        )
        connection.executemany(
            "INSERT INTO lexical_chunks(search_text, content, metadata_json, rel_path) VALUES (?, ?, ?, ?)",
            rows,
        )


def delete_paths(index_path: str, rel_paths: Iterable[str]) -> None:
    paths = sorted({str(path or "") for path in rel_paths if str(path or "")})
    if not paths or not os.path.exists(lexical_db_path(index_path)):
        return
    with _open(index_path) as connection:
        connection.executemany(
            "DELETE FROM lexical_chunks WHERE rel_path = ?",
            ((path,) for path in paths),
        )


def is_ready(index_path: str) -> bool:
    path = lexical_db_path(index_path)
    if not os.path.isfile(path):
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(path, timeout=2.0)
        row = connection.execute(
            "SELECT value FROM lexical_meta WHERE key = 'schema_version'"
        ).fetchone()
        return bool(row and str(row[0]) == _SCHEMA_VERSION)
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()


def rebuild_from_vectorstore(index_path: str, vectorstore: Any, *, page_size: int = 3000) -> int:
    """Build the sidecar once for indexes created before FTS5 support."""

    collection = getattr(vectorstore, "_collection", None)
    total = int(collection.count()) if collection is not None else 0
    temp_path = f"{lexical_db_path(index_path)}.rebuild"
    if os.path.exists(temp_path):
        os.remove(temp_path)

    temp_dir = str(Path(temp_path).parent)
    temp_name = Path(temp_path).name
    try:
        with _open(temp_dir, db_filename=temp_name) as connection:
            for offset in range(0, total, page_size):
                payload = vectorstore.get(
                    include=["documents", "metadatas"],
                    limit=min(page_size, total - offset),
                    offset=offset,
                )
                documents = list(payload.get("documents") or [])
                metadatas = list(payload.get("metadatas") or [])
                rows = [
                    _document_row(
                        Document(
                            page_content=str(documents[index] or "")
                            if index < len(documents)
                            else "",
                            metadata=dict(metadata or {}),
                        )
                    )
                    for index, metadata in enumerate(metadatas)
                ]
                connection.executemany(
                    """
                    INSERT INTO lexical_chunks(search_text, content, metadata_json, rel_path)
                    VALUES (?, ?, ?, ?)
                    """,
                    rows,
                )
        os.replace(temp_path, lexical_db_path(index_path))
        return total
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def ensure_from_vectorstore(index_path: str, vectorstore: Any) -> bool:
    if is_ready(index_path):
        return True
    try:
        rebuild_from_vectorstore(index_path, vectorstore)
        return True
    except (OSError, sqlite3.Error, TypeError, ValueError):
        _LOGGER.exception("Unable to build lexical FTS sidecar for %s", index_path)
        return False


def search(
    index_path: str,
    query: str,
    *,
    limit: int,
    stopwords: Collection[str] = (),
) -> list[Document]:
    # Function words are dropped before the 24-token cap: in a long citation
    # sentence they used to push the content words at its end out of the query.
    tokens = []
    seen: set[str] = set()
    for match in _QUERY_TOKEN_RE.findall(str(query or "").lower()):
        token = match.strip(".+-")
        if len(token) < 2 or token in seen or token in stopwords:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) >= 24:
            break
    if not tokens or not is_ready(index_path):
        return []

    quoted_tokens = [f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens]
    match_query = " OR ".join(quoted_tokens)
    connection = sqlite3.connect(lexical_db_path(index_path), timeout=5.0)
    try:
        rows = connection.execute(
            """
            SELECT content, metadata_json
            FROM lexical_chunks
            WHERE lexical_chunks MATCH ?
            ORDER BY bm25(lexical_chunks)
            LIMIT ?
            """,
            (match_query, max(1, int(limit))),
        ).fetchall()
    finally:
        connection.close()

    documents: list[Document] = []
    for content, metadata_json in rows:
        try:
            metadata = json.loads(str(metadata_json or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        documents.append(Document(page_content=str(content or ""), metadata=metadata))
    return documents


_file_metadata_cache: dict[str, tuple[tuple[int, int], list[dict[str, Any]]]] = {}


def file_metadata(index_path: str) -> list[dict[str, Any]]:
    """The metadata of every indexed file, one record each (from one of its chunks).

    What a "list all my papers" request reads: the index is what the chat
    knows, with or without Zotero. Scanning 90k chunks takes ~0.6 s, so the
    result is kept until the sidecar file changes.
    """
    path = lexical_db_path(index_path)
    if not is_ready(index_path):
        return []
    try:
        stat = os.stat(path)
    except OSError:
        return []
    signature = (int(stat.st_mtime_ns), int(stat.st_size))
    cached = _file_metadata_cache.get(path)
    if cached and cached[0] == signature:
        return [dict(record) for record in cached[1]]
    connection = sqlite3.connect(path, timeout=5.0)
    try:
        rows = connection.execute(
            "SELECT rel_path, metadata_json FROM lexical_chunks GROUP BY rel_path"
        ).fetchall()
    finally:
        connection.close()
    records: list[dict[str, Any]] = []
    for rel_path, metadata_json in rows:
        try:
            metadata = json.loads(str(metadata_json or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if isinstance(metadata, dict):
            metadata.setdefault("rel_path", rel_path)
            records.append(metadata)
    _file_metadata_cache[path] = (signature, records)
    return [dict(record) for record in records]


def file_chunks(index_path: str, rel_path: str) -> list[Document]:
    """Every indexed chunk of one file, in the order it was indexed.

    What a question about a whole paper reads to pick its abstract, chapter
    openings and conclusion. ``rel_path`` is not an indexed column, so this
    scans the table: about 0.3 s on 90k chunks, only for that kind of question.
    """
    wanted = str(rel_path or "").strip()
    if not wanted or not is_ready(index_path):
        return []
    connection = sqlite3.connect(lexical_db_path(index_path), timeout=5.0)
    try:
        rows = connection.execute(
            "SELECT content, metadata_json FROM lexical_chunks WHERE rel_path = ? ORDER BY rowid",
            (wanted,),
        ).fetchall()
    finally:
        connection.close()
    documents: list[Document] = []
    for content, metadata_json in rows:
        try:
            metadata = json.loads(str(metadata_json or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.setdefault("rel_path", wanted)
        documents.append(Document(page_content=str(content or ""), metadata=metadata))
    return documents


__all__ = [
    "delete_paths",
    "ensure_from_vectorstore",
    "file_chunks",
    "file_metadata",
    "is_ready",
    "lexical_db_path",
    "rebuild_from_vectorstore",
    "replace_paths",
    "search",
]
