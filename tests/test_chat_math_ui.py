from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import read_app_javascript

ROOT = Path(__file__).resolve().parents[1]

# Reported 2026-09-27 (user report 0db4a3f4): the formulas showed up as
# "\ p(t) = ..." because the fallback Markdown parser kept the doubled
# delimiter backslashes, and P_{ac} got wrapped in $...$ inside \(...\).
REPORTED_POWER = r"\( p(t) = \int_w \mathbf{E} \cdot (\mathbf{J} \delta) \, dw \)"
REPORTED_AVERAGE = r"\( P_{ac} = \frac{1}{T} \int_0^T p(t) \, dt \)"
REPORTED_ANSWER = (
    "Les pertes AC peuvent alors être calculées via l'intégration de la "
    f"puissance instantanée {REPORTED_POWER}, suivie d'une moyenne sur une "
    f"période {REPORTED_AVERAGE} [S2]."
)
DISPLAY_MATH = "Soit\n\n" + r"\[ \Delta = \{ x \mid x_1 * x_2 \} \\ y \]" + "\n\nfin."


def _slice_js(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    return source[start : source.index(end_marker, start)]


def _run_renderer(samples: dict[str, str]) -> dict[str, dict[str, str]]:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend math regression test")

    source = read_app_javascript()
    functions = "\n\n".join(
        [
            _slice_js(source, "function _looksLikeSafeInlineMath(", "function renderMath("),
            _slice_js(source, "function escapeHtml(", "const CHAT_DEPTH_VALUES"),
        ]
    )
    script = f"""
const window = {{}};
{functions}

const samples = {json.dumps(samples)};
const result = {{}};
for (const [name, text] of Object.entries(samples)) {{
  window.marked = undefined;
  window.DOMPurify = undefined;
  const fallback = parseAssistantMarkdown(text);
  window.marked = require('./static/vendor/marked/marked.umd.js');
  window.DOMPurify = {{ sanitize: (html) => html }};
  result[name] = {{ fallback, marked: parseAssistantMarkdown(text) }};
}}
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        [node, "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
    )
    return json.loads(completed.stdout)


def test_assistant_math_survives_markdown_before_katex() -> None:
    rendered = _run_renderer(
        {
            "reported": REPORTED_ANSWER,
            "single_dollar": "training curves for $Nb_3Sn$ Rutherford cables",
            "explicit": r"Einstein wrote \(E=mc^2\).",
            "display": DISPLAY_MATH,
        }
    )

    for path in ("fallback", "marked"):
        reported = rendered["reported"][path]
        assert REPORTED_POWER in reported, path
        assert REPORTED_AVERAGE in reported, path
        assert "$P_{ac}$" not in reported, path
        assert "\\\\(" not in reported, path

        assert "$Nb_3Sn$" in rendered["single_dollar"][path], path
        assert r"\(E=mc^2\)" in rendered["explicit"][path], path
        assert r"\[ \Delta = \{ x \mid x_1 * x_2 \} \\ y \]" in rendered["display"][path], path


def test_assistant_math_is_html_escaped_when_restored() -> None:
    rendered = _run_renderer({"lt": r"si \( a < b \) alors <b>x</b>"})

    for path in ("fallback", "marked"):
        assert r"\( a &lt; b \)" in rendered["lt"][path], path


def _normalize_inline_math(samples: dict[str, str]) -> dict[str, str]:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend math regression test")

    source = read_app_javascript()
    functions = _slice_js(source, "function _looksLikeSafeInlineMath(", "function _convertHtmlSubSupToLatex(")
    script = f"""
{functions}
const samples = {json.dumps(samples)};
const result = {{}};
for (const [name, text] of Object.entries(samples)) result[name] = _normalizeSingleDollarMathInText(text);
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        [node, "-e", script], check=True, capture_output=True, text=True, encoding="utf-8", cwd=ROOT
    )
    return json.loads(completed.stdout)


def test_inline_math_normalization_leaves_prose_parentheses_alone() -> None:
    # Codex audit BB-02 (2026-10-25): a valid REBCO / E(J) answer rendered with
    # raw "$n$", "(par exemple 28 ou 31)" set as a formula and
    # "(souvent $10^{-4}$ V/m)" wrapped in nested delimiters.
    out = _normalize_inline_math(
        {
            "symbols": "l'exposant $n$ (par exemple 28 ou 31), le champ $B$ et la température $T$.",
            "nested": "le critère $E_c$ (souvent $10^{-4}$ V/m) est fixé",
            "power_law": "la loi $E = E_c (J/J_c)^n$ s'applique",
            "price": "entre $5 et $10 (2 ans)",
            "price_then_math": "$5 ou $x$ final",
            "double_paren": r"la résistivité ((\rho_{eq})) puis (x_1 + x_2)",
            "existing": r"déjà \(a_1\) et $$x^2$$",
            "prose_with_math": r"(voir \alpha pour plus de détails)",
        }
    )

    assert out["symbols"] == (
        r"l'exposant \(n\) (par exemple 28 ou 31), le champ \(B\) et la température \(T\)."
    )
    assert out["nested"] == r"le critère \(E_c\) (souvent \(10^{-4}\) V/m) est fixé"
    assert out["power_law"] == r"la loi \(E = E_c (J/J_c)^n\) s'applique"
    assert out["price"] == "entre $5 et $10 (2 ans)"
    assert out["price_then_math"] == r"$5 ou \(x\) final"
    assert out["double_paren"] == r"la résistivité \(\rho_{eq}\) puis \(x_1 + x_2\)"
    assert out["existing"] == r"déjà \(a_1\) et $$x^2$$"
    assert out["prose_with_math"] == r"(voir \alpha pour plus de détails)"


def test_inline_math_keeps_long_formula_with_sized_delimiters() -> None:
    # Tester report 2026-10-28: the answer showed
    # "$e = \left[ \frac{E_0}{J_c} \left\frac{|j|}{J_c} \right^{n-1} ...$".
    # The command names counted as prose words, so the dollar span was left as
    # text and "\left( ... \right)" was split into a separate formula.
    formula = r"e = \left[ \frac{E_0}{J_c} \left( \frac{|j|}{J_c} \right)^{n-1} \right] \star j"
    out = _normalize_inline_math(
        {
            "power_law": f"La loi s'écrit ${formula}$ dans ce modèle.",
            "bare_left_right": r"avec \left( \frac{a}{b} \right) seul",
            "sentence": r"$Voir la figure \ref{fig} pour plus de détails. Ensuite$",
        }
    )

    assert out["power_law"] == f"La loi s'écrit \\({formula}\\) dans ce modèle."
    assert out["bare_left_right"] == r"avec \left( \frac{a}{b} \right) seul"
    assert out["sentence"] == r"$Voir la figure \ref{fig} pour plus de détails. Ensuite$"
