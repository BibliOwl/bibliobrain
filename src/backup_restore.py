from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from src.machine_config import MachineConfigError, load_machine_settings

BACKUP_FORMAT = "bibliobrain-backup"
BACKUP_FORMAT_VERSION = 1
_KNOWN_SOURCE_KINDS = {"fixed", "home", "storage", "chroma", "data", "zotero"}
_EPHEMERAL_RELATIVE_PATHS = {"runtime/server.pid", "runtime/ollama.pid"}


class BackupError(RuntimeError):
    """Raised when a backup cannot be created or safely restored."""


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _normalized_environment(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {str(name).upper(): str(value) for name, value in source.items()}


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _setting_path(
    name: str,
    default: Path,
    *,
    settings: Mapping[str, str],
    environment: Mapping[str, str],
) -> Path:
    raw = str(environment.get(name) or settings.get(name) or "").strip()
    return _resolved(raw) if raw else _resolved(default)


def discover_backup_sources(
    *,
    include_external_documents: bool = False,
    local_app_data: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    env = _normalized_environment(environment)
    local_base = _resolved(
        local_app_data
        or env.get("LOCALAPPDATA")
        or (Path.home() / "AppData" / "Local")
    )
    fixed_root = local_base / "bibliobrain"
    configuration_file = fixed_root / "configuration.json"
    try:
        settings = load_machine_settings(configuration_file)
    except MachineConfigError:
        settings = {}

    home = _setting_path(
        "BIBLIOBRAIN_HOME",
        fixed_root,
        settings=settings,
        environment=env,
    )
    storage = _setting_path(
        "STORAGE_DIR",
        home / "storage",
        settings=settings,
        environment=env,
    )
    chroma = _setting_path(
        "CHROMA_PATH",
        home / "chroma_db",
        settings=settings,
        environment=env,
    )
    data = _setting_path(
        "DATA_PATH",
        home / "data",
        settings=settings,
        environment=env,
    )
    zotero_raw = str(env.get("ZOTERO_DB_PATH") or settings.get("ZOTERO_DB_PATH") or "").strip()
    zotero = _resolved(zotero_raw) if zotero_raw else None

    core_paths = (fixed_root, home, storage, chroma)
    data_is_core = any(_is_within(data, root) for root in core_paths)
    candidates: list[tuple[str, Path, bool]] = [
        ("fixed", fixed_root, True),
        ("home", home, True),
        ("storage", storage, True),
        ("chroma", chroma, True),
        ("data", data, data_is_core or include_external_documents),
    ]
    if zotero is not None:
        zotero_is_core = any(_is_within(zotero, root) for root in core_paths)
        candidates.append(("zotero", zotero, zotero_is_core or include_external_documents))

    roots: list[tuple[Path, str]] = []
    sources: list[dict[str, Any]] = []
    for kind, path, included in candidates:
        covering = next(((root, prefix) for root, prefix in roots if _is_within(path, root)), None)
        if included and covering:
            root, prefix = covering
            relative = path.relative_to(root).as_posix()
            archive_prefix = prefix if relative == "." else f"{prefix}/{relative}"
            covered_by = str(root)
        elif included:
            archive_prefix = f"payload/{kind}"
            covered_by = ""
            roots.append((path, archive_prefix))
        else:
            archive_prefix = ""
            covered_by = ""
        sources.append(
            {
                "kind": kind,
                "original_path": str(path),
                "archive_prefix": archive_prefix,
                "included": bool(included),
                "exists": path.exists(),
                "path_type": "file" if path.is_file() else "directory",
                "covered_by": covered_by,
            }
        )

    resolved_paths = {
        "fixed_root": str(fixed_root),
        "bibliobrain_home": str(home),
        "storage_dir": str(storage),
        "chroma_path": str(chroma),
        "data_path": str(data),
        "zotero_db_path": str(zotero) if zotero is not None else "",
    }
    return sources, resolved_paths


def _iter_source_files(source: Path) -> list[tuple[Path, str]]:
    if not source.exists():
        return []
    if source.is_file():
        return [(source, "")]
    files: list[tuple[Path, str]] = []
    for item in source.rglob("*"):
        is_junction = bool(getattr(item, "is_junction", lambda: False)())
        if item.is_symlink() or is_junction or not item.is_file():
            continue
        relative = item.relative_to(source).as_posix()
        if relative.casefold() in _EPHEMERAL_RELATIVE_PATHS:
            continue
        files.append((item, relative))
    return files


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def create_backup(
    output_path: str | os.PathLike[str],
    *,
    include_external_documents: bool = False,
    local_app_data: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    output = _resolved(output_path)
    if output.suffix.casefold() != ".zip":
        raise BackupError("Le fichier de sauvegarde doit utiliser l'extension .zip.")
    if output.exists():
        raise BackupError(f"Le fichier de sauvegarde existe déjà : {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    sources, resolved_paths = discover_backup_sources(
        include_external_documents=include_external_documents,
        local_app_data=local_app_data,
        environment=environment,
    )

    physical_sources: list[tuple[Path, str]] = []
    for source in sources:
        if not source["included"] or source["covered_by"]:
            continue
        source_path = _resolved(source["original_path"])
        if _is_within(output, source_path):
            raise BackupError("Le fichier de sauvegarde ne peut pas être créé dans les données sauvegardées.")
        physical_sources.append((source_path, str(source["archive_prefix"])))

    descriptor, temporary_name = tempfile.mkstemp(
        dir=output.parent,
        prefix=f".{output.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    file_count = 0
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for source_path, archive_prefix in physical_sources:
                for file_path, relative in _iter_source_files(source_path):
                    entry = archive_prefix if not relative else f"{archive_prefix}/{relative}"
                    archive.write(file_path, entry)
                    file_count += 1

            version_path = Path(__file__).resolve().parents[1] / "VERSION"
            application_version = (
                version_path.read_text(encoding="utf-8").strip() if version_path.exists() else ""
            )
            manifest = {
                "format": BACKUP_FORMAT,
                "format_version": BACKUP_FORMAT_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "application_version": application_version,
                "external_documents_included": include_external_documents,
                "resolved_paths": resolved_paths,
                "sources": sources,
                "file_count": file_count,
                "draft_document_included": any(
                    entry.filename.casefold().endswith("/drafting_document.json")
                    for entry in archive.infolist()
                ),
                "dpapi_notice": (
                    "Les secrets DPAPI ne sont déchiffrables que par le même compte Windows."
                ),
            }
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    digest = _sha256_file(output)
    return {
        "ok": True,
        "archive": str(output),
        "bytes": output.stat().st_size,
        "sha256": digest,
        "file_count": file_count,
        "external_documents_included": include_external_documents,
    }


def _load_manifest(archive: zipfile.ZipFile) -> dict[str, Any]:
    try:
        payload = json.loads(archive.read("manifest.json").decode("utf-8"))
    except (KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("La sauvegarde ne contient pas de manifeste Bibliobrain valide.") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("format") != BACKUP_FORMAT
        or payload.get("format_version") != BACKUP_FORMAT_VERSION
        or not isinstance(payload.get("sources"), list)
    ):
        raise BackupError("Le format de cette sauvegarde Bibliobrain n'est pas pris en charge.")
    return payload


def _validate_archive_members(archive: zipfile.ZipFile) -> None:
    for info in archive.infolist():
        name = info.filename
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise BackupError(f"Entrée dangereuse refusée dans la sauvegarde : {name}")


def _validate_restore_target(path: Path, *, environment: Mapping[str, str]) -> None:
    target = _resolved(path)
    anchor = Path(target.anchor)
    broad_targets = {anchor, _resolved(Path.home())}
    for variable in (
        "LOCALAPPDATA",
        "APPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "WINDIR",
    ):
        value = str(environment.get(variable) or "").strip()
        if value:
            broad_targets.add(_resolved(value))
    if target in broad_targets:
        raise BackupError(f"Cible de restauration trop large refusée : {target}")


def _mapped_restore_target(
    original: Path,
    *,
    manifest: Mapping[str, Any],
    current_fixed_root: Path,
) -> Path:
    resolved_paths = manifest.get("resolved_paths")
    if not isinstance(resolved_paths, dict):
        return original
    old_fixed_raw = str(resolved_paths.get("fixed_root") or "").strip()
    old_home_raw = str(resolved_paths.get("bibliobrain_home") or "").strip()
    if old_fixed_raw:
        old_fixed = _resolved(old_fixed_raw)
        if _is_within(original, old_fixed):
            return current_fixed_root / original.relative_to(old_fixed)
    if old_home_raw:
        old_home = _resolved(old_home_raw)
        if _is_within(original, old_home):
            return old_home / original.relative_to(old_home)
    return original


def restore_backup(
    archive_path: str | os.PathLike[str],
    *,
    confirm: bool = False,
    create_rollback: bool = True,
    local_app_data: str | os.PathLike[str] | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if not confirm:
        raise BackupError("La restauration exige une confirmation explicite.")
    source_archive = _resolved(archive_path)
    if not source_archive.is_file():
        raise BackupError(f"Sauvegarde introuvable : {source_archive}")
    env = _normalized_environment(environment)
    local_base = _resolved(
        local_app_data
        or env.get("LOCALAPPDATA")
        or (Path.home() / "AppData" / "Local")
    )
    current_fixed_root = local_base / "bibliobrain"

    with zipfile.ZipFile(source_archive, "r") as archive:
        _validate_archive_members(archive)
        manifest = _load_manifest(archive)

    rollback = ""
    if create_rollback:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        rollback_path = source_archive.with_name(f"{source_archive.stem}-pre-restore-{stamp}.zip")
        rollback_result = create_backup(
            rollback_path,
            include_external_documents=bool(manifest.get("external_documents_included")),
            local_app_data=local_base,
            environment=env,
        )
        rollback = str(rollback_result["archive"])

    restored_files = 0
    with zipfile.ZipFile(source_archive, "r") as archive:
        for raw_source in manifest["sources"]:
            if not isinstance(raw_source, dict) or not raw_source.get("included"):
                continue
            kind = str(raw_source.get("kind") or "")
            if kind not in _KNOWN_SOURCE_KINDS:
                raise BackupError(f"Type de source inconnu dans la sauvegarde : {kind}")
            prefix = str(raw_source.get("archive_prefix") or "").strip("/")
            if not prefix or raw_source.get("covered_by"):
                continue
            original = _resolved(str(raw_source.get("original_path") or ""))
            target_root = _mapped_restore_target(
                original,
                manifest=manifest,
                current_fixed_root=current_fixed_root,
            )
            _validate_restore_target(target_root, environment=env)

            entries = [
                info
                for info in archive.infolist()
                if info.filename == prefix or info.filename.startswith(f"{prefix}/")
            ]
            source_is_file = str(raw_source.get("path_type") or "") == "file"
            for info in entries:
                if info.is_dir():
                    continue
                relative = "" if info.filename == prefix else info.filename[len(prefix) + 1 :]
                destination = target_root if source_is_file else target_root / PurePosixPath(relative)
                destination = _resolved(destination)
                allowed_root = target_root.parent if source_is_file else target_root
                if not _is_within(destination, allowed_root):
                    raise BackupError(f"Destination de restauration dangereuse refusée : {destination}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    dir=destination.parent,
                    prefix=f".{destination.name}.",
                    suffix=".restore",
                )
                os.close(descriptor)
                temporary = Path(temporary_name)
                try:
                    with archive.open(info, "r") as source, temporary.open("wb") as target:
                        shutil.copyfileobj(source, target)
                    os.replace(temporary, destination)
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
                restored_files += 1

    return {
        "ok": True,
        "archive": str(source_archive),
        "restored_files": restored_files,
        "rollback_archive": rollback,
        "dpapi_notice": manifest.get("dpapi_notice", ""),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sauvegarde et restauration Bibliobrain")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--output", required=True)
    backup.add_argument("--include-external-documents", action="store_true")
    restore = subparsers.add_parser("restore")
    restore.add_argument("--input", required=True)
    restore.add_argument("--confirm", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(
                args.output,
                include_external_documents=bool(args.include_external_documents),
            )
        else:
            result = restore_backup(args.input, confirm=bool(args.confirm))
    except (BackupError, OSError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
