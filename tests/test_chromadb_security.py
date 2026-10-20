from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APPLICATION_FILES = (
    PROJECT_ROOT / "app_api.py",
    *sorted((PROJECT_ROOT / "src").rglob("*.py")),
)


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _application_trees() -> list[tuple[Path, ast.AST]]:
    return [
        (path, ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path)))
        for path in APPLICATION_FILES
    ]


def test_chromadb_network_server_is_not_exposed() -> None:
    violations: list[str] = []

    for path, tree in _application_trees():
        relative = path.relative_to(PROJECT_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "chromadb.server"
            ):
                violations.append(f"{relative}:{node.lineno}: import serveur Chroma")
            elif isinstance(node, ast.Import) and any(
                alias.name.startswith("chromadb.server") for alias in node.names
            ):
                violations.append(f"{relative}:{node.lineno}: import serveur Chroma")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "chromadb.auth"
            ):
                violations.append(f"{relative}:{node.lineno}: fournisseur auth Chroma")
            elif isinstance(node, ast.Import) and any(
                alias.name.startswith("chromadb.auth") for alias in node.names
            ):
                violations.append(f"{relative}:{node.lineno}: fournisseur auth Chroma")
            elif isinstance(node, ast.Call) and _call_name(node) in {
                "HttpClient",
                "AsyncHttpClient",
            }:
                violations.append(f"{relative}:{node.lineno}: client Chroma distant")
            elif isinstance(node, ast.Name) and node.id == "SimpleRBACAuthorizationProvider":
                violations.append(f"{relative}:{node.lineno}: SimpleRBAC Chroma")

    assert not violations, "\n".join(violations)


def test_every_chroma_store_has_an_explicit_embedding_function() -> None:
    violations: list[str] = []
    chroma_calls = 0

    for path, tree in _application_trees():
        relative = path.relative_to(PROJECT_ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "Chroma":
                continue
            chroma_calls += 1
            embedding = next(
                (keyword.value for keyword in node.keywords if keyword.arg == "embedding_function"),
                None,
            )
            if embedding is None or (
                isinstance(embedding, ast.Constant) and embedding.value is None
            ):
                violations.append(f"{relative}:{node.lineno}: embedding explicite manquant")

    assert chroma_calls > 0, "Aucune construction langchain_chroma.Chroma détectée"
    assert not violations, "\n".join(violations)


def test_chroma_collection_configuration_cannot_load_remote_code() -> None:
    violations: list[str] = []

    for path, tree in _application_trees():
        relative = path.relative_to(PROJECT_ROOT)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"create_collection", "get_or_create_collection", "modify"}:
                    violations.append(
                        f"{relative}:{node.lineno}: configuration directe de collection Chroma"
                    )
            if not isinstance(node, ast.keyword) or node.arg != "trust_remote_code":
                continue
            if isinstance(node.value, ast.Constant) and node.value.value is True:
                violations.append(f"{relative}:{node.value.lineno}: trust_remote_code=True")

    assert not violations, "\n".join(violations)


def test_security_workflow_always_runs_release_scans() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "ENABLE_EXTERNAL_SECURITY_SCANS" not in workflow
    assert "npm ci --ignore-scripts --no-audit --no-fund" in workflow
    assert "npm audit --audit-level=high --ignore-scripts" in workflow
    assert "gitleaks/gitleaks-action@v2" in workflow
    for advisory in (
        "PYSEC-2026-311",
        "PYSEC-2026-3813",
        "PYSEC-2026-3814",
        "PYSEC-2026-3815",
    ):
        assert f"--ignore-vuln {advisory}" in workflow
