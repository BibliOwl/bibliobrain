from __future__ import annotations

import importlib
from pathlib import Path

import pytest


def test_relative_paths_are_read_from_the_application_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Started from another folder (a branch worktree, a shortcut), a relative
    # CHROMA_PATH used to point at a new, empty index there.
    import src.config as config

    app_root = Path(config.__file__).resolve().parents[1]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHROMA_PATH", "./chroma_db_zotero")
    monkeypatch.setenv("DATA_PATH", str(tmp_path / "absolute"))
    try:
        reloaded = importlib.reload(config)
        assert Path(reloaded.CHROMA_PATH) == app_root / "chroma_db_zotero"
        assert Path(reloaded.DATA_PATH) == tmp_path / "absolute"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_a_relative_path_in_the_configuration_is_saved_absolute(tmp_path: Path) -> None:
    import json

    from src import machine_config

    app_root = Path(machine_config.__file__).resolve().parents[1]
    target = tmp_path / "configuration.json"
    target.write_text(
        json.dumps(
            {
                "version": machine_config.CONFIGURATION_VERSION,
                "settings": {"CHROMA_PATH": "./src", "DATA_PATH": "./does-not-exist"},
            }
        ),
        encoding="utf-8",
    )

    settings = machine_config._absolutize_relative_paths(
        machine_config.load_machine_settings(target), target
    )

    assert settings["CHROMA_PATH"] == str(app_root / "src")
    assert settings["DATA_PATH"] == str(app_root / "does-not-exist")
    saved = machine_config.load_machine_settings(target)
    # Only a path that exists is written back: a wrong guess stays reversible.
    assert saved["CHROMA_PATH"] == str(app_root / "src")
    assert saved["DATA_PATH"] == "./does-not-exist"


def test_a_worktree_reads_relative_paths_from_the_main_checkout(tmp_path: Path) -> None:
    from src.machine_config import application_root

    main = tmp_path / "bibliobrain"
    (main / ".git" / "worktrees" / "fix-x").mkdir(parents=True)
    worktree = main / ".claude" / "worktrees" / "fix-x"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(
        f"gitdir: {main / '.git' / 'worktrees' / 'fix-x'}\n", encoding="utf-8"
    )

    assert application_root(worktree) == main
    assert application_root(main) == main
