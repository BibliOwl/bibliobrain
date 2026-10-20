"""Notebooks HTTP routes and feature-specific helpers."""

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from src import config
from src.api.common import (
    iter_sync_gen,
    sse,
)
from src.api.streams import (
    register_stream_abort_signal,
    release_stream_abort_signal,
    stop_local_ollama_model_async,
)
from src.api_models import (
    NotebookAppendAnalysisRequest,
    NotebookChatRequest,
    NotebookCreateRequest,
    NotebookImportPathsRequest,
    NotebookImportZoteroCollectionRequest,
    NotebookNotesRequest,
    NotebookSourceAddRequest,
    NotebookTitleRequest,
)
from src.chat_harness import plan_notebook_tools, tool_event
from src.document_formats import load_document_units
from src.file_access import resolve_document_path
from src.library import get_zotero_facets, init_zotero, list_zotero_collection_pdfs
from src.model_profiles import resolve_effective_thinking
from src.notebooks import (
    add_source,
    add_sources,
    clear_messages,
    create_notebook,
    delete_notebook,
    get_notebook,
    list_notebooks,
    public_notebook_payload,
    public_source_payload,
    remove_message,
    remove_source,
    stream_notebook_chat,
    stream_notebook_thematic_synthesis,
    update_notes,
    update_source_collection,
    update_title,
)
from src.visual_analysis import (
    analyze_pdf_visuals,
    results_as_markdown,
    visual_analysis_model_settings,
)

_logger = logging.getLogger(__name__)

router = APIRouter()


def _notebook_pdf_visual_payload(
    full_path: Path,
    *,
    page_texts: list[str] | None = None,
    max_pages: int = 6,
) -> dict[str, Any]:
    """Return optional figure evidence for notebook sources in index mode."""
    mode, model, analysis_model = visual_analysis_model_settings()
    if mode != "index" or not (model or analysis_model):
        return {}
    payload = analyze_pdf_visuals(
        str(full_path),
        model=model,
        analysis_model=analysis_model,
        page_texts=page_texts,
        max_pages=max(1, min(6, int(max_pages or 6))),
    )
    results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(results, list) or not results:
        return {}
    pages: list[int] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        page_number = item.get("page")
        if page_number is None:
            continue
        try:
            pages.append(int(page_number))
        except (TypeError, ValueError):
            continue
    return {
        "visual_content": results_as_markdown(payload),
        "visual_model": " + ".join(value for value in (model, analysis_model) if value),
        "visual_pages": pages,
    }


@router.get("/api/notebooks")
def api_list_notebooks(limit: int = 100, offset: int = 0):
    page_size = max(1, min(limit, 500))
    page_offset = max(0, offset)
    notebooks = list_notebooks()
    page = notebooks[page_offset : page_offset + page_size]
    return {
        "notebooks": page,
        "pagination": {
            "limit": page_size,
            "offset": page_offset,
            "total": len(notebooks),
            "has_more": page_offset + len(page) < len(notebooks),
        },
    }


@router.post("/api/notebooks")
def api_create_notebook(req: NotebookCreateRequest):
    nb = create_notebook(req.title)
    return {"id": nb["id"], "title": nb["title"], "created": nb["created"]}


@router.get("/api/notebooks/{nb_id}")
def api_get_notebook(nb_id: str):
    from fastapi import HTTPException

    nb = get_notebook(nb_id)
    if nb is None:
        raise HTTPException(status_code=404, detail="Carnet introuvable")
    return public_notebook_payload(nb)


@router.delete("/api/notebooks/{nb_id}")
def api_delete_notebook(nb_id: str):
    return {"ok": delete_notebook(nb_id)}


@router.put("/api/notebooks/{nb_id}/title")
def api_update_title(nb_id: str, req: NotebookTitleRequest):
    return {"ok": update_title(nb_id, req.title)}


@router.put("/api/notebooks/{nb_id}/notes")
def api_update_notes(nb_id: str, req: NotebookNotesRequest):
    return {"ok": update_notes(nb_id, req.notes)}


@router.post("/api/notebooks/{nb_id}/sources")
async def api_add_source(nb_id: str, req: NotebookSourceAddRequest):
    from fastapi import HTTPException

    if get_notebook(nb_id) is None:
        raise HTTPException(status_code=404, detail="Carnet introuvable")
    visual_payload: dict[str, Any] = {}
    if req.source_path:
        full = resolve_document_path(config.DATA_PATH, req.source_path)
        if full is not None and full.suffix.lower() == ".pdf":
            try:
                visual_payload = await asyncio.to_thread(
                    _notebook_pdf_visual_payload,
                    full,
                )
            except Exception:
                # Figure analysis is optional and must not prevent adding the source.
                _logger.debug("Recovered from an error in api_add_source.", exc_info=True)
                visual_payload = {}
    src = add_source(
        nb_id,
        req.title,
        req.content,
        req.source_path,
        **visual_payload,
    )
    if src is None:
        raise HTTPException(status_code=404, detail="Carnet introuvable")
    return {"source": public_source_payload(src)}


@router.post("/api/notebooks/import-paths")
async def api_import_notebook_paths(req: NotebookImportPathsRequest):
    from fastapi import HTTPException

    import src.config as _cfg

    rel_paths = [p.strip() for p in (req.paths or []) if p and p.strip()]
    if not rel_paths:
        raise HTTPException(status_code=400, detail="Aucun document à importer")

    created = False
    nb_id = (req.notebook_id or "").strip()
    if nb_id:
        if get_notebook(nb_id) is None:
            raise HTTPException(status_code=404, detail="Carnet introuvable")
    else:
        nb = create_notebook((req.title or "").strip() or "Nouveau carnet (graphe)")
        nb_id = nb["id"]
        created = True

    nb_before = get_notebook(nb_id) or {"sources": []}
    existing_paths = {
        (src.get("source_path") or "").strip().replace("\\", "/").lower()
        for src in nb_before.get("sources", [])
        if isinstance(src, dict)
    }

    max_pages = max(1, min(40, int(req.max_pages or 15)))
    max_chars = max(2000, min(120000, int(req.max_chars or 20000)))

    added = 0
    skipped = 0
    failed = 0
    errors: list[dict] = []
    imported_paths: list[str] = []

    for rel in rel_paths:
        norm = rel.replace("\\", "/").lower()
        if norm in existing_paths:
            skipped += 1
            continue

        full = resolve_document_path(_cfg.DATA_PATH, rel)
        if full is None:
            failed += 1
            errors.append({"path": rel, "error": "Document introuvable"})
            continue

        try:
            units = (await asyncio.to_thread(load_document_units, full))[:max_pages]
            text = "\n\n".join(unit.text for unit in units)[:max_chars].strip()
            visual_payload: dict[str, Any] = {}
            if full.suffix.lower() == ".pdf":
                try:
                    visual_payload = await asyncio.to_thread(
                        _notebook_pdf_visual_payload,
                        full,
                        page_texts=[unit.text for unit in units],
                        max_pages=max_pages,
                    )
                except Exception:
                    _logger.debug(
                        "Recovered from an error in api_import_notebook_paths.", exc_info=True
                    )
                    visual_payload = {}
            if not text and not visual_payload.get("visual_content"):
                failed += 1
                errors.append({"path": rel, "error": "Document vide"})
                continue
            src = add_source(
                nb_id,
                title=full.stem,
                content=text,
                source_path=rel,
                **visual_payload,
            )
            if src is None:
                failed += 1
                errors.append({"path": rel, "error": "Carnet introuvable"})
                continue

            existing_paths.add(norm)
            imported_paths.append(rel)
            added += 1
        except Exception:
            failed += 1
            _logger.exception("Notebook source import failed for %s", rel)
            errors.append({"path": rel, "error": "source_import_failed"})

    nb_after = get_notebook(nb_id) or {"title": req.title}
    return {
        "ok": True,
        "notebook_id": nb_id,
        "notebook_title": nb_after.get("title", req.title),
        "created": created,
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "imported_paths": imported_paths,
        "errors": errors[:12],
    }


async def _import_notebook_zotero_collection(
    req: NotebookImportZoteroCollectionRequest,
    progress_cb=None,
) -> dict:
    from fastapi import HTTPException

    def progress(event: dict) -> None:
        if progress_cb is not None:
            progress_cb(event)

    collection = (req.collection or "").strip()
    if not collection:
        raise HTTPException(status_code=400, detail="Collection Zotero requise")

    progress({"type": "preparing", "collection": collection})
    await asyncio.sleep(0)
    facets = get_zotero_facets()
    if int((facets.get("stats") or {}).get("items", 0) or 0) == 0:
        await asyncio.to_thread(init_zotero, config.ZOTERO_DB_PATH or "")

    records = await asyncio.to_thread(
        list_zotero_collection_pdfs,
        collection,
        include_descendants=bool(req.include_descendants),
    )
    if not records:
        facets = get_zotero_facets()
        if int((facets.get("stats") or {}).get("items", 0) or 0) == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Zotero n'est pas charge. Detectez ou chargez Zotero dans les reglages, "
                    "puis reessayez."
                ),
            )
        raise HTTPException(
            status_code=404,
            detail="Aucun article PDF trouve pour cette collection Zotero",
        )

    created = False
    nb_id = (req.notebook_id or "").strip()
    if nb_id:
        if get_notebook(nb_id) is None:
            raise HTTPException(status_code=404, detail="Carnet introuvable")
    else:
        default_title = f"Zotero - {collection}"
        nb = create_notebook((req.title or "").strip() or default_title)
        nb_id = nb["id"]
        created = True

    nb_before = get_notebook(nb_id) or {"sources": []}
    existing_paths = {
        (src.get("source_path") or "").strip().replace("\\", "/").lower()
        for src in nb_before.get("sources", [])
        if isinstance(src, dict)
    }

    max_pages = max(1, min(40, int(req.max_pages or 15)))
    max_chars = max(2000, min(120000, int(req.max_chars or 20000)))
    added = 0
    skipped = 0
    failed = 0
    imported_paths: list[str] = []
    errors: list[dict] = []

    pending: list[tuple[int, dict]] = []
    for index, record in enumerate(records):
        rel_path = str(record.get("path") or "").strip()
        norm = rel_path.replace("\\", "/").lower()
        if norm in existing_paths:
            skipped += 1
            continue
        pending.append((index, record))

    processed = skipped
    progress(
        {
            "type": "start",
            "collection": collection,
            "total": len(records),
            "processed": processed,
            "added": 0,
            "skipped": skipped,
            "failed": 0,
        }
    )

    semaphore = asyncio.Semaphore(min(4, max(1, len(pending))))

    def load_pdf_text(full_path: Path) -> tuple[str, list[str]]:
        units = load_document_units(full_path, max_units=max_pages)
        page_texts = [unit.text for unit in units]
        return "\n\n".join(page_texts)[:max_chars].strip(), page_texts

    async def prepare_source(index: int, record: dict) -> tuple[int, dict | None, str | None]:
        rel_path = str(record.get("path") or "").strip()
        full_path = Path(str(record.get("full_path") or ""))

        item = record.get("item")
        title = str(record.get("title") or full_path.stem or rel_path).strip()
        metadata_text = ""
        if item is not None and hasattr(item, "as_markdown"):
            try:
                metadata_text = str(item.as_markdown()).strip()
            except Exception:
                _logger.debug("Recovered from an error in prepare_source.", exc_info=True)
                metadata_text = ""

        pdf_text = ""
        page_texts: list[str] = []
        visual_payload: dict[str, Any] = {}
        extraction_error = None
        try:
            async with semaphore:
                pdf_text, page_texts = await asyncio.to_thread(load_pdf_text, full_path)
                try:
                    visual_payload = await asyncio.to_thread(
                        _notebook_pdf_visual_payload,
                        full_path,
                        page_texts=page_texts,
                        max_pages=max_pages,
                    )
                except Exception:
                    _logger.debug("Recovered from an error in prepare_source.", exc_info=True)
                    visual_payload = {}
        except Exception:
            _logger.exception("Notebook PDF extraction failed for %s", rel_path)
            extraction_error = "pdf_extraction_failed"

        content_parts = [part for part in (metadata_text, pdf_text) if part]
        if not content_parts:
            return index, None, extraction_error or "Aucun contenu exploitable"

        return (
            index,
            {
                "title": title,
                "content": "\n\n---\n\n".join(content_parts),
                "source_path": rel_path,
                **visual_payload,
            },
            extraction_error,
        )

    prepared: list[tuple[int, dict]] = []
    tasks = [asyncio.create_task(prepare_source(index, record)) for index, record in pending]
    for task in asyncio.as_completed(tasks):
        index, source, error = await task
        rel_path = str(records[index].get("path") or "").strip()
        processed += 1
        if error:
            errors.append({"path": rel_path, "error": error})
        if source is None:
            failed += 1
        else:
            prepared.append((index, source))
        progress(
            {
                "type": "progress",
                "collection": collection,
                "total": len(records),
                "processed": processed,
                "added": len(prepared),
                "skipped": skipped,
                "failed": failed,
                "current": str(records[index].get("title") or rel_path),
            }
        )

    prepared.sort(key=lambda row: row[0])
    sources_to_add = [source for _index, source in prepared]
    if sources_to_add:
        stored_sources = await asyncio.to_thread(add_sources, nb_id, sources_to_add)
        if stored_sources is None:
            failed += len(sources_to_add)
            errors.append({"path": "", "error": "Carnet introuvable"})
        else:
            added = len(stored_sources)
            imported_paths = [source["source_path"] for source in sources_to_add]

    if not update_source_collection(
        nb_id,
        provider="zotero",
        name=collection,
        include_descendants=bool(req.include_descendants),
    ):
        raise HTTPException(status_code=404, detail="Carnet introuvable")

    nb_after = get_notebook(nb_id) or {"title": req.title}
    return {
        "ok": True,
        "notebook_id": nb_id,
        "notebook_title": nb_after.get("title", req.title),
        "created": created,
        "collection": collection,
        "matched": len(records),
        "added": added,
        "skipped": skipped,
        "failed": failed,
        "imported_paths": imported_paths,
        "errors": errors[:12],
    }


@router.post("/api/notebooks/import-zotero-collection")
async def api_import_notebook_zotero_collection(
    req: NotebookImportZoteroCollectionRequest,
    request: Request,
):
    accepts_sse = "text/event-stream" in str(request.headers.get("accept") or "").lower()
    if not accepts_sse:
        return await _import_notebook_zotero_collection(req)

    async def generate():
        from fastapi import HTTPException

        events: asyncio.Queue = asyncio.Queue()

        async def run_import() -> None:
            try:
                result = await _import_notebook_zotero_collection(req, events.put_nowait)
                events.put_nowait({"type": "done", **result})
            except HTTPException as exc:
                events.put_nowait(
                    {"type": "error", "status": exc.status_code, "detail": exc.detail}
                )
            except Exception:
                _logger.exception("Zotero notebook import failed")
                events.put_nowait(
                    {"type": "error", "status": 500, "detail": "notebook_import_failed"}
                )

        task = asyncio.create_task(run_import())
        try:
            while True:
                event = await events.get()
                yield sse(event)
                if event.get("type") in {"done", "error"}:
                    break
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/notebooks/append-analysis")
async def api_append_analysis_to_notebook(req: NotebookAppendAnalysisRequest):
    from fastapi import HTTPException

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Aucune analyse a copier")

    created = False
    nb_id = (req.notebook_id or "").strip()
    if nb_id:
        nb = get_notebook(nb_id)
        if nb is None:
            raise HTTPException(status_code=404, detail="Carnet introuvable")
    else:
        nb = create_notebook((req.title or "").strip() or "Nouveau carnet")
        nb_id = nb["id"]
        created = True

    nb_before = get_notebook(nb_id)
    if nb_before is None:
        raise HTTPException(status_code=404, detail="Carnet introuvable")

    existing_notes = str(nb_before.get("notes") or "").strip()
    combined_notes = text if not existing_notes else f"{existing_notes}\n\n---\n\n{text}"
    if not update_notes(nb_id, combined_notes):
        raise HTTPException(status_code=500, detail="Impossible de mettre a jour les notes")

    nb_after = get_notebook(nb_id) or {"title": req.title}
    return {
        "ok": True,
        "created": created,
        "notebook_id": nb_id,
        "notebook_title": nb_after.get("title", req.title),
        "notes_length": len(combined_notes),
    }


@router.delete("/api/notebooks/{nb_id}/sources/{source_id}")
def api_remove_source(nb_id: str, source_id: str):
    return {"ok": remove_source(nb_id, source_id)}


@router.delete("/api/notebooks/{nb_id}/messages")
def api_clear_messages(nb_id: str):
    return {"ok": clear_messages(nb_id)}


@router.delete("/api/notebooks/{nb_id}/messages/{message_index}")
def api_remove_message(nb_id: str, message_index: int):
    return {"ok": remove_message(nb_id, message_index)}


@router.post("/api/notebooks/{nb_id}/chat")
async def api_notebook_chat(nb_id: str, req: NotebookChatRequest):
    async def generate():
        request_id, abort_signal = register_stream_abort_signal(req.model)
        tool_plan = plan_notebook_tools(
            notebook_id=nb_id,
            message=req.message,
            append_to_notes=req.append_to_notes,
        )
        tool_calls = [
            call
            for call in tool_plan.calls
            if not (req.read_only and call.name == "notebook.append_notes")
        ]
        append_to_notes = (not req.read_only) and any(
            call.name == "notebook.append_notes" for call in tool_calls
        )
        if any(call.name == "notebook.thematic_synthesis" for call in tool_calls):
            gen = stream_notebook_thematic_synthesis(
                nb_id,
                req.message,
                req.model,
                persist_history=req.persist_history,
                read_only=req.read_only,
                abort_signal=abort_signal,
            )
        else:
            gen = stream_notebook_chat(
                nb_id,
                req.message,
                req.model,
                append_to_notes=append_to_notes,
                persist_history=req.persist_history,
                read_only=req.read_only,
                thinking_enabled=resolve_effective_thinking(
                    req.model,
                    req.thinking,
                    allow_small_model_override=req.force_thinking_small_models,
                ),
                abort_signal=abort_signal,
            )
        try:
            yield sse({"type": "stream_meta", "request_id": request_id})
            for call in tool_calls:
                yield sse(tool_event("tool_started", call))
            saw_error = False
            async for chunk in iter_sync_gen(gen):
                if chunk.get("type") == "error":
                    saw_error = True
                yield sse(chunk)
            for call in tool_calls:
                yield sse(tool_event("tool_result", call, ok=not saw_error))
        except InterruptedError:
            await stop_local_ollama_model_async(req.model)
            return
        except asyncio.CancelledError:
            abort_signal.abort()
            await stop_local_ollama_model_async(req.model)
            raise
        finally:
            if abort_signal.is_aborted():
                await stop_local_ollama_model_async(req.model)
            release_stream_abort_signal(request_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
