"""Citation plumbing for moving drafts between BiblioBrain, LaTeX and Word.

The editor sends LaTeX exports with each citation wrapped in private-use
markers (``CITE_OPEN key CITE_CLOSE``) so the keys survive the Markdown to
LaTeX conversion untouched and come out as ``\\cite{...}``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from src.chat_export import _escape_latex

CITE_OPEN = "\ue020"
CITE_CLOSE = "\ue021"
# Opaque placeholder used while the Markdown body goes through the LaTeX
# converter: digits only, so escaping cannot alter it.
_PLACEHOLDER_OPEN = "\ue022"
_PLACEHOLDER_CLOSE = "\ue023"

_MARKER_RUN_RE = re.compile(
    f"{CITE_OPEN}[^{CITE_OPEN}{CITE_CLOSE}]*{CITE_CLOSE}"
    f"(?:[ \\t]*{CITE_OPEN}[^{CITE_OPEN}{CITE_CLOSE}]*{CITE_CLOSE})*"
)
_MARKER_RE = re.compile(f"{CITE_OPEN}([^{CITE_OPEN}{CITE_CLOSE}]*){CITE_CLOSE}")
_PLACEHOLDER_RE = re.compile(f"{_PLACEHOLDER_OPEN}(\\d+){_PLACEHOLDER_CLOSE}")
_MAX_KEY_CHARS = 200


def sanitize_cite_key(value: Any) -> str:
    """Keep the characters BibTeX accepts in a key (same set as ZoteroItem)."""
    return re.sub(r"[^A-Za-z0-9:_\-.]+", "", str(value or ""))[:_MAX_KEY_CHARS]


def extract_citation_markers(content: str) -> tuple[str, list[list[str]]]:
    """Swap marker runs for numbered placeholders; adjacent markers form one group."""
    groups: list[list[str]] = []

    def replace(match: re.Match[str]) -> str:
        keys: list[str] = []
        for raw in _MARKER_RE.findall(match.group(0)):
            key = sanitize_cite_key(raw)
            if key and key not in keys:
                keys.append(key)
        if not keys:
            return ""
        groups.append(keys)
        return f"{_PLACEHOLDER_OPEN}{len(groups) - 1}{_PLACEHOLDER_CLOSE}"

    return _MARKER_RUN_RE.sub(replace, str(content or "")), groups


def restore_latex_citations(latex: str, groups: list[list[str]]) -> str:
    def replace(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(groups):
            return ""
        return "\\cite{" + ",".join(groups[index]) + "}"

    return _PLACEHOLDER_RE.sub(replace, str(latex or ""))


def strip_citation_markers(content: str) -> str:
    return _MARKER_RE.sub("", str(content or ""))


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _latex_reference_text(record: dict[str, Any], key: str) -> str:
    authors = _clean(record.get("authors_display") or record.get("authors") or record.get("author"))
    title = _clean(record.get("title"))
    journal = _clean(record.get("journal"))
    year = _clean(record.get("year"))
    doi = _clean(record.get("doi"))
    if not (authors or title):
        return f"\\texttt{{{_escape_latex(key)}}}"
    parts: list[str] = []
    if authors:
        parts.append(_escape_latex(authors))
    if title:
        parts.append(f"``{_escape_latex(title)}''")
    if journal:
        parts.append(f"\\emph{{{_escape_latex(journal)}}}")
    if year:
        parts.append(_escape_latex(year))
    text = ", ".join(parts)
    if doi:
        text += f", doi: {_escape_latex(doi)}"
    return text if text.endswith(".") else f"{text}."


def render_latex_bibliography(records: Iterable[dict[str, Any]]) -> str:
    """A self-contained ``thebibliography`` whose keys match the ``\\cite`` keys."""
    items: list[str] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            continue
        key = sanitize_cite_key(record.get("latex_key") or record.get("citation_id"))
        if not key or key in seen:
            continue
        seen.add(key)
        items.append(f"\\bibitem{{{key}}} {_latex_reference_text(record, key)}")
    if not items:
        return ""
    return (
        "% Bibliographie générée par BiblioBrain. Pour utiliser votre propre fichier .bib,\n"
        "% remplacez cet environnement par \\bibliographystyle{plain} et \\bibliography{references}.\n"
        "\\renewcommand{\\refname}{Bibliographie}\n"
        "\\begin{thebibliography}{99}\n" + "\n".join(items) + "\n\\end{thebibliography}\n"
    )


def insert_before_end_document(latex: str, section: str) -> str:
    text = str(latex or "").rstrip()
    if not section:
        return text + "\n"
    match = re.search(r"\\end\{document\}\s*$", text)
    if not match:
        return f"{text}\n\n{section}"
    return f"{text[: match.start()].rstrip()}\n\n{section}\n\\end{{document}}\n"


def citation_record_from_item(item: Any) -> dict[str, Any]:
    first_author = str(item.first_author() or "").strip()
    year = str(item.year or "").strip()
    return {
        "citation_id": item.citation_key_or_fallback(),
        "citation_key": item.citation_key_or_fallback(),
        "title": str(item.title or "").strip(),
        "author": first_author,
        "authors_display": item.authors_str() or first_author,
        "year": year,
        "collection": item.collections[0] if item.collections else "",
        "journal": str(item.journal or "").strip(),
        "doi": str(item.doi or "").strip(),
        "apa": item.apa_citation(),
        "bibtex": item.bibtex_entry(),
        "label": f"{first_author or 'Source'} ({year or 'n.d.'})",
        "citation_count": int(item.citation_count or 0),
        "influential_citation_count": int(item.influential_citation_count or 0),
        "support_status": "imported",
    }


# Zotero item type -> CSL type, for the item data stored in Word fields.
_CSL_TYPES = {
    "journalArticle": "article-journal",
    "magazineArticle": "article-magazine",
    "newspaperArticle": "article-newspaper",
    "book": "book",
    "bookSection": "chapter",
    "conferencePaper": "paper-conference",
    "thesis": "thesis",
    "report": "report",
    "webpage": "webpage",
    "preprint": "article",
    "manuscript": "manuscript",
    "patent": "patent",
    "dataset": "dataset",
}


def _csl_name(author: str) -> dict[str, str]:
    family, sep, given = str(author or "").partition(",")
    if sep and family.strip():
        return {"family": family.strip(), "given": given.strip()}
    return {"literal": str(author or "").strip()}


def csl_item_from_zotero_item(item: Any) -> dict[str, Any]:
    """CSL-JSON item data, the fallback Zotero uses when it cannot find the item."""
    data: dict[str, Any] = {
        "type": _CSL_TYPES.get(str(item.item_type or ""), "article"),
        "title": str(item.title or "").strip(),
    }
    authors = [_csl_name(author) for author in item.authors if str(author or "").strip()]
    if authors:
        data["author"] = authors
    if str(item.journal or "").strip():
        data["container-title"] = str(item.journal).strip()
    if re.fullmatch(r"\d{4}", str(item.year or "").strip()):
        data["issued"] = {"date-parts": [[int(item.year)]]}
    if str(item.doi or "").strip():
        data["DOI"] = str(item.doi).strip()
    return data


def zotero_item_uri(item: Any, user_library_uri: str) -> str:
    """Zotero URI of an item: groups/<id>/items/<key> or <user prefix>/items/<key>."""
    key = str(getattr(item, "item_key", "") or "").strip()
    if not re.fullmatch(r"[A-Z0-9]{8}", key):
        return ""
    group_id = int(getattr(item, "group_id", 0) or 0)
    if getattr(item, "library_type", "user") == "group" and group_id:
        return f"http://zotero.org/groups/{group_id}/items/{key}"
    return f"{user_library_uri}/items/{key}" if user_library_uri else ""


def resolve_word_citation_items(keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Map citation keys to the ``uris`` / ``itemData`` of a Zotero Word field.

    Keys missing from the library are left out: the editor then builds the
    item data from its own citation record.
    """
    from src.library import find_items_by_citation_keys, zotero_user_library_uri

    requested = [str(key or "").strip()[:_MAX_KEY_CHARS] for key in keys]
    items = find_items_by_citation_keys([key for key in requested if key])
    user_uri = zotero_user_library_uri() if items else ""
    result: dict[str, dict[str, Any]] = {}
    for key, item in items.items():
        uri = zotero_item_uri(item, user_uri)
        result[key] = {"uris": [uri] if uri else [], "itemData": csl_item_from_zotero_item(item)}
    return result


def resolve_citation_records(keys: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Map each requested citation key to a drafting citation record, when known."""
    from src.library import find_items_by_citation_keys

    requested = [str(key or "").strip()[:_MAX_KEY_CHARS] for key in keys]
    items = find_items_by_citation_keys([key for key in requested if key])
    return {key: citation_record_from_item(item) for key, item in items.items()}
