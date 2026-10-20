from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

# LaTeX-generated PDFs often extract accents as a spacing mark next to the
# letter ("c´ eramiques", "` a", "poss´ edent"). Map each spacing mark to its
# combining counterpart so the text can be recomposed ("céramiques", "à").
# Plain "^" is deliberately absent: it is an exponent in formulas ("(J/Jc)^n").
_SPACING_ACCENTS = {
    "´": ("́", "aeiouyAEIOUY"),  # acute
    "`": ("̀", "aeiouAEIOU"),  # grave
    "ˆ": ("̂", "aeiouAEIOU"),  # circumflex
    "¨": ("̈", "aeiouyAEIOUY"),  # diaeresis
    "˜": ("̃", "anoANO"),  # tilde
}
_SPACING_ACCENT_RE = re.compile(r"([´`ˆ¨˜])[ \t]?([A-Za-z])")
_LIGATURES = str.maketrans(
    {
        "ﬀ": "ff",
        "ﬁ": "fi",
        "ﬂ": "fl",
        "ﬃ": "ffi",
        "ﬄ": "ffl",
        "ﬅ": "st",
        "ﬆ": "st",
    }
)
_CEDILLA_RE = re.compile(r"¸[ \t]?([cC])|([cC])[ \t]?¸")


def sanitize_utf8_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        # Recompose valid surrogate pairs into proper Unicode code points and
        # replace any remaining lone surrogates with U+FFFD.
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")


def repair_pdf_text_artifacts(value: Any) -> str:
    """Repair extraction artifacts that break verbatim matching and reading.

    Only unambiguous artifacts are touched: typographic ligatures and spacing
    accents detached from the letter they belong to. Everything else is kept.
    """
    text = sanitize_utf8_text(value)
    if not text:
        return text
    text = text.translate(_LIGATURES)
    if not any(mark in text for mark in "´`ˆ¨˜¸"):
        return text

    def _accent(match: re.Match[str]) -> str:
        combining, letters = _SPACING_ACCENTS[match.group(1)]
        letter = match.group(2)
        return letter + combining if letter in letters else match.group(0)

    def _cedilla(match: re.Match[str]) -> str:
        return (match.group(1) or match.group(2)) + "̧"

    text = _SPACING_ACCENT_RE.sub(_accent, text)
    text = _CEDILLA_RE.sub(_cedilla, text)
    return unicodedata.normalize("NFC", text)


def sanitize_utf8_data(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_utf8_text(value)
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            cleaned_key = sanitize_utf8_text(key) if isinstance(key, str) else key
            cleaned[cleaned_key] = sanitize_utf8_data(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_utf8_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_utf8_data(item) for item in value)
    if isinstance(value, set):
        return {sanitize_utf8_data(item) for item in value}
    return value


def json_dumps_utf8(payload: Any, **kwargs: Any) -> str:
    return json.dumps(sanitize_utf8_data(payload), **kwargs)
