"""Move the active vector index from one computer to another.

An archive holds one index profile (Chroma, lexical sidecar, manifest) and a
header describing how it was built: embedding model, chunk size, the Zotero
library it was matched against, and the Zotero identity of every indexed
document. Nothing machine-specific travels: no configuration.json, no secret,
no absolute path. Before anything is replaced, ``inspect_archive`` compares
that header with this computer, so the user learns which documents are
missing, which ones the next reload will re-embed, and which embedding model
must be installed.
"""

from __future__ import annotations

import contextlib
import gc
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from src import config, zotero_drops
from src.app_settings import get_app_settings
from src.document_formats import document_index_variant
from src.llm_provider import (
    current_embedding_profile_label,
    get_embedding_provider_mode,
    required_embedding_model_name,
    set_runtime_embedding_model,
)
from src.rag_settings import get_persisted_chunk_size, set_persisted_chunk_size
from src.rel_paths import candidate_rel_paths
from src.visual_analysis import index_visual_signature
from src.zotero_db import (
    ZoteroItem,
    filter_active_zotero_pdf_entries,
    find_zotero_db,
    is_zotero_storage_path,
    load_zotero_library,
    load_zotero_user_library_uri,
    lookup_zotero_item,
)

from . import state
from .embedding_equivalence import compare_with_current_embedder
from .embedding_equivalence import profile_probe as _index_probe
from .vector_store import (
    _INDEX_PROFILE_VERSION,
    _close_chroma_store,
    _current_chunk_size,
    _current_embedding_profile,
    _index_profiles_root,
    _manifest_chunk_count,
    build_vector_index_profile,
    get_vector_database_status,
    set_active_vector_index_profile,
)
from .zotero_integration import _file_signature, _normalize_text, _zotero_signature

ARCHIVE_FORMAT = "bibliobrain-index"
ARCHIVE_FORMAT_VERSION = 1
HEADER_NAME = "bibliobrain-index.json"
_INDEX_PREFIX = "index/"
_SQLITE_SUFFIXES = (".sqlite3", ".sqlite", ".db")
# SQLite side files are folded into the snapshot; lock files are per process.
_SKIPPED_SUFFIXES = ("-wal", "-shm", "-journal", ".lock")
_PROVIDER_LABELS = {"ollama": "Ollama", "lmstudio": "LM Studio", "remote": "un service distant"}
_LISTED_DOCUMENTS = 50
_logger = logging.getLogger(__name__)


class IndexTransferError(RuntimeError):
    """A transfer step failed; ``code`` is stable, ``message`` is for the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _same_model(left: str, right: str) -> bool:
    return _normalize_text(left) == _normalize_text(right)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _application_version() -> str:
    version_path = Path(__file__).resolve().parents[2] / "VERSION"
    try:
        return version_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


# ------ Metadata library ----------------------------------------


def _metadata_library() -> tuple[str, str, dict[str, ZoteroItem]]:
    """Metadata source, its path and library, as the next reload would load them."""
    try:
        settings = get_app_settings()
    except (OSError, TypeError, ValueError):
        settings = {}
    source = str(settings.get("metadata_source") or "none").strip().lower()
    if source not in {"zotero", "bibtex"}:
        return source, "", {}
    path = str(settings.get("zotero_path") or config.ZOTERO_DB_PATH or "").strip()
    if not path and source == "zotero":
        path = find_zotero_db() or ""
    if not path or not os.path.exists(path):
        return source, path, {}
    try:
        return source, path, load_zotero_library(path, zotero_drops.fingerprints())
    except Exception:
        _logger.warning("Unable to read the metadata library %s", path, exc_info=True)
        return source, path, {}


def _zotero_account(source: str, path: str) -> tuple[str, str]:
    """Hashed library identity and its kind ("synced" or "local"), or empty strings.

    A synced library has the same Zotero user ID on every computer; a local
    one gets a random key per installation, so it never matches elsewhere.
    """
    if source != "zotero" or not path:
        return "", ""
    uri = load_zotero_user_library_uri(path)
    if not uri:
        return "", ""
    kind = "local" if "/users/local/" in uri else "synced"
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()[:16], kind


def _document_identity(rel_path: str, library: dict[str, ZoteroItem]) -> dict[str, str]:
    item = lookup_zotero_item(rel_path, library)
    if item is None:
        return {"key": "", "sig": "", "title": ""}
    return {
        "key": str(item.item_key or ""),
        "sig": _zotero_signature(item),
        "title": str(item.title or ""),
    }


# ------ Embedding model -----------------------------------------


def _ollama_model_entry(model_name: str) -> tuple[bool, dict[str, Any] | None]:
    """(Ollama reachable, the exact local model entry or None).

    Tags are compared exactly: two tags of one family (``:0.6b``, ``:8b``)
    are different models whose vectors do not mix.
    """
    from src.llm_provider import _embedding_ollama_base_url, _read_ollama_json

    try:
        payload = _read_ollama_json(_embedding_ollama_base_url(), "/api/tags", timeout=4)
    except Exception:
        _logger.debug("Ollama unreachable while checking an index model", exc_info=True)
        return False, None
    wanted = model_name.strip().lower()
    if ":" not in wanted:
        wanted = f"{wanted}:latest"
    for entry in payload.get("models", []):
        if not isinstance(entry, dict):
            continue
        names = {str(entry.get("name") or "").lower(), str(entry.get("model") or "").lower()}
        if wanted in names:
            return True, entry
    return True, None


def _current_model_digest(model_name: str) -> str:
    if get_embedding_provider_mode() != "ollama":
        return ""
    _reachable, entry = _ollama_model_entry(model_name)
    return str((entry or {}).get("digest") or "")


def embedding_compatibility(index_info: dict[str, Any]) -> dict[str, Any]:
    """Compare the index's embedding model with this computer's setup.

    ``action`` tells the user what to do: ``ready`` (nothing), ``equivalent``
    (the model configured here, whatever its service or name, reproduces the
    index's vectors), ``switch`` (Bibliobrain will select the model after
    import), ``download`` (Ollama can fetch it), ``install_manually`` (LM
    Studio or a remote Ollama) or ``provider_mismatch`` (nothing configured
    here produces the same vectors).
    """
    from src.llm_provider import _embedding_ollama_base_url, resolve_local_lmstudio_model_name
    from src.ollama_utils import is_local_ollama_url

    label = str(index_info.get("embedding_model") or "")
    model_name = str(index_info.get("embedding_model_name") or label)
    provider = str(index_info.get("embedding_provider") or "ollama")
    current_provider = get_embedding_provider_mode()
    current_label = _current_embedding_profile()
    same_profile = _same_model(label, current_label)
    adoptable = provider == current_provider and _same_model(
        current_embedding_profile_label(model_name), label
    )

    installed: bool | None = None
    reachable = True
    local_name = ""
    local_digest = ""
    if adoptable or same_profile:
        if provider == "ollama":
            reachable, entry = _ollama_model_entry(model_name)
            if reachable:
                installed = entry is not None
                local_name = str((entry or {}).get("name") or "")
                local_digest = str((entry or {}).get("digest") or "")
        elif provider == "lmstudio":
            local_name = resolve_local_lmstudio_model_name(model_name)
            installed = bool(local_name)
        else:
            installed = True

    index_digest = str(index_info.get("embedding_model_digest") or "")
    digest_matches = None
    if index_digest and local_digest:
        digest_matches = index_digest == local_digest
    can_download = bool(
        provider == "ollama"
        and installed is False
        and is_local_ollama_url(_embedding_ollama_base_url())
    )

    equivalence = {"status": "skipped", "similarity": None, "detail": ""}
    if not same_profile:
        equivalence = compare_with_current_embedder(
            list(index_info.get("embedding_probe") or []),
            embedding_prompts=str(index_info.get("embedding_prompts") or ""),
        )

    if same_profile and installed is not False:
        action = "ready"
    elif equivalence["status"] == "same":
        action = "equivalent"
    elif installed is False:
        action = "download" if can_download else "install_manually"
    elif adoptable:
        action = "switch"
    else:
        action = "provider_mismatch"

    return {
        "action": action,
        "equivalence": equivalence,
        "model": label,
        "model_name": model_name,
        "provider": provider,
        "provider_label": _PROVIDER_LABELS.get(provider, provider),
        "current_model": current_label,
        "current_model_name": required_embedding_model_name(),
        "current_provider": current_provider,
        "current_provider_label": _PROVIDER_LABELS.get(current_provider, current_provider),
        "same_as_current": same_profile,
        "installed": installed,
        "provider_reachable": reachable,
        "local_model_name": local_name,
        "digest_matches": digest_matches,
        "can_download": can_download,
        "install_command": f"ollama pull {model_name}" if provider == "ollama" else "",
    }


def pull_embedding_model(model_name: str) -> None:
    """Download an embedding model into the local Ollama (can take minutes)."""
    from src.llm_provider import (
        _embedding_ollama_base_url,
        _ollama_headers,
        _ollama_httpx_verify,
        _open_url_json,
    )
    from src.ollama_utils import is_local_ollama_url

    base_url = _embedding_ollama_base_url().rstrip("/")
    if not is_local_ollama_url(base_url):
        raise IndexTransferError(
            "local_ollama_required",
            "Le téléchargement automatique exige un Ollama installé sur ce PC.",
        )
    try:
        _open_url_json(
            f"{base_url}/api/pull",
            method="POST",
            json_payload={"model": model_name, "stream": False},
            headers=_ollama_headers(include_json_content_type=True),
            timeout=3600,
            ssl_verify=_ollama_httpx_verify(),
        )
    except Exception as exc:
        _logger.warning("Ollama pull failed for %s", model_name, exc_info=True)
        raise IndexTransferError(
            "model_download_failed",
            f"Téléchargement de {model_name} impossible ({exc}). "
            f"Essayez dans un terminal : ollama pull {model_name}",
        ) from exc


# ------ Export --------------------------------------------------


def _read_manifest(index_dir: Path) -> dict[str, Any]:
    try:
        payload = json.loads((index_dir / state._MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise IndexTransferError(
            "manifest_unreadable", "Le manifeste de l'index est illisible."
        ) from exc
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, dict):
        raise IndexTransferError("manifest_unreadable", "Le manifeste de l'index est illisible.")
    return payload


def _sqlite_snapshot(source: Path, destination: Path) -> None:
    """Consistent copy of a live database, WAL content included."""
    source_connection = sqlite3.connect(str(source), timeout=30)
    try:
        target_connection = sqlite3.connect(str(destination))
        try:
            source_connection.backup(target_connection)
        finally:
            target_connection.close()
    finally:
        source_connection.close()


def _index_files(index_dir: Path) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for path in sorted(index_dir.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith(_SKIPPED_SUFFIXES) or path.name == state._INCOMPLETE_INDEX_FILE:
            continue
        files.append((path, path.relative_to(index_dir).as_posix()))
    return files


def default_export_name() -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _normalize_text(required_embedding_model_name())).strip("-")
    stamp = datetime.now().strftime("%Y-%m-%d")
    return f"Bibliobrain-index-{slug[:40] or 'embedding'}-{stamp}.zip"


def export_active_index(output_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Write the active, complete index profile to a zip archive."""
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() != ".zip":
        raise IndexTransferError("invalid_extension", "L'export doit être un fichier .zip.")

    status = get_vector_database_status()
    if not status.get("profile_ready"):
        raise IndexTransferError(
            "no_ready_index",
            "Aucun index complet pour le modèle d'embedding actuel. "
            "Préparez d'abord vos articles, puis exportez.",
        )
    index_dir = Path(str(status["current_profile_path"]))
    if _is_within(output, Path(status["path"])):
        raise IndexTransferError(
            "invalid_destination",
            "Choisissez un emplacement hors du dossier des bases vectorielles.",
        )

    manifest = _read_manifest(index_dir)
    entries: dict[str, dict[str, Any]] = manifest["entries"]
    if not entries:
        raise IndexTransferError("empty_index", "L'index actuel ne contient aucun document.")

    source, metadata_path, library = _metadata_library()
    account_id, account_kind = _zotero_account(source, metadata_path)
    model_name = required_embedding_model_name()
    chunk_size = int(manifest.get("chunk_size") or _current_chunk_size())
    header: dict[str, Any] = {
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "application_version": _application_version(),
        "index": {
            "profile_id": str(status.get("current_profile_id") or ""),
            "embedding_model": _current_embedding_profile(),
            "embedding_model_name": model_name,
            "embedding_provider": get_embedding_provider_mode(),
            "embedding_model_digest": _current_model_digest(model_name),
            "embedding_probe": _index_probe(index_dir),
            "embedding_prompts": str(manifest.get("embedding_prompts") or ""),
            "chunk_size": chunk_size,
            "schema_version": _INDEX_PROFILE_VERSION,
            "document_count": len(entries),
            "chunk_count": sum(_manifest_chunk_count(entry) for entry in entries.values()),
            "visual_signature": index_visual_signature(),
        },
        "library": {
            "metadata_source": source,
            "zotero_library_id": account_id,
            "zotero_account": account_kind,
            "zotero_storage_layout": is_zotero_storage_path(config.DATA_PATH, metadata_path),
            "item_count": len({item.item_key for item in library.values() if item.item_key}),
        },
        "documents": {rel_path: _document_identity(rel_path, library) for rel_path in entries},
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent, prefix=f".{output.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with (
            tempfile.TemporaryDirectory(prefix="bibliobrain-index-") as snapshot_dir,
            zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True
            ) as archive,
        ):
            archive.writestr(HEADER_NAME, json.dumps(header, ensure_ascii=False, indent=2))
            for path, relative in _index_files(index_dir):
                if path.name.lower().endswith(_SQLITE_SUFFIXES):
                    snapshot = Path(snapshot_dir) / f"{uuid.uuid4().hex}.sqlite3"
                    _sqlite_snapshot(path, snapshot)
                    archive.write(snapshot, _INDEX_PREFIX + relative)
                    snapshot.unlink()
                else:
                    archive.write(path, _INDEX_PREFIX + relative)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "archive": str(output),
        "bytes": output.stat().st_size,
        "document_count": header["index"]["document_count"],
        "chunk_count": header["index"]["chunk_count"],
        "embedding_model": model_name,
        "chunk_size": chunk_size,
    }


# ------ Archive reading -----------------------------------------


def _read_archive(archive_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(archive_path)
    if not path.is_file():
        raise IndexTransferError("archive_not_found", f"Fichier introuvable : {path}")
    if not zipfile.is_zipfile(path):
        raise IndexTransferError("not_an_index", "Ce fichier n'est pas une archive zip.")
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = {info.filename for info in members}
        for info in members:
            pure = PurePosixPath(info.filename)
            unsafe = pure.is_absolute() or ".." in pure.parts or "\\" in info.filename
            if unsafe or not (info.filename == HEADER_NAME or info.filename.startswith(_INDEX_PREFIX)):
                raise IndexTransferError(
                    "not_an_index", "Ce fichier n'est pas un index exporté par Bibliobrain."
                )
        if HEADER_NAME not in names:
            raise IndexTransferError(
                "not_an_index",
                "Ce fichier n'est pas un index exporté par Bibliobrain "
                "(une sauvegarde complète se restaure avec Restore-Bibliobrain).",
            )
        try:
            header = json.loads(archive.read(HEADER_NAME).decode("utf-8"))
            manifest = json.loads(archive.read(_INDEX_PREFIX + state._MANIFEST_FILE).decode("utf-8"))
        except (KeyError, UnicodeError, ValueError) as exc:
            raise IndexTransferError("corrupted_archive", "L'archive est incomplète.") from exc
        if _INDEX_PREFIX + "chroma.sqlite3" not in names:
            raise IndexTransferError("corrupted_archive", "L'archive ne contient pas de base Chroma.")
        uncompressed = sum(info.file_size for info in members)

    if not isinstance(header, dict) or header.get("format") != ARCHIVE_FORMAT:
        raise IndexTransferError("not_an_index", "Ce fichier n'est pas un index Bibliobrain.")
    if int(header.get("format_version") or 0) > ARCHIVE_FORMAT_VERSION:
        raise IndexTransferError(
            "newer_format",
            "Cet index vient d'une version plus récente de Bibliobrain. "
            "Mettez Bibliobrain à jour sur ce PC avant de l'importer.",
        )
    index_info = header.get("index")
    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(index_info, dict) or not isinstance(entries, dict):
        raise IndexTransferError("corrupted_archive", "L'en-tête de l'archive est invalide.")
    try:
        chunk_size = int(index_info.get("chunk_size") or 0)
    except (TypeError, ValueError):
        chunk_size = 0
    if chunk_size <= 0 or not str(index_info.get("embedding_model") or "").strip():
        raise IndexTransferError("corrupted_archive", "L'en-tête de l'archive est invalide.")
    documents = header.get("documents")
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "uncompressed_bytes": uncompressed,
        "header": header,
        "index": {**index_info, "chunk_size": chunk_size},
        "library": header.get("library") if isinstance(header.get("library"), dict) else {},
        "documents": documents if isinstance(documents, dict) else {},
        "entries": entries,
    }


def read_index_info(archive_path: str | os.PathLike[str]) -> dict[str, Any]:
    """The archive's index description, embedding probe included."""
    return _read_archive(archive_path)["index"]


def _free_bytes() -> int:
    candidate = Path(_index_profiles_root())
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        return -1


# ------ Pre-import check ----------------------------------------


def _locate_document(rel_path: str, data_path: str) -> str:
    if not data_path:
        return ""
    for candidate in candidate_rel_paths(rel_path):
        full_path = os.path.join(data_path, *candidate.split("/"))
        if os.path.isfile(full_path):
            return full_path
    return ""


def _content_matches(full_path: str, file_sig: str) -> bool:
    """Mirror the reload's comparison so a match here means no re-embedding."""
    parts = str(file_sig or "").split(":")
    try:
        size = os.path.getsize(full_path)
    except OSError:
        return False
    if not parts or not parts[0].isdigit() or int(parts[0]) != size:
        return False
    if len(parts) == 2:
        # Pre-hash manifests stored size:mtime; the reload accepts a size match.
        return True
    return len(parts) == 3 and parts[1] == "sha256" and _file_signature(full_path) == file_sig


def _document_row(rel_path: str, identity: dict[str, Any]) -> dict[str, str]:
    title = str(identity.get("title") or "").strip() or Path(rel_path).stem
    return {"rel_path": rel_path, "title": title}


def analyze_documents(
    entries: dict[str, dict[str, Any]],
    identities: dict[str, Any],
    library: dict[str, ZoteroItem],
) -> dict[str, Any]:
    data_path = str(config.DATA_PATH or "")
    data_path_exists = bool(data_path) and os.path.isdir(data_path)
    visual_signature = index_visual_signature()
    missing: list[dict[str, str]] = []
    content_changed: list[dict[str, str]] = []
    metadata_changed: list[dict[str, str]] = []
    not_in_metadata = 0
    visual_changed = 0
    reindexed: set[str] = set()
    found = 0

    for rel_path, entry in entries.items():
        raw_identity = identities.get(rel_path)
        identity: dict[str, Any] = raw_identity if isinstance(raw_identity, dict) else {}
        row = _document_row(rel_path, identity)
        full_path = _locate_document(rel_path, data_path) if data_path_exists else ""
        if not full_path:
            missing.append(row)
            continue
        found += 1
        if not _content_matches(full_path, str(entry.get("file_sig") or "")):
            content_changed.append(row)
            reindexed.add(rel_path)
        exported_sig = str(identity.get("sig") or "")
        item = lookup_zotero_item(rel_path, library)
        local_sig = _zotero_signature(item) if item else ""
        if exported_sig and item is None:
            not_in_metadata += 1
        if exported_sig != local_sig:
            metadata_changed.append(row)
            reindexed.add(rel_path)
        variant = document_index_variant(full_path, pdf_variant=visual_signature)
        if str(entry.get("visual_sig", "off") or "off") != (variant or "off"):
            visual_changed += 1
            reindexed.add(rel_path)

    indexed = {
        candidate.lower() for rel_path in entries for candidate in candidate_rel_paths(rel_path)
    }
    new_local = 0
    if data_path_exists:
        # Same candidate list as the reload: trashed Zotero attachments are skipped.
        local_documents = filter_active_zotero_pdf_entries(
            state.find_documents(),
            library,
            data_path=data_path,
            zotero_db_path=config.ZOTERO_DB_PATH,
        )
        for rel_path, _full_path in local_documents:
            if not {candidate.lower() for candidate in candidate_rel_paths(rel_path)} & indexed:
                new_local += 1

    return {
        "data_path": data_path,
        "data_path_exists": data_path_exists,
        "total": len(entries),
        "found": found,
        "missing_count": len(missing),
        "missing": missing[:_LISTED_DOCUMENTS],
        "content_changed_count": len(content_changed),
        "content_changed": content_changed[:_LISTED_DOCUMENTS],
        "metadata_changed_count": len(metadata_changed),
        "metadata_changed": metadata_changed[:_LISTED_DOCUMENTS],
        "not_in_metadata_count": not_in_metadata,
        "visual_changed_count": visual_changed,
        "reindex_count": len(reindexed),
        "unchanged_count": found - len(reindexed),
        "new_local_count": new_local,
    }


def compare_library(exported: dict[str, Any]) -> tuple[dict[str, Any], dict[str, ZoteroItem]]:
    source, path, library = _metadata_library()
    account_id, account_kind = _zotero_account(source, path)
    exported_id = str(exported.get("zotero_library_id") or "")
    same_account = None
    if exported_id and account_id:
        same_account = exported_id == account_id
    return (
        {
            "exported_source": str(exported.get("metadata_source") or "none"),
            "current_source": source,
            "current_path_found": bool(path) and os.path.exists(path),
            "exported_account": str(exported.get("zotero_account") or ""),
            "current_account": account_kind,
            "same_account": same_account,
            "exported_item_count": int(exported.get("item_count") or 0),
            "current_item_count": len({item.item_key for item in library.values() if item.item_key}),
            "exported_storage_layout": bool(exported.get("zotero_storage_layout")),
            "current_storage_layout": is_zotero_storage_path(config.DATA_PATH, path),
        },
        library,
    )


def _existing_profile(index_info: dict[str, Any], label: str) -> dict[str, Any]:
    profile = build_vector_index_profile(
        chunk_size=int(index_info["chunk_size"]), embedding_model=label
    )
    target = Path(str(profile["path"]))
    count = 0
    if (target / state._MANIFEST_FILE).is_file():
        try:
            count = len(_read_manifest(target)["entries"])
        except IndexTransferError:
            count = 0
    return {"exists": target.is_dir() and any(target.iterdir()), "document_count": count}


def _issue(level: str, code: str, message: str) -> dict[str, str]:
    return {"level": level, "code": code, "message": message}


def _plural(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def finalize_report(report: dict[str, Any]) -> dict[str, Any]:
    """(Re)build the issue list and verdict from the report's sections."""
    issues: list[dict[str, str]] = []
    embedding = report["embedding"]
    model = embedding["model_name"]
    action = embedding["action"]
    equivalence = embedding.get("equivalence") or {}
    if action == "equivalent":
        issues.append(_issue(
            "info",
            "model_equivalent",
            f"Le modèle configuré sur ce PC ({embedding['current_model_name']} via "
            f"{embedding['current_provider_label']}) produit les mêmes vecteurs que {model} "
            f"(similarité {equivalence.get('similarity')}). L'index sera utilisé avec ce modèle, "
            "sans rien télécharger.",
        ))
    elif action == "download":
        issues.append(_issue(
            "warning",
            "model_missing",
            f"Le modèle d'embedding {model} n'est pas installé sur ce PC. "
            "Vous pouvez le télécharger avec le bouton proposé (rien ne part sans votre "
            "accord), ou choisir un modèle équivalent déjà installé.",
        ))
    elif action == "install_manually":
        where = "LM Studio" if embedding["provider"] == "lmstudio" else "l'Ollama configuré"
        issues.append(_issue(
            "warning",
            "model_missing",
            f"Le modèle d'embedding {model} est introuvable dans {where}. Téléchargez-le "
            f"dans {where} (recherchez « {model} »), puis relancez la vérification.",
        ))
    elif action == "switch":
        issues.append(_issue(
            "info",
            "model_switch",
            f"Ce PC utilise actuellement {embedding['current_model_name'] or 'un autre modèle'}. "
            f"Après l'import, Bibliobrain passera à {model} pour utiliser cet index.",
        ))
    elif action == "provider_mismatch":
        if equivalence.get("status") == "unavailable":
            reason = (
                f"le modèle configuré ici ({embedding['current_model_name']} via "
                f"{embedding['current_provider_label']}) n'a pas pu être interrogé pour comparer"
            )
        else:
            reason = (
                f"le modèle configuré ici ({embedding['current_model_name']} via "
                f"{embedding['current_provider_label']}) ne produit pas les mêmes vecteurs"
            )
        issues.append(_issue(
            "warning",
            "provider_mismatch",
            f"Cet index a été construit avec {model} ({embedding['provider_label']}) et {reason}. "
            "Sélectionnez ce même modèle, quel que soit le service (Ollama, LM Studio ou API), "
            "puis relancez la vérification. Sinon l'index sera importé mais restera inactif.",
        ))
    if not embedding["provider_reachable"]:
        issues.append(_issue(
            "warning",
            "provider_unreachable",
            "Ollama ne répond pas : impossible de vérifier que le modèle est installé.",
        ))
    if embedding["digest_matches"] is False:
        issues.append(_issue(
            "warning",
            "model_version_differs",
            f"{model} est installé mais dans une autre version que sur le PC d'origine. "
            "La recherche risque d'être moins précise : mettez le modèle à jour "
            "sur les deux PC (ollama pull) ou reconstruisez l'index.",
        ))

    chunk = report["chunk_size"]
    if not chunk["same"]:
        issues.append(_issue(
            "info",
            "chunk_size",
            f"L'index découpe les textes en blocs de {chunk['index']} caractères "
            f"(réglage actuel : {chunk['current']}). Le réglage sera aligné sur l'index.",
        ))

    library = report["library"]
    if library["exported_source"] != library["current_source"]:
        issues.append(_issue(
            "warning",
            "metadata_source",
            "L'index a été construit avec les métadonnées "
            f"« {library['exported_source']} » ; ce PC utilise « {library['current_source']} ». "
            "Configurez la même source avant de recharger, sinon les documents seront "
            "réindexés avec d'autres métadonnées.",
        ))
    elif library["same_account"] is False:
        detail = (
            " (bibliothèque Zotero locale, non synchronisée)"
            if "local" in {library["exported_account"], library["current_account"]}
            else ""
        )
        issues.append(_issue(
            "warning",
            "zotero_library_differs",
            f"La bibliothèque Zotero de ce PC n'est pas celle de l'export{detail}. "
            "Les notices qui diffèrent entraîneront une réindexation des documents concernés.",
        ))
    if library["exported_storage_layout"] and not library["current_storage_layout"]:
        issues.append(_issue(
            "warning",
            "storage_layout",
            "Sur le PC d'origine, les documents étaient lus dans le dossier storage de Zotero. "
            "Choisissez aussi ce dossier sur ce PC, sinon les chemins ne correspondront pas.",
        ))

    documents = report["documents"]
    if not documents["data_path_exists"]:
        issues.append(_issue(
            "warning",
            "data_path_missing",
            "Le dossier des documents n'est pas configuré ou introuvable sur ce PC. "
            "Configurez-le avant de recharger la bibliothèque.",
        ))
    elif documents["missing_count"]:
        issues.append(_issue(
            "warning",
            "documents_missing",
            f"{_plural(documents['missing_count'], 'document de l’index est introuvable', 'documents de l’index sont introuvables')} "
            "sur ce PC. Copiez-les avant de recharger la bibliothèque : sinon ils seront "
            "retirés de l'index.",
        ))
    if documents["content_changed_count"]:
        issues.append(_issue(
            "info",
            "documents_changed",
            f"{_plural(documents['content_changed_count'], 'fichier diffère', 'fichiers diffèrent')} "
            "de la version indexée : ils seront réindexés.",
        ))
    if documents["metadata_changed_count"]:
        issues.append(_issue(
            "info",
            "metadata_changed",
            f"{_plural(documents['metadata_changed_count'], 'notice diffère', 'notices diffèrent')} "
            "entre les deux bibliothèques : les documents concernés seront réindexés.",
        ))
    if documents["visual_changed_count"]:
        issues.append(_issue(
            "info",
            "visual_settings",
            "Le réglage « Comprendre les figures » diffère du PC d'origine : "
            f"{_plural(documents['visual_changed_count'], 'document sera réindexé', 'documents seront réindexés')}.",
        ))

    existing = report["existing_index"]
    if existing["exists"]:
        issues.append(_issue(
            "info",
            "replaces_existing",
            "Un index existe déjà pour ce modèle sur ce PC"
            + (f" ({_plural(existing['document_count'], 'document', 'documents')})" if existing["document_count"] else "")
            + ". Il sera remplacé ; une copie de sécurité est conservée.",
        ))

    disk = report["disk"]
    if not disk["ok"]:
        issues.append(_issue(
            "error",
            "disk_space",
            "Espace disque insuffisant pour importer cet index "
            f"({disk['required_bytes'] // (1024 * 1024)} Mo nécessaires).",
        ))

    levels = {issue["level"] for issue in issues}
    report["issues"] = issues
    report["verdict"] = "blocked" if "error" in levels else ("attention" if "warning" in levels else "ready")
    report["can_import"] = "error" not in levels
    return report


def inspect_archive(archive_path: str | os.PathLike[str]) -> dict[str, Any]:
    """Everything the user should know before importing ``archive_path``."""
    archive = _read_archive(archive_path)
    index_info = archive["index"]
    embedding = embedding_compatibility(index_info)
    library_report, library = compare_library(archive["library"])
    required = int(archive["uncompressed_bytes"] * 1.1)
    free = _free_bytes()
    report = {
        "archive": {
            "path": archive["path"],
            "name": Path(archive["path"]).name,
            "bytes": archive["bytes"],
            "created_at": str(archive["header"].get("created_at") or ""),
            "application_version": str(archive["header"].get("application_version") or ""),
        },
        "index": {
            "embedding_model": str(index_info.get("embedding_model") or ""),
            "embedding_model_name": str(index_info.get("embedding_model_name") or ""),
            "embedding_provider": str(index_info.get("embedding_provider") or ""),
            "embedding_model_digest": str(index_info.get("embedding_model_digest") or ""),
            "chunk_size": index_info["chunk_size"],
            "document_count": len(archive["entries"]),
            "chunk_count": int(index_info.get("chunk_count") or 0),
        },
        "embedding": embedding,
        "chunk_size": {
            "index": index_info["chunk_size"],
            "current": get_persisted_chunk_size(),
            "same": index_info["chunk_size"] == get_persisted_chunk_size(),
        },
        "library": library_report,
        "documents": analyze_documents(archive["entries"], archive["documents"], library),
        "existing_index": _existing_profile(
            index_info,
            embedding["current_model"]
            if embedding["action"] == "equivalent"
            else str(index_info["embedding_model"]),
        ),
        "disk": {"required_bytes": required, "free_bytes": free, "ok": free < 0 or free >= required},
    }
    return finalize_report(report)


# ------ Import --------------------------------------------------


def _release_runtime_index() -> None:
    with state._state_lock:
        current_store = state.vectorstore
    state.clear_runtime_state()
    _close_chroma_store(current_store)
    gc.collect()


def _replace_with_retries(source: Path, destination: Path) -> None:
    """Rename a directory; Windows refuses while a just-closed handle lingers."""
    for attempt in range(1, 7):
        try:
            os.replace(source, destination)
            return
        except OSError:
            if attempt == 6:
                raise
            gc.collect()
            time.sleep(0.3 * attempt)


def _extract_index(archive_path: str, staging: Path) -> None:
    """Unpack the index; zip CRCs catch a file damaged in transit.

    No ``PRAGMA quick_check``: Python's SQLite reports Chroma's FTS5 table as
    malformed even on the live, healthy database.
    """
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                if info.is_dir() or not info.filename.startswith(_INDEX_PREFIX):
                    continue
                relative = info.filename[len(_INDEX_PREFIX):]
                destination = staging.joinpath(*PurePosixPath(relative).parts)
                if not _is_within(destination, staging):
                    raise IndexTransferError("corrupted_archive", "Chemin refusé dans l'archive.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
    except (zipfile.BadZipFile, EOFError) as exc:
        raise IndexTransferError(
            "corrupted_archive", "L'archive est endommagée (copie incomplète ?)."
        ) from exc
    try:
        with contextlib.closing(sqlite3.connect(str(staging / "chroma.sqlite3"))) as connection:
            tables = {
                row[0]
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
    except sqlite3.DatabaseError as exc:
        raise IndexTransferError(
            "corrupted_archive", "La base Chroma de l'archive est illisible."
        ) from exc
    if "embeddings" not in tables:
        raise IndexTransferError("corrupted_archive", "La base Chroma de l'archive est illisible.")


def _prune_backups(profiles_root: Path, profile_id: str, keep: Path | None) -> None:
    prefix = f".backup-{profile_id}-"
    for entry in profiles_root.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix) and entry != keep:
            shutil.rmtree(entry, ignore_errors=True)


def import_archive(
    archive_path: str | os.PathLike[str],
    *,
    adopt_settings: bool = True,
) -> dict[str, Any]:
    """Install the archive's index; the previous one with the same profile is kept aside.

    With ``adopt_settings`` the chunk size and, when the model is installed,
    the embedding model are aligned on the index so the next load uses it.
    """
    archive = _read_archive(archive_path)
    free = _free_bytes()
    if 0 <= free < archive["uncompressed_bytes"]:
        raise IndexTransferError("disk_space", "Espace disque insuffisant pour importer cet index.")
    index_info = archive["index"]
    compatibility = embedding_compatibility(index_info)
    label = str(index_info["embedding_model"])
    if compatibility["action"] == "equivalent":
        # Same vectors under another name or service: file it under this computer's model.
        label = _current_embedding_profile()
    chunk_size = int(index_info["chunk_size"])
    profile = build_vector_index_profile(chunk_size=chunk_size, embedding_model=label)
    target = Path(str(profile["path"]))
    profiles_root = target.parent
    profiles_root.mkdir(parents=True, exist_ok=True)
    staging = profiles_root / f".import-{uuid.uuid4().hex[:12]}"
    backup: Path | None = None

    try:
        _extract_index(archive["path"], staging)
        if label != str(index_info["embedding_model"]):
            manifest_path = staging / state._MANIFEST_FILE
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["embedding_model"] = label
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        _release_runtime_index()
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = profiles_root / f".backup-{target.name}-{stamp}"
            try:
                _replace_with_retries(target, backup)
            except OSError as exc:
                raise IndexTransferError(
                    "index_locked",
                    "L'index actuel est encore ouvert. Redémarrez Bibliobrain puis "
                    "relancez l'import.",
                ) from exc
        try:
            _replace_with_retries(staging, target)
        except OSError as exc:
            if backup is not None:
                os.replace(backup, target)
            raise IndexTransferError(
                "import_failed", "Impossible d'installer l'index importé."
            ) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    _prune_backups(profiles_root, target.name, backup)

    adopted = {"chunk_size": False, "embedding_model": False}
    if adopt_settings:
        if get_persisted_chunk_size() != chunk_size:
            set_persisted_chunk_size(chunk_size)
            adopted["chunk_size"] = True
        if compatibility["action"] == "switch":
            set_runtime_embedding_model(
                compatibility["local_model_name"] or compatibility["model_name"],
                provider_mode=compatibility["provider"],
            )
            adopted["embedding_model"] = True
    active = _same_model(_current_embedding_profile(), label) and (
        get_persisted_chunk_size() == chunk_size
    )
    if active:
        set_active_vector_index_profile(chunk_size=chunk_size, embedding_model=label)
    state.clear_document_scan_cache()

    return {
        "profile_id": target.name,
        "active": active,
        "adopted": adopted,
        "backup": str(backup) if backup else "",
        "document_count": len(archive["entries"]),
        "embedding": compatibility,
    }
