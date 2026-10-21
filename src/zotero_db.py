"""
Zotero connector based on direct read-only access to zotero.sqlite.

The loader keeps the implementation stdlib-only so the application can enrich
PDFs, notes, tags and collections without extra runtime dependencies.
"""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import asdict, dataclass, field, fields
from functools import cache, lru_cache
from html import unescape
from typing import Any

_logger = logging.getLogger(__name__)

_ZOTERO_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")
# Records copied from another computer by an index import; used like a BibTeX file.
METADATA_SNAPSHOT_FORMAT = "bibliobrain-metadata"


@dataclass
class ZoteroAnnotation:
    kind: str = "annotation"
    text: str = ""
    comment: str = ""
    page_label: str = ""
    color: str = ""

    def as_text(self) -> str:
        parts: list[str] = []
        if self.text:
            parts.append(f"Highlighted text: {self.text}")
        if self.comment:
            parts.append(f"Researcher comment: {self.comment}")
        if self.page_label:
            parts.append(f"Page: {self.page_label}")
        return "\n".join(parts).strip()


@dataclass
class ZoteroItem:
    item_id: int = 0
    item_key: str = ""
    library_id: int = 1
    library_type: str = "user"
    group_id: int = 0
    attachment_id: int = 0
    attachment_key: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    journal: str = ""
    abstract: str = ""
    item_type: str = ""
    date_added: str = ""
    tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    doi: str = ""
    citation_key: str = ""
    citation_count: int = 0
    influential_citation_count: int = 0
    notes: list[str] = field(default_factory=list)
    annotations: list[ZoteroAnnotation] = field(default_factory=list)
    # Another record has a PDF of the same name and used to be matched in its
    # place: only a path can match this one (see load_zotero_library).
    shared_filename: bool = False

    def first_author(self) -> str:
        return self.authors[0] if self.authors else ""

    def authors_str(self) -> str:
        if not self.authors:
            return ""
        if len(self.authors) <= 3:
            return ", ".join(self.authors)
        return ", ".join(self.authors[:3]) + " et al."

    def citation_str(self) -> str:
        author = self.first_author() or "Unknown author"
        year = self.year or "n.d."
        title = self.title or "Untitled"
        journal = f" {self.journal}." if self.journal else ""
        return f"{author} ({year}). {title}.{journal}".strip()

    def citation_key_or_fallback(self) -> str:
        if self.citation_key:
            return self.citation_key
        if self.item_key:
            return self.item_key
        author = re.sub(r"[^A-Za-z0-9]+", "", self.first_author() or "item")
        year = re.sub(r"[^0-9]+", "", self.year or "") or "nd"
        return f"{author}{year}"

    def apa_citation(self) -> str:
        return self.citation_str()

    def _bibtex_type(self) -> str:
        normalized = (self.item_type or "").strip().lower()
        if normalized in {
            "article",
            "book",
            "booklet",
            "conference",
            "inbook",
            "incollection",
            "inproceedings",
            "manual",
            "mastersthesis",
            "misc",
            "phdthesis",
            "proceedings",
            "techreport",
            "thesis",
            "unpublished",
        }:
            return normalized
        mapping = {
            "journalArticle": "article",
            "conferencePaper": "inproceedings",
            "book": "book",
            "thesis": "phdthesis",
            "report": "techreport",
        }
        return mapping.get(self.item_type, "misc")

    def _bibtex_escape(self, value: str) -> str:
        return (value or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")

    def _bibtex_authors(self) -> str:
        if not self.authors:
            return ""
        return " and ".join(author.strip() for author in self.authors if author.strip())

    def bibtex_entry(self) -> str:
        entry_type = self._bibtex_type()
        key = re.sub(r"[^A-Za-z0-9:_\\-\\.]+", "", self.citation_key_or_fallback())
        fields: list[tuple[str, str]] = []
        authors = self._bibtex_authors()
        if authors:
            fields.append(("author", authors))
        if self.title:
            fields.append(("title", self.title))
        if self.journal:
            fields.append(("journal", self.journal))
        if self.year:
            fields.append(("year", self.year))
        if self.doi:
            fields.append(("doi", self.doi))

        if not fields:
            fields.append(("note", "No metadata available"))

        body = ",\n".join(f"  {name} = {{{self._bibtex_escape(value)}}}" for name, value in fields)
        return f"@{entry_type}{{{key},\n{body}\n}}"

    def all_note_texts(self) -> list[str]:
        texts = [text for text in self.notes if text]
        texts.extend(
            annotation.as_text() for annotation in self.annotations if annotation.as_text()
        )
        return texts

    def as_markdown(self) -> str:
        lines: list[str] = []
        if self.title:
            lines.append(f"## {self.title}")
        lines.append(f"**Citation:** {self.citation_str()}")
        if self.authors:
            lines.append(f"**Authors:** {', '.join(self.authors)}")
        if self.year:
            lines.append(f"**Year:** {self.year}")
        if self.journal:
            lines.append(f"**Publication:** {self.journal}")
        if self.doi:
            lines.append(f"**DOI:** [{self.doi}](https://doi.org/{self.doi})")
        if self.citation_count:
            lines.append(f"**Citations:** {self.citation_count}")
        if self.influential_citation_count:
            lines.append(f"**Influential citations:** {self.influential_citation_count}")
        lines.append(f"**Citation Key:** `{self.citation_key_or_fallback()}`")
        if self.item_type:
            lines.append(f"**Type:** {self.item_type}")
        if self.collections:
            lines.append(f"**Collections:** {' | '.join(self.collections)}")
        if self.tags:
            lines.append(f"**Tags:** {', '.join(self.tags)}")
        if self.abstract:
            lines.append(f"\n---\n\n**Abstract**\n\n{self.abstract}")

        note_texts = self.all_note_texts()
        if note_texts:
            preview = "\n\n".join(f"- {text[:500]}" for text in note_texts[:5])
            lines.append(f"\n---\n\n**Research notes and annotations**\n\n{preview}")

        return "\n\n".join(lines) if lines else "_No Zotero metadata available._"


def casefold_sort_key(value: str) -> tuple[str, str]:
    """Case-insensitive order that still ranks "HTS" and "hts" the same way every run.

    With ``key=str.lower`` alone, two tags differing only by case tie and keep
    the order of the set they came from, which changes with each process
    (string hashing is randomized): the Zotero signature then changed on every
    restart and the RAG reported documents as modified.
    """
    return (value.lower(), value)


def _clean_rich_text(value: str | None) -> str:
    if not value:
        return ""

    text = value
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|li|ul|ol|h[1-6])>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = text.replace("\xa0", " ")

    lines = []
    for line in text.splitlines():
        compact = re.sub(r"\s+", " ", line).strip()
        if compact:
            lines.append(compact)
    return "\n".join(lines)


def _extract_citation_key(extra_value: str | None, fallback_key: str = "") -> str:
    extra = _clean_rich_text(extra_value or "")
    if extra:
        patterns = [
            r"(?im)^citation key\s*:\s*([A-Za-z0-9:_\-.]+)\s*$",
            r"(?im)^citekey\s*:\s*([A-Za-z0-9:_\-.]+)\s*$",
        ]
        for pattern in patterns:
            match = re.search(pattern, extra)
            if match:
                return match.group(1).strip()
    return (fallback_key or "").strip()


def _extract_extra_int(extra_value: str | None, *labels: str) -> int:
    extra = _clean_rich_text(extra_value or "")
    if not extra:
        return 0
    for label in labels:
        pattern = rf"(?im)^\s*{re.escape(label)}\s*:\s*([0-9][0-9,.\s]*)\s*$"
        match = re.search(pattern, extra)
        if not match:
            continue
        digits = re.sub(r"[^0-9]+", "", match.group(1))
        if not digits:
            continue
        try:
            return max(0, min(int(digits), 10_000_000))
        except ValueError:
            continue
    return 0


_PREF_LINE_RE = re.compile(r'^\s*user_pref\(\s*"([^"]+)"\s*,\s*(.+?)\s*\)\s*;\s*$', re.MULTILINE)
_PREF_PREFIXES = ("extensions.zotero.", "extensions.zotmoov.")
_MAX_PREFS_BYTES = 4 * 1024 * 1024


def _zotero_prefs_files() -> list[str]:
    """prefs.js of each Zotero profile (Windows, macOS, Linux)."""
    files: list[str] = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        files += glob.glob(os.path.join(appdata, "Zotero", "Zotero", "Profiles", "*", "prefs.js"))
    home = os.path.expanduser("~")
    files += glob.glob(
        os.path.join(home, "Library", "Application Support", "Zotero", "Profiles", "*", "prefs.js")
    )
    files += glob.glob(os.path.join(home, ".zotero", "zotero", "*", "prefs.js"))
    return files


def read_zotero_prefs() -> dict[str, object]:
    """Zotero and ZotMoov preferences; the first profile wins on a clash."""
    prefs: dict[str, object] = {}
    for path in _zotero_prefs_files():
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read(_MAX_PREFS_BYTES)
        except OSError:
            continue
        for name, raw_value in _PREF_LINE_RE.findall(text):
            if not name.startswith(_PREF_PREFIXES) or name in prefs:
                continue
            try:
                prefs[name] = json.loads(raw_value)
            except ValueError:
                continue
    return prefs


def _pref_text(prefs: dict[str, object], name: str) -> str:
    value = prefs.get(name)
    return value.strip() if isinstance(value, str) else ""


def find_zotero_db() -> str | None:
    """Searches standard locations for zotero.sqlite.

    A data directory chosen in Zotero's settings comes first, then the default
    one (~/Zotero), then the profile folder Zotero 4 used.
    """
    candidates: list[str] = []

    prefs = read_zotero_prefs()
    data_dir = _pref_text(prefs, "extensions.zotero.dataDir")
    if data_dir and prefs.get("extensions.zotero.useDataDir") is not False:
        configured = os.path.join(data_dir, "zotero.sqlite")
        if os.path.isfile(configured):
            candidates.append(configured)

    home = os.path.expanduser("~")
    candidates += glob.glob(os.path.join(home, "Zotero", "zotero.sqlite"))

    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates += glob.glob(
            os.path.join(
                appdata,
                "Zotero",
                "Zotero",
                "profiles",
                "*",
                "zotero",
                "zotero.sqlite",
            )
        )

    candidates += glob.glob(
        os.path.join(
            home,
            "Library",
            "Application Support",
            "Zotero",
            "Profiles",
            "*",
            "zotero",
            "zotero.sqlite",
        )
    )
    candidates += glob.glob(os.path.join(home, ".zotero", "zotero", "*", "zotero.sqlite"))

    return candidates[0] if candidates else None


def find_zotero_storage(db_path: str) -> str | None:
    """Returns the Zotero storage/ directory next to zotero.sqlite."""
    storage = os.path.join(os.path.dirname(db_path), "storage")
    return storage if os.path.isdir(storage) else None


def _is_within(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(folder))]
        ) == os.path.normcase(os.path.abspath(folder))
    except ValueError:
        return False


def _too_broad_for_pdf_folder(folder: str) -> bool:
    """A drive root or the home folder would index far more than papers."""
    normalized = os.path.normcase(os.path.abspath(folder))
    home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    return normalized == home or os.path.dirname(normalized) == normalized


def find_zotero_linked_pdf_folder(
    db_path: str, *, prefs: dict[str, object] | None = None
) -> str | None:
    """Folder holding the PDFs Zotero only links to, when most of them are.

    ZotMoov (and Zotero's "linked files") moves PDFs out of storage/, which
    then keeps only what was not moved. Returns ZotMoov's destination folder,
    or else the common folder of the linked PDFs, when at least half of them
    sit inside it. None when most PDFs are still in storage/.
    """
    if not db_path or is_bibtex_source(db_path):
        return None
    try:
        conn = _open_db(db_path)
    except (OSError, ValueError, sqlite3.Error):
        return None
    try:
        rows = conn.execute(
            """
            SELECT ia.path
            FROM itemAttachments ia
            LEFT JOIN deletedItems deleted ON deleted.itemID = ia.itemID
            WHERE ia.contentType = 'application/pdf'
              AND ia.parentItemID IS NOT NULL
              AND deleted.itemID IS NULL
            """
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    prefs = read_zotero_prefs() if prefs is None else prefs
    base_dir = _pref_text(prefs, "extensions.zotero.baseAttachmentPath")
    stored = 0
    linked = 0
    linked_dirs: list[str] = []
    for (raw_path,) in rows:
        path = str(raw_path or "").strip()
        if not path:
            continue
        if path.startswith("storage:"):
            stored += 1
            continue
        linked += 1
        resolved = _linked_attachment_path(path, base_dir)
        if resolved:
            linked_dirs.append(os.path.dirname(os.path.normpath(resolved)))
    if linked <= stored:
        return None

    candidates: list[str] = []
    zotmoov_dir = _pref_text(prefs, "extensions.zotmoov.dst_dir")
    if zotmoov_dir:
        candidates.append(zotmoov_dir)
    if linked_dirs:
        try:
            candidates.append(os.path.commonpath(linked_dirs))
        except ValueError:
            pass  # PDFs spread over several drives
    for folder in candidates:
        if not os.path.isdir(folder) or _too_broad_for_pdf_folder(folder):
            continue
        if not linked_dirs and folder == zotmoov_dir:
            return folder
        inside = sum(1 for directory in linked_dirs if _is_within(directory, folder))
        if linked_dirs and inside * 2 >= len(linked_dirs):
            return folder
    return None


def suggest_zotero_pdf_folder(db_path: str) -> str | None:
    """Where this Zotero library's PDFs are: linked-files folder, else storage/."""
    if not db_path or is_bibtex_source(db_path):
        return None
    return find_zotero_linked_pdf_folder(db_path) or find_zotero_storage(db_path)


def is_bibtex_source(path: str) -> bool:
    """Return whether a user-selected file is a BibTeX export.

    Besides the usual extensions, accept text exports when their contents look
    like BibTeX. This keeps SQLite validation strict while supporting exports
    saved by browsers or editors as ``.txt``.
    """
    candidate = str(path or "").strip()
    extension = os.path.splitext(candidate)[1].lower()
    if extension in (".bib", ".bibtex") or is_metadata_snapshot(candidate):
        return True
    if extension in (".sqlite", ".sqlite3") or not os.path.isfile(candidate):
        return False
    try:
        with open(candidate, "rb") as handle:
            sample = handle.read(512 * 1024)
    except OSError:
        return False
    if not sample or b"\x00" in sample:
        return False
    text = sample.decode("utf-8-sig", errors="replace")
    return bool(re.search(r"(?im)^\s*@[a-z][\w-]*\s*[\{\(]", text))


def is_metadata_snapshot(path: str) -> bool:
    """Whether ``path`` is a records snapshot written by an index import.

    Such a file is a metadata source like a BibTeX export: ``is_bibtex_source``
    accepts it, so it is never opened as a Zotero database.
    """
    candidate = str(path or "").strip()
    if not candidate.lower().endswith(".json") or not os.path.isfile(candidate):
        return False
    try:
        with open(candidate, "rb") as handle:
            sample = handle.read(256)
    except OSError:
        return False
    return METADATA_SNAPSHOT_FORMAT.encode("ascii") in sample


def item_to_snapshot(item: ZoteroItem) -> dict[str, Any]:
    return asdict(item)


def _item_from_snapshot(data: dict[str, Any]) -> ZoteroItem:
    """Rebuild a record; unknown fields (a newer version's) are ignored."""
    known = {item_field.name for item_field in fields(ZoteroItem)}
    values: dict[str, Any] = {key: value for key, value in data.items() if key in known}
    annotation_fields = {item_field.name for item_field in fields(ZoteroAnnotation)}
    values["annotations"] = [
        ZoteroAnnotation(**{key: value for key, value in annotation.items() if key in annotation_fields})
        for annotation in values.get("annotations") or []
        if isinstance(annotation, dict)
    ]
    return ZoteroItem(**values)


def write_metadata_snapshot(path: str, items: dict[str, dict[str, Any]], *, origin: str = "") -> None:
    """Write records keyed by document path; the format marker comes first."""
    payload = {"format": METADATA_SNAPSHOT_FORMAT, "format_version": 1, "origin": origin, "items": items}
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = f"{path}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(temporary, path)


def snapshot_items(payload: Any) -> dict[str, ZoteroItem]:
    """The library of a snapshot payload, keyed like ``lookup_zotero_item`` expects."""
    if not isinstance(payload, dict) or payload.get("format") != METADATA_SNAPSHOT_FORMAT:
        raise RuntimeError("Not a Bibliobrain records snapshot.")
    items = payload.get("items")
    library: dict[str, ZoteroItem] = {}
    for rel_path, data in (items if isinstance(items, dict) else {}).items():
        key = str(rel_path or "").replace("\\", "/").strip("/").lower()
        if key and isinstance(data, dict):
            library[key] = _item_from_snapshot(data)
    return library


def _load_metadata_snapshot(path: str) -> dict[str, ZoteroItem]:
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Impossible to open the records snapshot: {exc}") from exc
    return snapshot_items(payload)


def is_zotero_storage_path(data_path: str, db_path: str | None) -> bool:
    """Return True when ``data_path`` points to the storage/ folder for ``db_path``."""
    if not data_path or not db_path:
        return False
    storage = find_zotero_storage(db_path)
    if not storage:
        return False
    try:
        return os.path.normcase(os.path.realpath(data_path)) == os.path.normcase(
            os.path.realpath(storage)
        )
    except Exception:
        _logger.debug("Recovered from an error in is_zotero_storage_path.", exc_info=True)
        return False


def _is_zotero_key(value: str) -> bool:
    return bool(_ZOTERO_KEY_RE.match(str(value or "").upper()))


def lookup_zotero_item(
    rel_path: str,
    library: dict[str, ZoteroItem],
    full_path: str | None = None,
) -> ZoteroItem | None:
    """Resolves a document path to its Zotero item, most precise match first.

    The path decides before the file name: the attachment key folder of
    storage/, then a linked file's absolute path, then the file name, which
    the library only keeps when a single record uses it.
    """
    if not library:
        return None

    normalized_rel_path = str(rel_path or "").replace("\\", "/").strip("/").lower()
    if normalized_rel_path in library:
        return library[normalized_rel_path]

    if full_path:
        linked = library.get(zotero_path_key(full_path))
        if linked is not None:
            return linked

    original_parts = [part for part in str(rel_path or "").replace("\\", "/").split("/") if part]
    for part in original_parts[:-1]:
        # Zotero names each storage/ folder after the attachment key, in upper
        # case. An unknown key there is an attachment Zotero no longer has:
        # no file-name guess for it. A lower-case "articles" is just a folder.
        if part.isupper() and _is_zotero_key(part):
            return library.get(part.lower())

    parts = [part for part in normalized_rel_path.split("/") if part]
    for index in range(len(parts) - 1):
        tail = "/".join(parts[index:])
        if tail in library:
            return library[tail]

    filename = os.path.basename(normalized_rel_path)
    if filename in library:
        return library[filename]

    stem = os.path.splitext(filename)[0]
    if stem and stem in library:
        return library[stem]

    return None


# ───────────────── Validation stricte du chemin SQLite (sécurité) ────────────────


def filter_active_zotero_pdf_entries(
    pdf_entries: list[tuple[str, str]],
    library: dict[str, ZoteroItem],
    *,
    data_path: str,
    zotero_db_path: str | None,
) -> list[tuple[str, str]]:
    """Keep only active Zotero attachments when scanning Zotero's storage folder."""
    if not is_zotero_storage_path(data_path, zotero_db_path):
        return list(pdf_entries)
    if not library:
        return list(pdf_entries)
    return [
        (rel_path, full_path)
        for rel_path, full_path in pdf_entries
        if lookup_zotero_item(rel_path, library, full_path) is not None
    ]


def _validate_sqlite_path(db_path: str) -> str:
    """Valide strictement le chemin vers la base SQLite avant connexion.

    Verifications :
    1. Le chemin n'est pas vide
    2. Resolution des symlinks via os.path.realpath
    3. Le fichier existe et est un fichier regulier (pas un dossier)
    4. L'extension est .sqlite ou .sqlite3
    """
    if not db_path or not str(db_path).strip():
        raise ValueError("Chemin de base de donnees vide.")
    resolved_path = os.path.realpath(str(db_path).strip())
    if not os.path.isfile(resolved_path):
        raise FileNotFoundError(f"Base de donnees introuvable: {resolved_path}")
    _, ext = os.path.splitext(resolved_path)
    if ext.lower() not in (".sqlite", ".sqlite3"):
        raise ValueError(
            f"Extension non autorisee: {ext}. Seules les bases .sqlite/.sqlite3 sont acceptees."
        )
    return resolved_path


def _open_db(db_path: str) -> sqlite3.Connection:
    """Ouvre la base SQLite en lecture seule apres validation stricte du chemin."""
    safe_path = _validate_sqlite_path(db_path)
    conn = sqlite3.connect(
        f"file:{safe_path}?mode=ro&immutable=1",
        uri=True,
        check_same_thread=False,
        timeout=5.0,
    )
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda value: value.decode("utf-8", errors="ignore")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def load_zotero_user_library_uri(db_path: str) -> str:
    """URI prefix Zotero gives items of the personal library, or "".

    ``http://zotero.org/users/<userID>`` once the library is synced,
    ``http://zotero.org/users/local/<localUserKey>`` otherwise. Word citation
    fields point at items through ``<prefix>/items/<key>``.
    """
    if not db_path or is_bibtex_source(db_path):
        return ""
    try:
        conn = _open_db(db_path)
    except Exception:
        return ""
    try:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE setting = 'account' AND key IN ('userID', 'localUserKey')"
        ).fetchall()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    account = {str(row["key"]): str(row["value"] or "").strip() for row in rows}
    if re.fullmatch(r"\d+", account.get("userID", "")):
        return f"http://zotero.org/users/{account['userID']}"
    if re.fullmatch(r"[A-Za-z0-9]+", account.get("localUserKey", "")):
        return f"http://zotero.org/users/local/{account['localUserKey']}"
    return ""


def _strip_outer_wrappers(value: str) -> str:
    text = str(value or "").strip()
    while len(text) >= 2:
        if text[0] == '"' and text[-1] == '"':
            text = text[1:-1].strip()
            continue
        if text[0] == "{" and text[-1] == "}":
            depth = 0
            balanced = True
            for index, char in enumerate(text):
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0 and index != len(text) - 1:
                        balanced = False
                        break
                    if depth < 0:
                        balanced = False
                        break
            if balanced and depth == 0:
                text = text[1:-1].strip()
                continue
        break
    return text


def _clean_bibtex_text(value: str | None) -> str:
    text = _strip_outer_wrappers(str(value or ""))
    text = text.replace("\\\\", "\\").replace('\\"', '"')
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s+", " ", text).strip()


def _split_top_level_csv(value: str) -> list[str]:
    text = str(value or "")
    if not text.strip():
        return []
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote = False
    escape = False
    for char in text:
        if escape:
            current.append(char)
            escape = False
            continue
        if char == "\\":
            current.append(char)
            escape = True
            continue
        if char == '"' and depth == 0:
            quote = not quote
            current.append(char)
            continue
        if not quote:
            if char == "{":
                depth += 1
            elif char == "}":
                depth = max(0, depth - 1)
            elif char == "," and depth == 0:
                part = "".join(current).strip()
                if part:
                    parts.append(part)
                current = []
                continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _parse_bibtex_value_part(text: str, start: int) -> tuple[str, int]:
    if start >= len(text):
        return "", start

    if text[start] == "{":
        depth = 0
        index = start
        while index < len(text):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    index += 1
                    break
            index += 1
        return text[start:index], index

    if text[start] == '"':
        index = start + 1
        escape = False
        while index < len(text):
            char = text[index]
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                index += 1
                break
            index += 1
        return text[start:index], index

    index = start
    while index < len(text) and text[index] not in ",#\r\n":
        index += 1
    return text[start:index].strip(), index


def _parse_bibtex_fields(body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    index = 0
    length = len(body)

    while index < length:
        while index < length and body[index] in " \t\r\n,":
            index += 1
        if index >= length:
            break

        name_start = index
        while index < length and (body[index].isalnum() or body[index] in "-_:."):
            index += 1
        if index == name_start:
            index += 1
            continue

        name = body[name_start:index].strip().lower()
        while index < length and body[index].isspace():
            index += 1
        if index >= length or body[index] != "=":
            continue
        index += 1

        parts: list[str] = []
        while index < length:
            while index < length and body[index].isspace():
                index += 1
            value_part, index = _parse_bibtex_value_part(body, index)
            if value_part:
                parts.append(value_part)
            while index < length and body[index].isspace():
                index += 1
            if index < length and body[index] == "#":
                index += 1
                continue
            break

        fields[name] = "".join(parts).strip()

        while index < length and body[index].isspace():
            index += 1
        if index < length and body[index] == ",":
            index += 1

    return fields


def _split_bibtex_entry_content(content: str) -> tuple[str, str]:
    depth = 0
    quote = False
    escape = False
    for index, char in enumerate(content):
        if escape:
            escape = False
            continue
        if char == "\\":
            escape = True
            continue
        if char == '"' and depth == 0:
            quote = not quote
            continue
        if quote:
            continue
        if char == "{":
            depth += 1
            continue
        if char == "}":
            depth = max(0, depth - 1)
            continue
        if char == "," and depth == 0:
            return content[:index].strip(), content[index + 1 :]
    return content.strip(), ""


def _iter_bibtex_entries(raw_text: str):
    index = 0
    length = len(raw_text)
    ignored_types = {"comment", "preamble", "string"}

    while index < length:
        at_index = raw_text.find("@", index)
        if at_index < 0:
            return

        index = at_index + 1
        while index < length and raw_text[index].isspace():
            index += 1
        type_start = index
        while index < length and (raw_text[index].isalnum() or raw_text[index] in "-_"):
            index += 1
        entry_type = raw_text[type_start:index].strip().lower()
        while index < length and raw_text[index].isspace():
            index += 1
        if index >= length or raw_text[index] not in "{(":
            continue

        opening = raw_text[index]
        closing = "}" if opening == "{" else ")"
        depth = 1
        index += 1
        body_start = index
        quote = False
        escape = False

        while index < length and depth > 0:
            char = raw_text[index]
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                quote = not quote
            elif not quote:
                if char == opening:
                    depth += 1
                elif char == closing:
                    depth -= 1
            index += 1

        if depth != 0:
            return

        content = raw_text[body_start : index - 1]
        if entry_type and entry_type not in ignored_types:
            yield entry_type, content


def _split_bibtex_authors(value: str) -> list[str]:
    authors = [
        _clean_bibtex_text(part)
        for part in re.split(r"\s+and\s+", str(value or ""), flags=re.IGNORECASE)
    ]
    return [author for author in authors if author]


def _extract_bibtex_year(fields: dict[str, str]) -> str:
    for key in ("year", "date"):
        raw_value = _clean_bibtex_text(fields.get(key, ""))
        match = re.search(r"(19|20)\d{2}", raw_value)
        if match:
            return match.group(0)
    return ""


def _extract_bibtex_tags(fields: dict[str, str]) -> list[str]:
    raw_keywords = _clean_bibtex_text(fields.get("keywords", "") or fields.get("keyword", ""))
    if not raw_keywords:
        return []
    if ";" in raw_keywords or "|" in raw_keywords:
        chunks = re.split(r"[;|]", raw_keywords)
    else:
        chunks = _split_top_level_csv(raw_keywords)
    return sorted(
        {chunk.strip() for chunk in chunks if chunk and chunk.strip()}, key=casefold_sort_key
    )


def _extract_bibtex_collections(fields: dict[str, str]) -> list[str]:
    raw_value = _clean_bibtex_text(
        fields.get("groups", "")
        or fields.get("group", "")
        or fields.get("collections", "")
        or fields.get("collection", "")
    )
    if not raw_value:
        return []
    if ";" in raw_value or "|" in raw_value:
        chunks = re.split(r"[;|]", raw_value)
    else:
        chunks = _split_top_level_csv(raw_value)
    return sorted(
        {chunk.strip() for chunk in chunks if chunk and chunk.strip()}, key=casefold_sort_key
    )


def _extract_pdf_candidates(value: str) -> list[str]:
    raw_value = _strip_outer_wrappers(str(value or ""))
    if ".pdf" not in raw_value.lower():
        return []

    matches = re.findall(r"([^;\n\r]+?\.pdf)", raw_value, flags=re.IGNORECASE)
    candidates: list[str] = []
    seen: set[str] = set()
    for match in matches:
        candidate = match.strip().strip('"').strip("{}").lstrip(":")
        candidate = candidate.replace("\\\\", "\\")
        candidate = re.split(r"[?#]", candidate, maxsplit=1)[0]
        normalized = candidate.replace("\\", "/").strip().strip("/")
        if normalized and normalized.lower() not in seen:
            seen.add(normalized.lower())
            candidates.append(normalized)
    return candidates


def _build_bibtex_lookup_keys(fields: dict[str, str], citation_key: str) -> set[str]:
    keys: set[str] = set()
    normalized_citation_key = str(citation_key or "").strip().lower()
    if normalized_citation_key:
        keys.add(normalized_citation_key)
        keys.add(f"{normalized_citation_key}.pdf")

    for field_name in ("file", "files", "pdf", "attachment", "attachments", "local-url", "url"):
        for candidate in _extract_pdf_candidates(fields.get(field_name, "")):
            normalized = candidate.replace("\\", "/").strip().strip("/").lower()
            if not normalized:
                continue
            keys.add(normalized)
            basename = os.path.basename(normalized)
            if basename:
                keys.add(basename)
                stem = os.path.splitext(basename)[0]
                if stem:
                    keys.add(stem)
            parts = [part for part in normalized.split("/") if part]
            for index in range(1, len(parts)):
                keys.add("/".join(parts[index:]))

    return {key for key in keys if key}


def _load_bibtex_library(bib_path: str) -> dict[str, ZoteroItem]:
    try:
        with open(bib_path, encoding="utf-8") as handle:
            raw_text = handle.read()
    except UnicodeDecodeError:
        with open(bib_path, encoding="latin-1") as handle:
            raw_text = handle.read()
    except OSError as exc:
        raise RuntimeError(f"Impossible to open BibTeX file: {exc}") from exc

    result: dict[str, ZoteroItem] = {}
    next_item_id = 1

    for entry_type, content in _iter_bibtex_entries(raw_text):
        citation_key, body = _split_bibtex_entry_content(content)
        if not citation_key:
            continue
        fields = _parse_bibtex_fields(body)
        journal = _clean_bibtex_text(
            fields.get("journal", "")
            or fields.get("journaltitle", "")
            or fields.get("booktitle", "")
            or fields.get("publisher", "")
        )
        item = ZoteroItem(
            item_id=next_item_id,
            title=_clean_bibtex_text(fields.get("title", "")),
            authors=_split_bibtex_authors(fields.get("author", "")),
            year=_extract_bibtex_year(fields),
            journal=journal,
            abstract=_clean_bibtex_text(
                fields.get("abstract", "") or fields.get("abstractnote", "")
            ),
            item_type=(entry_type or "misc").strip(),
            tags=_extract_bibtex_tags(fields),
            collections=_extract_bibtex_collections(fields),
            doi=_clean_bibtex_text(fields.get("doi", "")),
            citation_key=str(citation_key).strip(),
            notes=[
                text
                for text in (
                    _clean_bibtex_text(fields.get("annote", "")),
                    _clean_bibtex_text(fields.get("note", "")),
                )
                if text
            ],
        )
        next_item_id += 1

        for key in _build_bibtex_lookup_keys(fields, item.citation_key):
            result[key] = item

    return result


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dropped_standalone_pdfs(
    cur: sqlite3.Cursor,
    db_path: str,
    dropped_pdfs: frozenset[tuple[int, str]],
    date_added_column: bool,
) -> list[dict]:
    """Standalone PDF attachments holding a PDF dropped into Bibliobrain.

    Zotero keeps a PDF it found no record for as a standalone attachment. Only
    the ones whose ``(size, sha256)`` is in ``dropped_pdfs`` are returned, so
    a renamed file is still found and any other standalone PDF stays out of
    the index. Each row stands in for its own parent item.
    """
    storage = find_zotero_storage(db_path)
    if not storage:
        return []
    date_added_expr = "attachment.dateAdded" if date_added_column else "''"
    cur.execute(
        f"""
        SELECT
            ia.itemID AS attachment_id,
            ia.itemID AS parent_id,
            ia.path AS attachment_path,
            attachment.key AS attachment_key,
            attachment.key AS parent_key,
            attachment.libraryID AS library_id,
            {date_added_expr} AS date_added
        FROM itemAttachments ia
        JOIN items attachment ON attachment.itemID = ia.itemID
        LEFT JOIN deletedItems deleted_attachment
          ON deleted_attachment.itemID = attachment.itemID
        WHERE ia.contentType = 'application/pdf'
          AND ia.parentItemID IS NULL
          AND ia.path LIKE 'storage:%'
          AND deleted_attachment.itemID IS NULL
        """
    )
    sizes = {size for size, _sha256 in dropped_pdfs}
    found: list[dict] = []
    for row in cur.fetchall():
        name = os.path.basename(row["attachment_path"][len("storage:"):].strip())
        path = os.path.join(storage, row["attachment_key"] or "", name)
        try:
            # Only a file of a remembered size is read through.
            size = os.path.getsize(path)
            if size in sizes and (size, _file_sha256(path)) in dropped_pdfs:
                found.append({**dict(row), "standalone": True})
        except OSError:
            continue
    return found


@lru_cache(maxsize=4)
def load_zotero_library(
    db_path: str, dropped_pdfs: frozenset[tuple[int, str]] = frozenset()
) -> dict[str, ZoteroItem]:
    """
    Loads all Zotero items that have a PDF attachment.

    A standalone PDF (no parent item) is loaded only when its ``(size, sha256)``
    is in ``dropped_pdfs``: see ``src/zotero_drops.py``.

    Returns a lookup dict keyed by lowercase PDF filename and attachment key.
    """
    if not db_path:
        return {}
    if is_metadata_snapshot(db_path):
        return _load_metadata_snapshot(str(db_path).strip())
    if is_bibtex_source(db_path):
        return _load_bibtex_library(str(db_path).strip())

    try:
        conn = _open_db(db_path)
        cur = conn.cursor()
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "lock" in msg or "busy" in msg:
            raise RuntimeError(
                "Zotero database is locked (the Zotero app may be running). "
                "Please close Zotero and retry."
            ) from exc
        raise RuntimeError(f"Impossible to open zotero.sqlite: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"Impossible to open zotero.sqlite: {exc}") from exc

    try:
        cur.execute("PRAGMA table_info(items)")
        item_columns = {str(row["name"]) for row in cur.fetchall()}
        date_added_expr = (
            "parent.dateAdded AS date_added"
            if "dateAdded" in item_columns
            else "'' AS date_added"
        )
        cur.execute(
            f"""
            SELECT
                ia.itemID AS attachment_id,
                ia.parentItemID AS parent_id,
                ia.path AS attachment_path,
                attachment.key AS attachment_key,
                parent.key AS parent_key,
                parent.libraryID AS library_id,
                {date_added_expr}
            FROM itemAttachments ia
            JOIN items attachment ON attachment.itemID = ia.itemID
            JOIN items parent ON parent.itemID = ia.parentItemID
            LEFT JOIN deletedItems deleted_attachment
              ON deleted_attachment.itemID = attachment.itemID
            LEFT JOIN deletedItems deleted_parent
              ON deleted_parent.itemID = parent.itemID
            WHERE ia.contentType = 'application/pdf'
              AND ia.parentItemID IS NOT NULL
              AND deleted_attachment.itemID IS NULL
              AND deleted_parent.itemID IS NULL
            """
        )
        attachments = [dict(row) for row in cur.fetchall()]
        if dropped_pdfs:
            attachments.extend(
                _dropped_standalone_pdfs(cur, db_path, dropped_pdfs, "dateAdded" in item_columns)
            )
        if not attachments:
            return {}

        parent_ids = sorted({row["parent_id"] for row in attachments})
        attachment_ids = sorted({row["attachment_id"] for row in attachments})
        parent_placeholders = ",".join("?" * len(parent_ids))
        attachment_placeholders = ",".join("?" * len(attachment_ids))

        cur.execute("SELECT libraryID, type FROM libraries")
        library_types = {
            int(row["libraryID"]): (row["type"] or "").strip().lower() for row in cur.fetchall()
        }

        cur.execute("SELECT libraryID, groupID FROM groups")
        group_ids_by_library = {
            int(row["libraryID"]): int(row["groupID"]) for row in cur.fetchall()
        }

        cur.execute(
            f"""
            SELECT id.itemID, f.fieldName, idv.value
            FROM itemData id
            JOIN fields f ON id.fieldID = f.fieldID
            JOIN itemDataValues idv ON id.valueID = idv.valueID
            WHERE id.itemID IN ({parent_placeholders})
              AND f.fieldName IN (
                  'title', 'date', 'publicationTitle', 'journalAbbreviation',
                  'abstractNote', 'DOI', 'bookTitle', 'conferenceName', 'extra'
              )
            """,
            parent_ids,
        )
        fields_by_item: dict[int, dict[str, str]] = {}
        for row in cur.fetchall():
            fields_by_item.setdefault(row["itemID"], {})[row["fieldName"]] = row["value"]

        cur.execute(
            f"""
            SELECT ic.itemID, c.lastName, c.firstName
            FROM itemCreators ic
            JOIN creators c ON ic.creatorID = c.creatorID
            JOIN creatorTypes ct ON ic.creatorTypeID = ct.creatorTypeID
            WHERE ic.itemID IN ({parent_placeholders})
              AND ct.creatorType = 'author'
            ORDER BY ic.itemID, ic.orderIndex
            """,
            parent_ids,
        )
        authors_by_item: dict[int, list[str]] = {}
        for row in cur.fetchall():
            last_name = (row["lastName"] or "").strip()
            first_name = (row["firstName"] or "").strip()
            if last_name and first_name:
                name = f"{last_name}, {first_name}"
            else:
                name = last_name or first_name
            if name:
                authors_by_item.setdefault(row["itemID"], []).append(name)

        cur.execute(
            f"""
            SELECT it.itemID, t.name
            FROM itemTags it
            JOIN tags t ON it.tagID = t.tagID
            WHERE it.itemID IN ({parent_placeholders})
            ORDER BY LOWER(t.name)
            """,
            parent_ids,
        )
        tags_by_item: dict[int, list[str]] = {}
        for row in cur.fetchall():
            tags_by_item.setdefault(row["itemID"], []).append(row["name"])

        cur.execute(
            f"""
            SELECT i.itemID, it.typeName
            FROM items i
            JOIN itemTypes it ON i.itemTypeID = it.itemTypeID
            WHERE i.itemID IN ({parent_placeholders})
            """,
            parent_ids,
        )
        types_by_item = {row["itemID"]: row["typeName"] for row in cur.fetchall()}

        cur.execute("SELECT collectionID, collectionName, parentCollectionID FROM collections")
        collections = cur.fetchall()
        collections_by_id = {
            row["collectionID"]: {
                "name": row["collectionName"],
                "parent_id": row["parentCollectionID"],
            }
            for row in collections
        }

        @cache
        def collection_path(collection_id: int) -> str:
            collection = collections_by_id.get(collection_id)
            if not collection:
                return ""
            parent_id = collection["parent_id"]
            name = collection["name"] or "Untitled collection"
            if not parent_id:
                return name
            parent_path = collection_path(parent_id)
            return f"{parent_path} / {name}" if parent_path else name

        cur.execute(
            f"""
            SELECT itemID, collectionID
            FROM collectionItems
            WHERE itemID IN ({parent_placeholders})
            """,
            parent_ids,
        )
        collections_by_item: dict[int, list[str]] = {}
        for row in cur.fetchall():
            path = collection_path(row["collectionID"])
            if path:
                collections_by_item.setdefault(row["itemID"], []).append(path)

        cur.execute(
            f"""
            SELECT parentItemID, title, note
            FROM itemNotes
            WHERE parentItemID IN ({parent_placeholders})
              AND note IS NOT NULL
              AND note != ''
            """,
            parent_ids,
        )
        notes_by_item: dict[int, list[str]] = {}
        for row in cur.fetchall():
            text = _clean_rich_text(row["note"])
            if row["title"]:
                title = _clean_rich_text(row["title"])
                if title and title not in text:
                    text = f"{title}\n{text}".strip()
            if text:
                notes_by_item.setdefault(row["parentItemID"], []).append(text)

        attachment_to_parent = {row["attachment_id"]: row["parent_id"] for row in attachments}
        cur.execute(
            f"""
            SELECT parentItemID, text, comment, color, pageLabel
            FROM itemAnnotations
            WHERE parentItemID IN ({attachment_placeholders})
            """,
            attachment_ids,
        )
        annotations_by_item: dict[int, list[ZoteroAnnotation]] = {}
        for row in cur.fetchall():
            parent_item_id = attachment_to_parent.get(row["parentItemID"])
            if not parent_item_id:
                continue
            annotation = ZoteroAnnotation(
                text=_clean_rich_text(row["text"]),
                comment=_clean_rich_text(row["comment"]),
                page_label=(row["pageLabel"] or "").strip(),
                color=(row["color"] or "").strip(),
            )
            if annotation.as_text():
                annotations_by_item.setdefault(parent_item_id, []).append(annotation)

    finally:
        conn.close()

    result: dict[str, ZoteroItem] = {}
    filename_owners: dict[str, list[ZoteroItem]] = {}
    base_dir = _pref_text(read_zotero_prefs(), "extensions.zotero.baseAttachmentPath")

    for row in attachments:
        parent_id = row["parent_id"]
        field_data = fields_by_item.get(parent_id, {})
        raw_date = field_data.get("date", "")
        year_match = re.search(r"(19|20)\d{2}", raw_date)
        year = year_match.group(0) if year_match else ""

        journal = (
            field_data.get("publicationTitle")
            or field_data.get("journalAbbreviation")
            or field_data.get("bookTitle")
            or field_data.get("conferenceName")
            or ""
        )
        extra = field_data.get("extra", "")
        attachment_path = row["attachment_path"] or ""
        standalone = row.get("standalone", False)
        title = field_data.get("title", "")
        if standalone:
            # Zotero titles it after the file: the name without ".pdf" reads better.
            title = re.sub(r"\.pdf$", "", title or os.path.basename(attachment_path), flags=re.I)

        item = ZoteroItem(
            item_id=parent_id,
            item_key=row["parent_key"] or "",
            library_id=int(row["library_id"] or 1),
            library_type=library_types.get(int(row["library_id"] or 1), "user"),
            group_id=group_ids_by_library.get(int(row["library_id"] or 1), 0),
            attachment_id=row["attachment_id"],
            attachment_key=row["attachment_key"] or "",
            title=title,
            authors=authors_by_item.get(parent_id, []),
            year=year,
            journal=journal,
            abstract=_clean_rich_text(field_data.get("abstractNote", "")),
            item_type="document" if standalone else types_by_item.get(parent_id, ""),
            date_added=str(row["date_added"] or "").strip(),
            tags=sorted(set(tags_by_item.get(parent_id, [])), key=casefold_sort_key),
            collections=sorted(
                set(collections_by_item.get(parent_id, [])),
                key=casefold_sort_key,
            ),
            doi=(field_data.get("DOI", "") or "").strip(),
            citation_key=_extract_citation_key(
                extra,
                fallback_key=row["parent_key"] or "",
            ),
            citation_count=_extract_extra_int(
                extra,
                "citation_count",
                "citation count",
                "citations",
                "cited_by_count",
                "cited by count",
                "cited by",
                "Semantic Scholar citationCount",
                "OpenAlex cited_by_count",
                "Crossref is-referenced-by-count",
            ),
            influential_citation_count=_extract_extra_int(
                extra,
                "influential_citation_count",
                "influential citation count",
                "influential citations",
                "influentialCitationCount",
                "Semantic Scholar influentialCitationCount",
            ),
            notes=notes_by_item.get(parent_id, []),
            annotations=annotations_by_item.get(parent_id, []),
        )

        filename = os.path.basename(attachment_path.replace("storage:", "").strip()).lower()
        if filename:
            owners = filename_owners.setdefault(filename, [])
            # A dropped standalone PDF never competes with a real record.
            if not (standalone and owners):
                owners.append(item)

        attachment_key = (row["attachment_key"] or "").lower()
        if attachment_key:
            result[attachment_key] = item

        linked_path = _linked_attachment_path(attachment_path, base_dir)
        if linked_path:
            result[zotero_path_key(linked_path)] = item

    # A file name is only a fallback for files found outside storage/ and away
    # from their linked path: shared by several records, it would pick one at
    # random, so those records are told apart by path or not at all.
    for filename, owners in filename_owners.items():
        winner = owners[-1]
        if len({owner.item_key or owner.item_id for owner in owners}) == 1:
            result.setdefault(filename, winner)
            continue
        # Matching by name used to give every such file the last record read.
        # Files of the other records were indexed under the wrong record.
        for owner in owners:
            if (owner.item_key or owner.item_id) != (winner.item_key or winner.item_id):
                owner.shared_filename = True

    return result


def zotero_path_key(path: str) -> str:
    """Library key for a linked attachment's absolute path."""
    return "path:" + os.path.normcase(os.path.normpath(str(path))).replace("\\", "/")


def _linked_attachment_path(attachment_path: str, base_dir: str) -> str:
    """Absolute path of a linked file; empty for files in storage/."""
    path = str(attachment_path or "").strip()
    if not path or path.startswith("storage:"):
        return ""
    if path.startswith("attachments:"):
        if not base_dir:
            return ""
        path = os.path.join(base_dir, path[len("attachments:") :])
    return path if os.path.isabs(path) else ""
