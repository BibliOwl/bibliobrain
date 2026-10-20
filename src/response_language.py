"""Answer in the language of the question.

The chat used to answer in the UI language, so an English question got a
French answer. The request sets the question's language here once; the
prompt builders and the user-facing messages read it back, and fall back to
the UI language when the question gives no clear signal ("REBCO ?").
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Iterator
from contextlib import contextmanager

_FRENCH_WORDS = frozenset(
    "le la les un une des du de au aux et ou est sont que qui quoi quel quelle quels quelles "
    "comment pourquoi dans sur pour par avec sans ce cette ces son sa ses leur leurs il elle "
    "ils elles nous vous je tu on ne pas plus peut peuvent faire fait entre selon mes mon ma "
    "quelles quel explique résume donne compare".split()
)
_ENGLISH_WORDS = frozenset(
    "the a an and or is are was were what which who whom whose how why when where does do did "
    "in on for with without by from this that these those it its they their we you i not can "
    "could should would will between according my explain summarize give compare of to".split()
)
_WORD_RE = re.compile(r"[a-zàâçéèêëîïôûùüÿœ']+", re.IGNORECASE)
_FRENCH_ACCENT_RE = re.compile(r"[àâçéèêëîïôûùüÿœ]", re.IGNORECASE)

_response_language: contextvars.ContextVar[str] = contextvars.ContextVar(
    "response_language", default=""
)


def detect_text_language(text: str) -> str:
    """"fr", "en", or "" when the text is too short or mixed to tell."""
    words = [word.strip("'").lower() for word in _WORD_RE.findall(str(text or ""))]
    french = sum(1 for word in words if word in _FRENCH_WORDS or "'" in word)
    english = sum(1 for word in words if word in _ENGLISH_WORDS)
    if _FRENCH_ACCENT_RE.search(str(text or "")):
        french += 1
    # One clear signal is enough for a short imperative ("Explain screening
    # currents"); a tie, or no signal at all ("REBCO ?"), stays undecided.
    if french == english:
        return ""
    return "fr" if french > english else "en"


_QUOTED_SPAN_RE = re.compile(r"«[^»]*»|“[^”]*”|\"[^\"]*\"")
_INSTRUCTION_END_RE = re.compile(r"[?:\n]")


def detect_question_language(question: str) -> str:
    """Language of the request itself, not of the text it quotes.

    "est-ce que ce papier valide cette phrase ? Within the framework of the
    CERN..." counted more English words than French ones, so a French
    question about a pasted English sentence got an English answer. The
    opening clause, up to the first "?", ":" or line break, is the request;
    quoted spans are material. The whole text decides only when the opening
    gives no signal ("REBCO ?").
    """
    text = _QUOTED_SPAN_RE.sub(" ", str(question or ""))
    instruction = _INSTRUCTION_END_RE.split(text, maxsplit=1)[0]
    return detect_text_language(instruction) or detect_text_language(text)


def current_response_language() -> str:
    """Language chosen for the current request, or "" outside a request."""
    return _response_language.get()


@contextmanager
def response_language_for(question: str, fallback: str) -> Iterator[str]:
    """Use the question's language for this request, else ``fallback``."""
    language = detect_question_language(question) or (fallback if fallback in {"fr", "en"} else "fr")
    token = _response_language.set(language)
    try:
        yield language
    finally:
        _response_language.reset(token)


def set_response_language(language: str) -> contextvars.Token[str]:
    """Set the answer language for the rest of the current task (a request)."""
    return _response_language.set(language if language in {"fr", "en"} else "")
