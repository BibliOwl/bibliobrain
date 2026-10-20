"""One-time migration from a legacy .env file to Bibliobrain stores.

The application itself never imports or reads .env files. This explicit helper
exists only so existing installations can move their values without exposing
secrets in command-line arguments. It never deletes the source file.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.api_secrets import (  # noqa: E402
    SECRET_DEFINITIONS,
    api_config_status,
    update_api_config,
)
from src.machine_config import (  # noqa: E402
    MACHINE_SETTING_NAMES,
    load_machine_settings,
    save_machine_settings,
)

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOTE_URL_ENV = "REMOTE_LLM_BASE_URL"


def _parse_legacy_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, raw_value = line.partition("=")
        name = name.strip()
        if not separator or not _NAME_RE.fullmatch(name):
            raise ValueError(f"Ligne {line_number} invalide dans {path.name}.")
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if value:
            values[name] = value
    return values


def _secret_updates(values: dict[str, str]) -> dict[str, str]:
    updates: dict[str, str] = {}
    for key, definition in SECRET_DEFINITIONS.items():
        names = (str(definition["environment"]), *definition.get("aliases", ()))
        value = next((values.get(name, "").strip() for name in names if values.get(name)), "")
        if value:
            updates[key] = value
    remote_url = values.get(_REMOTE_URL_ENV, "").strip()
    if remote_url:
        updates["remote_llm_base_url"] = remote_url
    return updates


def _storage_dir(values: dict[str, str]) -> Path:
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA")
        or (Path.home() / "AppData" / "Local")
    )
    app_home = Path(
        values.get("BIBLIOBRAIN_HOME", "").strip()
        or (local_app_data / "bibliobrain")
    )
    return Path(values.get("STORAGE_DIR", "").strip() or (app_home / "storage"))


def _stored_api_config_keys(storage_dir: Path) -> set[str]:
    """Return only keys already persisted in the DPAPI store, never their values."""
    status = api_config_status(storage_dir)
    stored = {
        key
        for key, metadata in status["secrets"].items()
        if metadata.get("source") == "secure_store"
    }
    if status["remote_llm_base_url"].get("source") == "secure_store":
        stored.add("remote_llm_base_url")
    return stored


def migrate(path: Path) -> tuple[Path, Path, int, int]:
    values = _parse_legacy_env(path)
    existing_settings = load_machine_settings()
    machine_updates = {
        name: value
        for name, value in values.items()
        if name in MACHINE_SETTING_NAMES and name not in existing_settings
    }
    config_target = save_machine_settings(machine_updates)

    # Existing path settings identify the active secure store. Legacy values are
    # only fallbacks, just like every other value handled by this migration.
    effective_storage_settings = dict(values)
    effective_storage_settings.update(existing_settings)
    storage_dir = _storage_dir(effective_storage_settings)
    stored_keys = _stored_api_config_keys(storage_dir)
    secrets = {
        key: value
        for key, value in _secret_updates(values).items()
        if key not in stored_keys
    }
    if secrets:
        update_api_config(storage_dir, updates=secrets)
    return config_target, storage_dir, len(machine_updates), len(secrets)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrer explicitement un ancien .env Bibliobrain.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="Ancien fichier à importer (défaut : .env du dossier courant).",
    )
    args = parser.parse_args()
    source = args.env_file.resolve()
    if not source.is_file():
        parser.error(f"fichier introuvable : {source}")
    try:
        config_path, storage_dir, public_count, secret_count = migrate(source)
    except Exception as exc:
        print(f"Migration impossible : {exc}", file=sys.stderr)
        return 1

    print(f"Configuration non sensible : {config_path}")
    print(f"Coffre chiffré : {storage_dir / 'api_secrets.dpapi.json'}")
    print(f"Valeurs migrées : {public_count} réglage(s), {secret_count} secret(s).")
    print("Le fichier source n'a pas été supprimé. Vérifiez le démarrage, puis supprimez-le manuellement.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
