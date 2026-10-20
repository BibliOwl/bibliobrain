from __future__ import annotations

import re
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class ModelProfile:
    family: str
    ollama_thinking_mode: str = "none"
    discovery_query_note: str = ""
    discovery_summary_note: str = ""
    discovery_validate_note: str = ""
    discovery_temperature: float = 0.05
    discovery_repeat_penalty: float = 1.1
    discovery_top_p: float = 0.2


_GENERIC_CHAT_RESPONSE_NOTE = (
    "Return only the final user-facing answer in natural prose. "
    "Do not use mandatory wrapper sections such as Answer, Explanation, or Response note. "
    "Ground factual claims in the provided context and preserve its exact technical terms. "
    "Cite source-grounded claims inline with the exact allowed [Sx] labels only. "
    "Preserve valid citation labels exactly and never invent or rewrite them. "
    "If the provided context does not answer the request, say so explicitly and do not guess from outside knowledge. "
    "Do not emit thinking sections, hidden reasoning, XML tags, channel markers, tool commentary, or schema explanations."
)


_GENERIC_DISCOVERY_QUERY_NOTE = ""

_GENERIC_DISCOVERY_SUMMARY_NOTE = (
    "Each summary must be a complete, concrete sentence."
)

_GENERIC_DISCOVERY_VALIDATE_NOTE = (
    "For every evaluated article, always provide an integer relevance_score and a non-empty rationale. "
    "Do not give the same score to every article unless the evidence truly supports that outcome."
)


def _build_mistral_generalist_profile(
    family: str,
) -> ModelProfile:
    return ModelProfile(
        family=family,
        ollama_thinking_mode="none",
        discovery_query_note=(
            _GENERIC_DISCOVERY_QUERY_NOTE
            + " Do not switch into tool-calling, agent commentary, or schema explanation."
        ),
        discovery_summary_note=(
            _GENERIC_DISCOVERY_SUMMARY_NOTE
            + " Keep the sentence direct and avoid assistant-style filler."
        ),
        discovery_validate_note=(
            _GENERIC_DISCOVERY_VALIDATE_NOTE
            + " Use differentiated scores when abstracts clearly differ."
        ),
        discovery_temperature=0.02,
        discovery_repeat_penalty=1.12,
        discovery_top_p=0.15,
    )


_MODEL_PROFILES: dict[str, ModelProfile] = {
    "generic": ModelProfile(
        family="generic",
        ollama_thinking_mode="none",
        discovery_query_note=_GENERIC_DISCOVERY_QUERY_NOTE,
        discovery_summary_note=_GENERIC_DISCOVERY_SUMMARY_NOTE,
        discovery_validate_note=_GENERIC_DISCOVERY_VALIDATE_NOTE,
    ),
    "qwen3.5": ModelProfile(
        family="qwen3.5",
        ollama_thinking_mode="none",
        discovery_query_note=(
            _GENERIC_DISCOVERY_QUERY_NOTE
            + " Return only the requested query fields without a preamble."
        ),
        discovery_summary_note=(
            _GENERIC_DISCOVERY_SUMMARY_NOTE
            + " Each summary must be one useful sentence, not a fragment."
        ),
        discovery_validate_note=(
            _GENERIC_DISCOVERY_VALIDATE_NOTE
            + " Never leave rationale empty. Avoid collapsing all articles to score 0 unless each abstract is clearly unrelated."
        ),
        discovery_temperature=0.0,
        discovery_repeat_penalty=1.15,
        discovery_top_p=0.12,
    ),
    "qwen3.6": ModelProfile(
        family="qwen3.6",
        ollama_thinking_mode="bool",
        discovery_query_note=(
            _GENERIC_DISCOVERY_QUERY_NOTE + " Keep reasoning hidden and avoid prefatory text."
        ),
        discovery_summary_note=(
            _GENERIC_DISCOVERY_SUMMARY_NOTE + " Use one concrete sentence per item."
        ),
        discovery_validate_note=(
            _GENERIC_DISCOVERY_VALIDATE_NOTE
            + " Always include a specific rationale and use differentiated scores when abstracts differ."
        ),
        discovery_temperature=0.0,
        discovery_repeat_penalty=1.14,
        discovery_top_p=0.12,
    ),
    "gemma4": ModelProfile(
        family="gemma4",
        ollama_thinking_mode="bool",
        discovery_query_note=(
            _GENERIC_DISCOVERY_QUERY_NOTE
            + " Never emit Gemma channel markers such as <|channel|> or thought blocks."
        ),
        discovery_summary_note=(
            _GENERIC_DISCOVERY_SUMMARY_NOTE
            + " Never emit Gemma channel markers or hidden thought tags."
        ),
        discovery_validate_note=(
            _GENERIC_DISCOVERY_VALIDATE_NOTE
            + " Never emit Gemma channel markers or hidden thought tags."
        ),
        discovery_temperature=0.04,
        discovery_repeat_penalty=1.08,
        discovery_top_p=0.18,
    ),
    "ministral-3": _build_mistral_generalist_profile(
        "ministral-3",
    ),
    "ministral-3:14b": _build_mistral_generalist_profile(
        "ministral-3",
    ),
    "mistral-small": _build_mistral_generalist_profile(
        "mistral-small",
    ),
    "mistral-medium": _build_mistral_generalist_profile(
        "mistral-medium",
    ),
    "mistral-large": _build_mistral_generalist_profile(
        "mistral-large",
    ),
    "codestral": ModelProfile(
        family="codestral",
        ollama_thinking_mode="none",
        discovery_query_note=(
            _GENERIC_DISCOVERY_QUERY_NOTE
            + " Do not switch into code-completion, tool, or agent behavior."
        ),
        discovery_summary_note=(
            _GENERIC_DISCOVERY_SUMMARY_NOTE
            + " Do not write code comments, schema notes, or assistant chatter."
        ),
        discovery_validate_note=(
            _GENERIC_DISCOVERY_VALIDATE_NOTE
            + " Evaluate papers as a research assistant, not as a coding copilot."
        ),
        discovery_temperature=0.01,
        discovery_repeat_penalty=1.12,
        discovery_top_p=0.14,
    ),
    "nemotron-3": ModelProfile(
        family="nemotron-3",
        ollama_thinking_mode="bool",
        discovery_query_note=(
            _GENERIC_DISCOVERY_QUERY_NOTE
            + " Return only the final answer even if the model reasons internally first."
        ),
        discovery_summary_note=(
            _GENERIC_DISCOVERY_SUMMARY_NOTE
            + " Return only the final answer even if the model reasons internally first."
        ),
        discovery_validate_note=(
            _GENERIC_DISCOVERY_VALIDATE_NOTE
            + " Return only the final answer even if the model reasons internally first."
        ),
        discovery_temperature=0.02,
        discovery_repeat_penalty=1.1,
        discovery_top_p=0.16,
    ),
    "qwen3": ModelProfile(
        family="qwen3",
        ollama_thinking_mode="bool",
        discovery_query_note=(
            _GENERIC_DISCOVERY_QUERY_NOTE + " Keep reasoning hidden and avoid prefatory text."
        ),
        discovery_summary_note=_GENERIC_DISCOVERY_SUMMARY_NOTE,
        discovery_validate_note=(
            _GENERIC_DISCOVERY_VALIDATE_NOTE + " Never leave rationale empty."
        ),
        discovery_temperature=0.0,
        discovery_repeat_penalty=1.12,
        discovery_top_p=0.12,
    ),
    "gpt-oss": ModelProfile(
        family="gpt-oss",
        ollama_thinking_mode="gpt_oss",
        discovery_query_note=_GENERIC_DISCOVERY_QUERY_NOTE,
        discovery_summary_note=_GENERIC_DISCOVERY_SUMMARY_NOTE,
        discovery_validate_note=_GENERIC_DISCOVERY_VALIDATE_NOTE,
        discovery_temperature=0.03,
        discovery_repeat_penalty=1.08,
        discovery_top_p=0.18,
    ),
    "llama3": ModelProfile(
        family="llama3",
        ollama_thinking_mode="none",
        discovery_query_note=_GENERIC_DISCOVERY_QUERY_NOTE,
        discovery_summary_note=_GENERIC_DISCOVERY_SUMMARY_NOTE,
        discovery_validate_note=(
            _GENERIC_DISCOVERY_VALIDATE_NOTE
            + " Always provide a non-empty rationale tied to the abstract content."
        ),
        discovery_temperature=0.02,
        discovery_repeat_penalty=1.12,
        discovery_top_p=0.15,
    ),
}


def _strict_discovery_override(
    base_key: str,
    *,
    query_suffix: str = "",
    summary_suffix: str = "",
    validate_suffix: str = "",
    temperature: float | None = None,
    repeat_penalty: float | None = None,
    top_p: float | None = None,
) -> ModelProfile:
    base = _MODEL_PROFILES[base_key]
    return replace(
        base,
        discovery_query_note=(f"{base.discovery_query_note} {query_suffix}".strip()),
        discovery_summary_note=(f"{base.discovery_summary_note} {summary_suffix}".strip()),
        discovery_validate_note=(f"{base.discovery_validate_note} {validate_suffix}".strip()),
        discovery_temperature=(
            base.discovery_temperature if temperature is None else float(temperature)
        ),
        discovery_repeat_penalty=(
            base.discovery_repeat_penalty if repeat_penalty is None else float(repeat_penalty)
        ),
        discovery_top_p=base.discovery_top_p if top_p is None else float(top_p),
    )


_MODEL_PROFILE_OVERRIDES: tuple[tuple[str, ModelProfile], ...] = (
    (
        "gemma4:e4b",
        _strict_discovery_override(
            "gemma4",
            query_suffix="",
            summary_suffix="Each summary must be a full sentence, never empty.",
            validate_suffix=(
                "Always provide a non-empty rationale for every article. "
                "Do not collapse all papers to score 0 unless every abstract is clearly unrelated."
            ),
            temperature=0.01,
            repeat_penalty=1.14,
            top_p=0.12,
        ),
    ),
    (
        "gemma4:26b",
        _strict_discovery_override(
            "gemma4",
            query_suffix="",
            summary_suffix="Each summary must be a full sentence, never empty.",
            validate_suffix=(
                "Always provide a non-empty rationale for every article, especially accepted ones. "
                "If a paper is only partially relevant, prefer an intermediate score instead of 0."
            ),
            temperature=0.01,
            repeat_penalty=1.12,
            top_p=0.12,
        ),
    ),
    (
        "gemma4:12b",
        _strict_discovery_override(
            "gemma4",
            query_suffix="",
            summary_suffix="Each summary must be a full sentence, never empty.",
            validate_suffix=(
                "Always provide a non-empty rationale for every article. "
                "Use intermediate scores for partially relevant papers instead of collapsing to 0."
            ),
            temperature=0.01,
            repeat_penalty=1.12,
            top_p=0.12,
        ),
    ),
    (
        "mistral-small3.2",
        _strict_discovery_override(
            "mistral-small",
            query_suffix="",
            summary_suffix="Return one complete sentence per article. Never leave summary empty.",
            validate_suffix=(
                "Never leave rationale empty. "
                "If abstracts differ, use differentiated scores rather than repeating one default score."
            ),
            temperature=0.0,
            repeat_penalty=1.16,
            top_p=0.1,
        ),
    ),
    (
        "ministral 3:3b",
        _strict_discovery_override(
            "ministral-3",
            query_suffix="",
            summary_suffix="Return one complete sentence per article. Never leave summary empty.",
            validate_suffix=(
                "Never assign score 0 to every article by default. "
                "Use intermediate scores for partially relevant papers and always provide a specific rationale."
            ),
            temperature=0.0,
            repeat_penalty=1.18,
            top_p=0.08,
        ),
    ),
    (
        "ministral-3:3b",
        _strict_discovery_override(
            "ministral-3",
            query_suffix="",
            summary_suffix="Return one complete sentence per article. Never leave summary empty.",
            validate_suffix=(
                "Never assign score 0 to every article by default. "
                "Use intermediate scores for partially relevant papers and always provide a specific rationale."
            ),
            temperature=0.0,
            repeat_penalty=1.18,
            top_p=0.08,
        ),
    ),
    (
        "gpt-oss:20b",
        _strict_discovery_override(
            "gpt-oss",
            query_suffix="",
            summary_suffix="Return one complete sentence per article. Never leave summary empty.",
            validate_suffix=(
                "Always provide a non-empty rationale for each article. "
                "Do not omit explanations for accepted papers."
            ),
            temperature=0.01,
            repeat_penalty=1.12,
            top_p=0.14,
        ),
    ),
)


def _normalize_model_name(value: str | None) -> str:
    return str(value or "").strip().lower()


def _tokenize_model_name(value: str | None) -> list[str]:
    normalized = _normalize_model_name(value)
    if not normalized:
        return []
    collapsed = re.sub(r"[^a-z0-9]+", " ", normalized)
    return [token for token in collapsed.split() if token]


def _contains_token(tokens: list[str], token: str) -> bool:
    wanted = str(token or "").strip().lower()
    return bool(wanted) and wanted in tokens


def _contains_token_prefix(tokens: list[str], prefix: str) -> bool:
    wanted = str(prefix or "").strip().lower()
    return bool(wanted) and any(token.startswith(wanted) for token in tokens)


def _contains_all_tokens(tokens: list[str], *wanted_tokens: str) -> bool:
    return all(_contains_token(tokens, token) for token in wanted_tokens)


def _contains_token_sequence(tokens: list[str], *sequence: str) -> bool:
    wanted = [str(token or "").strip().lower() for token in sequence if str(token or "").strip()]
    if not wanted:
        return False
    if len(wanted) == 1:
        return _contains_token(tokens, wanted[0])
    for index in range(0, len(tokens) - len(wanted) + 1):
        if tokens[index : index + len(wanted)] == wanted:
            return True
    return False


def estimate_model_size_b(model_name: str | None) -> float | None:
    raw = _normalize_model_name(model_name)
    if not raw:
        return None
    token = ""
    digits_started = False
    for ch in raw:
        if ch.isdigit() or (ch == "." and digits_started):
            token += ch
            digits_started = True
            continue
        if digits_started and ch == "b":
            try:
                return float(token)
            except (TypeError, ValueError):
                return None
        if digits_started:
            token = ""
            digits_started = False
    return None


def is_tiny_model_family_size(model_name: str | None, *, threshold_b: float = 1.5) -> bool:
    size_b = estimate_model_size_b(model_name)
    return bool(size_b is not None and size_b <= float(threshold_b))


# Families that pack solid JSON adherence into a small footprint.
# Skips the Read-then-Parse two-step path even when below the size threshold.
_STRONG_SMALL_JSON_FAMILIES: frozenset[str] = frozenset({"ministral-3"})

# Families known to collapse on combined RAG + strict JSON tasks even when
# their size is unknown. Forces the two-step path as a safety net.
_WEAK_JSON_SMALL_FAMILIES: frozenset[str] = frozenset({"gemma4", "llama3", "qwen3"})


def resolve_effective_thinking(
    model_name: str | None,
    requested: bool,
    *,
    allow_small_model_override: bool = False,
) -> bool:
    """Return the thinking-mode flag actually applied to the call.

    Small models (those routed through the Read-then-Parse two-step pipeline)
    collapse when forced to emit a CoT trace alongside the structured output,
    so we silently disable thinking for them — even if the UI checkbox is on.
    The caller can opt out via ``allow_small_model_override`` (debug-only
    escape hatch surfaced as a checkbox in the settings panel).
    """
    if not requested:
        return False
    if allow_small_model_override:
        return True
    return not needs_two_step_json(model_name)


def needs_two_step_json(model_name: str | None, *, threshold_b: float = 4.0) -> bool:
    """True when the model should run the Read-then-Parse two-step pipeline.

    Small models (<= 4B) suffer "task collapse" when asked to read a complex
    RAG context AND emit a strict Pydantic JSON in a single call. Splitting
    the call into (1) free-form generation and (2) JSON extraction keeps the
    answer grounded.

    Exceptions:
      - Families in ``_STRONG_SMALL_JSON_FAMILIES`` (e.g. Ministral-3) handle
        strict JSON well even at 3B; they stay on the single-step path.
      - Families in ``_WEAK_JSON_SMALL_FAMILIES`` opt into the two-step path
        when their size cannot be parsed from the model id.
    """
    family = resolve_model_family(model_name)
    if family in _STRONG_SMALL_JSON_FAMILIES:
        return False
    size_b = estimate_model_size_b(model_name)
    if size_b is not None:
        return size_b <= float(threshold_b)
    return family in _WEAK_JSON_SMALL_FAMILIES


def resolve_model_family(model_name: str | None) -> str:
    raw = _normalize_model_name(model_name)
    tokens = _tokenize_model_name(model_name)
    if not raw:
        return "generic"
    if (
        raw.startswith("qwen3.6")
        or _contains_token_prefix(tokens, "qwen3.6")
        or _contains_token_sequence(tokens, "qwen", "3", "6")
        or _contains_token_sequence(tokens, "qwen", "3", "6", "a3b")
    ):
        return "qwen3.6"
    if (
        raw.startswith("qwen3.5")
        or _contains_token_prefix(tokens, "qwen3.5")
        or _contains_token_sequence(tokens, "qwen", "3", "5")
    ):
        return "qwen3.5"
    if (
        raw.startswith("qwen3")
        or _contains_token_prefix(tokens, "qwen3")
        or _contains_token_sequence(tokens, "qwen", "3")
    ):
        return "qwen3"
    if _contains_token_prefix(tokens, "codestral") or "codestral" in raw:
        return "codestral"
    if (
        raw.startswith("ministral-3")
        or raw.startswith("ministral3")
        or raw.startswith("mistralai-mini")
        or _contains_token_prefix(tokens, "ministral")
        or _contains_all_tokens(tokens, "mistral", "mini")
    ):
        return "ministral-3"
    if (
        raw.startswith("mistral-small")
        or raw.startswith("mistralai-small")
        or _contains_all_tokens(tokens, "mistral", "small")
        or _contains_all_tokens(tokens, "mistralai", "small")
    ):
        return "mistral-small"
    if (
        raw.startswith("mistral-medium")
        or raw.startswith("mistralai-medium")
        or _contains_all_tokens(tokens, "mistral", "medium")
        or _contains_all_tokens(tokens, "mistralai", "medium")
    ):
        return "mistral-medium"
    if (
        raw.startswith("mistral-large")
        or raw.startswith("mistralai-large")
        or _contains_all_tokens(tokens, "mistral", "large")
        or _contains_all_tokens(tokens, "mistralai", "large")
    ):
        return "mistral-large"
    if (
        raw.startswith("nemotron-3")
        or _contains_token_sequence(tokens, "nemotron", "3")
        or _contains_token_sequence(tokens, "nemotron", "3", "nano")
    ):
        return "nemotron-3"
    if (
        raw.startswith("gemma4")
        or raw.startswith("gemma-4")
        or _contains_token_prefix(tokens, "gemma4")
        or _contains_token_sequence(tokens, "gemma", "4")
    ):
        return "gemma4"
    if (
        raw.startswith("gpt-oss")
        or _contains_token_sequence(tokens, "gpt", "oss")
        or _contains_token_prefix(tokens, "gptoss")
    ):
        return "gpt-oss"
    if (
        raw.startswith("llama")
        or _contains_token_prefix(tokens, "llama")
        or _contains_token_sequence(tokens, "meta", "llama")
    ):
        return "llama3"
    return "generic"


def get_model_profile(model_name: str | None) -> ModelProfile:
    raw = _normalize_model_name(model_name)
    for prefix, profile in _MODEL_PROFILE_OVERRIDES:
        if raw.startswith(prefix):
            return profile
    family = resolve_model_family(model_name)
    return _MODEL_PROFILES.get(family, _MODEL_PROFILES["generic"])


def supports_ollama_thinking(model_name: str | None) -> bool:
    return get_model_profile(model_name).ollama_thinking_mode in {"bool", "gpt_oss"}


def resolve_ollama_think_request(
    model_name: str | None,
    thinking_enabled: bool,
) -> bool | str | None:
    if resolve_model_family(model_name) == "qwen3.5":
        return False
    profile = get_model_profile(model_name)
    mode = profile.ollama_thinking_mode
    if mode == "gpt_oss":
        return "medium" if thinking_enabled else False
    if mode == "bool":
        return True if thinking_enabled else False
    # Models without a dedicated profile (granite4.2, ornith-1.5, ...) can still
    # reason: Ollama enables thinking whenever the field is absent, so disabling
    # it must be explicit. Ollama accepts think=false on non-thinking models.
    return None if thinking_enabled else False


def get_chat_response_note(model_name: str | None) -> str:
    family = resolve_model_family(model_name)
    note = _GENERIC_CHAT_RESPONSE_NOTE

    if family == "qwen3.5":
        note = (
            "Answer the current request directly in natural prose using only the provided context. "
            "Reuse exact technical names from the context. "
            "Copy each valid citation label exactly, including the S in [S1], [S2], and similar labels. "
            "Never convert [S1] to [1], invent facts, or add outside examples. "
            "If the requested comparison or recommendation is absent, say that it cannot be determined "
            "from the provided context."
        )
    elif family == "qwen3.6":
        note += (
            " Keep reasoning hidden and answer directly."
            " Start with the specific formulations, methods, or findings that answer the request."
        )
    elif family == "gemma4":
        note += (
            " Never emit Gemma channel markers such as <|channel|> or empty thought blocks."
            " Never output drafting notes, self-correction, final-polish sections, checks, or repeated internal revisions."
            " Never say that the request is not a question."
            " Treat search-intent wording as a concrete question to answer."
        )
    elif family in {"ministral-3", "mistral-small", "mistral-medium", "mistral-large"}:
        note += (
            " Stay concise and avoid agentic, tool, or workflow narration."
            " Preserve citation markers exactly as written."
            " Never replace citations with placeholders such as ****, **, empty brackets, or filler text."
            " If a citation does not apply, omit it rather than inventing a replacement."
            " If the requested comparison or recommendation is not answerable from the provided context,"
            " say so explicitly instead of filling the gap with outside examples."
        )
    elif family == "codestral":
        note += " Answer as a research assistant, not as a coding copilot."
    elif family == "nemotron-3":
        note += " Return only the final answer even if the model reasons internally first."
    elif family == "gpt-oss":
        note += " Do not expose hidden reasoning or intermediate deliberation."
    elif family == "llama3":
        note += (
            " Reuse the exact formulation names and key technical terms found in the provided context."
            " Do not replace them with alternate method names, broader theory names, or guessed expansions."
            " If the context does not answer the question, say that it is not answerable from the provided context."
        )

    if needs_two_step_json(model_name):
        note += (
            " Small-chat-model mode: prefer literal source-grounded wording over paraphrase."
            " Reuse the exact formulation names and key keywords from the context whenever possible."
            " If the context does not provide the requested ranking, speed comparison, or recommendation,"
            " say that it is not answerable from the provided context and stop there."
            " When two source excerpts matter, cite each relevant source tag at least once in the answer or explanation."
        )

    if is_tiny_model_family_size(model_name):
        note += (
            " Small-model mode: prefer the simplest stable wording, keep the answer short,"
            " and omit optional side remarks instead of improvising structure."
        )
    return note


def get_discovery_prompt_note(model_name: str | None, task: str) -> str:
    profile = get_model_profile(model_name)
    task_key = str(task or "").strip().lower()
    note = {
        "query": profile.discovery_query_note,
        "summary": profile.discovery_summary_note,
        "validate": profile.discovery_validate_note,
    }.get(task_key, "").strip()

    if is_tiny_model_family_size(model_name):
        tiny_note = (
            "Small-model mode: prefer the most literal schema-compliant output, "
            "avoid optional prose, and never leave required fields empty."
        )
        if note:
            return f"{note} {tiny_note}"
        return tiny_note
    return note


def get_discovery_generation_settings(model_name: str | None) -> dict[str, float]:
    profile = get_model_profile(model_name)
    return {
        "temperature": float(profile.discovery_temperature),
        "repeat_penalty": float(profile.discovery_repeat_penalty),
        "top_p": float(profile.discovery_top_p),
    }
