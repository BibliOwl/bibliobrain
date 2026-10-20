"""Safe, dependency-light extraction for documents indexed by Bibliobrain.

Each format keeps its natural navigation unit (page, section, chapter, or
slide).  The RAG pipeline remains format-agnostic after this extraction step.
"""

from __future__ import annotations

import html
import os
import posixpath
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import islice
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from src.document_guard import DocumentRejected, check_document
from src.zotero_db import ZoteroItem, lookup_zotero_item

SUPPORTED_DOCUMENT_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".docx",
        ".odt",
        ".epub",
        ".pptx",
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".tex",
        ".csv",
        ".tsv",
    }
)

_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown", ".rst", ".tex", ".csv", ".tsv"})
_MAX_ARCHIVE_MEMBER_BYTES = 24 * 1024 * 1024
_MAX_ARCHIVE_TEXT_BYTES = 96 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 20_000
_MAX_DOCUMENT_CHARS = 8_000_000
_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:18|19|20|21)\d{2}\b")


class DocumentFormatError(RuntimeError):
    """A supported document could not be extracted safely."""


@dataclass(frozen=True)
class DocumentUnit:
    text: str
    page: int | str
    content_type: str
    location_label: str


def is_supported_document(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_DOCUMENT_EXTENSIONS


def document_kind(path: str | os.PathLike[str]) -> str:
    suffix = Path(path).suffix.lower()
    return {
        ".pdf": "document",
        ".docx": "document",
        ".odt": "document",
        ".epub": "book",
        ".pptx": "presentation",
    }.get(suffix, "document")


def document_index_variant(path: str | os.PathLike[str], *, pdf_variant: str = "off") -> str:
    """Preserve the historical PDF cache signature; version new parsers independently."""
    suffix = Path(path).suffix.lower()
    return pdf_variant if suffix == ".pdf" else f"document-formats-v1:{suffix}"


def _local_name(tag: str) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _normalize_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:_MAX_DOCUMENT_CHARS]


def _element_text(element: ET.Element) -> str:
    return _normalize_text(" ".join(part.strip() for part in element.itertext() if part.strip()))


def _parse_xml(payload: bytes, *, label: str) -> ET.Element:
    # Office, ODF and EPUB package parts never need entity declarations; they
    # are only used to amplify a small payload into gigabytes of text.
    if b"<!ENTITY" in payload:
        raise DocumentFormatError(f"Declaration d'entite interdite dans {label}")
    try:
        return ET.fromstring(payload)
    except ET.ParseError as exc:
        raise DocumentFormatError(f"XML invalide dans {label}: {exc}") from exc


def _safe_member_name(name: str) -> str:
    normalized = posixpath.normpath(str(name or "").replace("\\", "/")).lstrip("/")
    if not normalized or normalized == "." or normalized.startswith("../"):
        raise DocumentFormatError("Chemin interne d'archive non autorise")
    return normalized


def _read_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    safe_name = _safe_member_name(name)
    try:
        info = archive.getinfo(safe_name)
    except KeyError as exc:
        raise DocumentFormatError(f"Composant manquant: {safe_name}") from exc
    if info.is_dir() or info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
        raise DocumentFormatError(f"Composant trop volumineux: {safe_name}")
    return archive.read(info)


def _validate_archive_budget(archive: zipfile.ZipFile) -> None:
    total = 0
    members = archive.infolist()
    if len(members) > _MAX_ARCHIVE_MEMBERS:
        raise DocumentFormatError("Archive contenant trop de composants")
    for info in members:
        _safe_member_name(info.filename)
        if info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
            raise DocumentFormatError(f"Composant trop volumineux: {info.filename}")
        total += int(info.file_size)
        if total > _MAX_ARCHIVE_TEXT_BYTES:
            raise DocumentFormatError("Document compresse trop volumineux")


def _archive(path: Path) -> zipfile.ZipFile:
    archive: zipfile.ZipFile | None = None
    try:
        archive = zipfile.ZipFile(path)
        _validate_archive_budget(archive)
        return archive
    except DocumentFormatError:
        if archive is not None:
            archive.close()
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        if archive is not None:
            archive.close()
        raise DocumentFormatError(f"Archive illisible: {exc}") from exc
    except Exception as exc:
        if archive is not None:
            archive.close()
        raise DocumentFormatError(f"Archive illisible: {exc}") from exc


class _VisibleHtmlParser(HTMLParser):
    _BLOCKS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "figcaption",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "p",
        "section",
        "td",
        "th",
        "tr",
    }
    _HIDDEN = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in self._HIDDEN:
            self.hidden_depth += 1
        elif not self.hidden_depth and lowered in self._BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._HIDDEN:
            self.hidden_depth = max(0, self.hidden_depth - 1)
        elif not self.hidden_depth and lowered in self._BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        return _normalize_text(html.unescape(" ".join(self.parts)))


def _html_text(payload: bytes) -> str:
    parser = _VisibleHtmlParser()
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            parser.feed(payload.decode(encoding))
            return parser.text()
        except UnicodeError:
            parser = _VisibleHtmlParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    return parser.text()


def _read_text(path: Path) -> str:
    try:
        size = path.stat().st_size
        if size > _MAX_ARCHIVE_MEMBER_BYTES:
            raise DocumentFormatError("Fichier texte trop volumineux")
        payload = path.read_bytes()
    except OSError as exc:
        raise DocumentFormatError(f"Fichier texte illisible: {exc}") from exc
    for encoding in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            return _normalize_text(payload.decode(encoding))
        except UnicodeError:
            continue
    return _normalize_text(payload.decode("utf-8", errors="replace"))


class PypdfLoader:
    """Page texts exactly as langchain's ``PyPDFLoader`` yields them.

    Importing langchain costs about 8 s per process, which the one-process-per-
    document parser cannot afford. This reads the same pypdf pages in "plain"
    extraction mode and strips them the same way.
    """

    def __init__(self, path: str) -> None:
        self.path = path

    def lazy_load(self):
        from pypdf import PdfReader

        with open(self.path, "rb") as handle:
            reader = PdfReader(handle)
            for number, page in enumerate(reader.pages):
                yield SimpleNamespace(
                    page_content=str(page.extract_text(extraction_mode="plain") or "").strip(),
                    metadata={"page": number},
                )

    def load(self) -> list:
        return list(self.lazy_load())


def _pdf_units(
    path: Path,
    *,
    loader_class: type | None = None,
    max_units: int | None = None,
) -> list[DocumentUnit]:
    if loader_class is None:
        from langchain_community.document_loaders import PyPDFLoader

        loader_class = PyPDFLoader

    loader = loader_class(str(path))
    lazy_load = getattr(loader, "lazy_load", None)
    if max_units and callable(lazy_load):
        pages = list(islice(lazy_load(), max_units))
    else:
        pages = loader.load()
    return [
        DocumentUnit(
            text=_normalize_text(page.page_content),
            page=(getattr(page, "metadata", None) or {}).get("page", index),
            content_type="pdf_page",
            location_label=f"Page {index + 1}",
        )
        for index, page in enumerate(pages)
        if _normalize_text(page.page_content)
    ]


def _docx_units(path: Path) -> list[DocumentUnit]:
    with _archive(path) as archive:
        root = _parse_xml(_read_zip_member(archive, "word/document.xml"), label=path.name)

    body = next((element for element in root.iter() if _local_name(element.tag) == "body"), root)
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    content: list[str] = []

    def flush() -> None:
        nonlocal content
        text = _normalize_text("\n\n".join(content))
        if text:
            sections.append((heading, [text]))
        content = []

    for child in list(body):
        name = _local_name(child.tag)
        text = _element_text(child)
        if not text:
            continue
        is_heading = False
        if name == "p":
            for descendant in child.iter():
                if _local_name(descendant.tag) != "pStyle":
                    continue
                style = next(
                    (
                        str(value)
                        for key, value in descendant.attrib.items()
                        if _local_name(key) == "val"
                    ),
                    "",
                )
                is_heading = style.lower().startswith(("heading", "titre"))
                break
        if is_heading:
            flush()
            heading = text
        else:
            content.append(text)
    flush()
    if not sections:
        text = _element_text(root)
        sections = [("", [text])] if text else []
    return [
        DocumentUnit(
            text=_normalize_text("\n\n".join(([title] if title else []) + parts)),
            page=index,
            content_type="document_section",
            location_label=f"Section {index}" + (f" — {title}" if title else ""),
        )
        for index, (title, parts) in enumerate(sections, start=1)
    ]


def _odt_units(path: Path) -> list[DocumentUnit]:
    with _archive(path) as archive:
        root = _parse_xml(_read_zip_member(archive, "content.xml"), label=path.name)
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    content: list[str] = []

    def flush() -> None:
        nonlocal content
        text = _normalize_text("\n\n".join(content))
        if text:
            sections.append((heading, [text]))
        content = []

    for element in root.iter():
        name = _local_name(element.tag)
        if name not in {"h", "p"}:
            continue
        text = _element_text(element)
        if not text:
            continue
        if name == "h":
            flush()
            heading = text
        else:
            content.append(text)
    flush()
    return [
        DocumentUnit(
            text=_normalize_text("\n\n".join(([title] if title else []) + parts)),
            page=index,
            content_type="document_section",
            location_label=f"Section {index}" + (f" — {title}" if title else ""),
        )
        for index, (title, parts) in enumerate(sections, start=1)
    ]


def _natural_slide_number(name: str) -> int:
    match = re.search(r"slide(\d+)\.xml$", name, flags=re.IGNORECASE)
    return int(match.group(1)) if match else 0


def _pptx_units(path: Path) -> list[DocumentUnit]:
    with _archive(path) as archive:
        names = sorted(
            (
                info.filename
                for info in archive.infolist()
                if re.fullmatch(r"ppt/slides/slide\d+\.xml", info.filename, flags=re.IGNORECASE)
            ),
            key=_natural_slide_number,
        )
        units: list[DocumentUnit] = []
        for name in names:
            slide_number = _natural_slide_number(name)
            root = _parse_xml(_read_zip_member(archive, name), label=name)
            paragraphs: list[str] = []
            for paragraph in (item for item in root.iter() if _local_name(item.tag) == "p"):
                text = _element_text(paragraph)
                if text:
                    paragraphs.append(text)
            text = _normalize_text("\n".join(paragraphs))
            if text:
                units.append(
                    DocumentUnit(
                        text=text,
                        page=slide_number,
                        content_type="presentation_slide",
                        location_label=f"Diapositive {slide_number}",
                    )
                )
        return units


def _epub_package_path(archive: zipfile.ZipFile) -> str:
    root = _parse_xml(
        _read_zip_member(archive, "META-INF/container.xml"),
        label="META-INF/container.xml",
    )
    for element in root.iter():
        if _local_name(element.tag) == "rootfile":
            for key, value in element.attrib.items():
                if _local_name(key) == "full-path" and str(value).strip():
                    return _safe_member_name(str(value))
    raise DocumentFormatError("Package EPUB introuvable")


def _epub_manifest_and_spine(
    root: ET.Element,
) -> tuple[dict[str, tuple[str, str]], list[str]]:
    manifest: dict[str, tuple[str, str]] = {}
    spine: list[str] = []
    for element in root.iter():
        name = _local_name(element.tag)
        attrs = {_local_name(key): str(value) for key, value in element.attrib.items()}
        if name == "item" and attrs.get("id") and attrs.get("href"):
            manifest[attrs["id"]] = (attrs["href"], attrs.get("media-type", ""))
        elif name == "itemref" and attrs.get("idref"):
            spine.append(attrs["idref"])
    return manifest, spine


def _epub_units(path: Path) -> list[DocumentUnit]:
    with _archive(path) as archive:
        package_path = _epub_package_path(archive)
        package = _parse_xml(_read_zip_member(archive, package_path), label=package_path)
        manifest, spine = _epub_manifest_and_spine(package)
        ordered_ids = spine or [
            item_id
            for item_id, (_href, media_type) in manifest.items()
            if media_type in {"application/xhtml+xml", "text/html"}
        ]
        base = PurePosixPath(package_path).parent
        units: list[DocumentUnit] = []
        for item_id in ordered_ids:
            item = manifest.get(item_id)
            if item is None:
                continue
            href, media_type = item
            if media_type not in {
                "application/xhtml+xml",
                "text/html",
            } and not href.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            member = _safe_member_name(str(base / PurePosixPath(href.split("#", 1)[0])))
            text = _html_text(_read_zip_member(archive, member))
            if not text:
                continue
            chapter = len(units) + 1
            units.append(
                DocumentUnit(
                    text=text,
                    page=chapter,
                    content_type="ebook_chapter",
                    location_label=f"Chapitre {chapter}",
                )
            )
        return units


def _check_or_raise(path: Path) -> None:
    try:
        check_document(path)
    except DocumentRejected as exc:
        raise DocumentFormatError(str(exc)) from exc


def extract_units_in_process(
    path: str | os.PathLike[str],
    *,
    pdf_loader_class: type | None = None,
    max_units: int | None = None,
) -> list[DocumentUnit]:
    """Parse a document in the calling process. Prefer ``load_document_units``."""
    full_path = Path(path)
    suffix = full_path.suffix.lower()
    if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise DocumentFormatError(
            f"Format de document non pris en charge: {suffix or 'sans extension'}"
        )
    _check_or_raise(full_path)
    if suffix == ".pdf":
        units = _pdf_units(full_path, loader_class=pdf_loader_class, max_units=max_units)
    elif suffix == ".docx":
        units = _docx_units(full_path)
    elif suffix == ".odt":
        units = _odt_units(full_path)
    elif suffix == ".epub":
        units = _epub_units(full_path)
    elif suffix == ".pptx":
        units = _pptx_units(full_path)
    elif suffix in _TEXT_EXTENSIONS:
        text = _read_text(full_path)
        units = (
            [
                DocumentUnit(
                    text=text,
                    page=1,
                    content_type="text_document",
                    location_label="Document",
                )
            ]
            if text
            else []
        )
    else:
        raise DocumentFormatError(f"Format de document non pris en charge: {suffix}")
    return units[:max_units] if max_units else units


def load_document_units(
    path: str | os.PathLike[str],
    *,
    pdf_loader_class: type | None = None,
    max_units: int | None = None,
    isolated: bool | None = None,
) -> list[DocumentUnit]:
    """Extract the navigation units of a document.

    The file is vetted (size, signature) and, unless isolation is turned off,
    parsed in a child process with a timeout and a memory cap. ``isolated=None``
    follows ``BIBLIOBRAIN_DOCUMENT_ISOLATION``. ``pdf_loader_class`` is an
    in-process seam for tests and is ignored when the parse is isolated.
    """
    full_path = Path(path)
    suffix = full_path.suffix.lower()
    if suffix not in SUPPORTED_DOCUMENT_EXTENSIONS:
        raise DocumentFormatError(
            f"Format de document non pris en charge: {suffix or 'sans extension'}"
        )
    _check_or_raise(full_path)

    from src.document_isolation import isolation_enabled, parse_in_subprocess

    if isolated is None:
        isolated = isolation_enabled()
    if isolated:
        return parse_in_subprocess(full_path, max_units=max_units)
    return extract_units_in_process(
        full_path, pdf_loader_class=pdf_loader_class, max_units=max_units
    )


def _core_properties(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        root = _parse_xml(_read_zip_member(archive, "docProps/core.xml"), label="core.xml")
    except DocumentFormatError:
        return {}
    values: dict[str, str] = {}
    for element in root.iter():
        name = _local_name(element.tag).lower()
        value = _element_text(element)
        if value and name in {"title", "creator", "subject", "keywords", "created", "modified"}:
            values.setdefault(name, value)
    return values


def _odt_properties(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        root = _parse_xml(_read_zip_member(archive, "meta.xml"), label="meta.xml")
    except DocumentFormatError:
        return {}
    values: dict[str, str] = {}
    for element in root.iter():
        name = _local_name(element.tag).lower()
        value = _element_text(element)
        if value and name in {"title", "creator", "initial-creator", "subject", "keyword", "date"}:
            values.setdefault(name, value)
    return values


def _epub_properties(archive: zipfile.ZipFile) -> dict[str, str]:
    package_path = _epub_package_path(archive)
    root = _parse_xml(_read_zip_member(archive, package_path), label=package_path)
    values: dict[str, str] = {}
    creators: list[str] = []
    identifiers: list[str] = []
    for element in root.iter():
        name = _local_name(element.tag).lower()
        value = _element_text(element)
        if not value:
            continue
        if name == "creator":
            creators.append(value)
        elif name == "identifier":
            identifiers.append(value)
        elif name in {"title", "date", "subject", "publisher"}:
            values.setdefault(name, value)
    if creators:
        values["creator"] = " | ".join(creators)
    doi = next((_extract_doi(value) for value in identifiers if _extract_doi(value)), "")
    if doi:
        values["doi"] = doi
    return values


def _extract_doi(value: str) -> str:
    match = _DOI_RE.search(str(value or ""))
    return match.group(0).rstrip(".,;)]}").lower() if match else ""


def _split_authors(value: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"\s*[|;]\s*|\s+and\s+", str(value or ""), flags=re.IGNORECASE)
        if part.strip()
    ]


def _split_keywords(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;|]", str(value or "")) if part.strip()][:24]


def _metadata_item(path: Path, values: dict[str, str]) -> ZoteroItem:
    title = values.get("title", "").strip() or path.stem
    creator = values.get("creator", "") or values.get("initial-creator", "")
    date = values.get("created", "") or values.get("date", "") or values.get("modified", "")
    year_match = _YEAR_RE.search(date)
    subject = values.get("subject", "")
    doi = values.get("doi", "") or _extract_doi(subject)
    keywords = values.get("keywords", "") or values.get("keyword", "")
    return ZoteroItem(
        title=title,
        authors=_split_authors(creator),
        year=year_match.group(0) if year_match else "",
        journal=values.get("publisher", ""),
        abstract=subject if subject and not doi else "",
        item_type=document_kind(path),
        tags=_split_keywords(keywords),
        doi=doi,
    )


def extract_document_metadata(path: str | os.PathLike[str]) -> ZoteroItem | None:
    full_path = Path(path)
    suffix = full_path.suffix.lower()
    if suffix in SUPPORTED_DOCUMENT_EXTENSIONS:
        _check_or_raise(full_path)
    if suffix == ".pdf":
        from src.pdf_metadata import extract_pdf_metadata

        return extract_pdf_metadata(str(full_path))
    if suffix in {".docx", ".pptx"}:
        with _archive(full_path) as archive:
            return _metadata_item(full_path, _core_properties(archive))
    if suffix == ".odt":
        with _archive(full_path) as archive:
            return _metadata_item(full_path, _odt_properties(archive))
    if suffix == ".epub":
        with _archive(full_path) as archive:
            return _metadata_item(full_path, _epub_properties(archive))
    if suffix in _TEXT_EXTENSIONS:
        text = _read_text(full_path)
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        title = re.sub(r"^#{1,6}\s*", "", first_line).strip()[:300] or full_path.stem
        return ZoteroItem(title=title, item_type="document", doi=_extract_doi(text[:12000]))
    return None


def resolve_document_metadata(
    rel_path: str,
    full_path: str,
    library: dict[str, ZoteroItem],
    *,
    allow_external_enrichment: bool = False,
) -> ZoteroItem | None:
    """Resolve Zotero/BibTeX, local file, then optional Crossref metadata."""
    from src.pdf_metadata import enrich_pdf_metadata, merge_missing_metadata, resolve_pdf_metadata

    if Path(full_path).suffix.lower() == ".pdf":
        return resolve_pdf_metadata(
            rel_path,
            full_path,
            library,
            allow_external_enrichment=allow_external_enrichment,
        )

    primary = lookup_zotero_item(rel_path, library)
    local = extract_document_metadata(full_path)
    lookup_item = merge_missing_metadata(primary, local)
    external = enrich_pdf_metadata(lookup_item, allow_network=allow_external_enrichment)
    enriched_fallback = merge_missing_metadata(external, local)
    return merge_missing_metadata(primary, enriched_fallback)


__all__ = [
    "DocumentFormatError",
    "DocumentUnit",
    "SUPPORTED_DOCUMENT_EXTENSIONS",
    "document_index_variant",
    "document_kind",
    "extract_document_metadata",
    "extract_units_in_process",
    "is_supported_document",
    "load_document_units",
    "resolve_document_metadata",
]
