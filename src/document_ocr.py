"""Conditional, local PDF OCR. Transcriptions survive vector index rebuilds."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from src import config
from src.document_guard import check_document
from src.pdf_render_lock import PDFIUM_LOCK

_logger = logging.getLogger(__name__)

VERSION = "ovis-page-v1"
DETECTION_VERSION = "quality-v2"
BLANK_DETECTOR = 2
_file_lock = threading.RLock()
DEFAULT_MODEL = "ovisocr2-benchmark:q8_0"
DEFAULT_MODEL_SOURCE = "hf.co/bartowski/ATH-MaaS_OvisOCR2-GGUF:Q8_0"
_DEFAULT_MODEL_FILES = {
    "ATH-MaaS_OvisOCR2-Q8_0.gguf": "sha256:8ca48f886bcd5f33636be25c935f07e0d2a83e839d9bce29c7fd3e4bfb344d33",
    "mmproj-ATH-MaaS_OvisOCR2-f16.gguf": "sha256:4e0e9cb9d79dd0f423ba152a51816aa82a1f1a9d1a0190b6f67b2cd4cc5dd681",
}
PROMPT = (
    "Extract all readable content from the image in natural human reading order "
    "and output the result as a single Markdown document. For charts or images, "
    'represent them using an HTML image tag: <img src="images/bbox_{left}_{top}_'
    '{right}_{bottom}.jpg" />, where left, top, right, bottom are bounding box '
    "coordinates scaled to [0, 1000). Format formulas as LaTeX. Format tables "
    "as HTML: <table>...</table>. Transcribe all other text as standard Markdown. "
    "Preserve the original text without translation or paraphrasing."
)


class OCRPaused(RuntimeError):
    """Cancellation leaves completed pages in the cache."""


class OCRInstallError(RuntimeError):
    """The pinned default OCR model could not be installed safely."""


def check_pause(stop: threading.Event):
    if stop.is_set():
        raise OCRPaused("OCR en pause. Les pages terminées sont conservées.")


def root() -> Path:
    return Path(config.STORAGE_DIR) / "document_ocr"


def read_json(path: Path, default=None):
    with _file_lock:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {} if default is None else default


def write_json(path: Path, value):
    # On Windows an open reader can prevent os.replace. Serialize our own readers
    # and tolerate short external locks (e.g. antivirus), while retaining atomicity.
    with _file_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            for attempt in range(8):
                try:
                    temporary.replace(path)
                    break
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(0.02 * (2**attempt))
        finally:
            temporary.unlink(missing_ok=True)


def settings() -> dict:
    saved = read_json(root() / "settings.json")
    return {
        "enabled": bool(saved.get("enabled", False)),
        "model": str(saved.get("model") or DEFAULT_MODEL),
    }


def save_settings(*, enabled: bool, model: str) -> dict:
    value = {"enabled": enabled, "model": model.strip() or DEFAULT_MODEL}
    write_json(root() / "settings.json", value)
    return value


def install_default_model(model: str, *, confirmed: bool) -> dict:
    """Install the pinned OvisOCR2 build and expose it under Bibliobrain's alias."""
    if not confirmed:
        raise OCRInstallError("confirmation_required")
    if str(model or "").strip().lower() != DEFAULT_MODEL.lower():
        raise OCRInstallError("model_not_allowed")

    from src.llm_provider import (
        _ollama_headers,
        _ollama_httpx_verify,
        _open_url_json,
        _read_ollama_json,
        document_tools_ollama_base_url,
    )

    base_url = document_tools_ollama_base_url().rstrip("/")
    try:
        tags = _read_ollama_json(base_url, "/api/tags", timeout=4)
    except Exception as exc:
        raise OCRInstallError(f"ollama_unavailable:{exc}") from exc
    if any(
        DEFAULT_MODEL in (item.get("name"), item.get("model"))
        for item in tags.get("models", [])
        if isinstance(item, dict)
    ):
        return {"ok": True, "model": DEFAULT_MODEL, "already_installed": True}

    def post(path: str, payload: dict, timeout: float | int) -> dict:
        return _open_url_json(
            f"{base_url}/{path.lstrip('/')}",
            method="POST",
            json_payload=payload,
            headers=_ollama_headers(include_json_content_type=True),
            timeout=timeout,
            ssl_verify=_ollama_httpx_verify(),
        )

    pull_error = ""
    try:
        post(
            "/api/pull",
            {"model": DEFAULT_MODEL_SOURCE, "stream": False},
            1800,
        )
    except Exception as exc:
        # Some Ollama versions download both immutable GGUF layers but fail on
        # the tiny Hugging Face manifest. The pinned-blob import below handles
        # that known case without touching Ollama's storage directly.
        _logger.debug("Recovered from an error in install_default_model.", exc_info=True)
        pull_error = str(exc)

    try:
        post(
            "/api/create",
            {
                "model": DEFAULT_MODEL,
                "files": _DEFAULT_MODEL_FILES,
                "parameters": {"temperature": 0},
                "stream": False,
            },
            120,
        )
    except Exception as exc:
        detail = pull_error or str(exc)
        raise OCRInstallError(f"model_install_failed:{detail}") from exc
    return {"ok": True, "model": DEFAULT_MODEL, "already_installed": False}


def native_quality(text: str) -> str:
    """Conservative heuristic: image presence is checked separately for sparse text."""
    compact = re.sub(r"\s", "", text)
    if len(compact) < 80:
        return "sparse"
    words = re.findall(r"\w+", text)
    bad = sum(ord(c) < 32 or c == "\ufffd" for c in compact)
    if bad / len(compact) > 0.01 or len(re.findall(r"\(cid:\d+\)", text)) > 5:
        return "degraded"
    if len(words) > 40 and sum(len(w) == 1 for w in words) / len(words) > 0.65:
        return "degraded"
    alnum_ratio = sum(c.isalnum() for c in compact) / len(compact)
    if alnum_ratio < 0.35:
        return "degraded"
    if (
        len(words) > 100
        and alnum_ratio < 0.6
        and sum(len(w) == 1 for w in words) / len(words) > 0.45
    ):
        return "degraded"
    return "native"


def scan_pdf(path: str, stop: threading.Event) -> dict:
    """Cache the page inventory by file content, not by embedding/chunk settings."""
    from pypdf import PdfReader

    check_document(path)
    check_pause(stop)
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while block := source.read(1024 * 1024):
            check_pause(stop)
            digest.update(block)
    fingerprint = digest.hexdigest()
    inventory_path = root() / "inventory" / f"{fingerprint}-{DETECTION_VERSION}.json"
    cached = read_json(inventory_path)
    if cached.get("fingerprint") == fingerprint:
        return cached
    pages = []
    with open(path, "rb") as source:
        reader = PdfReader(source)
        for number, page in enumerate(reader.pages):
            check_pause(stop)
            try:
                text = page.extract_text() or ""
            except Exception:
                _logger.debug("Recovered from an error in scan_pdf.", exc_info=True)
                text = ""
            quality = native_quality(text)
            candidate = quality == "degraded"
            if quality == "sparse":
                # Covers, diagrams and blank born-digital pages alone aren't scans.
                # XObjects are inspected without decoding every full-resolution image.
                def has_scan_image(resources, depth=0):
                    if depth > 8:
                        return False
                    objects = (resources or {}).get("/XObject", {})
                    for value in objects.values():
                        obj = value.get_object()
                        if (
                            obj.get("/Subtype") == "/Image"
                            and min(int(obj.get("/Width", 0)), int(obj.get("/Height", 0))) >= 300
                        ):
                            return True
                        if obj.get("/Subtype") == "/Form" and has_scan_image(
                            obj.get("/Resources"), depth + 1
                        ):
                            return True
                    return False

                candidate = has_scan_image(page.get("/Resources"))
            pages.append(
                {
                    "page": number,
                    "reason": quality if candidate else "native",
                    "native_chars": len(re.sub(r"\s", "", text)),
                }
            )
    result = {"fingerprint": fingerprint, "pages": pages, "version": VERSION}
    write_json(inventory_path, result)
    return result


def render_page(path: str, page: int) -> tuple[bytes, bool]:
    import pypdfium2 as pdfium

    check_document(path)
    # PDFium is not thread safe, even across different documents.
    with PDFIUM_LOCK:
        with pdfium.PdfDocument(path) as pdf:
            with closing(pdf[page]) as source:
                bitmap = source.render(scale=200 / 72)
                try:
                    picture = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
    # Exclude scanner/reliure marks at the edge; tolerate isolated dust specks.
    margin_x, margin_y = int(picture.width * 0.03), int(picture.height * 0.03)
    interior = picture.crop(
        (margin_x, margin_y, picture.width - margin_x, picture.height - margin_y)
    )
    histogram = interior.convert("L").histogram()
    blank = sum(histogram[:180]) / (interior.width * interior.height) < 0.0003
    interior.close()
    output = io.BytesIO()
    picture.save(output, format="PNG")
    picture.close()
    return output.getvalue(), blank


def model_identity(model: str) -> str:
    from src.llm_provider import _read_ollama_json, document_tools_ollama_base_url

    payload = _read_ollama_json(document_tools_ollama_base_url(), "/api/tags")
    for item in payload.get("models", []):
        if item.get("name") == model or item.get("model") == model:
            if item.get("digest"):
                return str(item["digest"])
    raise RuntimeError(f"Modèle OCR absent d’Ollama : {model}. Installez-le puis reprenez.")


def repeated_tail(text: str) -> bool:
    tail = re.sub(r"\s+", " ", text[-10000:]).strip()
    return any(
        tail.endswith(tail[-width:] * 20) for width in range(1, min(160, len(tail) // 20) + 1)
    )


def output_issue(text: str, final: dict) -> str:
    if not final.get("done"):
        return "incomplete_stream"
    if final.get("done_reason") == "length":
        return "truncated"
    if repeated_tail(text):
        return "repetition"
    if not clean_for_index(text).strip():
        return "no_readable_text"
    if text.count("<table") != text.count("</table>"):
        return "incomplete_table"
    return ""


def clean_for_index(text: str) -> str:
    # A bbox image placeholder contains no transcription or chart interpretation.
    return re.sub(r"<img\b[^>]*>", "", text, flags=re.I).strip()


def request_transcription(
    png: bytes, model: str, penalty: float, stop: threading.Event
) -> tuple[str, dict]:
    from src.llm_provider import _open_ollama_url, document_tools_ollama_base_url

    base_url = document_tools_ollama_base_url()
    payload = {
        "model": model,
        "stream": True,
        "keep_alive": "10m",
        "think": False,
        "options": {
            "temperature": 0,
            "num_ctx": 24576,
            "num_predict": 8192,
            "repeat_penalty": penalty,
            "seed": 42,
        },
        "messages": [
            {"role": "user", "content": PROMPT, "images": [base64.b64encode(png).decode("ascii")]}
        ],
    }
    parts, final = [], {}
    started = time.monotonic()
    with _open_ollama_url(
        base_url.rstrip("/") + "/api/chat",
        method="POST",
        json_payload=payload,
        timeout=90,
    ) as response:
        for line in response:
            check_pause(stop)
            if time.monotonic() - started > 180:
                raise TimeoutError("OCR page timeout")
            if not line.strip():
                continue
            final = json.loads(line)
            if final.get("error"):
                raise RuntimeError(str(final["error"]))
            parts.append(str(final.get("message", {}).get("content", "")))
            if final.get("done"):
                break
    return "".join(parts), final


def transcribe_page(
    path: str,
    fingerprint: str,
    page: int,
    model: str,
    digest: str,
    stop: threading.Event,
    *,
    retry_failed: bool = False,
) -> dict:
    key = hashlib.sha256(f"{fingerprint}:{page}:{digest}:{VERSION}".encode()).hexdigest()
    cache_path = root() / "pages" / key[:2] / f"{key}.json"
    cached = read_json(cache_path)
    if cached:
        if cached.get("status") in {"ok", "blank"}:
            return {**cached, "cache_hit": True}
        if not retry_failed:
            if cached.get("blank_detector") != BLANK_DETECTOR:
                check_pause(stop)
                _, blank = render_page(path, page)
                if blank:
                    cached.update(status="blank", text="")
                cached["blank_detector"] = BLANK_DETECTOR
                write_json(cache_path, cached)
            return {**cached, "cache_hit": True}
    check_pause(stop)
    png, blank = render_page(path, page)
    result: dict[str, Any] = {
        "key": key,
        "page": page,
        "fingerprint": fingerprint,
        "model": model,
        "model_digest": digest,
        "version": VERSION,
        "text": "",
        "attempts": [],
        "status": "blank" if blank else "review",
        "blank_detector": BLANK_DETECTOR,
    }
    if not blank:
        for penalty in (1.0, 1.15):
            check_pause(stop)
            started = time.monotonic()
            try:
                text, final = request_transcription(png, model, penalty, stop)
                issue = output_issue(text, final)
                result["text"] = text
            except OCRPaused:
                raise
            except Exception as exc:
                _logger.debug("Recovered from an error in transcribe_page.", exc_info=True)
                text, final, issue = "", {}, type(exc).__name__
            result["attempts"].append(
                {
                    "penalty": penalty,
                    "issue": issue,
                    "seconds": round(time.monotonic() - started, 3),
                    "text": text,
                    "done": bool(final.get("done")),
                    "done_reason": final.get("done_reason", ""),
                }
            )
            if not issue:
                result["status"] = "ok"
                break
    write_json(cache_path, result)
    return {**result, "cache_hit": False}
