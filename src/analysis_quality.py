"""Shared intent detection and guidance for substantive article analysis."""

from __future__ import annotations

import re
import unicodedata


def _normalize_analysis_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_text).strip()


def is_deep_article_analysis_request(message: str) -> bool:
    """Return whether the user explicitly asks for an in-depth article analysis."""
    text = _normalize_analysis_text(message)
    if not text:
        return False
    asks_for_analysis = any(
        marker in text
        for marker in (
            "analyse detaillee",
            "analyse approfondie",
            "analyse critique",
            "analyse cet article",
            "analyse l'article",
            "analyse le papier",
            "analyse ce document",
            "faire une analyse de cet article",
            "faire une analyse de l'article",
            "analyser en detail",
            "approfondis l'analyse",
            "approfondir l'analyse",
            "detaille l'analyse",
            "detailed analysis",
            "in-depth analysis",
            "deep analysis",
            "critical analysis",
            "review this article",
            "analyse this article",
            "analyze this article",
        )
    )
    expansion_followup = (
        ("detaille" in text and "plus" in text)
        or "plus de details" in text
        or "davantage de details" in text
        or "go into more detail" in text
        or "expand the analysis" in text
    )
    article_ref = any(
        marker in text
        for marker in (
            "article",
            "papier",
            "paper",
            "source",
            "document",
            "etude",
            "study",
            "cet ",
            "cette ",
            "celui-ci",
            "celle-ci",
            "this ",
        )
    )
    return (asks_for_analysis and article_ref) or expansion_followup


def is_detailed_article_summary_request(message: str) -> bool:
    """Return whether the user asks for a long, substantive summary."""
    text = _normalize_analysis_text(message)
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "resume detaille",
            "resume approfondi",
            "resume complet",
            "resume en detail",
            "synthese detaillee",
            "synthese approfondie",
            "synthese complete",
            "detailed summary",
            "in-depth summary",
            "comprehensive summary",
            "summarize in detail",
        )
    )


def detailed_summary_guidance(*, language: str = "fr", citation_format: str = "[Sx]") -> str:
    """Build an evidence-aware response contract for a detailed summary."""
    if language == "en":
        return (
            "Write a detailed summary of the substantive document, not of its bibliographic front matter. "
            "When the evidence supports it, cover the research context and objective, the proposed method "
            "or mechanism, the experimental or analytical setup, the main quantitative results, the authors' "
            "conclusion, and the limitations or open questions stated or directly supported by the text. "
            "Prioritize the abstract and the main body over publisher notices, related-content lists, and "
            f"references. Attach {citation_format} to concrete source-based claims and never invent missing details."
        )
    return (
        "Redige un resume detaille du fond du document, pas de sa page bibliographique. Lorsque les sources "
        "le permettent, couvre le contexte et l'objectif de recherche, la methode ou le mecanisme propose, "
        "le dispositif experimental ou analytique, les principaux resultats quantitatifs, la conclusion des "
        "auteurs, ainsi que les limites ou questions ouvertes mentionnees ou directement appuyees par le texte. "
        "Privilegie l'abstract et le corps de l'article par rapport aux notices d'editeur, listes de contenus "
        f"connexes et references. Associe {citation_format} aux affirmations factuelles et n'invente aucun detail absent."
    )


def deep_analysis_guidance(*, language: str = "fr", citation_format: str = "[Sx]") -> str:
    """Build a rigorous, evidence-aware response contract for article analysis."""
    if language == "en":
        return (
            "This is an in-depth analysis, not an expanded abstract or a simple summary. "
            "When the evidence supports it, aim for roughly 700 to 1,200 words and use clear, "
            "descriptive headings. Cover: (1) research problem and objective; (2) precise original "
            "contribution and difference from prior approaches; (3) methodological chain, inputs, "
            "assumptions and key equations or mechanisms actually present in the sources; "
            "(4) validation protocol, baselines, quantitative results and what they demonstrate; "
            "(5) interpretation, scope and practical or scientific implications; (6) limitations, "
            "threats to validity and unresolved questions; (7) internal consistency: values that "
            "disagree between sections, conclusions that outrun the results, assumptions not respected, "
            "weak statistics, citing both passages and rating severity, and saying so explicitly when "
            "none is found; and (8) a concise overall assessment ending with a robustness verdict. "
            "Information absent from the sources is not an inconsistency. "
            "Distinguish explicitly between author claims, evidence-supported interpretation, and "
            "information absent from the available text. Never invent a value, equation, comparison, "
            f"or limitation. Attach {citation_format} to each concrete source-based claim. Avoid "
            "repeating the same high-level summary across sections and do not pad the answer when the "
            "available evidence is limited."
        )
    return (
        "Il s'agit d'une analyse approfondie, pas d'un resume allonge ni d'une paraphrase de "
        "l'abstract. Lorsque les sources le permettent, vise environ 700 a 1 200 mots et utilise des "
        "intertitres descriptifs. Traite: (1) le probleme scientifique et l'objectif; (2) la "
        "contribution originale precise et sa difference avec les approches anterieures; (3) la chaine "
        "methodologique, les entrees, hypotheses et equations ou mecanismes effectivement presents "
        "dans les sources; (4) le protocole de validation, les references de comparaison, les "
        "resultats quantitatifs et ce qu'ils demontrent; (5) l'interpretation, le domaine de validite "
        "et les implications scientifiques ou pratiques; (6) les limites, menaces sur la validite et "
        "questions ouvertes; (7) la coherence interne: valeurs discordantes entre sections, "
        "conclusions qui depassent les resultats, hypotheses non respectees, statistiques fragiles, "
        "en citant les deux passages et en qualifiant la gravite, et en le disant explicitement si "
        "rien n'est trouve; puis (8) une appreciation globale concise terminee par un verdict de "
        "robustesse. Une information absente des sources n'est pas une incoherence. Distingue explicitement les "
        "affirmations des auteurs, les interpretations appuyees par les preuves et les informations "
        "absentes du texte disponible. N'invente jamais une valeur, une equation, une comparaison ou "
        f"une limite. Associe {citation_format} a chaque affirmation factuelle issue des sources. Evite "
        "de repeter le meme resume general dans chaque section et ne remplis pas artificiellement si "
        "les preuves disponibles sont limitees."
    )


__all__ = [
    "deep_analysis_guidance",
    "detailed_summary_guidance",
    "is_deep_article_analysis_request",
    "is_detailed_article_summary_request",
]
