r"""Generate a reproducible Markdown inventory of the active Python environment.

The report distinguishes:

- packages reachable from ``requirements.txt``;
- packages used only by ``requirements-dev.txt``;
- installed packages that are not explained by either requirements file;
- missing direct requirements;
- the complete active dependency graph as an adjacency tree.

Run from the repository root with the Bibliobrain virtual environment:

    .\.venv\Scripts\python.exe scripts\dependency_inventory.py \
        --output docs\5-DEVELOPMENT\python-dependency-tree.md
"""

from __future__ import annotations

import argparse
import platform
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from importlib.metadata import Distribution, distributions
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


@dataclass(frozen=True)
class RootRequirement:
    requirement: Requirement
    source: str


def _read_roots(path: Path) -> list[RootRequirement]:
    roots: list[RootRequirement] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        roots.append(RootRequirement(Requirement(line), path.name))
    return roots


def _marker_is_active(requirement: Requirement, extras: set[str]) -> bool:
    if requirement.marker is None:
        return True
    environment = default_environment()
    return any(
        requirement.marker.evaluate({**environment, "extra": extra})
        for extra in (extras or {""})
    )


def _installed_distributions() -> dict[str, Distribution]:
    return {
        canonicalize_name(distribution.metadata["Name"]): distribution
        for distribution in distributions()
        if distribution.metadata.get("Name")
    }


def _display(distribution: Distribution) -> str:
    return f"{distribution.metadata['Name']}=={distribution.version}"


def _active_dependencies(
    distribution: Distribution,
    extras: set[str],
    installed: dict[str, Distribution],
) -> list[tuple[str, set[str]]]:
    dependencies: dict[str, set[str]] = {}
    for raw_requirement in distribution.requires or []:
        requirement = Requirement(raw_requirement)
        if not _marker_is_active(requirement, extras):
            continue
        name = canonicalize_name(requirement.name)
        if name not in installed:
            continue
        dependencies.setdefault(name, set()).update(requirement.extras)
    return sorted(dependencies.items())


def _dependency_closure(
    roots: list[RootRequirement],
    installed: dict[str, Distribution],
) -> tuple[set[str], dict[str, set[str]]]:
    nodes: set[str] = set()
    edges: dict[str, set[str]] = defaultdict(set)
    queue: deque[tuple[str, set[str]]] = deque(
        (
            canonicalize_name(root.requirement.name),
            set(root.requirement.extras),
        )
        for root in roots
    )
    visited_states: set[tuple[str, tuple[str, ...]]] = set()

    while queue:
        name, extras = queue.popleft()
        state = (name, tuple(sorted(extras)))
        if state in visited_states or name not in installed:
            continue
        visited_states.add(state)
        nodes.add(name)
        for dependency, dependency_extras in _active_dependencies(
            installed[name], extras, installed
        ):
            edges[name].add(dependency)
            queue.append((dependency, dependency_extras))

    return nodes, edges


def _all_default_edges(
    installed: dict[str, Distribution],
) -> dict[str, set[str]]:
    return {
        name: {
            dependency
            for dependency, _ in _active_dependencies(distribution, set(), installed)
        }
        for name, distribution in installed.items()
    }


def _undeclared_roots(
    undeclared: set[str],
    all_edges: dict[str, set[str]],
) -> list[str]:
    incoming: dict[str, set[str]] = defaultdict(set)
    for parent in undeclared:
        for child in all_edges[parent] & undeclared:
            incoming[child].add(parent)
    return sorted(name for name in undeclared if not incoming[name])


def _subtree_nodes(
    root: str,
    allowed: set[str],
    all_edges: dict[str, set[str]],
) -> set[str]:
    result: set[str] = set()
    stack = [root]
    while stack:
        name = stack.pop()
        if name in result or name not in allowed:
            continue
        result.add(name)
        stack.extend(all_edges[name] & allowed)
    return result


def _render_graph(
    title: str,
    nodes: set[str],
    edges: dict[str, set[str]],
    installed: dict[str, Distribution],
    direct_roots: set[str],
) -> list[str]:
    lines = [f"## {title}", ""]
    for name in sorted(nodes):
        direct_label = " — racine directe" if name in direct_roots else ""
        lines.append(f"- `{_display(installed[name])}`{direct_label}")
        dependencies = sorted(edges.get(name, set()) & nodes)
        if not dependencies:
            lines.append("  - *(feuille : aucune dépendance Python active installée)*")
            continue
        for dependency in dependencies:
            lines.append(f"  - `{_display(installed[dependency])}`")
    lines.append("")
    return lines


def _render_report(project_root: Path) -> str:
    production_roots = _read_roots(project_root / "requirements.txt")
    development_roots = _read_roots(project_root / "requirements-dev.txt")
    installed = _installed_distributions()

    production_nodes, production_edges = _dependency_closure(
        production_roots, installed
    )
    development_nodes, development_edges = _dependency_closure(
        development_roots, installed
    )
    development_only = development_nodes - production_nodes
    declared_union = production_nodes | development_nodes

    # pip is the virtual-environment package manager. setuptools can also be a
    # real transitive build dependency, so remove bootstrap tools only when they
    # are not already explained by an application/development root.
    bootstrap = {"pip", "setuptools"} & set(installed)
    undeclared = set(installed) - declared_union - bootstrap
    missing_roots = [
        root
        for root in production_roots + development_roots
        if canonicalize_name(root.requirement.name) not in installed
    ]

    all_edges = _all_default_edges(installed)
    undeclared_roots = _undeclared_roots(undeclared, all_edges)
    production_direct = {
        canonicalize_name(root.requirement.name) for root in production_roots
    }
    development_direct = {
        canonicalize_name(root.requirement.name) for root in development_roots
    }

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    executable = Path(sys.executable).resolve()
    try:
        executable_label = str(executable.relative_to(project_root))
    except ValueError:
        executable_label = str(executable)
    lines = [
        "# Arbre complet des dépendances Python",
        "",
        "> Fichier généré par `scripts/dependency_inventory.py`. Ne pas modifier",
        "> manuellement : régénérer l'inventaire depuis l'environnement à auditer.",
        "",
        "## Périmètre du snapshot",
        "",
        f"- Généré le : `{generated_at}`",
        f"- Python : `{platform.python_version()}`",
        f"- Exécutable : `{executable_label}`",
        f"- Plateforme : `{platform.platform()}`",
        f"- Distributions installées : **{len(installed)}**",
        f"- Fermeture des dépendances de production : **{len(production_nodes)}**",
        f"- Dépendances uniquement développement : **{len(development_only)}**",
        f"- Distributions installées non expliquées par les manifests : **{len(undeclared)}**",
        "",
        "La « fermeture » contient chaque racine directe et toutes ses dépendances",
        "transitives actives pour cette plateforme et les extras demandés.",
        "",
        "## Racines déclarées",
        "",
        "### Production (`requirements.txt`)",
        "",
    ]
    lines.extend(
        f"- `{root.requirement}`" for root in production_roots
    )
    lines.extend(["", "### Développement (`requirements-dev.txt`)", ""])
    lines.extend(
        f"- `{root.requirement}`" for root in development_roots
    )
    lines.extend(["", "## Exigences directes absentes", ""])
    if missing_roots:
        lines.extend(
            f"- `{root.requirement}` ({root.source})" for root in missing_roots
        )
    else:
        lines.append("- Aucune.")
    lines.append("")

    lines.extend(
        _render_graph(
            "Graphe complet de production",
            production_nodes,
            production_edges,
            installed,
            production_direct,
        )
    )
    lines.extend(
        _render_graph(
            "Graphe des dépendances uniquement développement",
            development_only,
            development_edges,
            installed,
            development_direct,
        )
    )

    lines.extend(
        [
            "## Distributions installées hors manifests",
            "",
            "Ces paquets ne sont atteignables depuis aucune racine active de",
            "`requirements.txt` ou `requirements-dev.txt`. Ils proviennent donc",
            "d'une installation manuelle, d'un ancien manifest ou d'un outil ajouté",
            "dans le même environnement. Cela ne prouve pas à lui seul qu'ils sont",
            "supprimables : vérifier les imports dynamiques et les outils opérateur.",
            "",
            "### Racines non déclarées probables",
            "",
        ]
    )
    if undeclared_roots:
        for root in undeclared_roots:
            subtree = _subtree_nodes(root, undeclared, all_edges)
            dependencies = sorted(subtree - {root})
            lines.append(f"- `{_display(installed[root])}`")
            if dependencies:
                lines.append(
                    "  - sous-arbre : "
                    + ", ".join(f"`{_display(installed[name])}`" for name in dependencies)
                )
            else:
                lines.append("  - aucun sous-paquet non déclaré")
    else:
        lines.append("- Aucune.")

    lines.extend(["", "### Liste exhaustive hors manifests", ""])
    if undeclared:
        lines.extend(f"- `{_display(installed[name])}`" for name in sorted(undeclared))
    else:
        lines.append("- Aucune.")

    lines.extend(["", "## Outils de bootstrap", ""])
    for name in sorted(bootstrap):
        lines.append(f"- `{_display(installed[name])}`")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    report = _render_report(args.project_root.resolve())
    if args.output:
        output = args.output
        if not output.is_absolute():
            output = args.project_root / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
