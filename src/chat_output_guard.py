from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

GuardSeverity = Literal["ok", "warning", "error"]

_MALFORMED_SOURCE_CITATION_RE = re.compile(
    r"\[\s*S\s*['’]\s*(\d+)\s*\]",
    flags=re.IGNORECASE,
)
_RAW_CHUNK_CITATION_RE = re.compile(r"\[\s*CHUNK\s+[^\]]+\]", flags=re.IGNORECASE)
_BRACKET_RE = re.compile(r"\[([^\]]*)\]")
_SOURCE_ID_RE = re.compile(r"^S(\d+)$", flags=re.IGNORECASE)
_RAW_NUMERIC_CITATION_RE = re.compile(r"\[\s*\d+\s*\]")
_EMPTY_BRACKET_RE = re.compile(r"\[\s*\]")
_PLACEHOLDER_CITATION_RE = re.compile(r"\[\s*\*+\s*\]|\*{4,}")
_UNRESOLVED_CHUNKISH_PART_RE = re.compile(r"^(?:CHUNK\s+)?\d+\s*:\s*.+$", flags=re.IGNORECASE)
_LABEL_RE = re.compile(r"(?im)^\s*\**\s*(Answer|Explanation|Response\s+note)\s*:\s*\**")
_DRAFTING_MARKER_RE = re.compile(
    r"(?im)^\s*(?:\*+\s*)?(?:Self[- ]Correction(?:\s+during\s+drafting)?|"
    r"Refined\s+Explanation|Final\s+Polish|Check\s+citations|Check\s+structure|"
    r"Check\s+language|Check\s+constraints)\s*:",
)
_HIDDEN_REASONING_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"<\s*/?\s*think\s*>", flags=re.IGNORECASE),
    re.compile(r"<\|?channel\|?>", flags=re.IGNORECASE),
    re.compile(r"\bchain[- ]of[- ]thought\b", flags=re.IGNORECASE),
    re.compile(r"\braisonnement interne\b", flags=re.IGNORECASE),
    re.compile(r"\bhidden reasoning\b", flags=re.IGNORECASE),
)
_RANKING_TERMS: tuple[str, ...] = (
    "best",
    "fastest",
    "meilleur",
    "meilleure",
    "plus rapide",
    "recommended",
    "recommande",
    "recommandee",
)
_UNKNOWN_PHRASES: tuple[str, ...] = (
    "je ne sais pas",
    "ne permet pas de determiner",
    "ne permettent pas de determiner",
    "pas possible de determiner",
    "impossible de determiner",
    "n est pas specifie",
    "n est pas specifiee",
    "pas dans le contexte",
    "aucune information ne permet",
    "ne fournit pas d information",
    "not answerable from the provided context",
    "not available in the context",
)
_TECHNICAL_HINTS: tuple[str, ...] = (
    "ac",
    "aimant",
    "champ",
    "comsol",
    "conducteur",
    "couplage",
    "courant",
    "foucault",
    "frequence",
    "formulation",
    "hysteresis",
    "hysteret",
    "magnet",
    "modele",
    "perte",
    "ruban",
    "simulation",
    "substrat",
    "supraconduct",
)


@dataclass(frozen=True)
class ChatGuardIssue:
    code: str
    severity: GuardSeverity
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


@dataclass(frozen=True)
class ChatGuardReport:
    ok: bool
    severity: GuardSeverity
    issues: list[ChatGuardIssue] = field(default_factory=list)
    normalized_text: str = ""
    cited_labels: list[str] = field(default_factory=list)
    valid_labels: list[str] = field(default_factory=list)
    missing_labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "severity": self.severity,
            "issues": [issue.to_dict() for issue in self.issues],
            "normalized_text": self.normalized_text,
            "cited_labels": list(self.cited_labels),
            "valid_labels": list(self.valid_labels),
            "missing_labels": list(self.missing_labels),
        }


def _normalize_for_matching(text: str | None) -> str:
    value = unicodedata.normalize("NFKD", str(text or "").lower())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _canonical_label(label: str | None) -> str:
    value = str(label or "").strip()
    if not value:
        return ""
    source_match = _SOURCE_ID_RE.fullmatch(value)
    if source_match:
        return f"S{int(source_match.group(1))}"
    return value.upper()


def _metadata_from_source(source: Any) -> dict[str, Any]:
    if isinstance(source, dict):
        return dict(source)
    metadata = getattr(source, "metadata", None)
    if isinstance(metadata, dict):
        return dict(metadata)
    return {}


def collect_valid_source_labels(sources: list[Any] | tuple[Any, ...] | None) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for index, source in enumerate(list(sources or []), start=1):
        candidates: list[str] = []
        if isinstance(source, str):
            candidates.append(source)
        metadata = _metadata_from_source(source)
        explicit_label = str(metadata.get("ref_label", "") or "").strip()
        article_id = str(metadata.get("article_id", "") or "").strip()
        chunk_id = metadata.get("chunk_id")
        if explicit_label:
            candidates.append(explicit_label)
        elif not isinstance(source, str):
            # Wiki passages use provenance labels; other unlabelled passages
            # fall back to their position. Never alias an explicit S9 to S1.
            if metadata.get("wiki_provenance") and article_id and chunk_id not in ("", None):
                candidates.append(f"{article_id}:{chunk_id}")
            else:
                candidates.append(f"S{index}")
        candidates.append(str(metadata.get("chunk_uid", "") or ""))
        if article_id and chunk_id not in ("", None):
            candidates.append(f"{article_id}:{chunk_id}")

        for candidate in candidates:
            canonical = _canonical_label(candidate)
            if canonical and canonical not in seen:
                labels.append(canonical)
                seen.add(canonical)
    return labels


def _extract_cited_labels(text: str) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for match in _BRACKET_RE.finditer(text):
        raw = str(match.group(1) or "").strip()
        if not raw:
            continue
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        for part in parts:
            canonical = ""
            if _SOURCE_ID_RE.fullmatch(part):
                canonical = _canonical_label(part)
            elif re.fullmatch(r"[A-Za-z0-9:_\-./]+", part) and (
                ":" in part or "/" in part or part.lower().startswith("chunk")
            ):
                canonical = _canonical_label(part)
            elif "source" in part.lower():
                canonical = _canonical_label(part)
            if canonical and canonical not in seen:
                labels.append(canonical)
                seen.add(canonical)
    return labels


def _looks_like_unresolved_citation_part(part: str) -> bool:
    value = str(part or "").strip()
    if not value:
        return False
    if _SOURCE_ID_RE.fullmatch(value):
        return False
    if re.fullmatch(r"[A-Za-z0-9:_\-./]+", value) and (
        ":" in value or "/" in value or value.lower().startswith("chunk")
    ):
        return False
    if "source" in value.lower():
        return False
    return bool(_UNRESOLVED_CHUNKISH_PART_RE.fullmatch(value))


def _canonical_citation_part(part: str) -> str:
    value = str(part or "").strip()
    if not value:
        return ""
    chunk_match = re.fullmatch(r"CHUNK\s+(.+)", value, flags=re.IGNORECASE)
    if chunk_match:
        value = str(chunk_match.group(1) or "").strip()
    if _SOURCE_ID_RE.fullmatch(value):
        return _canonical_label(value)
    if re.fullmatch(r"[A-Za-z0-9:_\-./]+", value) and (
        ":" in value or "/" in value or value.lower().startswith("chunk")
    ):
        return _canonical_label(value)
    if "source" in value.lower():
        return _canonical_label(value)
    return ""


def _cleanup_citation_artifacts(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"\b(et|ou|and)\s+([,.;:!?])", r"\2", value, flags=re.IGNORECASE)
    value = re.sub(r"\(\s*\)", "", value)
    value = re.sub(r",\s*,+", ", ", value)
    value = re.sub(r"[ \t]+([,.;:!?])", r"\1", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def strip_invalid_citations(
    text: str | None,
    *,
    sources: list[Any] | tuple[Any, ...] | None = None,
) -> str:
    """Remove citation brackets that do not resolve to provided sources.

    The sentence is preserved; only invalid citation markers are removed. Valid
    mixed citations such as ``[S1, S9]`` are reduced to ``[S1]``.
    """

    valid_label_set = set(collect_valid_source_labels(sources))
    if not valid_label_set:
        return _cleanup_citation_artifacts(str(text or ""))

    normalized_text = _MALFORMED_SOURCE_CITATION_RE.sub(r"[S\1]", str(text or ""))
    normalized_text = _RAW_CHUNK_CITATION_RE.sub(
        lambda match: f"[{str(match.group(0))[1:-1].strip()[5:].strip()}]",
        normalized_text,
    )

    def _replace(match: re.Match[str]) -> str:
        raw = str(match.group(1) or "").strip()
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        if not parts:
            return ""

        kept: list[str] = []
        saw_citation_like = False
        for part in parts:
            if re.fullmatch(r"\d+", part):
                saw_citation_like = True
                continue
            if re.fullmatch(r"\*+", part):
                saw_citation_like = True
                continue
            # The prompt's own notation, copied literally by small models.
            if re.fullmatch(r"S[xn]", part, flags=re.IGNORECASE):
                saw_citation_like = True
                continue
            canonical = _canonical_citation_part(part)
            if canonical:
                saw_citation_like = True
                if canonical in valid_label_set and canonical not in kept:
                    kept.append(canonical)
                continue
            if _looks_like_unresolved_citation_part(part):
                saw_citation_like = True
                continue

        if kept:
            return f"[{', '.join(kept)}]"
        return "" if saw_citation_like else match.group(0)

    return _cleanup_citation_artifacts(_BRACKET_RE.sub(_replace, normalized_text))


def _find_unresolved_citation_brackets(text: str) -> list[str]:
    unresolved: list[str] = []
    seen: set[str] = set()
    for match in _BRACKET_RE.finditer(text):
        raw = str(match.group(1) or "").strip()
        if not raw:
            continue
        parts = [part.strip() for part in raw.split(",") if part.strip()]
        flagged = [part for part in parts if _looks_like_unresolved_citation_part(part)]
        if not flagged:
            continue
        rendered = f"[{', '.join(flagged)}]"
        if rendered in seen:
            continue
        seen.add(rendered)
        unresolved.append(rendered)
    return unresolved


def _severity_from_issues(issues: list[ChatGuardIssue]) -> GuardSeverity:
    if any(issue.severity == "error" for issue in issues):
        return "error"
    if any(issue.severity == "warning" for issue in issues):
        return "warning"
    return "ok"


def _has_unknown_caveat(text: str) -> bool:
    normalized = _normalize_for_matching(text)
    return any(phrase in normalized for phrase in _UNKNOWN_PHRASES)


def _contains_ranking_language(text: str) -> bool:
    normalized = _normalize_for_matching(text)
    return any(term in normalized for term in _RANKING_TERMS)


def _sentence_has_citation(sentence: str) -> bool:
    return bool(_BRACKET_RE.search(sentence))


def _sentence_looks_technical(sentence: str) -> bool:
    normalized = _normalize_for_matching(sentence)
    if len(normalized.split()) < 7:
        return False
    return any(hint in normalized for hint in _TECHNICAL_HINTS)


def _find_uncited_technical_sentences(text: str) -> list[str]:
    body = re.sub(r"(?im)^\s*(Answer|Explanation|Response\s+note)\s*:\s*", "", text)
    sentences = re.split(r"(?<=[.!?])\s+|\n+", body)
    flagged: list[str] = []
    for sentence in sentences:
        value = sentence.strip()
        if not value or _sentence_has_citation(value):
            continue
        if _sentence_looks_technical(value):
            flagged.append(value)
        if len(flagged) >= 3:
            break
    return flagged


def validate_chat_output(
    text: str | None,
    *,
    sources: list[Any] | tuple[Any, ...] | None = None,
    context_text: str | None = None,
    require_structure: bool = False,
    flag_uncited_technical_sentences: bool = False,
) -> ChatGuardReport:
    original = str(text or "")
    normalized_text = _MALFORMED_SOURCE_CITATION_RE.sub(r"[S\1]", original)
    normalized_text = _RAW_CHUNK_CITATION_RE.sub(
        lambda match: f"[{str(match.group(0))[1:-1].strip()[5:].strip()}]",
        normalized_text,
    ).strip()
    issues: list[ChatGuardIssue] = []

    if not normalized_text:
        issues.append(
            ChatGuardIssue(
                code="empty_answer",
                severity="error",
                message="La reponse est vide.",
            )
        )

    leak_patterns = [
        pattern.pattern
        for pattern in _HIDDEN_REASONING_PATTERNS
        if pattern.search(normalized_text)
    ]
    if leak_patterns:
        issues.append(
            ChatGuardIssue(
                code="hidden_reasoning_leak",
                severity="error",
                message="La reponse contient des traces de raisonnement cache.",
                detail={"patterns": leak_patterns},
            )
        )

    if _DRAFTING_MARKER_RE.search(normalized_text):
        issues.append(
            ChatGuardIssue(
                code="drafting_marker",
                severity="error",
                message="La reponse contient des marqueurs de brouillon.",
            )
        )

    raw_chunk_citations = [match.group(0) for match in _RAW_CHUNK_CITATION_RE.finditer(original)]
    if raw_chunk_citations:
        issues.append(
            ChatGuardIssue(
                code="raw_chunk_citation",
                severity="error",
                message="La reponse contient une citation CHUNK brute non resolue.",
                detail={"citations": raw_chunk_citations},
            )
        )

    if _RAW_NUMERIC_CITATION_RE.search(normalized_text):
        issues.append(
            ChatGuardIssue(
                code="raw_numeric_citation",
                severity="error",
                message="La reponse contient une citation numerique brute.",
            )
        )

    if _EMPTY_BRACKET_RE.search(normalized_text):
        issues.append(
            ChatGuardIssue(
                code="empty_bracket_citation",
                severity="error",
                message="La reponse contient des crochets de citation vides.",
            )
        )

    if _PLACEHOLDER_CITATION_RE.search(normalized_text):
        issues.append(
            ChatGuardIssue(
                code="placeholder_citation",
                severity="error",
                message="La reponse contient une citation placeholder.",
            )
        )

    labels = [match.group(1).lower().replace(" ", "_") for match in _LABEL_RE.finditer(normalized_text)]
    if require_structure:
        missing = [
            label
            for label in ("answer", "explanation", "response_note")
            if labels.count(label) != 1
        ]
        if missing:
            issues.append(
                ChatGuardIssue(
                    code="missing_required_structure",
                    severity="error",
                    message="La structure Answer / Explanation / Response note est incomplete ou dupliquee.",
                    detail={"labels": labels, "missing_or_duplicate": missing},
                )
            )

    valid_labels = collect_valid_source_labels(sources)
    valid_label_set = set(valid_labels)
    cited_labels = _extract_cited_labels(normalized_text)
    unresolved_brackets = _find_unresolved_citation_brackets(normalized_text)
    if unresolved_brackets:
        issues.append(
            ChatGuardIssue(
                code="unresolved_citation_bracket",
                severity="error",
                message="La reponse contient une pseudo-citation qui ne correspond a aucune source exploitable.",
                detail={"citations": unresolved_brackets},
            )
        )
    missing_labels = [
        label for label in cited_labels if valid_label_set and label not in valid_label_set
    ]
    if missing_labels:
        issues.append(
            ChatGuardIssue(
                code="unknown_source_citation",
                severity="error",
                message="La reponse cite une source qui n'existe pas dans le contexte fourni.",
                detail={"missing_labels": missing_labels, "valid_labels": valid_labels},
            )
        )

    if (
        _contains_ranking_language(normalized_text)
        and not _has_unknown_caveat(normalized_text)
        and not _contains_ranking_language(context_text or "")
    ):
        issues.append(
            ChatGuardIssue(
                code="unsupported_ranking_language",
                severity="warning",
                message="La reponse contient un classement ou une recommandation non soutenue par le contexte.",
            )
        )

    if flag_uncited_technical_sentences and sources:
        uncited = _find_uncited_technical_sentences(normalized_text)
        if uncited:
            issues.append(
                ChatGuardIssue(
                    code="uncited_technical_sentence",
                    severity="warning",
                    message="Certaines phrases techniques ne contiennent pas de citation.",
                    detail={"sentences": uncited},
                )
            )

    severity = _severity_from_issues(issues)
    return ChatGuardReport(
        ok=severity != "error",
        severity=severity,
        issues=issues,
        normalized_text=normalized_text,
        cited_labels=cited_labels,
        valid_labels=valid_labels,
        missing_labels=missing_labels,
    )


__all__ = [
    "ChatGuardIssue",
    "ChatGuardReport",
    "collect_valid_source_labels",
    "strip_invalid_citations",
    "validate_chat_output",
]
