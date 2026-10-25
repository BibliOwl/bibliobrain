"""Deterministic check of the chemical names and acronyms of a chat answer.

The figures of an answer are looked up in its passages (chat_grounding);
names were not. A 3B model read "iron-based superconductors (Fe-SCs)" and
wrote "composés de fer basés sur l'iode": no passage says iodine. Here each
chemical element named in the answer (in French or English), each chemical
formula and each acronym is looked up in the passages, the question and the
conversation, and the ones found nowhere are listed. Nothing calls a model.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

# (symbol, English name, French name). "Or" (gold) and "lead" are left out:
# they are ordinary words ("or", "to lead") far more often than metals.
_ELEMENTS: tuple[tuple[str, str, str], ...] = (
    ("H", "hydrogen", "hydrogene"),
    ("He", "helium", "helium"),
    ("Li", "lithium", "lithium"),
    ("Be", "beryllium", "beryllium"),
    ("B", "boron", "bore"),
    ("C", "carbon", "carbone"),
    ("N", "nitrogen", "azote"),
    ("O", "oxygen", "oxygene"),
    ("F", "fluorine", "fluor"),
    ("Ne", "neon", "neon"),
    ("Na", "sodium", "sodium"),
    ("Mg", "magnesium", "magnesium"),
    ("Al", "aluminium", "aluminium"),
    ("Si", "silicon", "silicium"),
    ("P", "phosphorus", "phosphore"),
    ("S", "sulfur", "soufre"),
    ("Cl", "chlorine", "chlore"),
    ("Ar", "argon", "argon"),
    ("K", "potassium", "potassium"),
    ("Ca", "calcium", "calcium"),
    ("Sc", "scandium", "scandium"),
    ("Ti", "titanium", "titane"),
    ("V", "vanadium", "vanadium"),
    ("Cr", "chromium", "chrome"),
    ("Mn", "manganese", "manganese"),
    ("Fe", "iron", "fer"),
    ("Co", "cobalt", "cobalt"),
    ("Ni", "nickel", "nickel"),
    ("Cu", "copper", "cuivre"),
    ("Zn", "zinc", "zinc"),
    ("Ga", "gallium", "gallium"),
    ("Ge", "germanium", "germanium"),
    ("As", "arsenic", "arsenic"),
    ("Se", "selenium", "selenium"),
    ("Br", "bromine", "brome"),
    ("Kr", "krypton", "krypton"),
    ("Rb", "rubidium", "rubidium"),
    ("Sr", "strontium", "strontium"),
    ("Y", "yttrium", "yttrium"),
    ("Zr", "zirconium", "zirconium"),
    ("Nb", "niobium", "niobium"),
    ("Mo", "molybdenum", "molybdene"),
    ("Ru", "ruthenium", "ruthenium"),
    ("Rh", "rhodium", "rhodium"),
    ("Pd", "palladium", "palladium"),
    ("Ag", "silver", "argent"),
    ("Cd", "cadmium", "cadmium"),
    ("In", "indium", "indium"),
    ("Sn", "tin", "etain"),
    ("Sb", "antimony", "antimoine"),
    ("Te", "tellurium", "tellure"),
    ("I", "iodine", "iode"),
    ("Xe", "xenon", "xenon"),
    ("Cs", "caesium", "cesium"),
    ("Ba", "barium", "baryum"),
    ("La", "lanthanum", "lanthane"),
    ("Ce", "cerium", "cerium"),
    ("Pr", "praseodymium", "praseodyme"),
    ("Nd", "neodymium", "neodyme"),
    ("Sm", "samarium", "samarium"),
    ("Eu", "europium", "europium"),
    ("Gd", "gadolinium", "gadolinium"),
    ("Tb", "terbium", "terbium"),
    ("Dy", "dysprosium", "dysprosium"),
    ("Ho", "holmium", "holmium"),
    ("Er", "erbium", "erbium"),
    ("Tm", "thulium", "thulium"),
    ("Yb", "ytterbium", "ytterbium"),
    ("Lu", "lutetium", "lutecium"),
    ("Hf", "hafnium", "hafnium"),
    ("Ta", "tantalum", "tantale"),
    ("W", "tungsten", "tungstene"),
    ("Re", "rhenium", "rhenium"),
    ("Os", "osmium", "osmium"),
    ("Ir", "iridium", "iridium"),
    ("Pt", "platinum", "platine"),
    ("Au", "gold", ""),
    ("Hg", "mercury", "mercure"),
    ("Tl", "thallium", "thallium"),
    ("Pb", "", "plomb"),
    ("Bi", "bismuth", "bismuth"),
    ("Po", "polonium", "polonium"),
    ("Rn", "radon", "radon"),
    ("Ra", "radium", "radium"),
    ("Th", "thorium", "thorium"),
    ("U", "uranium", "uranium"),
    ("Pu", "plutonium", "plutonium"),
)
_SYMBOLS = frozenset(symbol for symbol, _en, _fr in _ELEMENTS)
_NAME_TO_ELEMENT: dict[str, tuple[str, str, str]] = {}
for _element in _ELEMENTS:
    for _name in _element[1:]:
        if _name:
            _NAME_TO_ELEMENT[_name] = _element

# "LaFeAsO", "YBa2Cu3O7", "MgB2", "Nb3Sn": two element symbols or more.
_FORMULA_RE = re.compile(r"(?<![A-Za-z])(?:[A-Z][a-z]?\d*(?:\.\d+)?){2,}(?:[+−-]?[xyδ])?(?![a-z])")
# "IOD", "REBCO", "Fe-SC", "AC": two capitals or more, digits allowed.
_ACRONYM_RE = re.compile(
    r"(?<![\w-])(?=[A-Za-z0-9-]*[A-Z][A-Za-z0-9-]*[A-Z])[A-Za-z0-9][A-Za-z0-9-]{1,11}(?![\w-])"
)
_BRACKET_GROUP_RE = re.compile(r"\[[^\]\n]*\]")
_ROMAN_RE = re.compile(r"^[IVXLCDM]+$")
_LATEX_RE = re.compile(r"\\(?:mathrm|text|textrm|mathit|mathbf)\s*\{([^}]*)\}|\\[A-Za-z]+|[{}_$^]")
_WORD_RE = re.compile(r"[a-z]+")
# Capitalised words of French and English prose that are not acronyms.
# The app's own words ("l'index RAG") are not facts of a paper.
_NOT_ACRONYMS = frozenset(
    {"OK", "NB", "PS", "VS", "ETC", "IE", "EG", "RAG", "PDF", "DOI", "URL", "IA", "AI", "LLM"}
)


def _plain(text: str) -> str:
    value = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(char for char in value if not unicodedata.combining(char))


def _unlatex(text: str) -> str:
    return _LATEX_RE.sub(lambda match: match.group(1) or "", str(text or ""))


def _compact(text: str) -> str:
    return re.sub(r"[\s‐-―−-]+", "", _unlatex(_plain(text)))


def _stem(name: str) -> str:
    """ "iod" covers iodine, iodide, iode, iodure; "selen" selenide, séléniure."""
    return name[: max(3, len(name) - 2)]


def _formula_symbols(formula: str) -> list[str]:
    return [symbol for symbol in re.findall(r"[A-Z][a-z]?", formula)]


def _is_formula(token: str) -> bool:
    symbols = _formula_symbols(token)
    return len(symbols) >= 2 and all(symbol in _SYMBOLS for symbol in symbols)


class _Pool:
    """What the passages, the question and the conversation name."""

    def __init__(self, texts: Sequence[str]) -> None:
        joined = "\n".join(_unlatex(_plain(text)) for text in texts)
        self.lower_words = set(_WORD_RE.findall(joined.lower()))
        self.compact = _compact(joined)
        self.compact_lower = self.compact.lower()
        self.symbols: set[str] = set()
        for token in re.findall(r"[A-Za-z0-9.+−-]+", joined):
            if _is_formula(token):
                self.symbols.update(_formula_symbols(token))
            elif len(token) == 2 and token in _SYMBOLS:
                # "Fe", "As", "Se" alone: an element, or "As" opening a sentence.
                self.symbols.add(token)
            for part in re.split(r"[-−]", token):
                if 2 <= len(part) <= 3 and part in _SYMBOLS:
                    self.symbols.add(part)

    def names_element(self, element: tuple[str, str, str]) -> bool:
        symbol, english, french = element
        if symbol in self.symbols:
            return True
        stems = [_stem(name) for name in (english, french) if name]
        return any(word.startswith(stem) for stem in stems for word in self.lower_words)

    def has_text(self, token: str) -> bool:
        compact = _compact(token)
        if not compact:
            return True
        if compact in self.compact:
            return True
        # "Fe-SCs" for a passage that writes "Fe-SC", and the reverse.
        lowered = compact.lower()
        if lowered.endswith("s") and lowered[:-1] in self.compact_lower:
            return True
        return lowered in self.compact_lower and not compact.isupper()


# An equation of the answer is not a list of names: "\\int_{A_{sc}}" read "Asc".
_MATH_RE = re.compile(r"\$\$.*?\$\$|\$[^$\n]+\$|\\\(.*?\\\)|\\\[.*?\\\]", re.DOTALL)


def _answer_text(answer: str) -> str:
    without_math = _MATH_RE.sub(" ", str(answer or ""))
    return _BRACKET_GROUP_RE.sub(" ", _unlatex(_plain(without_math)))


def ungrounded_terms(
    answer: str | None,
    passages: Sequence[str],
    *,
    question: str = "",
    extra_texts: Sequence[str] = (),
) -> list[str]:
    """Elements, formulas and acronyms of the answer found in no passage.

    Each is listed once, as the answer writes it. An answer without passages
    is not checked: there is nothing to compare it with.
    """
    text = _answer_text(str(answer or ""))
    if not text.strip() or not any(str(passage or "").strip() for passage in passages):
        return []
    pool = _Pool([*passages, question, *extra_texts])
    found: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            found.append(term)

    for match in re.finditer(r"[A-Za-z]+", text):
        word = match.group(0)
        lowered = word.lower()
        element = _NAME_TO_ELEMENT.get(lowered) or (
            _NAME_TO_ELEMENT.get(lowered[:-1]) if lowered.endswith("s") else None
        )
        if element is None or word.isupper():
            continue
        if not pool.names_element(element):
            add(word)

    for match in _FORMULA_RE.finditer(text):
        token = match.group(0)
        if token.isupper() and (len(token) <= 2 or _ROMAN_RE.match(token)):
            # "II", "CO": a numeral or an acronym, checked as an acronym below.
            continue
        if _is_formula(token) and not pool.has_text(token):
            # A formula the passages write with other indices still names
            # elements they contain: only the elements they lack are flagged.
            missing = [symbol for symbol in _formula_symbols(token) if symbol not in pool.symbols]
            if missing:
                add(token)

    for match in _ACRONYM_RE.finditer(text):
        token = match.group(0).strip("-")
        bare = token.rstrip("s")
        capitals = len(re.sub(r"[^A-Z]", "", token))
        letters = len(re.sub(r"[^A-Za-z]", "", token))
        if (
            capitals < 2
            # "Long-Term" is a capitalised compound, "Fe-SCs" an acronym.
            or capitals * 2 < letters
            or token.upper() in _NOT_ACRONYMS
            or _ROMAN_RE.match(bare)
            or _is_formula(token)
            or re.fullmatch(r"S\d+", token)
        ):
            continue
        if not pool.has_text(token):
            add(token)
    return found


def is_chemical_term(term: str) -> bool:
    """An element name or a formula: a fact of the paper, worth a rewrite.

    An acronym the passages lack may be the model's own abbreviation of a
    phrase they contain, so it is only listed.
    """
    lowered = str(term or "").lower()
    if lowered in _NAME_TO_ELEMENT or lowered.rstrip("s") in _NAME_TO_ELEMENT:
        return True
    return _is_formula(str(term or "")) and not str(term or "").isupper()


__all__ = ["is_chemical_term", "ungrounded_terms"]
