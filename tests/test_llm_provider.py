from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import src.llm_provider as llm_provider
from src.llm_provider import sanitize_assistant_content


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:1234/v1",
        "http://127.0.0.1:1234/v1",
        "http://10.0.0.20:1234/v1",
        "http://172.16.0.20:1234/v1",
        "http://192.168.1.20:1234/v1",
        "http://[fd00::20]:1234/v1",
    ],
)
def test_validate_remote_base_url_accepts_local_providers(url: str) -> None:
    assert llm_provider._validate_remote_base_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",
        "http://169.254.10.20/provider",
        "http://metadata.google.internal/computeMetadata/v1",
        "http://[fe80::1]/provider",
    ],
)
def test_validate_remote_base_url_rejects_metadata_endpoints(url: str) -> None:
    with pytest.raises(ValueError, match="link-local/metadata"):
        llm_provider._validate_remote_base_url(url)


def test_provider_state_preserves_concurrent_atomic_updates(
    workspace_tmp_dir: Path,
    monkeypatch,
) -> None:
    storage_dir = workspace_tmp_dir / "provider-state"
    monkeypatch.setattr(llm_provider.config, "STORAGE_DIR", str(storage_dir))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda index: llm_provider._save_provider_state({f"key_{index}": index}),
                range(24),
            )
        )

    assert llm_provider._load_provider_state() == {
        f"key_{index}": index for index in range(24)
    }
    assert list(storage_dir.glob(".provider_state.json.*.tmp")) == []


def test_sanitize_assistant_content_removes_visible_drafting_sections() -> None:
    raw = (
        "Question: quels sont les types de pertes AC ?\n\n"
        "Self-Correction during drafting: I should not show this.\n\n"
        "Final Polish:\n"
        "Answer: Les pertes incluent l'hysteresis [S1].\n\n"
        "Explanation: Le contexte le dit [S'3].\n\n"
        "Response note: Aucune note utile.\n\n"
        "*Wait, check source S3 again."
    )

    visible = sanitize_assistant_content(raw, thinking_enabled=False)

    assert visible.startswith("Answer: Les pertes")
    assert "Self-Correction" not in visible
    assert "Final Polish" not in visible
    assert "Wait, check" not in visible
    assert "[S'3]" not in visible
    assert "[S3]" in visible


def test_sanitize_assistant_content_drops_draft_before_orphan_closing_tag() -> None:
    raw = (
        "Brouillon : la formulation H est generale [S1].\n</think>\n"
        "La formulation H est generale mais couteuse [S1]."
    )

    visible = sanitize_assistant_content(raw, thinking_enabled=False)

    assert visible == "La formulation H est generale mais couteuse [S1]."
    assert sanitize_assistant_content(raw, thinking_enabled=True) == raw


def test_sanitize_assistant_content_truncates_late_drafting_tail() -> None:
    raw = (
        "Answer: Les pertes incluent l'hysteresis [S1].\n\n"
        "Explanation: Le contexte le dit [S1].\n\n"
        "Response note: Aucune note utile.\n\n"
        "Refined Explanation: hidden revision text."
    )

    visible = sanitize_assistant_content(raw, thinking_enabled=False)

    assert visible.endswith("Response note: Aucune note utile.")
    assert "Refined Explanation" not in visible


def test_native_thinking_does_not_inject_model_control_tokens() -> None:
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Question"},
    ]

    prepared = llm_provider._prepare_ollama_chat_messages_for_thinking(
        "gemma4:12b",
        messages,
        True,
    )

    assert prepared == messages
    assert prepared is not messages
    assert all("<|think|>" not in message["content"] for message in prepared)


def test_qwen35_non_thinking_appends_native_directive() -> None:
    messages = [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Question"},
    ]

    prepared = llm_provider._prepare_ollama_chat_messages_for_thinking(
        "qwen3.5:4b",
        messages,
        False,
    )

    assert prepared[-1]["content"].endswith("/no_think")
    assert "Keep source labels exactly" in prepared[-1]["content"]
    assert messages[-1]["content"] == "Question"


def test_qwen35_prompt_compaction_keeps_context_and_question() -> None:
    prepared = llm_provider._prepare_ollama_chat_messages_for_thinking(
        "qwen3.5:2b",
        [
            {
                "role": "system",
                "content": "Long repeated rules.\n\nContexte : [S1] Formulation H.",
            },
            {
                "role": "user",
                "content": (
                    "Quelle formulation ?\n\nStyle de reponse a respecter:\n"
                    "Many repeated rules."
                ),
            },
        ],
        False,
    )

    assert "Long repeated rules" not in prepared[0]["content"]
    assert "[S1] Formulation H." in prepared[0]["content"]
    assert prepared[1]["content"].startswith("Quelle formulation ?")
    assert "Many repeated rules" not in prepared[1]["content"]


def test_qwen35_numeric_citations_restore_only_known_source_labels() -> None:
    normalized = llm_provider._normalize_qwen35_citation_labels(
        "Supported [[1]], unknown PDF reference [53].",
        [{"role": "system", "content": "Context: [S1] source one."}],
    )

    assert normalized == "Supported [S1], unknown PDF reference [53]."


def test_qwen35_infers_missing_citation_from_clear_source_overlap() -> None:
    normalized = llm_provider._normalize_qwen35_citation_labels(
        "La formulation T-A reduit le cout pour les bobines minces.",
        [
            {
                "role": "system",
                "content": (
                    "Context:\n"
                    "[S1] La formulation H represente le champ magnetique.\n"
                    "[S2] La formulation T-A reduit le cout pour les bobines minces."
                ),
            }
        ],
    )

    assert normalized.endswith("[S2]")
    assert "[S1]" not in normalized


def test_qwen35_does_not_cite_a_source_for_words_every_passage_shares() -> None:
    # S2 shares "which", "magnetic" and "field" with the answer, like most
    # passages of one library; only S1 holds the answer's own words.
    normalized = llm_provider._normalize_qwen35_citation_labels(
        "The screening current, which distorts the magnetic field, grows with tape width [S1].",
        [
            {
                "role": "system",
                "content": (
                    "Context:\n"
                    "[S1] The screening current distorts the magnetic field and grows with tape width.\n"
                    "[S2] Cooling losses, which depend on the magnetic field, were measured."
                ),
            }
        ],
    )

    assert "[S2]" not in normalized


class _FakeOllamaStream:
    def __init__(self, pieces: list[str]) -> None:
        lines = [json.dumps({"message": {"content": piece}, "done": False}) for piece in pieces]
        lines.append(json.dumps({"message": {"content": ""}, "done": True, "eval_count": 9}))
        self.lines = [line.encode("utf-8") + b"\n" for line in lines]

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""

    def close(self) -> None:
        self.lines = []


_QWEN35_SOURCES = [
    {"role": "system", "content": "Contexte :\n[S1] Paper A\nfield 0.19 T\n\n[S2] Paper B\nfield"},
    {"role": "user", "content": "Question"},
]


def test_qwen35_answers_are_streamed_with_repaired_labels(monkeypatch) -> None:
    captured: dict = {}

    def fake_open(_path, payload, **kwargs):
        captured.update(payload=payload, **kwargs)
        return _FakeOllamaStream(
            ["Le champ ", "vaut 0.19 T [", "1] selon la source", " S2", ". Fin."]
        )

    monkeypatch.setattr(llm_provider, "_open_ollama_request", fake_open)

    chunks = list(llm_provider.stream_ollama_chat("qwen3.5:2b", _QWEN35_SOURCES, think=False))

    assert captured["payload"]["stream"] is True and captured["think"] is False
    assert chunks[0]["content"] == "Le champ"  # shown before the answer is complete
    text = "".join(chunk["content"] for chunk in chunks)
    assert text == "Le champ vaut 0.19 T [S1] selon la [S2]. Fin."
    assert all("[1]" not in chunk["content"] for chunk in chunks)
    assert "content_replace" not in chunks[-1]
    assert chunks[-1]["done"] is True and chunks[-1]["timings"]["output_tokens"] == 9


@pytest.mark.parametrize(
    "answer",
    [
        "Le champ vaut 0.19 T [1] et 0.11 T [[S2]].",
        "\n\nSelon la source S1, le champ vaut 0.19 T ; citation S2 le confirme.",
        "Valeurs [1, 2] et [S12] inconnue [voir texte",
        "Un mot field field field sans label",
        "Liste :\n- 0.19 T [1]\n- 0.11 T [2]\n",
    ],
)
def test_qwen35_label_repair_streams_the_same_text_whatever_the_chunking(answer) -> None:
    expected = llm_provider._normalize_qwen35_citation_labels(answer, _QWEN35_SOURCES).lstrip()
    for size in (1, 2, 3, 7):
        stream = llm_provider._Qwen35CitationStream(_QWEN35_SOURCES)
        released = "".join(
            stream.push(answer[start : start + size]) for start in range(0, len(answer), size)
        )
        rest, full = stream.finish()
        assert released + rest == full == expected


def test_ollama_stream_rejects_preaborted_request_before_open(monkeypatch) -> None:
    signal = llm_provider.StreamAbortSignal()
    signal.abort("user_stop")
    opened = False

    def fake_open(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("request should not open")

    monkeypatch.setattr(llm_provider, "_open_ollama_request", fake_open)

    with pytest.raises(InterruptedError):
        list(
            llm_provider.stream_ollama_chat(
                "gemma4:12b",
                [{"role": "user", "content": "Return JSON."}],
                think=False,
                abort_signal=signal,
                format_schema={"type": "object"},
            )
        )

    assert opened is False
    assert signal.abort_reason() == "user_stop"


def test_qwen35_native_structured_retries_empty_payload(monkeypatch) -> None:
    class Payload(llm_provider.BaseModel):
        answer: str

    responses = iter(
        [
            b'{"response":"{\\"answer\\":\\"\\"}"}',
            b'{"response":"{\\"answer\\":\\"Formulation H [S1].\\"}"}',
        ]
    )

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return self.body

    monkeypatch.setattr(
        llm_provider,
        "_open_ollama_request",
        lambda *_args, **_kwargs: FakeResponse(next(responses)),
    )

    result = llm_provider._invoke_qwen35_native_structured(
        model_name="qwen3.5:2b",
        messages=[{"role": "user", "content": "Name the formulation."}],
        response_model=Payload,
        settings={"temperature": 0.0, "repeat_penalty": 1.1, "top_p": 0.2},
    )

    assert isinstance(result, Payload)
    assert result.answer == "Formulation H [S1]."


def test_qwen35_native_structured_falls_back_to_text_answer(monkeypatch) -> None:
    class Payload(llm_provider.BaseModel):
        answer: str

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"response":"{\\"answer\\":\\"\\"}"}'

    monkeypatch.setattr(
        llm_provider,
        "_open_ollama_request",
        lambda *_args, **_kwargs: FakeResponse(),
    )
    monkeypatch.setattr(
        llm_provider,
        "invoke_ollama_chat",
        lambda *_args, **_kwargs: ("", "Formulation H [S1].", {}),
    )

    result = llm_provider._invoke_qwen35_native_structured(
        model_name="qwen3.5:0.8b",
        messages=[{"role": "user", "content": "Name the formulation."}],
        response_model=Payload,
        settings={"temperature": 0.0, "repeat_penalty": 1.1, "top_p": 0.2},
    )

    assert isinstance(result, Payload)
    assert result.answer == "Formulation H [S1]."


def test_qwen35_structured_values_normalize_nested_citations() -> None:
    normalized = llm_provider._normalize_qwen35_structured_value(
        {
            "answer": "Source S1 supports this.",
            "items": ["Citation S2 supports that."],
        },
        [
            {
                "role": "system",
                "content": "Context: [S1] first source.\n[S2] second source.",
            }
        ],
    )

    assert normalized == {
        "answer": "[S1] supports this.",
        "items": ["[S2] supports that."],
    }


def test_remote_httpx_verify_honors_disabled_ssl(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_SSL_VERIFY", "false")
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)

    assert llm_provider._remote_httpx_verify() is False


def test_remote_httpx_verify_uses_custom_ca(monkeypatch, workspace_tmp_dir: Path) -> None:
    cert_path = workspace_tmp_dir / "ca.pem"
    cert_path.write_text("dummy cert", encoding="utf-8")
    monkeypatch.setenv("REMOTE_SSL_VERIFY", "true")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", str(cert_path))

    assert llm_provider._remote_httpx_verify() == str(cert_path)


def test_lmstudio_lan_url_bypasses_environment_proxy(monkeypatch) -> None:
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    normalized = llm_provider._lmstudio_api_base_url(
        "http://192.168.1.150:1234"
    )

    assert normalized == "http://192.168.1.150:1234/v1"
    assert "192.168.1.150" in os.environ["NO_PROXY"].split(",")
    assert "192.168.1.150" in os.environ["no_proxy"].split(",")
    assert llm_provider._url_targets_local_network(normalized) is True


def test_lmstudio_embeddings_send_raw_strings(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "get_embedding_provider_mode", lambda: "lmstudio")
    monkeypatch.setattr(
        llm_provider,
        "_validate_lmstudio_config",
        lambda: (True, "LM Studio disponible."),
    )
    monkeypatch.setattr(
        llm_provider,
        "_lmstudio_api_base_url",
        lambda *_args, **_kwargs: "http://127.0.0.1:1234/v1",
    )

    embeddings = llm_provider.create_embeddings("text-embedding-model")

    assert embeddings.check_embedding_ctx_length is False


@pytest.mark.parametrize(
    ("base_url", "sends_token_ids"),
    [
        ("https://api.openai.com/v1", True),
        ("https://openrouter.ai/api/v1", False),
        ("http://192.168.1.20:8080/v1", False),
    ],
)
def test_remote_embeddings_send_text_to_non_openai_apis(
    monkeypatch, base_url: str, sends_token_ids: bool
) -> None:
    # OpenAI token ids mean nothing to another provider's embedding model.
    monkeypatch.setattr(llm_provider, "get_embedding_provider_mode", lambda: "remote")
    monkeypatch.setattr(llm_provider, "_runtime_remote_embedding_model", "some-embedding-model")
    monkeypatch.setenv("REMOTE_LLM_BASE_URL", base_url)
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "test-key")

    embeddings = llm_provider.create_embeddings("ignored")

    assert embeddings.check_embedding_ctx_length is sends_token_ids


def test_index_is_computed_on_this_computer_whatever_answers_the_chat(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(llm_provider, "_runtime_embedding_source", "local")
    monkeypatch.setattr(llm_provider, "_runtime_remote_embedding_model", None)
    monkeypatch.setattr(llm_provider, "_load_persisted_embedding_model", lambda *_args: None)

    monkeypatch.setattr(llm_provider, "_runtime_mode", "lmstudio")
    monkeypatch.setattr(llm_provider, "_runtime_lmstudio_base_url", "http://192.168.1.20:1234")
    assert llm_provider.get_embedding_provider_mode() == "ollama"
    assert llm_provider.requires_local_ollama_embeddings() is True

    # Ollama answering from another computer does not take the index with it.
    monkeypatch.setattr(llm_provider, "_runtime_mode", "ollama")
    monkeypatch.setattr(llm_provider, "_runtime_ollama_base_url", "http://192.168.1.20:11434")
    assert llm_provider.embedding_base_url() == "http://localhost:11434"

    # A custom local address is this computer too.
    monkeypatch.setattr(llm_provider, "_runtime_ollama_base_url", "http://127.0.0.1:11500")
    assert llm_provider.embedding_base_url() == "http://127.0.0.1:11500"


def test_index_can_follow_the_chat_service(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "_runtime_embedding_source", "chat")
    monkeypatch.setattr(llm_provider, "_runtime_remote_embedding_model", None)
    monkeypatch.setattr(llm_provider, "_load_persisted_embedding_model", lambda *_args: None)

    monkeypatch.setattr(llm_provider, "_runtime_mode", "lmstudio")
    monkeypatch.setattr(llm_provider, "_runtime_lmstudio_base_url", "http://192.168.1.20:1234")
    assert llm_provider.get_embedding_provider_mode() == "lmstudio"
    assert llm_provider.embedding_base_url() == "http://192.168.1.20:1234"

    monkeypatch.setattr(llm_provider, "_runtime_mode", "ollama")
    monkeypatch.setattr(llm_provider, "_runtime_ollama_base_url", "http://192.168.1.20:11434")
    assert llm_provider.embedding_base_url() == "http://192.168.1.20:11434"


@pytest.mark.parametrize(
    ("saved_state", "expected"),
    [
        ({}, "local"),
        ({"mode": "ollama", "ollama_base_url": "http://localhost:11434"}, "local"),
        ({"mode": "remote"}, "local"),
        ({"mode": "lmstudio"}, "chat"),
        ({"mode": "ollama", "ollama_base_url": "http://192.168.1.20:11434"}, "chat"),
        ({"mode": "lmstudio", "embedding_source": "local"}, "local"),
    ],
)
def test_older_installs_keep_the_service_their_index_was_built_with(
    monkeypatch, workspace_tmp_dir: Path, saved_state: dict, expected: str
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    storage_dir.mkdir()
    monkeypatch.setattr(llm_provider.config, "STORAGE_DIR", str(storage_dir))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    if saved_state:
        llm_provider._save_provider_state(saved_state)

    assert llm_provider._load_persisted_embedding_source() == expected


@pytest.mark.parametrize(
    ("saved_state", "expected"),
    [
        ({}, "local"),
        ({"mode": "lmstudio"}, "local"),
        # Figure analysis used to follow this address: it keeps doing so.
        ({"mode": "ollama", "ollama_base_url": "http://192.168.1.20:11434"}, "chat"),
        (
            {"ollama_base_url": "http://192.168.1.20:11434", "document_tools_source": "local"},
            "local",
        ),
    ],
)
def test_document_tools_stay_local_except_where_they_already_ran_elsewhere(
    monkeypatch, workspace_tmp_dir: Path, saved_state: dict, expected: str
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    storage_dir.mkdir()
    monkeypatch.setattr(llm_provider.config, "STORAGE_DIR", str(storage_dir))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    if saved_state:
        llm_provider._save_provider_state(saved_state)

    assert llm_provider._load_persisted_document_tools_source() == expected


def test_moving_the_chat_to_another_computer_leaves_the_document_tools_here(
    monkeypatch, workspace_tmp_dir: Path
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    storage_dir.mkdir()
    monkeypatch.setattr(llm_provider.config, "STORAGE_DIR", str(storage_dir))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(llm_provider, "_runtime_document_tools_source", "local")
    monkeypatch.setattr(llm_provider, "_runtime_embedding_source", "local")
    monkeypatch.setattr(llm_provider, "_runtime_ollama_base_url", None)

    llm_provider.set_runtime_ollama_base_url("http://192.168.1.20:11434")

    assert llm_provider.document_tools_ollama_base_url() == "http://localhost:11434"
    # Read again after a restart, the new address must not look like an older install.
    assert llm_provider._load_persisted_document_tools_source() == "local"
    assert llm_provider._load_persisted_embedding_source() == "local"

    llm_provider.set_runtime_document_tools_source("chat")
    assert llm_provider.document_tools_ollama_base_url() == "http://192.168.1.20:11434"


def test_changing_the_chat_service_does_not_move_a_new_index(
    monkeypatch, workspace_tmp_dir: Path
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    storage_dir.mkdir()
    monkeypatch.setattr(llm_provider.config, "STORAGE_DIR", str(storage_dir))
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.setattr(llm_provider, "_runtime_embedding_source", "local")
    monkeypatch.setattr(llm_provider, "_runtime_mode", None)
    monkeypatch.setattr(llm_provider, "_fallback_active", False)
    monkeypatch.setattr(llm_provider, "_fallback_reason", "")

    llm_provider.set_runtime_mode("lmstudio")

    # Read again after a restart, LM Studio must not look like an older install.
    assert llm_provider._load_persisted_embedding_source() == "local"


def test_chat_model_classification_excludes_embedding_and_ocr_models() -> None:
    assert llm_provider._is_chat_model_candidate("qwen3.5:4b") is True
    assert llm_provider._is_chat_model_candidate("llava:7b") is True
    assert llm_provider._is_chat_model_candidate("embeddinggemma:latest") is False
    assert llm_provider._is_chat_model_candidate("nomic-embed-text:latest") is False
    assert llm_provider._is_chat_model_candidate("glm-ocr:q8_0") is False
    assert llm_provider._is_chat_model_candidate("deepseek-ocr:3b") is False


def test_local_ollama_chat_models_exclude_ocr_models(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_provider,
        "_read_ollama_json",
        lambda *_args, **_kwargs: {
            "models": [
                {"name": "glm-ocr:q8_0", "size": 1},
                {"name": "embeddinggemma:latest", "size": 2},
                {"name": "nomic-embed-text:latest", "size": 3},
                {"name": "qwen3.5:4b", "size": 4},
                {"name": "llava:7b", "size": 5},
            ]
        },
    )

    models, status = llm_provider.list_local_ollama_models()

    assert models == ["qwen3.5:4b", "llava:7b"]
    assert "2 modele(s) de chat" in status


def test_embeddinggemma_is_the_default_local_embedding_model(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "_runtime_mode", "ollama")
    monkeypatch.setattr(llm_provider, "_runtime_ollama_embedding_model", None)
    monkeypatch.setattr(llm_provider, "_load_persisted_embedding_model", lambda *_args: None)
    monkeypatch.delenv("OLLAMA_EMBEDDING_MODEL", raising=False)
    monkeypatch.setattr(
        llm_provider,
        "resolve_local_ollama_model_name",
        lambda requested, *_args, **_kwargs: requested,
    )

    assert llm_provider.required_embedding_model_name() == "embeddinggemma"


def test_embeddinggemma_is_preferred_among_installed_embedding_models() -> None:
    assert (
        llm_provider.recommend_local_embedding_model(
            ["nomic-embed-text-v2-moe:latest", "embeddinggemma:latest"]
        )
        == "embeddinggemma:latest"
    )


def test_cached_chat_models_are_reclassified_before_use(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_provider,
        "_load_provider_state",
        lambda: {
            llm_provider._MODEL_LIST_CACHE_STATE_KEY: {
                "ollama:http://localhost:11434": {
                    "models": ["glm-ocr:q8_0", "qwen3.5:4b"],
                    "status": "2 modele(s)",
                    "cached_at": 9_999_999_999,
                }
            }
        },
    )

    cached = llm_provider._load_cached_model_list("ollama:http://localhost:11434")

    assert cached is not None
    assert cached[0] == ["qwen3.5:4b"]


def test_openai_compatible_chat_models_exclude_ocr_and_embeddings(monkeypatch) -> None:
    monkeypatch.setattr(
        llm_provider,
        "_open_url_json",
        lambda *_args, **_kwargs: {
            "data": [
                {"id": "glm-ocr:q8_0"},
                {"id": "text-embedding-3-small"},
                {"id": "remote-chat-model"},
            ]
        },
    )

    models, status = llm_provider._list_openai_compatible_models(
        base_url="https://models.example.test/v1",
        label="API distante",
    )

    assert models == ["remote-chat-model"]
    assert "1 modele(s)" in status


def test_list_available_models_caches_remote_results_and_uses_short_timeout(
    monkeypatch,
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    storage_dir.mkdir()
    monkeypatch.setattr(llm_provider.config, "STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(llm_provider, "_runtime_mode", "remote")
    monkeypatch.setenv("REMOTE_LLM_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "test-key")
    monkeypatch.setenv("REMOTE_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("REMOTE_MODEL_LIST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("MODEL_LIST_CACHE_TTL_SECONDS", "300")
    calls: list[dict] = []

    def fake_list_models(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ["model-a", "model-b"], "2 modele(s) detecte(s) sur API distante."
        return [], "API distante inaccessible : timeout"

    monkeypatch.setattr(llm_provider, "_list_openai_compatible_models", fake_list_models)

    first = llm_provider.list_available_models(force_refresh=True)
    cached = llm_provider.list_available_models()
    fallback = llm_provider.list_available_models(force_refresh=True)

    assert first[0] == ["model-a", "model-b"]
    assert cached[0] == ["model-a", "model-b"]
    assert "(cache)" in cached[1]
    assert fallback[0] == ["model-a", "model-b"]
    assert "Derniere liste connue" in fallback[1]
    assert len(calls) == 2
    assert calls[0]["timeout"] == 5


def test_remote_embedding_models_use_ids_and_advertised_capabilities(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_LLM_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "test-key")
    monkeypatch.setattr(
        llm_provider,
        "_open_url_json",
        lambda *_args, **_kwargs: {
            "data": [
                {"id": "chat-only"},
                {"id": "text-embedding-3-small"},
                {"id": "vendor-vector-model", "capabilities": {"embeddings": True}},
                {"id": "feature-model", "task": "feature-extraction"},
            ]
        },
    )

    models, status = llm_provider.list_remote_embedding_models()

    assert models == [
        "feature-model",
        "text-embedding-3-small",
        "vendor-vector-model",
    ]
    assert "3 modele(s)" in status


def test_remote_embedding_selection_is_persisted_per_provider(
    monkeypatch,
    workspace_tmp_dir: Path,
) -> None:
    storage_dir = workspace_tmp_dir / "storage"
    storage_dir.mkdir()
    monkeypatch.setattr(llm_provider.config, "STORAGE_DIR", str(storage_dir))
    monkeypatch.setattr(llm_provider, "_runtime_mode", "remote")
    monkeypatch.setattr(llm_provider, "_runtime_remote_embedding_model", None)
    monkeypatch.setenv("REMOTE_LLM_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "test-key")
    monkeypatch.delenv("REMOTE_EMBEDDING_MODEL", raising=False)

    selected = llm_provider.set_runtime_embedding_model(
        "text-embedding-3-small",
        provider_mode="remote",
    )

    assert selected == "text-embedding-3-small"
    assert llm_provider.get_embedding_provider_mode() == "remote"
    assert llm_provider.required_embedding_model_name() == "text-embedding-3-small"
    assert llm_provider._load_persisted_embedding_model("remote") == selected

    llm_provider.disable_runtime_remote_embeddings()

    assert llm_provider.get_embedding_provider_mode() == "ollama"
    assert llm_provider._remote_embedding_model() == ""
    assert (
        llm_provider._load_persisted_embedding_model("remote")
        == llm_provider._REMOTE_EMBEDDINGS_DISABLED
    )


def test_remote_embedding_profile_identity_is_scoped_to_the_endpoint(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "_runtime_mode", "remote")
    monkeypatch.setattr(
        llm_provider,
        "_runtime_remote_embedding_model",
        "text-embedding-3-small",
    )
    monkeypatch.setenv("REMOTE_LLM_BASE_URL", "https://first.example.test/v1")
    first = llm_provider.current_embedding_profile_label("text-embedding-3-small")

    monkeypatch.setenv("REMOTE_LLM_BASE_URL", "https://second.example.test/v1")
    second = llm_provider.current_embedding_profile_label("text-embedding-3-small")

    assert first.startswith("remote:text-embedding-3-small@")
    assert second.startswith("remote:text-embedding-3-small@")
    assert first != second


def test_validate_remote_embedding_model_probes_embeddings_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("REMOTE_LLM_BASE_URL", "https://models.example.test/v1")
    monkeypatch.setenv("REMOTE_LLM_API_KEY", "test-key")
    captured: dict = {}

    def fake_open(url: str, **kwargs):
        captured["url"] = url
        captured["payload"] = kwargs.get("json_payload")
        return {"data": [{"embedding": [0.1, 0.2]}]}

    monkeypatch.setattr(llm_provider, "_open_url_json", fake_open)

    ok, resolved, message = llm_provider.validate_remote_embedding_model("embed-model")

    assert ok is True
    assert resolved == "embed-model"
    assert message == ""
    assert captured["url"] == "https://models.example.test/v1/embeddings"
    assert captured["payload"] == {"model": "embed-model", "input": ["ping"]}


def test_embedding_probe_stops_after_transient_timeout(monkeypatch) -> None:
    monkeypatch.setattr(llm_provider, "_runtime_mode", "ollama")
    monkeypatch.setattr(
        llm_provider,
        "resolve_local_ollama_model_name",
        lambda *_args, **_kwargs: "embed-model",
    )
    calls: list[float] = []

    def timed_out(*_args, **kwargs):
        calls.append(float(kwargs["timeout"]))
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm_provider, "_open_ollama_url", timed_out)

    ok, resolved, message = llm_provider.validate_local_embedding_model(
        "embed-model",
        probe_timeout_seconds=3,
    )

    assert ok is True
    assert resolved == "embed-model"
    assert "incertaine" in message
    assert calls == [3.0]


def test_format_llm_exception_strips_instructor_retry_xml() -> None:
    exc = RuntimeError(
        "<failed_attempts><generation><exception>Connection error.</exception></generation>"
        "</failed_attempts><last_exception> Connection error. </last_exception>"
    )

    message = llm_provider._format_llm_exception(exc)

    assert message == "Connection error."
    assert "failed_attempts" not in message


def test_instructor_cache_closes_previous_clients_when_endpoint_changes(monkeypatch) -> None:
    class _Closable:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    endpoint = {"base_url": "http://first/v1"}
    http_clients: list[_Closable] = []
    instructor_clients: list[_Closable] = []

    def fake_endpoint():
        http_client = _Closable()
        http_clients.append(http_client)
        return endpoint["base_url"], "key", 10.0, http_client

    def fake_build(**_kwargs):
        client = _Closable()
        instructor_clients.append(client)
        return client

    monkeypatch.setattr(llm_provider, "_instructor_client_cache", None)
    monkeypatch.setattr(llm_provider, "_instructor_endpoint", fake_endpoint)
    monkeypatch.setattr(llm_provider, "build_instructor_client", fake_build)

    first, *_ = llm_provider._get_cached_instructor_client()
    endpoint["base_url"] = "http://second/v1"
    second, *_ = llm_provider._get_cached_instructor_client()

    assert first is instructor_clients[0]
    assert second is instructor_clients[1]
    assert instructor_clients[0].closed is True
    assert instructor_clients[1].closed is False
    assert http_clients[0].closed is True


class _RecordingEmbedder:
    def __init__(self) -> None:
        self.documents: list[str] = []
        self.queries: list[str] = []

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.documents.extend(texts)
        return [[1.0] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return [1.0]


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        ("embeddinggemma", "embeddinggemma"),
        ("embeddinggemma:latest", "embeddinggemma"),
        ("embeddinggemma-2:440m", "embeddinggemma"),
        ("text-embedding-embeddinggemma-300m", "embeddinggemma"),
        ("google/EmbeddingGemma-2", "embeddinggemma"),
        ("qwen3-embedding:0.6b", ""),
        ("bge-m3", ""),
        ("", ""),
    ],
)
def test_default_embedding_prompts_follow_the_embeddinggemma_family(
    model_name: str, expected: str
) -> None:
    assert llm_provider.default_embedding_prompts(model_name) == expected


def test_embeddinggemma_prompts_prefix_queries_and_passages() -> None:
    inner = _RecordingEmbedder()
    embeddings = llm_provider.with_embedding_prompts(inner, "embeddinggemma")

    embeddings.embed_documents(["Critical current of HTS tapes."])
    embeddings.embed_query("courant critique")

    assert inner.documents == ["title: none | text: Critical current of HTS tapes."]
    assert inner.queries == ["task: search result | query: courant critique"]


@pytest.mark.parametrize("scheme", ["", None, "unknown-scheme"])
def test_raw_or_unknown_prompt_scheme_keeps_the_embedder(scheme: str | None) -> None:
    inner = _RecordingEmbedder()

    assert llm_provider.with_embedding_prompts(inner, scheme) is inner
