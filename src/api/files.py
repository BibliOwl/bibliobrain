"""Files HTTP routes and feature-specific helpers."""

import asyncio
import logging
import mimetypes

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from src.api import application
from src.document_formats import DocumentFormatError, load_document_units
from src.file_access import resolve_document_path
from src.static_assets import versioned_index_html

_logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/")
async def index():
    html = await asyncio.to_thread(versioned_index_html, application.STATIC_DIR)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, max-age=0"})


@router.get("/api/open-pdf")
async def api_open_pdf(path: str):
    """Serve a supported library document (legacy route name kept for clients)."""
    import src.config as _cfg

    full = resolve_document_path(_cfg.DATA_PATH, path)
    if full is None:
        return JSONResponse({"error": "Document introuvable"}, status_code=404)
    media_type = mimetypes.guess_type(full.name)[0] or "application/octet-stream"
    inline = full.suffix.lower() in {".pdf", ".txt", ".md", ".markdown", ".rst", ".tex"}
    return FileResponse(
        str(full),
        media_type=media_type,
        filename=full.name,
        content_disposition_type="inline" if inline else "attachment",
        headers={
            # This route is displayed by Bibliobrain's same-origin PDF iframe.
            # Keep framing disabled everywhere else via SecurityHeadersMiddleware.
            "X-Frame-Options": "SAMEORIGIN",
            "Content-Security-Policy": "frame-ancestors 'self'",
        },
    )


@router.get("/api/extract-pdf-text")
async def api_extract_pdf_text(path: str):
    import src.config as _cfg

    full = resolve_document_path(_cfg.DATA_PATH, path)
    if full is None:
        return JSONResponse({"error": "Document introuvable"}, status_code=404)
    try:
        units = (await asyncio.to_thread(load_document_units, full))[:15]
        text = "\n\n".join(unit.text for unit in units)[:20000]
        return {"text": text, "pages": len(units), "units": len(units)}
    except (OSError, ValueError, DocumentFormatError):
        _logger.exception("Document text extraction failed")
        return JSONResponse({"error": "document_text_extraction_failed"}, status_code=500)
