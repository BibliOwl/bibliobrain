from src.model_profiles import (
    get_chat_response_note,
    get_discovery_generation_settings,
    get_model_profile,
    resolve_model_family,
    resolve_ollama_think_request,
    supports_ollama_thinking,
)


def test_mistralai_mini_alias_uses_ministral_family() -> None:
    assert resolve_model_family("mistralai-mini") == "ministral-3"
    assert resolve_model_family("mistralai-mini-latest") == "ministral-3"


def test_mistralai_generalist_aliases_use_mistral_families() -> None:
    assert resolve_model_family("mistralai-small") == "mistral-small"
    assert resolve_model_family("mistralai-medium") == "mistral-medium"
    assert resolve_model_family("mistralai-large") == "mistral-large"


def test_ministral_14b_is_kept_in_ministral_family() -> None:
    assert resolve_model_family("ministral-3:14b") == "ministral-3"


def test_ministral_14b_chat_note_preserves_citations() -> None:
    note = get_chat_response_note("ministral-3:14b").lower()
    assert "preserve citation markers" in note
    assert "placeholders" in note
    assert "omit" in note or "supprime" in note
    assert "answer:" not in note
    assert "explanation:" not in note
    assert "response note:" not in note


def test_small_chat_models_note_unknown_and_verbatim_keywords() -> None:
    note = get_chat_response_note("llama3.2:3b").lower()
    assert "not answerable from the provided context" in note
    assert "reuse the exact formulation names" in note
    assert "cite each relevant source tag" in note


def test_qwen35_chat_note_warns_against_fastest_guessing() -> None:
    note = get_chat_response_note("qwen3.5:0.8b").lower()
    assert "cannot be determined" in note
    assert "copy each valid citation label exactly" in note
    assert "never convert [s1] to [1]" in note


def test_qwen35_chat_thinking_is_disabled_for_rag_stability() -> None:
    assert supports_ollama_thinking("qwen3.5:4b") is False
    assert resolve_ollama_think_request("qwen3.5:4b", False) is False
    assert resolve_ollama_think_request("qwen3.5:4b", True) is False


def test_profileless_models_disable_ollama_thinking_explicitly() -> None:
    # Omitting "think" lets Ollama reason by default on thinking-capable models,
    # which exhausted granite4.2's output budget in the library48 benchmark.
    for model in ("granite4.2:3b", "granite4.2:8b", "ornith-1.5:9b"):
        assert supports_ollama_thinking(model) is False
        assert resolve_ollama_think_request(model, False) is False
        assert resolve_ollama_think_request(model, True) is None


def test_gemma4_12b_uses_strict_stable_profile() -> None:
    assert get_model_profile("gemma4:12B").family == "gemma4"
    assert get_discovery_generation_settings("gemma4:12B") == {
        "temperature": 0.01,
        "repeat_penalty": 1.12,
        "top_p": 0.12,
    }


def test_generic_chat_note_requires_grounding_and_unknown_admission() -> None:
    note = get_chat_response_note("granite4.1:8b").lower()
    assert "provided context" in note
    assert "preserve valid citation labels" in note
    assert "do not guess from outside knowledge" in note


def test_chat_notes_keep_family_guards_without_legacy_sections() -> None:
    for model in ("gemma4:e4b", "llama3.2:3b", "ministral-3:14b", "qwen3.5:4b"):
        note = get_chat_response_note(model).lower()
        assert "answer:" not in note
        assert "explanation:" not in note
        assert "response note:" not in note
        assert "citation" in note or "[sx]" in note
