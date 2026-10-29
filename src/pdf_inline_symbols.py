"""Detect PDF pages that draw symbols as small pictures instead of text.

Some publisher PDFs (IEEE papers of 2005-2011 for instance) draw a µ, an
exponent or a math term as a tiny 1-bit image mask placed inside the line of
text. No text extractor can read such a symbol: "1 µV/cm" is extracted as
"1 V/cm". The chat cannot recover the symbol, but it can say that the page
has some, so the reader checks the units on the page itself.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

_logger = logging.getLogger(__name__)

# A symbol drawn at text size stays small even at high resolution (the µ of
# Hahn et al. 2011 is a 56x66 px mask drawn 5.4x6.4 pt); figures are larger.
_MAX_SYMBOL_PIXELS = 120
# Publishers draw rules and fills with 1x1 px masks: those are not symbols.
_MIN_SYMBOL_PIXELS = 4


def _is_symbol_mask(xobject: Any) -> bool:
    # Only stencil masks: 1-bit pictures that are not masks are pictograms or
    # scanned bits, and flagged 20 pages of a book of icons on the library scan.
    if xobject.get("/Subtype") != "/Image" or not bool(xobject.get("/ImageMask")):
        return False
    try:
        width = int(xobject.get("/Width", 0) or 0)
        height = int(xobject.get("/Height", 0) or 0)
    except (TypeError, ValueError):
        return False
    return (
        _MIN_SYMBOL_PIXELS <= width <= _MAX_SYMBOL_PIXELS
        and _MIN_SYMBOL_PIXELS <= height <= _MAX_SYMBOL_PIXELS
    )


def _count_symbol_masks(path: str, page_index: int) -> int:
    from pypdf import PdfReader

    with open(path, "rb") as handle:
        reader = PdfReader(handle)
        if not 0 <= page_index < len(reader.pages):
            return 0
        resources = reader.pages[page_index].get("/Resources")
        resources = resources.get_object() if resources is not None else {}
        xobjects = resources.get("/XObject") if resources else None
        if xobjects is None:
            return 0
        xobjects = xobjects.get_object()
        return sum(1 for name in xobjects if _is_symbol_mask(xobjects[name].get_object()))


@lru_cache(maxsize=256)
def _page_has_image_symbols(path: str, page_index: int, size: int, mtime_ns: int) -> bool:
    del size, mtime_ns  # cache key only: a replaced file is read again
    try:
        return _count_symbol_masks(path, page_index) > 0
    except Exception:  # an unreadable page is not a reason to warn
        _logger.debug("Symbol image check failed for %s page %s", path, page_index, exc_info=True)
        return False


def page_has_image_symbols(path: str | os.PathLike[str], page_index: int) -> bool:
    """True when page ``page_index`` (0-based) of the PDF draws small symbols as images."""
    try:
        stat = os.stat(path)
    except OSError:
        return False
    return _page_has_image_symbols(str(path), int(page_index), stat.st_size, stat.st_mtime_ns)


__all__ = ["page_has_image_symbols"]
