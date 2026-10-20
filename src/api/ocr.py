"""Local OCR settings and resumable library enrichment."""

import asyncio

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src import document_ocr
from src.rag import ocr_ingestion

router = APIRouter()


class OCRSettingsRequest(BaseModel):
    enabled: bool
    model: str = Field(default=document_ocr.DEFAULT_MODEL, min_length=1, max_length=200)


class OCRInstallRequest(BaseModel):
    model: str = Field(default=document_ocr.DEFAULT_MODEL, min_length=1, max_length=200)
    confirmed: bool = False


@router.get("/api/ocr/status")
def api_ocr_status():
    return ocr_ingestion.status()


@router.get("/api/ocr/readiness")
def api_ocr_readiness(model: str = Query(default="", max_length=200)):
    from src.ocr_readiness import readiness

    return readiness(model.strip() or document_ocr.settings()["model"])


@router.put("/api/ocr/settings")
def api_ocr_settings(req: OCRSettingsRequest):
    if ocr_ingestion.status()["active"]:
        return JSONResponse(status_code=409, content={"ok": False, "msg": "Mettez l’OCR en pause avant de modifier ses réglages."})
    return {"ok": True, "settings": document_ocr.save_settings(enabled=req.enabled, model=req.model)}


@router.post("/api/ocr/install")
async def api_ocr_install(req: OCRInstallRequest):
    try:
        return await asyncio.to_thread(
            document_ocr.install_default_model,
            req.model,
            confirmed=req.confirmed,
        )
    except document_ocr.OCRInstallError as exc:
        code = str(exc)
        status = 400 if code in {"confirmation_required", "model_not_allowed"} else 503
        return JSONResponse(status_code=status, content={"ok": False, "error": code})


@router.post("/api/ocr/start")
def api_ocr_start(retry_failed: bool = False):
    result = ocr_ingestion.start(retry_failed=retry_failed)
    return JSONResponse(status_code=200 if result["ok"] else 409, content=result)


@router.post("/api/ocr/pause")
def api_ocr_pause():
    return ocr_ingestion.pause()


@router.get("/api/ocr/inventory")
def api_ocr_inventory():
    return ocr_ingestion.inventory_status()


@router.post("/api/ocr/inventory")
def api_ocr_inventory_start():
    result = ocr_ingestion.start_inventory()
    return JSONResponse(status_code=200 if result["ok"] else 409, content=result)
