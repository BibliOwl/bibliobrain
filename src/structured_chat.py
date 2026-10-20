from __future__ import annotations

import re
import unicodedata
from typing import Any

import instructor
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator

# Fail-fast: small models stuck in JSON-repair loops freeze the GPU.
# Single attempt; the caller surfaces the validation error.
STRUCTURED_OUTPUT_MAX_RETRIES = 1

_CHUNK_CITATION_PATTERN = re.compile(
    r"\[\s*CHUNK\s+([^\]]+?)\s*\]",
    flags=re.IGNORECASE,
)


def normalize_chunk_citation_markup(text: str | None) -> str:
    def _replace(match: re.Match[str]) -> str:
        inner = str(match.group(1) or "").strip()
        return f"[{inner}]" if inner else "[]"

    return _CHUNK_CITATION_PATTERN.sub(_replace, str(text or ""))


_CHAT_ANSWER_FIELD_ALIASES: dict[str, str] = {
    "answer": "answer",
    "reponse": "answer",
    "final_answer": "answer",
    "explanation": "explanation",
    "justification": "explanation",
    "response_note": "response_note",
    "response note": "response_note",
    "note": "response_note",
}

_CONTENT_VALUE_KEYS = {"texte", "text", "content", "value", "resume", "summary"}
_LEGACY_CHAT_SECTION_RE = re.compile(
    r"(?im)^\s*\*{0,2}\s*(Answer|Explanation|Response note)\s*\*{0,2}\s*:\s*"
)
_NO_USEFUL_NOTE_RE = re.compile(r"(?is)\s*(aucune note utile\.?|no useful note\.?)\s*")


def _normalize_json_key(value: Any) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", ascii_value).strip("_")


def _stringify_section_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = [_stringify_section_value(item) for item in value]
        parts = [part for part in parts if part]
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return "\n".join(f"- {part}" for part in parts)
    if isinstance(value, dict):
        normalized_keys = {_normalize_json_key(key): key for key in value}
        if len(value) == 1:
            return _stringify_section_value(next(iter(value.values())))
        for content_key in _CONTENT_VALUE_KEYS:
            if content_key in normalized_keys:
                content = _stringify_section_value(value[normalized_keys[content_key]])
                if content:
                    return content
        mapping_parts: list[str] = []
        for key, item in value.items():
            text = _stringify_section_value(item)
            if text:
                mapping_parts.append(f"{str(key).strip()}: {text}")
        return "\n".join(mapping_parts).strip()
    return str(value).strip()


def _coerce_sectioned_json_to_chat_answer(value: Any) -> Any:
    if not isinstance(value, dict):
        return value

    coerced: dict[str, Any] = {}
    sections: list[tuple[str, str]] = []
    for key, item in value.items():
        raw_key = str(key or "").strip()
        normalized_key = _normalize_json_key(raw_key)
        target = _CHAT_ANSWER_FIELD_ALIASES.get(normalized_key) or _CHAT_ANSWER_FIELD_ALIASES.get(
            raw_key.lower()
        )
        if target:
            coerced[target] = item
            continue

        text = _stringify_section_value(item)
        if text:
            sections.append((raw_key.replace("_", " "), text))

    if "answer" in coerced:
        if sections:
            extra = "\n\n".join(f"**{title}**\n{text}" for title, text in sections if title)
            if extra:
                current_answer = _stringify_section_value(coerced["answer"])
                coerced["answer"] = "\n\n".join(part for part in [current_answer, extra] if part)
        return coerced

    if sections:
        coerced["answer"] = "\n\n".join(f"**{title}**\n{text}" for title, text in sections if title)
        return coerced

    return value


def clean_chat_answer_markdown(text: str | None) -> str:
    value = str(text or "").strip()
    if not value:
        return ""

    matches = list(_LEGACY_CHAT_SECTION_RE.finditer(value))
    if not matches:
        return value

    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        label = match.group(1).lower().replace(" ", "_")
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        content = value[start:end].strip()
        if content and label not in sections:
            sections[label] = content

    answer = sections.get("answer", "").strip()
    explanation = sections.get("explanation", "").strip()
    note = sections.get("response_note", "").strip()
    if _NO_USEFUL_NOTE_RE.fullmatch(note):
        note = ""

    return "\n\n".join(part for part in (answer, explanation, note) if part).strip() or value


class ChatAnswer(BaseModel):
    answer: str = Field(
        ...,
        description=(
            "Direct answer to the user's question. Plain text, no markdown fences, "
            "no meta-commentary about the corpus or excerpts."
        ),
    )
    explanation: str = Field(
        "",
        description=(
            "Short evidence-grounded justification of the answer. "
            "Empty string when no extra justification is useful."
        ),
    )
    response_note: str = Field(
        "",
        description=(
            "Caveat, limitation, or 'no useful note' marker. "
            "Empty string when there is nothing relevant to add."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def coerce_sectioned_json(cls, value: Any) -> Any:
        return _coerce_sectioned_json_to_chat_answer(value)

    def format_as_markdown(self) -> str:
        answer = normalize_chunk_citation_markup(self.answer).strip()
        explanation = normalize_chunk_citation_markup(self.explanation).strip()
        note = normalize_chunk_citation_markup(self.response_note).strip()
        lines = ["Answer:", answer, "", "Explanation:", explanation, "", "Response note:"]
        if note:
            lines.append(note)
        return "\n".join(lines).strip()

    def format_as_analysis_markdown(self) -> str:
        answer = normalize_chunk_citation_markup(self.answer).strip()
        explanation = normalize_chunk_citation_markup(self.explanation).strip()
        note = normalize_chunk_citation_markup(self.response_note).strip()
        if _NO_USEFUL_NOTE_RE.fullmatch(note):
            note = ""
        return "\n\n".join(part for part in (answer, explanation, note) if part).strip()


def build_instructor_client(
    *,
    base_url: str,
    api_key: str,
    timeout: float | None = None,
    http_client: Any = None,
) -> instructor.Instructor:
    openai_kwargs: dict[str, Any] = {"base_url": base_url, "api_key": api_key or "not-needed"}
    if timeout is not None:
        openai_kwargs["timeout"] = timeout
    if http_client is not None:
        openai_kwargs["http_client"] = http_client
    return instructor.from_openai(OpenAI(**openai_kwargs), mode=instructor.Mode.JSON)


__all__ = [
    "STRUCTURED_OUTPUT_MAX_RETRIES",
    "ChatAnswer",
    "build_instructor_client",
    "clean_chat_answer_markdown",
    "normalize_chunk_citation_markup",
]
