import logging
import os
import warnings
from pathlib import Path

from src.machine_config import MachineConfigError, activate_machine_settings, application_root

# Persistent non-secret configuration is loaded from the fixed per-user JSON
# file before paths and providers are resolved. Bibliobrain no longer loads a
# .env file. Explicit process variables remain useful for ephemeral overrides.
try:
    _LOADED_CONFIGURATION_PATH = activate_machine_settings()
except MachineConfigError as exc:  # pragma: no cover - degraded startup
    _LOADED_CONFIGURATION_PATH = None
    logging.getLogger(__name__).warning("Unable to load local configuration: %s", exc)

# Supprime les avertissements de pypdf sur les PDF mal formes (inoffensifs)
logging.getLogger("pypdf").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", category=UserWarning, module="pypdf")

# SQLite et ChromaDB necessitent NTFS (pas exFAT ni reseau).
# Par defaut, tout est regroupe sous %LOCALAPPDATA%\bibliobrain\ :
#   - data\
#   - storage\
#   - chroma\
# Remplacable via variables d'environnement.
_LOCAL_APP_DATA = os.environ.get("LOCALAPPDATA") or os.path.join(
    os.path.expanduser("~"), "AppData", "Local"
)
_DEFAULT_APP_HOME = os.path.join(_LOCAL_APP_DATA, "bibliobrain")
_DEFAULT_CHROMA_DIRNAME = "chroma"
_LEGACY_CHROMA_DIRNAME = "chroma_db_zotero"


# A relative path in the configuration (the old .env used "./chroma_db_zotero")
# used to depend on the folder Bibliobrain was started from: started from a
# branch worktree or a shortcut, it built a new, empty index there. It is now
# read relative to the application folder, where the .env it came from lived
# (the main checkout when running from a git worktree).
_APP_ROOT = application_root()


def _env_path(name: str) -> str:
    value = str(os.getenv(name, "") or "").strip()
    if not value:
        return ""
    expanded = os.path.expandvars(os.path.expanduser(value))
    if os.path.isabs(expanded):
        return expanded
    resolved = str((_APP_ROOT / expanded).resolve())
    logging.getLogger(__name__).info(
        "%s is a relative path (%s): using %s. Set an absolute path to be explicit.",
        name,
        value,
        resolved,
    )
    return resolved


def _resolve_app_home() -> str:
    configured = _env_path("BIBLIOBRAIN_HOME")
    return configured or _DEFAULT_APP_HOME


def _resolve_default_chroma_path(app_home: str) -> str:
    app_home_path = Path(app_home)
    current_path = app_home_path / _DEFAULT_CHROMA_DIRNAME
    legacy_path = app_home_path / _LEGACY_CHROMA_DIRNAME
    if current_path.exists() or not legacy_path.exists():
        return str(current_path)
    return str(legacy_path)


BIBLIOBRAIN_HOME = _resolve_app_home()
DATA_PATH = _env_path("DATA_PATH") or os.path.join(BIBLIOBRAIN_HOME, "data")
CHROMA_PATH = _env_path("CHROMA_PATH") or _resolve_default_chroma_path(BIBLIOBRAIN_HOME)
STORAGE_DIR = _env_path("STORAGE_DIR") or os.path.join(BIBLIOBRAIN_HOME, "storage")
ZOTERO_DB_PATH = _env_path("ZOTERO_DB_PATH")
# Process state written by the managed launcher (PIDs, update result) and
# rotating application logs. Both live next to the user data, never next to the
# code, so an installation directory stays read-only after setup.
RUNTIME_DIR = os.path.join(BIBLIOBRAIN_HOME, "runtime")
LOG_DIR = os.path.join(BIBLIOBRAIN_HOME, "logs")

# Les clés saisies dans l'interface sont chiffrées avec Windows DPAPI et
# injectées uniquement dans le processus Bibliobrain courant.
try:
    from src.api_secrets import activate_persisted_api_config

    activate_persisted_api_config(STORAGE_DIR)
except Exception as exc:  # pragma: no cover - démarrage dégradé, journal sans secret
    logging.getLogger(__name__).warning(
        "Unable to load the encrypted API configuration: %s",
        exc,
    )


def set_data_path(path: str) -> None:
    """Met a jour DATA_PATH a chaud (utilise quand on pointe vers le stockage Zotero)."""
    global DATA_PATH
    DATA_PATH = path


def set_zotero_path(path: str) -> None:
    """Met a jour le chemin de la base Zotero chargee."""
    global ZOTERO_DB_PATH
    ZOTERO_DB_PATH = path
