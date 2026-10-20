from __future__ import annotations

import ast
from pathlib import Path

from src.machine_config import (
    INTERNAL_ENVIRONMENT_NAMES,
    MACHINE_SETTING_NAMES,
    SECRET_ENVIRONMENT_NAMES,
)

_SYSTEM_ENV = {"APPDATA", "LOCALAPPDATA"}


def _direct_environment_names() -> set[str]:
    names: set[str] = set()
    root = Path(__file__).resolve().parents[1]
    for path in [root / "app_api.py", *(root / "src").rglob("*.py")]:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = node.func
            is_environment_read = (
                isinstance(function, ast.Attribute)
                and function.attr in {"getenv", "get"}
                and (
                    isinstance(function.value, ast.Name)
                    and function.value.id == "os"
                    or isinstance(function.value, ast.Attribute)
                    and isinstance(function.value.value, ast.Name)
                    and function.value.value.id == "os"
                    and function.value.attr == "environ"
                )
            )
            if is_environment_read and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str) and value.isupper():
                    names.add(value)
    return names


def test_direct_environment_variables_have_an_explicit_owner() -> None:
    owned = (
        MACHINE_SETTING_NAMES
        | SECRET_ENVIRONMENT_NAMES
        | INTERNAL_ENVIRONMENT_NAMES
        | _SYSTEM_ENV
    )
    assert _direct_environment_names() - owned == set()


def test_application_no_longer_loads_dotenv() -> None:
    root = Path(__file__).resolve().parents[1]
    config_source = (root / "src" / "config.py").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")

    assert "load_dotenv" not in config_source
    assert "dotenv_values" not in config_source
    assert "python-dotenv" not in requirements
