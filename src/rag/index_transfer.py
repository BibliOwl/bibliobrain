"""Move the active vector index from one computer to another.

An archive holds one index profile (Chroma, lexical sidecar, manifest) and a
header describing how it was built: embedding model, chunk size, the Zotero
library it was matched against, and the Zotero identity of every indexed
document. Nothing machine-specific travels: no configuration.json, no secret,
no absolute path. Before anything is replaced, ``inspect_archive`` compares
that header with this computer, so the user learns which documents are
missing, which ones the next reload will re-embed, and which embedding model
must be installed.

Format 2 may also carry the indexed documents themselves under
``documents/<rel_path>``, with their size and SHA-256 in the header. An
archive without documents is still written as format 1, so older versions
of Bibliobrain keep reading it. On import, a document is only ever copied
where none exists yet: nothing on this computer is overwritten.
"""

from __future__ import annotations

import contextlib
import errno
import gc
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from src import config, zotero_drops
from src.app_settings import get_app_settings, index_standalone_pdfs_enabled
from src.document_formats import document_index_variant
from src.llm_provider import (
    current_embedding_profile_label,
    get_embedding_provider_mode,
    required_embedding_model_name,
    set_runtime_embedding_model,
)
from src.rag_settings import get_persisted_chunk_size, set_persisted_chunk_size
from src.rel_paths import candidate_rel_paths, normalize_rel_path
from src.visual_analysis import index_visual_signature
from src.zotero_db import (
    METADATA_SNAPSHOT_FORMAT,
    ZoteroItem,
    filter_active_zotero_pdf_entries,
    find_zotero_db,
    is_zotero_storage_path,
    item_to_snapshot,
    load_zotero_library,
    load_zotero_user_library_uri,
    lookup_zotero_item,
    snapshot_items,
    write_metadata_snapshot,
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
ARCHIVE_FORMAT_VERSION = 2
_INDEX_ONLY_FORMAT_VERSION = 1
HEADER_NAME = "bibliobrain-index.json"
_INDEX_PREFIX = "index/"
_DOCUMENTS_PREFIX = "documents/"
# Created in the user's Documents folder when this computer has no PDF folder yet.
DEFAULT_IMPORT_FOLDER_NAME = "Bibliobrain PDF"
_PART_SUFFIX = ".bibliobrain-part"
# zipfile needs to know in advance that a streamed member will pass 2 GiB.
_ZIP64_MEMBER_BYTES = 2**31 - 2**26
_COPY_MODES = {"new_folder", "data_path", "zotero_storage"}
# Records of the indexed documents, used where this computer has no metadata source.
_METADATA_NAME = "metadata.json"
_SNAPSHOT_DIR_NAME = "imported_metadata"


# ------ Progress ------------------------------------------------
# One transfer runs at a time (they hold the RAG lock); the UI polls this
# state while its request is in flight.

_progress_lock = threading.Lock()
_progress: dict[str, Any] = {"active": False}


def transfer_progress() -> dict[str, Any]:
    with _progress_lock:
        return dict(_progress)


def _progress_begin(operation: str) -> None:
    with _progress_lock:
        _progress.clear()
        _progress.update(
            active=True, operation=operation, phase="", done=0, total=0, label="", started_at=time.time()
        )


def _progress_update(phase: str, done: int, total: int, label: str = "") -> None:
    with _progress_lock:
        if _progress.get("active"):
            _progress.update(phase=phase, done=int(done), total=int(total), label=label)


def _progress_end() -> None:
    with _progress_lock:
        _progress.clear()
        _progress["active"] = False


class _ByteCounter:
    """Bytes done within one phase, reported at most every 4 MB."""

    def __init__(self, phase: str, total: int) -> None:
        self.phase = phase
        self.total = total
        self.done = 0
        self._reported = 0
        _progress_update(phase, 0, total)

    def add(self, count: int, label: str = "") -> None:
        self.done += count
        if self.done - self._reported >= 4 * 1024 * 1024 or self.done >= self.total:
            self._reported = self.done
            _progress_update(self.phase, self.done, self.total, label)
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
        return source, path, load_zotero_library(path, zotero_drops.fingerprints(), index_standalone_pdfs_enabled())
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


def _document_identity(
    rel_path: str, library: dict[str, ZoteroItem], full_path: str = ""
) -> dict[str, str]:
    item = lookup_zotero_item(rel_path, library, full_path or None)
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


def _pull_events(base_url: str, model_name: str) -> Any:
    """Ollama's streamed pull: one JSON object per line."""
    import ssl
    import urllib.request

    from src.llm_provider import (
        _ollama_headers,
        _ollama_httpx_verify,
        _open_request_with_local_proxy_bypass,
    )

    request = urllib.request.Request(
        f"{base_url}/api/pull",
        data=json.dumps({"model": model_name, "stream": True}).encode("utf-8"),
        headers=_ollama_headers(include_json_content_type=True),
        method="POST",
    )
    verify = _ollama_httpx_verify()
    context = None
    if isinstance(verify, str):
        context = ssl.create_default_context(cafile=verify)
    elif not verify:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    # The timeout applies to each read: Ollama sends a line at least every few seconds.
    with _open_request_with_local_proxy_bypass(request, timeout=300, context=context) as response:
        for raw in response:
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                yield event


def pull_embedding_model(model_name: str) -> None:
    """Download an embedding model into the local Ollama (can take minutes).

    The download's progress (bytes of all layers) is published for the UI.
    """
    from src.llm_provider import _embedding_ollama_base_url
    from src.ollama_utils import is_local_ollama_url

    base_url = _embedding_ollama_base_url().rstrip("/")
    if not is_local_ollama_url(base_url):
        raise IndexTransferError(
            "local_ollama_required",
            "Le téléchargement automatique exige un Ollama installé sur ce PC.",
        )
    layers: dict[str, tuple[int, int]] = {}
    succeeded = False
    _progress_begin("pull")
    try:
        _progress_update("download", 0, 0, model_name)
        for event in _pull_events(base_url, model_name):
            if event.get("error"):
                raise RuntimeError(str(event["error"]))
            digest = str(event.get("digest") or "")
            if digest and event.get("total"):
                layers[digest] = (int(event.get("completed") or 0), int(event["total"]))
            done = sum(completed for completed, _total in layers.values())
            total = sum(size for _completed, size in layers.values())
            _progress_update("download", done, total, model_name)
            succeeded = succeeded or event.get("status") == "success"
        if not succeeded:
            raise RuntimeError("téléchargement interrompu")
    except Exception as exc:
        _logger.warning("Ollama pull failed for %s", model_name, exc_info=True)
        raise IndexTransferError(
            "model_download_failed",
            f"Téléchargement de {model_name} impossible ({exc}). "
            f"Essayez dans un terminal : ollama pull {model_name}",
        ) from exc
    finally:
        _progress_end()


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


def _safe_rel_path(rel_path: str) -> str:
    """``rel_path`` as a safe relative POSIX path inside an archive, or ""."""
    normalized = normalize_rel_path(rel_path)
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or ".." in pure.parts
        or ":" in normalized
        or not pure.parts
    ):
        return ""
    return pure.as_posix()


def _exportable_documents(entries: dict[str, Any]) -> list[tuple[str, str, Path, int]]:
    """(manifest key, path in the archive, local file, size) of the indexed files found here."""
    data_path = str(config.DATA_PATH or "")
    if not data_path or not os.path.isdir(data_path):
        return []
    rows: list[tuple[str, str, Path, int]] = []
    for rel_path in sorted(entries):
        safe = _safe_rel_path(rel_path)
        full_path = _locate_document(rel_path, data_path) if safe else ""
        if not full_path:
            continue
        try:
            size = os.path.getsize(full_path)
        except OSError:
            continue
        rows.append((rel_path, safe, Path(full_path), size))
    return rows


def _ready_index_dir() -> tuple[dict[str, Any], Path]:
    status = get_vector_database_status()
    if not status.get("profile_ready"):
        raise IndexTransferError(
            "no_ready_index",
            "Aucun index complet pour le modèle d'embedding actuel. "
            "Préparez d'abord vos articles, puis exportez.",
        )
    return status, Path(str(status["current_profile_path"]))


def estimate_export() -> dict[str, Any]:
    """Sizes of an export with and without the documents, before choosing where to save."""
    _status, index_dir = _ready_index_dir()
    entries: dict[str, Any] = _read_manifest(index_dir)["entries"]
    documents = _exportable_documents(entries)
    index_bytes = 0
    for path, _relative in _index_files(index_dir):
        with contextlib.suppress(OSError):
            index_bytes += path.stat().st_size
    return {
        "document_count": len(entries),
        "documents_found": len(documents),
        "documents_bytes": sum(size for *_rest, size in documents),
        "index_bytes": index_bytes,
    }


def _zip_time(path: Path) -> tuple[int, int, int, int, int, int]:
    try:
        stamp = time.localtime(path.stat().st_mtime)
    except (OSError, OverflowError, ValueError):
        stamp = time.localtime()
    moment = (stamp.tm_year, stamp.tm_mon, stamp.tm_mday, stamp.tm_hour, stamp.tm_min, stamp.tm_sec)
    # Zip dates start in 1980.
    return max(moment, (1980, 1, 1, 0, 0, 0))


def _store_document(
    archive: zipfile.ZipFile,
    member: str,
    source: Any,
    path: Path,
    size: int,
    counter: _ByteCounter | None = None,
) -> str:
    """Copy an open file into the archive uncompressed (PDFs already are); returns its SHA-256."""
    info = zipfile.ZipInfo(member, date_time=_zip_time(path))
    info.compress_type = zipfile.ZIP_STORED
    digest = hashlib.sha256()
    with archive.open(info, "w", force_zip64=size >= _ZIP64_MEMBER_BYTES) as target:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            target.write(chunk)
            if counter is not None:
                counter.add(len(chunk), path.name)
    return digest.hexdigest()


def _metadata_records(
    entries: dict[str, Any], library: dict[str, ZoteroItem]
) -> dict[str, dict[str, Any]]:
    """The full record of each indexed document, for a computer without that library."""
    records: dict[str, dict[str, Any]] = {}
    data_path = str(config.DATA_PATH or "")
    for rel_path in entries:
        # The full path finds linked files (ZotMoov) whose name several records share.
        full_path = _locate_document(rel_path, data_path) or None
        item = lookup_zotero_item(rel_path, library, full_path)
        if item is not None:
            records[rel_path] = item_to_snapshot(item)
    return records


def default_export_name() -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", _normalize_text(required_embedding_model_name())).strip("-")
    stamp = datetime.now().strftime("%Y-%m-%d")
    return f"Bibliobrain-index-{slug[:40] or 'embedding'}-{stamp}.zip"


def export_active_index(
    output_path: str | os.PathLike[str], *, include_documents: bool = False
) -> dict[str, Any]:
    """Write the active, complete index profile to a zip archive.

    With ``include_documents`` the indexed files found on this computer travel
    too, so the other computer needs nothing else. The records of the indexed
    documents always do (``metadata.json``): a computer without the same
    library uses them instead of re-indexing everything.
    """
    _progress_begin("export")
    try:
        return _export_active_index(output_path, include_documents=include_documents)
    except OSError as exc:
        if not _is_disk_full(exc):
            raise
        raise IndexTransferError(
            "disk_space",
            "Le disque de destination est plein : rien n'a été enregistré. Choisissez un autre "
            "emplacement (clé USB, autre disque) ou libérez de la place.",
        ) from exc
    finally:
        _progress_end()


def _export_active_index(
    output_path: str | os.PathLike[str], *, include_documents: bool
) -> dict[str, Any]:
    output = Path(output_path).expanduser().resolve()
    if output.suffix.lower() != ".zip":
        raise IndexTransferError("invalid_extension", "L'export doit être un fichier .zip.")

    status, index_dir = _ready_index_dir()
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
    documents = _exportable_documents(entries) if include_documents else []
    if documents and _is_within(output, Path(str(config.DATA_PATH))):
        raise IndexTransferError(
            "invalid_destination",
            "Choisissez un emplacement hors du dossier de vos PDF : l'export les contient.",
        )
    header: dict[str, Any] = {
        "format": ARCHIVE_FORMAT,
        "format_version": ARCHIVE_FORMAT_VERSION if documents else _INDEX_ONLY_FORMAT_VERSION,
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
        "documents": {
            rel_path: _document_identity(
                rel_path, library, _locate_document(rel_path, str(config.DATA_PATH or ""))
            )
            for rel_path in entries
        },
    }
    records = _metadata_records(entries, library)
    if records:
        header["format_version"] = ARCHIVE_FORMAT_VERSION
        header["metadata"] = {"count": len(records)}
    files: dict[str, dict[str, Any]] = {}
    index_files = _index_files(index_dir)
    total_bytes = sum(size for *_rest, size in documents)
    for path, _relative in index_files:
        with contextlib.suppress(OSError):
            total_bytes += path.stat().st_size
    # The documents are stored as they are: their size is a floor for the file.
    floor = check_disk_space([(str(output.parent), sum(size for *_rest, size in documents))])
    if not floor["ok"]:
        raise IndexTransferError(
            "disk_space",
            f"Espace disque insuffisant à cet emplacement : les PDF seuls demandent "
            f"{_format_bytes(floor['required_bytes'])}, il reste {_format_bytes(floor['free_bytes'])}. "
            "Choisissez un autre emplacement ou exportez sans les PDF (Options).",
        )
    counter = _ByteCounter("export", total_bytes)

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
            for path, relative in index_files:
                if path.name.lower().endswith(_SQLITE_SUFFIXES):
                    snapshot = Path(snapshot_dir) / f"{uuid.uuid4().hex}.sqlite3"
                    _sqlite_snapshot(path, snapshot)
                    archive.write(snapshot, _INDEX_PREFIX + relative)
                    snapshot.unlink()
                else:
                    archive.write(path, _INDEX_PREFIX + relative)
                with contextlib.suppress(OSError):
                    counter.add(path.stat().st_size)
            for rel_path, safe_path, path, size in documents:
                try:
                    document = path.open("rb")
                except OSError:
                    _logger.warning("Document left out of the export: %s", path, exc_info=True)
                    continue
                with document:
                    digest = _store_document(
                        archive, _DOCUMENTS_PREFIX + safe_path, document, path, size, counter
                    )
                    written = document.tell()
                files[rel_path] = {"path": safe_path, "bytes": written, "sha256": digest}
            if files:
                header["files"] = files
            if records:
                archive.writestr(
                    _METADATA_NAME,
                    json.dumps({"format": METADATA_SNAPSHOT_FORMAT, "items": records}, ensure_ascii=False),
                )
            if not files and not records:
                header["format_version"] = _INDEX_ONLY_FORMAT_VERSION
            # Written last: it lists the documents that actually made it in.
            archive.writestr(HEADER_NAME, json.dumps(header, ensure_ascii=False, indent=2))
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    return {
        "archive": str(output),
        "bytes": output.stat().st_size,
        "document_count": header["index"]["document_count"],
        "chunk_count": header["index"]["chunk_count"],
        "documents_included": len(files),
        "documents_bytes": sum(int(item["bytes"]) for item in files.values()),
        "records_included": len(records),
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
        sizes = {info.filename: info.file_size for info in members}
        for info in members:
            pure = PurePosixPath(info.filename)
            unsafe = pure.is_absolute() or ".." in pure.parts or "\\" in info.filename
            known = (
                info.filename in {HEADER_NAME, _METADATA_NAME}
                or info.filename.startswith(_INDEX_PREFIX)
                or info.filename.startswith(_DOCUMENTS_PREFIX)
            )
            if unsafe or not known:
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
            records_payload = (
                json.loads(archive.read(_METADATA_NAME).decode("utf-8"))
                if _METADATA_NAME in names
                else None
            )
        except (KeyError, UnicodeError, ValueError) as exc:
            raise IndexTransferError("corrupted_archive", "L'archive est incomplète.") from exc
        if _INDEX_PREFIX + "chroma.sqlite3" not in names:
            raise IndexTransferError("corrupted_archive", "L'archive ne contient pas de base Chroma.")
        uncompressed = sum(
            info.file_size for info in members if not info.filename.startswith(_DOCUMENTS_PREFIX)
        )

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
        "files": _archive_files(header, names, sizes),
        "records": _archive_records(records_payload),
    }


def _archive_records(payload: Any) -> dict[str, dict[str, Any]]:
    """The archive's records, checked by rebuilding them as a library."""
    if payload is None:
        return {}
    try:
        snapshot_items(payload)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise IndexTransferError("corrupted_archive", "Les notices de l'archive sont illisibles.") from exc
    items = payload.get("items")
    return {str(key): value for key, value in items.items() if isinstance(value, dict)} if isinstance(items, dict) else {}


def _archive_files(
    header: dict[str, Any], names: set[str], sizes: dict[str, int]
) -> dict[str, dict[str, Any]]:
    """The documents carried by the archive, each matched to its zip member.

    Every ``documents/`` member must be listed in the header with a safe path,
    so nothing unannounced is ever written on this computer.
    """
    listed = header.get("files") or {}
    if not isinstance(listed, dict):
        raise IndexTransferError("corrupted_archive", "L'en-tête de l'archive est invalide.")
    files: dict[str, dict[str, Any]] = {}
    for rel_path, item in listed.items():
        if not isinstance(item, dict):
            raise IndexTransferError("corrupted_archive", "L'en-tête de l'archive est invalide.")
        safe = _safe_rel_path(str(item.get("path") or ""))
        member = _DOCUMENTS_PREFIX + safe
        digest = str(item.get("sha256") or "")
        if not safe or member not in names or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise IndexTransferError("corrupted_archive", "L'en-tête de l'archive est invalide.")
        files[str(rel_path)] = {"path": safe, "member": member, "bytes": sizes[member], "sha256": digest}
    announced = {item["member"] for item in files.values()}
    if any(name.startswith(_DOCUMENTS_PREFIX) and name not in announced for name in names):
        raise IndexTransferError("corrupted_archive", "L'archive contient des fichiers non annoncés.")
    return files


def read_index_info(archive_path: str | os.PathLike[str]) -> dict[str, Any]:
    """The archive's index description, embedding probe included."""
    return _read_archive(archive_path)["index"]


def _free_bytes(location: str | os.PathLike[str] | None = None) -> int:
    candidate = Path(location if location is not None else _index_profiles_root())
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        return -1


def _disk_key(location: str | os.PathLike[str]) -> Any:
    """The device holding ``location`` (or its nearest existing parent)."""
    candidate = Path(location)
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    try:
        return os.stat(candidate).st_dev
    except OSError:
        return str(candidate)


def check_disk_space(needs: list[tuple[str, int]]) -> dict[str, Any]:
    """Free space for every (location, bytes) need; needs on one disk add up.

    The PDF copy and the index usually land on the same disk: checking them
    one by one let an import fill the disk halfway through.
    """
    disks: dict[Any, dict[str, Any]] = {}
    for location, required in needs:
        if required <= 0:
            continue
        disk = disks.setdefault(
            _disk_key(location),
            {"location": str(location), "required_bytes": 0, "free_bytes": _free_bytes(location)},
        )
        disk["required_bytes"] += int(required)
    listed = list(disks.values())
    for disk in listed:
        disk["ok"] = disk["free_bytes"] < 0 or disk["free_bytes"] >= disk["required_bytes"]
    short = [disk for disk in listed if not disk["ok"]]
    worst = short[0] if short else (listed[0] if listed else {"required_bytes": 0, "free_bytes": -1})
    return {
        "ok": not short,
        "disks": listed,
        # Kept for reports saved by earlier versions and their readers.
        "required_bytes": worst["required_bytes"],
        "free_bytes": worst["free_bytes"],
    }


def _import_needs(archive: dict[str, Any], copy: dict[str, Any]) -> list[tuple[str, int]]:
    needs = [(str(_index_profiles_root()), int(archive["uncompressed_bytes"] * 1.1))]
    if copy.get("mode") in _COPY_MODES and copy.get("copy_bytes"):
        # A small margin for the file system's own bookkeeping.
        needs.append((str(copy["target"]), int(copy["copy_bytes"] * 1.02)))
    return needs


def _is_disk_full(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and (
        exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in {39, 112}
    )


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


def default_import_folder() -> str:
    documents = Path.home() / "Documents"
    return str((documents if documents.is_dir() else Path.home()) / DEFAULT_IMPORT_FOLDER_NAME)


def _data_path_configured() -> bool:
    data_path = str(config.DATA_PATH or "")
    if not data_path or not os.path.isdir(data_path):
        return False
    try:
        return bool(get_app_settings().get("data_path_configured", True))
    except (OSError, TypeError, ValueError):
        return True


def plan_document_copy(
    files: dict[str, dict[str, Any]], library_report: dict[str, Any]
) -> dict[str, Any]:
    """Where the archive's documents would go on this computer, and which ones.

    ``mode``: ``none`` (no documents in the archive), ``new_folder`` (no PDF
    folder here yet: one is created in Documents), ``data_path`` (the PDF
    folder), ``zotero_storage`` (Zotero's storage folder of the same synced
    library) or ``zotero_other_library`` (Zotero's storage of another library:
    nothing is copied there, it would leave files Zotero does not know).
    """
    plan: dict[str, Any] = {
        "included_count": len(files),
        "included_bytes": sum(int(item["bytes"]) for item in files.values()),
        "mode": "none",
        "target": "",
        "set_data_path": False,
        "copy_count": 0,
        "copy_bytes": 0,
        "rel_paths": [],
    }
    if not files:
        return plan
    data_path = str(config.DATA_PATH or "")
    if not _data_path_configured():
        plan.update(mode="new_folder", target=default_import_folder(), set_data_path=True)
    elif library_report.get("current_storage_layout"):
        same = library_report.get("same_account") is True
        plan.update(mode="zotero_storage" if same else "zotero_other_library", target=data_path)
    else:
        plan.update(mode="data_path", target=data_path)
    if plan["mode"] not in _COPY_MODES:
        return plan
    target_exists = os.path.isdir(plan["target"])
    for rel_path, item in sorted(files.items()):
        if target_exists and _locate_document(rel_path, plan["target"]):
            continue
        plan["rel_paths"].append(rel_path)
        plan["copy_bytes"] += int(item["bytes"])
    plan["copy_count"] = len(plan["rel_paths"])
    # Free space is checked with the index's needs: see check_disk_space.
    return plan


def plan_records(records: dict[str, Any], library_report: dict[str, Any]) -> dict[str, Any]:
    """Whether the archive's records will serve as this computer's metadata source.

    Only when no source is configured here: a Zotero library or a BibTeX file
    of this computer always wins.
    """
    use = bool(records) and library_report.get("current_source") not in {"zotero", "bibtex"}
    return {"count": len(records), "use": use}


def _records_library(records: dict[str, Any]) -> dict[str, ZoteroItem]:
    return snapshot_items({"format": METADATA_SNAPSHOT_FORMAT, "items": records})


def _adopt_records(records: dict[str, Any], archive_name: str) -> str:
    """Save the records as a file and make it the metadata source, like a BibTeX file."""
    from src.app_settings import set_app_settings

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = str(Path(config.STORAGE_DIR) / _SNAPSHOT_DIR_NAME / f"notices-{stamp}.json")
    write_metadata_snapshot(path, records, origin=archive_name)
    config.set_zotero_path(path)
    try:
        set_app_settings(metadata_source="bibtex", zotero_path=path)
    except Exception:
        _logger.warning("Unable to save the imported records as metadata source", exc_info=True)
    return path


def _public_copy_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if key != "rel_paths"}


def analyze_documents(
    entries: dict[str, dict[str, Any]],
    identities: dict[str, Any],
    library: dict[str, ZoteroItem],
    *,
    data_path: str | None = None,
    incoming: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare the index with the documents of ``data_path`` (the PDF folder).

    ``incoming`` lists the archive's documents that the import will copy
    there: they count as present, identical to their archived version.
    """
    current_folder = str(config.DATA_PATH or "")
    data_path = current_folder if data_path is None else str(data_path or "")
    incoming = incoming or {}
    data_path_exists = bool(data_path) and os.path.isdir(data_path)
    visual_signature = index_visual_signature()
    missing: list[dict[str, str]] = []
    content_changed: list[dict[str, str]] = []
    metadata_changed: list[dict[str, str]] = []
    not_in_metadata = 0
    visual_changed = 0
    reindexed: set[str] = set()
    found = 0
    copied = 0

    for rel_path, entry in entries.items():
        raw_identity = identities.get(rel_path)
        identity: dict[str, Any] = raw_identity if isinstance(raw_identity, dict) else {}
        row = _document_row(rel_path, identity)
        full_path = _locate_document(rel_path, data_path) if data_path_exists else ""
        arriving = incoming.get(rel_path) if not full_path else None
        if not full_path and arriving is None:
            missing.append(row)
            continue
        if arriving is not None:
            copied += 1
            file_sig = str(entry.get("file_sig") or "")
            same_content = file_sig == f"{arriving['bytes']}:sha256:{arriving['sha256']}" or (
                # Pre-hash manifests stored size:mtime; the reload accepts a size match.
                len(file_sig.split(":")) == 2 and file_sig.split(":")[0] == str(arriving["bytes"])
            )
            full_path = rel_path
        else:
            found += 1
            same_content = _content_matches(full_path, str(entry.get("file_sig") or ""))
        if not same_content:
            content_changed.append(row)
            reindexed.add(rel_path)
        exported_sig = str(identity.get("sig") or "")
        item = lookup_zotero_item(rel_path, library, full_path)
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
    if data_path_exists and data_path == current_folder:
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
        "copied_count": copied,
        "missing_count": len(missing),
        "missing": missing[:_LISTED_DOCUMENTS],
        "content_changed_count": len(content_changed),
        "content_changed": content_changed[:_LISTED_DOCUMENTS],
        "metadata_changed_count": len(metadata_changed),
        "metadata_changed": metadata_changed[:_LISTED_DOCUMENTS],
        "not_in_metadata_count": not_in_metadata,
        "visual_changed_count": visual_changed,
        "reindex_count": len(reindexed),
        "unchanged_count": found + copied - len(reindexed),
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


def _disk_space_message(disk: dict[str, Any], *, copying: bool) -> str:
    short = [item for item in disk.get("disks") or [disk] if not item.get("ok", False)] or [disk]
    item = short[0]
    what = "l'index et les PDF de l'archive" if copying else "l'index"
    free = int(item.get("free_bytes", -1))
    where = f" sur le disque de {item['location']}" if item.get("location") else ""
    return (
        f"Espace disque insuffisant{where} : {what} demandent {_format_bytes(int(item['required_bytes']))}"
        + (f", il reste {_format_bytes(free)}." if free >= 0 else ".")
        + " Libérez de la place (le fichier importé peut aussi être placé sur un autre disque), "
        "puis relancez la vérification."
    )


def _format_bytes(value: int) -> str:
    if value >= 1024**3:
        return f"{value / 1024**3:.1f} Go".replace(".", ",")
    return f"{max(1, round(value / 1024**2))} Mo"


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
    records = report.get("records") or {}
    if records.get("use"):
        issues.append(_issue(
            "info",
            "records_used",
            f"Ce PC n'a pas de source de métadonnées : les {_plural(records['count'], 'notice', 'notices')} "
            "de l'archive (titres, auteurs, tags, notes) seront utilisées, sans réindexation. "
            "Vous pourrez connecter Zotero plus tard dans Mes documents ; les documents dont la "
            "notice diffère seront alors réindexés.",
        ))
    elif library["exported_source"] != library["current_source"]:
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
    copy = report.get("documents_copy") or {}
    # Copied documents keep the archive's paths, which match the index.
    copying = copy.get("mode") in _COPY_MODES
    if library["exported_storage_layout"] and not library["current_storage_layout"] and not copying:
        issues.append(_issue(
            "warning",
            "storage_layout",
            "Sur le PC d'origine, les documents étaient lus dans le dossier storage de Zotero. "
            "Choisissez aussi ce dossier sur ce PC, sinon les chemins ne correspondront pas.",
        ))

    if copy.get("copy_count"):
        where = copy["target"]
        issues.append(_issue(
            "info",
            "documents_copied",
            f"{_plural(copy['copy_count'], 'document de l’archive sera copié', 'documents de l’archive seront copiés')} "
            f"dans {where} ({_format_bytes(copy['copy_bytes'])}). Aucun fichier existant n'est remplacé."
            + (" Ce dossier deviendra le dossier de vos PDF." if copy.get("set_data_path") else ""),
        ))
    if copy.get("mode") == "zotero_other_library":
        issues.append(_issue(
            "warning",
            "documents_not_copied",
            f"L'archive contient {_plural(copy['included_count'], 'document', 'documents')}, "
            "mais ils ne sont pas copiés : le dossier storage de Zotero de ce PC appartient à une "
            "autre bibliothèque, et Zotero ne connaîtrait pas ces fichiers. Copiez-les vous-même "
            "ou choisissez un autre dossier de PDF, puis relancez la vérification.",
        ))

    documents = report["documents"]
    if not documents["data_path_exists"] and copy.get("mode") != "new_folder":
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
        issues.append(_issue("error", "disk_space", _disk_space_message(disk, copying=bool(copy.get("copy_count")))))

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
    records = plan_records(archive["records"], library_report)
    if records["use"]:
        library = _records_library(archive["records"])
    copy = plan_document_copy(archive["files"], library_report)
    incoming = {rel_path: archive["files"][rel_path] for rel_path in copy["rel_paths"]}
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
        "documents": analyze_documents(
            archive["entries"],
            archive["documents"],
            library,
            data_path=copy["target"] or None,
            incoming=incoming,
        ),
        "documents_copy": _public_copy_plan(copy),
        "records": records,
        "existing_index": _existing_profile(
            index_info,
            embedding["current_model"]
            if embedding["action"] == "equivalent"
            else str(index_info["embedding_model"]),
        ),
        "disk": check_disk_space(_import_needs(archive, copy)),
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
            members = [
                info
                for info in archive.infolist()
                if not info.is_dir() and info.filename.startswith(_INDEX_PREFIX)
            ]
            counter = _ByteCounter("install", sum(info.file_size for info in members))
            for info in members:
                relative = info.filename[len(_INDEX_PREFIX):]
                destination = staging.joinpath(*PurePosixPath(relative).parts)
                if not _is_within(destination, staging):
                    raise IndexTransferError("corrupted_archive", "Chemin refusé dans l'archive.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(chunk)
                        counter.add(len(chunk))
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


def _copy_documents(
    archive_path: str, files: dict[str, dict[str, Any]], plan: dict[str, Any]
) -> dict[str, Any]:
    """Copy the planned documents into ``plan["target"]``, never over an existing file.

    Each file is written under a temporary name and only takes its real name
    once its size and SHA-256 match the header.
    """
    target = Path(plan["target"])
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    counter = _ByteCounter("copy", int(plan["copy_bytes"]))
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for rel_path in plan["rel_paths"]:
                item = files[rel_path]
                destination = target.joinpath(*PurePosixPath(item["path"]).parts)
                if not _is_within(destination, target):
                    raise IndexTransferError("corrupted_archive", "Chemin refusé dans l'archive.")
                if destination.exists() or _locate_document(rel_path, str(target)):
                    skipped += 1
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                part = destination.with_name(destination.name + _PART_SUFFIX)
                part.unlink(missing_ok=True)
                digest = hashlib.sha256()
                size = 0
                try:
                    with archive.open(item["member"]) as source, part.open("xb") as handle:
                        for chunk in iter(lambda: source.read(1024 * 1024), b""):
                            digest.update(chunk)
                            size += len(chunk)
                            handle.write(chunk)
                            counter.add(len(chunk), destination.name)
                    if size != int(item["bytes"]) or digest.hexdigest() != item["sha256"]:
                        raise IndexTransferError(
                            "corrupted_archive",
                            f"Le document {item['path']} est endommagé dans l'archive.",
                        )
                    if destination.exists():
                        # Appeared during the copy: the file already here wins.
                        part.unlink()
                        skipped += 1
                        continue
                    os.rename(part, destination)
                except BaseException:
                    part.unlink(missing_ok=True)
                    raise
                copied += 1
    except (zipfile.BadZipFile, EOFError) as exc:
        raise IndexTransferError(
            "corrupted_archive", "L'archive est endommagée (copie incomplète ?)."
        ) from exc
    return {"copied": copied, "skipped": skipped}


def _adopt_data_path(path: str) -> None:
    """Make ``path`` the PDF folder, as choosing it in the settings does."""
    from src.app_settings import set_app_settings

    Path(path).mkdir(parents=True, exist_ok=True)
    config.set_data_path(path)
    try:
        set_app_settings(data_path=path)
    except Exception:
        _logger.warning("Unable to save the imported PDF folder", exc_info=True)


def _prune_backups(profiles_root: Path, profile_id: str, keep: Path | None) -> None:
    prefix = f".backup-{profile_id}-"
    for entry in profiles_root.iterdir():
        if entry.is_dir() and entry.name.startswith(prefix) and entry != keep:
            shutil.rmtree(entry, ignore_errors=True)


def import_archive(
    archive_path: str | os.PathLike[str],
    *,
    adopt_settings: bool = True,
    copy_documents: bool = True,
) -> dict[str, Any]:
    """Install the archive's index; the previous one with the same profile is kept aside.

    With ``adopt_settings`` the chunk size and, when the model is installed,
    the embedding model are aligned on the index so the next load uses it.
    With ``copy_documents`` the archive's documents missing here are copied
    first, following ``plan_document_copy``. The archive's records become the
    metadata source when this computer has none (``plan_records``).
    """
    _progress_begin("import")
    try:
        return _import_archive(
            archive_path, adopt_settings=adopt_settings, copy_documents=copy_documents
        )
    except OSError as exc:
        if not _is_disk_full(exc):
            raise
        # Something else filled the disk meanwhile; the index was not replaced.
        raise IndexTransferError(
            "disk_space",
            "Le disque est plein : l'import s'est arrêté sans remplacer l'index. "
            "Les PDF déjà copiés restent en place et ne seront pas recopiés. "
            "Libérez de la place, puis relancez l'import.",
        ) from exc
    finally:
        _progress_end()


def _import_archive(
    archive_path: str | os.PathLike[str],
    *,
    adopt_settings: bool,
    copy_documents: bool,
) -> dict[str, Any]:
    archive = _read_archive(archive_path)
    documents: dict[str, Any] = {"copied": 0, "skipped": 0, "target": "", "data_path_set": False}
    records_result: dict[str, Any] = {"used": False, "path": "", "count": 0}
    library_report: dict[str, Any] | None = None
    use_records = False
    if archive["records"]:
        library_report, _library = compare_library(archive["library"])
        use_records = plan_records(archive["records"], library_report)["use"]
    plan: dict[str, Any] = {"mode": "none", "copy_count": 0, "copy_bytes": 0}
    if copy_documents and archive["files"]:
        if library_report is None:
            library_report, _library = compare_library(archive["library"])
        plan = plan_document_copy(archive["files"], library_report)
    # Everything is checked before the first byte is written.
    disk = check_disk_space(_import_needs(archive, plan))
    if not disk["ok"]:
        raise IndexTransferError(
            "disk_space", _disk_space_message(disk, copying=bool(plan.get("copy_count")))
        )
    if plan["mode"] in _COPY_MODES:
        documents["target"] = plan["target"]
        if plan["copy_count"]:
            documents.update(_copy_documents(archive["path"], archive["files"], plan))
        if plan["set_data_path"]:
            _adopt_data_path(plan["target"])
            documents["data_path_set"] = True
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
    if use_records:
        # Only once the index is in place: a failed import leaves the metadata source alone.
        records_result.update(
            used=True,
            path=_adopt_records(archive["records"], Path(archive["path"]).name),
            count=len(archive["records"]),
        )

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
        "documents": documents,
        "records": records_result,
        "embedding": compatibility,
    }
