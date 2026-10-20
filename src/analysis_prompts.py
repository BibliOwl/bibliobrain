from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from src import config
from src.prompts import ANALYSIS_PROMPTS, ANALYSIS_PROMPTS_EN, get_response_language_instruction

_logger = logging.getLogger(__name__)

_ANALYSIS_ERRORS: dict[str, dict[str, str]] = {
    "analysis_unknown_type": {
        "fr": "Type d'analyse inconnu.",
        "en": "Unknown analysis type.",
    },
    "analysis_prompt_empty": {
        "fr": "Le prompt ne peut pas être vide.",
        "en": "The prompt cannot be empty.",
    },
    "analysis_prompt_missing_placeholder": {
        "fr": "Le prompt doit contenir le placeholder {text}.",
        "en": "The prompt must contain the {text} placeholder.",
    },
}


def _ae(key: str) -> str:
    """Return a localized analysis error message (defaults to fr)."""
    entry = _ANALYSIS_ERRORS.get(key, {})
    # Errors are shown to the developer/admin; default to French
    return entry.get("fr", f"<{key}>")

_PROMPTS_PATH = Path(config.STORAGE_DIR) / "analysis_prompts.json"

_ANALYSIS_TYPE_LABELS_EN: dict[str, str] = {
    "Résumé rapide": "Quick summary",
    "Résumé complet": "Full summary",
    "Analyse critique approfondie": "In-depth critical analysis",
    "Méthodes utilisées": "Methods used",
    "Principales conclusions": "Main conclusions",
}


def _normalize_language(language: str = "fr") -> str:
    return "en" if str(language or "").strip().lower() == "en" else "fr"


def _read_overrides() -> dict[str, str]:
    try:
        data = json.loads(_PROMPTS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception:
        _logger.debug("Recovered from an error in _read_overrides.", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    prompts = data.get("prompts", data)
    if not isinstance(prompts, dict):
        return {}
    return {
        str(key).strip(): str(value)
        for key, value in prompts.items()
        if str(key).strip() and isinstance(value, str)
    }


def _write_overrides(overrides: dict[str, str]) -> None:
    _PROMPTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"prompts": dict(sorted(overrides.items()))}
    _PROMPTS_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def list_analysis_types() -> list[str]:
    return list(ANALYSIS_PROMPTS.keys())


def _default_analysis_prompts(language: str = "fr") -> dict[str, str]:
    if _normalize_language(language) == "en":
        prompts = dict(ANALYSIS_PROMPTS)
        prompts.update(ANALYSIS_PROMPTS_EN)
        return prompts
    return dict(ANALYSIS_PROMPTS)


def get_effective_analysis_prompts(language: str = "fr") -> dict[str, str]:
    prompts = _default_analysis_prompts(language)
    for analysis_type, prompt in _read_overrides().items():
        if analysis_type in prompts:
            prompts[analysis_type] = prompt
    return prompts


def get_analysis_prompt(analysis_type: str, language: str = "fr") -> str:
    safe_type = str(analysis_type or "").strip()
    prompts = get_effective_analysis_prompts(language)
    raw = (
        prompts.get(safe_type)
        or prompts.get("Resume dense")
        or prompts.get("Résumé dense")
        or next(iter(prompts.values()))
    )
    instruction = get_response_language_instruction(language)
    if "{language_instruction}" in raw:
        return raw.replace("{language_instruction}", instruction)
    # User overrides or legacy prompts without the placeholder: append at end
    return f"{raw}\n\n{instruction}"


def analysis_prompt_fingerprint(analysis_type: str, language: str = "fr") -> str:
    prompt = get_analysis_prompt(analysis_type, language=language)
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return digest[:16]


def build_analysis_cache_fingerprint(
    file_fingerprint: str,
    analysis_type: str,
    language: str = "fr",
) -> str:
    base = str(file_fingerprint or "").strip()
    safe_language = _normalize_language(language)
    prompt_fp = analysis_prompt_fingerprint(analysis_type, language=safe_language)
    prompt_key = f"{safe_language}:{prompt_fp}"
    return f"{base}:prompt:{prompt_key}" if base else f"prompt:{prompt_key}"


def list_analysis_prompt_payloads(language: str = "fr") -> list[dict[str, Any]]:
    safe_language = _normalize_language(language)
    overrides = _read_overrides()
    defaults = _default_analysis_prompts(safe_language)
    result: list[dict[str, Any]] = []
    for analysis_type in ANALYSIS_PROMPTS:
        default_prompt = defaults[analysis_type]
        override = overrides.get(analysis_type)
        effective = override if override is not None else default_prompt
        result.append(
            {
                "type": analysis_type,
                "label": (
                    _ANALYSIS_TYPE_LABELS_EN.get(analysis_type, analysis_type)
                    if safe_language == "en"
                    else analysis_type
                ),
                "prompt": effective,
                "default_prompt": default_prompt,
                "custom": override is not None,
                "fingerprint": analysis_prompt_fingerprint(
                    analysis_type,
                    language=safe_language,
                ),
                "language": safe_language,
            }
        )
    return result


def set_analysis_prompt_override(
    analysis_type: str,
    prompt: str,
    language: str = "fr",
) -> dict[str, Any]:
    safe_type = str(analysis_type or "").strip()
    safe_prompt = str(prompt or "").strip()
    safe_language = _normalize_language(language)
    if safe_type not in ANALYSIS_PROMPTS:
        raise ValueError(_ae("analysis_unknown_type"))
    if not safe_prompt:
        raise ValueError(_ae("analysis_prompt_empty"))
    if "{text}" not in safe_prompt:
        raise ValueError(_ae("analysis_prompt_missing_placeholder"))
    overrides = _read_overrides()
    default_prompt = _default_analysis_prompts(safe_language)[safe_type]
    if safe_prompt == default_prompt:
        overrides.pop(safe_type, None)
    else:
        overrides[safe_type] = safe_prompt
    _write_overrides(overrides)
    return get_analysis_prompt_payload(safe_type, language=safe_language)


def reset_analysis_prompt_override(
    analysis_type: str,
    language: str = "fr",
) -> dict[str, Any]:
    safe_type = str(analysis_type or "").strip()
    safe_language = _normalize_language(language)
    if safe_type not in ANALYSIS_PROMPTS:
        raise ValueError(_ae("analysis_unknown_type"))
    overrides = _read_overrides()
    overrides.pop(safe_type, None)
    _write_overrides(overrides)
    return get_analysis_prompt_payload(safe_type, language=safe_language)


def get_analysis_prompt_payload(
    analysis_type: str,
    language: str = "fr",
) -> dict[str, Any]:
    safe_type = str(analysis_type or "").strip()
    for payload in list_analysis_prompt_payloads(language=language):
        if payload["type"] == safe_type:
            return payload
    raise ValueError(_ae("analysis_unknown_type"))
