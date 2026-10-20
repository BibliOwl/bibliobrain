"""Backward-compatible, lazy public entry point for the RAG package."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "chat": ".query_engine",
    "format_sources": ".query_engine",
    "format_sources_api": ".query_engine",
    "init_rag": ".query_engine",
    "reload_db": ".query_engine",
    "stream_chat_api": ".query_engine",
    "try_init": ".query_engine",
    "clear_runtime_state": ".state",
    "find_documents": ".state",
    "find_pdfs": ".state",
    "get_runtime_status": ".state",
    "pause_vector_loading": ".state",
    "get_indexed_rel_paths": ".vector_store",
    "get_vector_database_status": ".vector_store",
    "get_vector_reload_preview": ".vector_store",
    "reset_vector_database": ".vector_store",
    "set_active_vector_index_profile": ".vector_store",
}


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(module_name, __name__), name)


__all__ = [
    "chat",
    "clear_runtime_state",
    "find_documents",
    "find_pdfs",
    "format_sources",
    "format_sources_api",
    "get_indexed_rel_paths",
    "get_runtime_status",
    "get_vector_database_status",
    "get_vector_reload_preview",
    "init_rag",
    "pause_vector_loading",
    "reload_db",
    "reset_vector_database",
    "set_active_vector_index_profile",
    "stream_chat_api",
    "try_init",
]
