import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import workspace_assistance as assistance
from src.api import discovery, drafting


def test_comparison_rejects_unsupported_quotes_and_keeps_article_identity(monkeypatch):
    monkeypatch.setattr(
        assistance,
        "_invoke_json",
        lambda *a, **kw: {
            "papers": [
                {
                    "id": 1,
                    "method": {"summary": "A trial", "quote": "randomized trial"},
                    "findings": {"summary": "Huge effect", "quote": "invented evidence"},
                },
                {"id": 0, "limitations": {"summary": "Small sample", "quote": "small sample"}},
                {"id": 9, "method": {"summary": "Wrong paper", "quote": "randomized trial"}},
            ]
        },
    )
    result = assistance.compare_discovered_articles(
        [
            {"id": 0, "title": "First", "abstract": "A survey with a small sample."},
            {"id": 1, "title": "Second", "abstract": "A randomized\ntrial showed improvement."},
            {"id": 2, "title": "No abstract", "abstract": ""},
        ],
        "test-model",
        "fr",
    )
    assert [p["id"] for p in result["papers"]] == [0, 1, 2]
    assert result["papers"][0]["limitations"]["summary"] == "Small sample"
    assert result["papers"][1]["method"]["quote"] == "randomized trial"
    assert result["papers"][1]["findings"] is None
    assert result["papers"][2]["method"] is None


@pytest.mark.parametrize("action", ["shorten", "rephrase"])
def test_rewrite_preserves_language_and_returns_only_generated_text(monkeypatch, action):
    prompts = []

    def generate(model, prompt, **kwargs):
        prompts.append(prompt)
        return {"text": " Résultat plus clair. "}

    monkeypatch.setattr(assistance, "_invoke_json", generate)
    assert assistance.rewrite_passage("Texte original.", action, "model") == {
        "text": "Résultat plus clair."
    }
    assert "Preserve the input language" in prompts[0]
    assert "Texte original." in prompts[0]


def test_rewrite_rejects_empty_model_output(monkeypatch):
    monkeypatch.setattr(assistance, "_invoke_json", lambda *a, **kw: {"text": " "})
    with pytest.raises(ValueError):
        assistance.rewrite_passage("Original", "shorten", "model")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(discovery.router)
    app.include_router(drafting.router)
    return TestClient(app)


def test_workspace_routes_validate_bounds_and_forward_requests(client, monkeypatch):
    calls = []

    def rewrite_passage(*args):
        calls.append(args)
        return {"text": "Revised"}

    monkeypatch.setattr(assistance, "rewrite_passage", rewrite_passage)
    response = client.post(
        "/api/drafting-rewrite", json={"text": "Original", "action": "shorten", "model": "model"}
    )
    assert response.status_code == 200
    assert calls == [("Original", "shorten", "model")]
    for text, action in [("x" * 12001, "shorten"), ("", "shorten"), ("Original", "invent")]:
        assert (
            client.post(
                "/api/drafting-rewrite", json={"text": text, "action": action, "model": "model"}
            ).status_code
            == 422
        )
    paper = {"id": 0, "title": "Paper", "abstract": "Abstract"}
    assert (
        client.post(
            "/api/article-discovery/compare", json={"papers": [paper] * 5, "model": "model"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/article-discovery/compare", json={"papers": [paper], "model": "model"}
        ).status_code
        == 422
    )
    monkeypatch.setattr(
        assistance, "compare_discovered_articles", lambda *args: {"papers": args[0]}
    )
    assert (
        client.post(
            "/api/article-discovery/compare",
            json={"papers": [paper, {**paper, "id": 1}], "model": "model"},
        ).status_code
        == 200
    )


def test_workspace_routes_return_recoverable_errors(client, monkeypatch):
    def fail(*args):
        raise RuntimeError("private provider details")

    monkeypatch.setattr(assistance, "rewrite_passage", fail)
    result = client.post(
        "/api/drafting-rewrite", json={"text": "Original", "action": "shorten", "model": "model"}
    )
    assert result.status_code == 502
    assert "private provider details" not in result.text
