from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, cast

import src.config as config


def _load_chat_history_db(tmp_path: Path) -> Any:
    """Charge le module avec un STORAGE_DIR temporaire avant son initialisation SQLite."""
    config.STORAGE_DIR = str(tmp_path)
    chat_history_db = cast(Any, importlib.import_module("src.chat_history_db"))
    chat_history_db = cast(Any, importlib.reload(chat_history_db))
    chat_history_db.DB_DIR = tmp_path
    chat_history_db.DB_PATH = tmp_path / "chat_history.sqlite3"
    chat_history_db.init_chat_history_db()
    return chat_history_db


def test_chat_history_round_trip(workspace_tmp_dir: Path) -> None:
    """Vérifie le cycle de base: création, ajout de messages, puis relecture cohérente."""
    chat_history_db = _load_chat_history_db(workspace_tmp_dir)

    created = chat_history_db.create_conversation(seed_message="Question de depart")
    chat_history_db.add_message(created["id"], "user", "Bonjour")
    chat_history_db.add_message(created["id"], "assistant", "Salut", model="tiny-model")

    conversations = chat_history_db.list_conversations()
    messages = chat_history_db.get_conversation_messages(created["id"])

    assert len(conversations) == 1
    assert conversations[0]["message_count"] == 2
    assert conversations[0]["last_role"] == "assistant"
    assert [message["role"] for message in messages] == ["user", "assistant"]
    assert messages[1]["model"] == "tiny-model"


def test_clear_all_chat_history_resets_rows(workspace_tmp_dir: Path) -> None:
    """Le nettoyage global doit supprimer messages et conversations, puis laisser une base vide."""
    chat_history_db = _load_chat_history_db(workspace_tmp_dir)

    created = chat_history_db.create_conversation(seed_message="A nettoyer")
    chat_history_db.add_message(created["id"], "user", "Message")

    result = chat_history_db.clear_all_chat_history()

    assert result["messages_deleted"] == 1
    assert result["conversations_deleted"] == 1
    assert chat_history_db.list_conversations() == []
