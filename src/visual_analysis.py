from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from collections import Counter
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src import config
from src.app_settings import get_app_settings
from src.document_guard import DocumentRejected, check_document
from src.llm_provider import (
    _ollama_headers,
    _ollama_httpx_verify,
    _open_url_json,
    document_tools_ollama_base_url,
    unload_ollama_model,
)
from src.ollama_utils import find_ollama_executable, is_local_ollama_url
from src.pdf_render_lock import pdfium_locked

logger = logging.getLogger(__name__)

RECOMMENDED_OCR_MODEL = "glm-ocr:q8_0"
RECOMMENDED_OCR_DOWNLOAD_BYTES = 1_600_000_000
VISUAL_CONTENT_TYPE = "figure_ocr"
_VISUAL_PIPELINE_VERSION = 4
_MAX_EMBEDDED_IMAGES_PER_PAGE = 4
# An embedded raster covering most of the page is a scan, not a figure: route it
# through the page renderer so the caption is masked like any other page.
_SCANNED_PAGE_AREA_RATIO = 0.8
_MAX_VISION_IMAGE_SIDE = 1800
_MAX_DESCRIPTION_ITEMS = 12
# Two short numbers drawn large: every working vision model reads at least one,
# while a model whose projector is broken answers without seeing them.
_VISION_PROBE_CODES = ("73", "58")
_VISION_PROBE_FILENAME = "vision_probe.json"
_VISION_PROBE_LOCK = threading.Lock()
_vision_probe_memory: dict[str, bool] = {}
_PLACEHOLDER_ITEM_PATTERN = re.compile(
    r"(?i)^(?:s[ée]ries?|panneau|ligne|colonne|lignes|colonnes|donn[ée]es"
    r"(?: de la grille)?|axe des (?:lignes|colonnes)|axe (?:horizontal|vertical)"
    r"|(?:horizontal|vertical) axis|[xy])\s*\d*$"
)
# Lightest general vision model that passed the perception probe (1.6 GB):
# the analysis option offered to modest machines.
LIGHT_ANALYSIS_MODEL = "minicpm-v4.6"
LIGHT_ANALYSIS_DOWNLOAD_BYTES = 1_600_000_000
_LIGHT_ANALYSIS_MAX_VRAM_BYTES = 8 * 1024**3
_ALLOWED_INSTALL_MODELS = {RECOMMENDED_OCR_MODEL, LIGHT_ANALYSIS_MODEL}
_GIB = 1024**3
_ANALYSIS_MODEL_PREFERENCES: tuple[tuple[int, tuple[str, ...]], ...] = (
    (20, ("gemma4:26b", "qwen3.5:27b", "gemma4:12b", "qwen3.5:9b")),
    (14, ("gemma4:12b", "ministral-3:14b", "qwen3.5:9b", "ministral-3:8b")),
    (8, ("qwen3.5:9b", "ministral-3:8b", "gemma4:12b", "qwen3.5:4b")),
    (5, ("qwen3.5:4b", "ministral-3:3b", "minicpm-v4.6", "gemma4:e2b", "qwen3.5:2b")),
    (0, ("qwen3.5:2b", "minicpm-v4.6", "ministral-3:3b", "gemma4:e2b", "qwen3.5:0.8b")),
)
_FIGURE_PATTERN = re.compile(
    r"(?im)^\s*(?:fig(?:ure)?\.?|table(?:au)?|graph(?:ique)?|scheme|sch[ée]ma)\s*\d+[a-z]?\b"
)
_VISUAL_REFERENCE_PATTERN = re.compile(
    r"(?i)\b(?P<kind>fig(?:ure)?\.?|table(?:au)?|graph(?:ique)?|scheme|sch[ée]ma)"
    r"\s*(?P<number>\d+)\s*(?P<suffix>[a-z])?\b"
)
_CAPTION_LINE_PATTERN = re.compile(
    r"^\s*(?P<label>(?:fig(?:ure)?\.?|table(?:au)?|graph(?:ique)?|scheme|sch[ée]ma)"
    r"\s*\d+[a-z]?)\s*[.\-:)]\s*(?P<body>.*)$",
    re.IGNORECASE,
)
_VISION_LOCK = threading.Lock()
_VISUAL_DESCRIPTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "figure_type": {"type": "string"},
        "summary": {"type": "string"},
        "axes": {"type": "array", "items": {"type": "string"}},
        "series": {"type": "array", "items": {"type": "string"}},
        "observations": {"type": "array", "items": {"type": "string"}},
        "quantitative_readings": {"type": "array", "items": {"type": "string"}},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "figure_type",
        "summary",
        "axes",
        "series",
        "observations",
        "quantitative_readings",
        "uncertainties",
    ],
}


class VisualAnalysisError(RuntimeError):
    pass


@dataclass(frozen=True)
class _CaptionRegion:
    label: str
    text: str
    bbox: tuple[float, float, float, float] | None = None

    @property
    def reference_keys(self) -> set[tuple[str, str, str]]:
        return _visual_reference_keys(self.label)


@dataclass(frozen=True)
class _EmbeddedVisual:
    image_bytes: bytes
    bbox: tuple[float, float, float, float]
    caption: _CaptionRegion | None = None


@dataclass(frozen=True)
class _PageVisualInputs:
    embedded: tuple[_EmbeddedVisual, ...]
    captions: tuple[_CaptionRegion, ...]
    page_size: tuple[float, float] | None = None


@dataclass
class _VisualJob:
    page_number: int
    image_bytes: bytes
    captions: tuple[_CaptionRegion, ...]
    figure_label: str
    visual_source: str
    isolated: bool
    ocr_text: str = ""
    visual_analysis: str = ""
    ocr_error: str = ""
    visual_analysis_error: str = ""


def _ollama_url() -> str:
    return document_tools_ollama_base_url().rstrip("/")


def _human_size(size: Any) -> str:
    try:
        byte_count = max(0, int(size or 0))
    except (TypeError, ValueError):
        byte_count = 0
    if byte_count >= 1_000_000_000:
        return f"{byte_count / 1_000_000_000:.1f} Go"
    if byte_count >= 1_000_000:
        return f"{byte_count / 1_000_000:.0f} Mo"
    return "taille inconnue"


def _gpu_vram_label(byte_count: int) -> str:
    gib = max(0, int(byte_count or 0)) / _GIB
    if not gib:
        return ""
    rounded = round(gib)
    return f"{rounded} Go" if abs(gib - rounded) < 0.15 else f"{gib:.1f} Go"


def _detect_total_gpu_vram() -> tuple[int, str]:
    """Return the largest dedicated NVIDIA VRAM pool without loading a model."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        return 0, ""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [
                executable,
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError):
        return 0, ""
    values = [
        int(match.group(0))
        for line in completed.stdout.splitlines()
        if (match := re.search(r"\d+", line))
    ]
    if completed.returncode != 0 or not values:
        return 0, ""
    return max(values) * 1024**2, "nvidia-smi"


def _model_matches_preference(name: str, preference: str) -> bool:
    normalized = str(name or "").strip().lower()
    target = str(preference or "").strip().lower()
    if ":" not in target and normalized == f"{target}:latest":
        return True
    return normalized == target or normalized.startswith(f"{target}-")


def _recommend_visual_analysis_model(
    models: list[dict[str, Any]],
    vram_bytes: int,
) -> str:
    candidates = [item for item in models if not item.get("specialized_ocr")]
    if not candidates:
        return ""

    if vram_bytes > 0:
        # Keep room for the projector, KV cache, desktop, and the lightweight reranker.
        fitting = [
            item
            for item in candidates
            if 0 < int(item.get("size") or 0) <= int(vram_bytes * 0.88)
        ]
        if fitting:
            candidates = fitting
        vram_gib = vram_bytes / _GIB
        preferences = next(
            values
            for minimum, values in _ANALYSIS_MODEL_PREFERENCES
            if vram_gib >= minimum
        )
    else:
        # With unknown hardware, retain the validated balanced default when installed.
        preferences = (
            "qwen3.5:9b",
            "gemma4:12b",
            "ministral-3:8b",
            "qwen3.5:4b",
            "ministral-3:3b",
        )

    for preference in preferences:
        match = next(
            (
                item
                for item in candidates
                if _model_matches_preference(str(item.get("name") or ""), preference)
            ),
            None,
        )
        if match:
            return str(match.get("name") or "")

    if vram_bytes > 0:
        return str(max(candidates, key=lambda item: int(item.get("size") or 0)).get("name") or "")
    return str(min(candidates, key=lambda item: int(item.get("size") or 0)).get("name") or "")


def _ollama_json(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float | int = 10,
) -> dict[str, Any]:
    return _open_url_json(
        f"{_ollama_url()}/{path.lstrip('/')}",
        method=method,
        json_payload=payload,
        headers=_ollama_headers(include_json_content_type=payload is not None),
        timeout=timeout,
        ssl_verify=_ollama_httpx_verify(),
    )


def _model_show_payload(base_url: str, name: str) -> dict[str, Any]:
    try:
        return _ollama_json(
            "/api/show", method="POST", payload={"model": name}, timeout=4
        )
    except Exception:
        logger.debug("Recovered from an error in _model_show_payload.", exc_info=True)
        return {}


def _vision_probe_image(code: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (512, 256), "white")
    ImageDraw.Draw(image).text(
        (256, 128),
        code,
        fill="black",
        font=ImageFont.load_default(size=150),
        anchor="mm",
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _vision_probe_key(model: str) -> str:
    digest = ""
    try:
        tags = _ollama_json("/api/tags", timeout=4)
        for item in tags.get("models", []):
            if isinstance(item, dict) and str(item.get("name") or "") == model:
                digest = str(item.get("digest") or "")
                break
    except Exception:
        logger.debug("Unable to read the Ollama model digest", exc_info=True)
    return f"{model}@{digest}"


def _read_vision_probe_cache() -> dict[str, bool]:
    path = Path(config.STORAGE_DIR) / "visual_analysis_cache" / _VISION_PROBE_FILENAME
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(cached, dict):
        return {}
    return {str(key): bool(value) for key, value in cached.items() if isinstance(value, bool)}


def _write_vision_probe_result(key: str, sees: bool) -> None:
    cached = _read_vision_probe_cache()
    cached[key] = sees
    try:
        (_cache_dir() / _VISION_PROBE_FILENAME).write_text(
            json.dumps(cached, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _probe_vision_model(model: str) -> bool | None:
    """Return whether the model reads a trivial image; None when inconclusive."""
    prompt = (
        "Text Recognition:"
        if model.split(":", 1)[0].lower() == "glm-ocr"
        else "Recopie le texte écrit sur cette image. Réponds uniquement par ce texte."
    )
    answered = False
    for code in _VISION_PROBE_CODES:
        payload = _ollama_json(
            "/api/generate",
            method="POST",
            payload={
                "model": model,
                "prompt": prompt,
                "images": [base64.b64encode(_vision_probe_image(code)).decode("ascii")],
                "stream": False,
                "think": False,
                # Probes run once per model digest; never keep two models resident.
                "keep_alive": 0,
                "options": {"temperature": 0, "num_predict": 96},
            },
            timeout=300,
        )
        response = str(payload.get("response") or "")
        if re.search(rf"(?<!\d){code}(?!\d)", response):
            return True
        answered = answered or bool(response.strip())
    return False if answered else None


def _ensure_models_can_see(models: Iterable[str]) -> None:
    """Refuse models that cannot perceive images instead of storing their guesses."""
    for model in dict.fromkeys(value for value in models if value):
        key = _vision_probe_key(model)
        with _VISION_PROBE_LOCK:
            sees = _vision_probe_memory.get(key)
            if sees is None:
                sees = _read_vision_probe_cache().get(key)
            if sees is None:
                try:
                    sees = _probe_vision_model(model)
                except Exception:
                    logger.debug("Vision probe failed for %s", model, exc_info=True)
                    sees = None
                if sees is not None:
                    _write_vision_probe_result(key, sees)
            if sees is not None:
                _vision_probe_memory[key] = sees
        if sees is False:
            raise VisualAnalysisError(f"vision_model_blind:{model}")


def list_installed_vision_models() -> dict[str, Any]:
    base_url = _ollama_url()
    try:
        tags = _ollama_json("/api/tags", timeout=4)
    except Exception as exc:
        logger.debug("Recovered from an error in list_installed_vision_models.", exc_info=True)
        return {
            "ok": False,
            "models": [],
            "recommended_model": RECOMMENDED_OCR_MODEL,
            "recommended_download_bytes": RECOMMENDED_OCR_DOWNLOAD_BYTES,
            "msg": f"Ollama inaccessible : {exc}",
        }

    raw_models = [item for item in tags.get("models", []) if isinstance(item, dict)]
    inspected: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(raw_models)))) as executor:
        futures = {
            executor.submit(_model_show_payload, base_url, str(item.get("name") or "")): item
            for item in raw_models
            if str(item.get("name") or "").strip()
        }
        for future in as_completed(futures):
            item = futures[future]
            name = str(item.get("name") or "").strip()
            inspected[name] = future.result()

    probe_results = {**_read_vision_probe_cache(), **_vision_probe_memory}
    models: list[dict[str, Any]] = []
    for item in raw_models:
        name = str(item.get("name") or "").strip()
        show = inspected.get(name, {})
        capabilities = [str(value).lower() for value in show.get("capabilities", [])]
        if "vision" not in capabilities:
            continue
        size = int(item.get("size") or 0)
        models.append(
            {
                "name": name,
                "size": size,
                "size_label": _human_size(size),
                "capabilities": capabilities,
                "specialized_ocr": name.split(":", 1)[0].lower() == "glm-ocr",
                "vision_blind": probe_results.get(f"{name}@{item.get('digest') or ''}") is False,
            }
        )
    models.sort(key=lambda item: (not item["specialized_ocr"], item["size"], item["name"]))
    vram_bytes, vram_source = _detect_total_gpu_vram()
    recommended_analysis_model = _recommend_visual_analysis_model(
        [item for item in models if not item["vision_blind"]],
        vram_bytes,
    )
    light_installed = any(
        _model_matches_preference(item["name"], LIGHT_ANALYSIS_MODEL) for item in models
    )
    return {
        "ok": True,
        "models": models,
        "recommended_model": RECOMMENDED_OCR_MODEL,
        "recommended_download_bytes": RECOMMENDED_OCR_DOWNLOAD_BYTES,
        "recommended_download_label": _human_size(RECOMMENDED_OCR_DOWNLOAD_BYTES),
        "recommended_analysis_model": recommended_analysis_model,
        "light_analysis_model": LIGHT_ANALYSIS_MODEL,
        "light_analysis_download_label": _human_size(LIGHT_ANALYSIS_DOWNLOAD_BYTES),
        "light_analysis_installed": light_installed,
        # Unknown VRAM counts as modest: offering 1.6 GB is safer than silence.
        "light_analysis_suggested": not light_installed
        and vram_bytes < _LIGHT_ANALYSIS_MAX_VRAM_BYTES,
        "gpu_vram_bytes": vram_bytes,
        "gpu_vram_label": _gpu_vram_label(vram_bytes),
        "gpu_vram_source": vram_source,
        "msg": (
            f"{len(models)} modèle(s) vision installé(s)."
            if models
            else "Aucun modèle vision installé. Aucun téléchargement n'a été lancé."
        ),
    }


def install_recommended_vision_model(model: str, *, confirmed: bool) -> dict[str, Any]:
    normalized = str(model or "").strip().lower().removesuffix(":latest")
    if not confirmed:
        raise VisualAnalysisError("confirmation_required")
    if normalized not in _ALLOWED_INSTALL_MODELS:
        raise VisualAnalysisError("model_not_allowed")
    base_url = _ollama_url()
    if not is_local_ollama_url(base_url):
        raise VisualAnalysisError("local_ollama_required")
    executable = find_ollama_executable()
    if not executable:
        raise VisualAnalysisError("ollama_not_installed")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        completed = subprocess.run(
            [executable, "pull", normalized],
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VisualAnalysisError(f"model_install_failed:{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "échec Ollama").strip()[-800:]
        raise VisualAnalysisError(f"model_install_failed:{detail}")
    # Ollama lists an untagged pull under ":latest"; return the name the UI will see.
    installed = normalized if ":" in normalized else f"{normalized}:latest"
    return {"ok": True, "model": installed, "msg": f"Modèle {installed} installé."}


def visual_analysis_model_settings() -> tuple[str, str, str]:
    settings = get_app_settings()
    mode = str(settings.get("visual_analysis_mode") or "off").strip().lower()
    ocr_model = str(settings.get("vision_model") or "").strip()
    analysis_model = str(settings.get("visual_description_model") or "").strip()
    return (
        mode if mode in {"off", "on_demand", "index"} else "off",
        ocr_model,
        analysis_model,
    )


def index_visual_signature() -> str:
    mode, ocr_model, analysis_model = visual_analysis_model_settings()
    if mode != "index" or not (ocr_model or analysis_model):
        return ""
    return (
        f"index-v{_VISUAL_PIPELINE_VERSION}:"
        f"ocr={ocr_model or '-'}:analysis={analysis_model or '-'}"
    )


def _visual_reference_keys(value: str) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for match in _VISUAL_REFERENCE_PATTERN.finditer(str(value or "")):
        raw_kind = match.group("kind").lower().rstrip(".")
        if raw_kind.startswith("fig"):
            kind = "figure"
        elif raw_kind.startswith("table"):
            kind = "table"
        elif raw_kind.startswith("graph"):
            kind = "graph"
        else:
            kind = "scheme"
        keys.add((kind, match.group("number"), (match.group("suffix") or "").lower()))
    return keys


def _caption_reference_keys(value: str) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for line in str(value or "").splitlines():
        match = _CAPTION_LINE_PATTERN.match(line)
        if match:
            keys.update(_visual_reference_keys(match.group("label")))
    return keys


def candidate_page_indexes(
    page_texts: Iterable[str],
    *,
    max_pages: int = 6,
    question: str = "",
) -> list[int]:
    texts = [str(text or "") for text in page_texts]
    candidates = [index for index, text in enumerate(texts) if _FIGURE_PATTERN.search(text)]
    requested = _visual_reference_keys(question)
    if not requested:
        return candidates[:max_pages]

    targeted_strict_captions: list[int] = []
    targeted_caption_candidates: list[int] = []
    targeted_mentions: list[int] = []
    for index, text in enumerate(texts):
        page_references = _visual_reference_keys(text)
        if page_references & requested:
            if _caption_reference_keys(text) & requested:
                targeted_strict_captions.append(index)
            elif index in candidates:
                targeted_caption_candidates.append(index)
            else:
                targeted_mentions.append(index)

    # Keep the usual caption-based fallback after exact question matches. This
    # still produces useful evidence when PDF text extraction misses the target
    # caption while preserving the global page budget.
    targeted = targeted_strict_captions + targeted_caption_candidates + targeted_mentions
    ordered = targeted + [index for index in candidates if index not in targeted]
    return ordered[:max_pages]


def _union_bbox(
    boxes: Iterable[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
    values = list(boxes)
    if not values:
        return None
    return (
        min(box[0] for box in values),
        min(box[1] for box in values),
        max(box[2] for box in values),
        max(box[3] for box in values),
    )


def _strip_unicode_noncharacters(value: str) -> str:
    cleaned: list[str] = []
    for character in str(value or ""):
        codepoint = ord(character)
        if character == "\ufffd":
            cleaned.append(" ")
            continue
        if 0xFDD0 <= codepoint <= 0xFDEF:
            continue
        if codepoint & 0xFFFF in {0xFFFE, 0xFFFF}:
            continue
        if codepoint < 32 and character not in {"\n", "\r", "\t"}:
            continue
        cleaned.append(character)
    return "".join(cleaned)


def _text_range_bbox(
    textpage: Any,
    text: str,
    start: int,
    end: int,
) -> tuple[float, float, float, float] | None:
    boxes: list[tuple[float, float, float, float]] = []
    for index in range(max(0, start), min(len(text), end)):
        if text[index].isspace():
            continue
        try:
            left, bottom, right, top = (float(value) for value in textpage.get_charbox(index))
        except Exception:
            logger.debug("Recovered from an error in _text_range_bbox.", exc_info=True)
            continue
        if right < left:
            left, right = right, left
        if top < bottom:
            bottom, top = top, bottom
        if right - left <= 0.0 and top - bottom <= 0.0:
            continue
        boxes.append((left, bottom, right, top))
    return _union_bbox(boxes)


def _caption_regions_from_text(value: str) -> list[_CaptionRegion]:
    captions: list[_CaptionRegion] = []
    lines = str(value or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        match = _CAPTION_LINE_PATTERN.match(line)
        if not match:
            continue
        parts = [line.strip()]
        for continuation in lines[index + 1 : index + 4]:
            normalized = continuation.strip()
            if not normalized or _CAPTION_LINE_PATTERN.match(normalized):
                break
            parts.append(normalized)
        caption_text = re.sub(
            r"\s+",
            " ",
            _strip_unicode_noncharacters(" ".join(parts)),
        ).strip()
        captions.append(
            _CaptionRegion(
                label=re.sub(r"\s+", " ", match.group("label")).strip(),
                text=caption_text,
            )
        )
    return captions


def _extract_caption_regions(textpage: Any) -> list[_CaptionRegion]:
    text = str(textpage.get_text_range() or "")
    if not text:
        return []
    line_records: list[tuple[str, int, int]] = []
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        line_records.append((content, offset, offset + len(content)))
        offset += len(raw_line)
    if offset < len(text):
        line_records.append((text[offset:], offset, len(text)))

    captions: list[_CaptionRegion] = []
    for line_index, (line, start, end) in enumerate(line_records):
        match = _CAPTION_LINE_PATTERN.match(line)
        if not match:
            continue
        line_bbox = _text_range_bbox(textpage, text, start, end)
        if line_bbox is None:
            continue
        parts = [line.strip()]
        boxes = [line_bbox]
        previous_bbox = line_bbox
        for continuation_line, continuation_start, continuation_end in line_records[
            line_index + 1 : line_index + 8
        ]:
            normalized = continuation_line.strip()
            if not normalized or _CAPTION_LINE_PATTERN.match(normalized):
                break
            continuation_bbox = _text_range_bbox(
                textpage,
                text,
                continuation_start,
                continuation_end,
            )
            if continuation_bbox is None:
                break
            vertical_gap = previous_bbox[1] - continuation_bbox[3]
            start_alignment = abs(continuation_bbox[0] - line_bbox[0])
            horizontal_overlap = max(
                0.0,
                min(continuation_bbox[2], line_bbox[2])
                - max(continuation_bbox[0], line_bbox[0]),
            )
            minimum_width = max(
                1.0,
                min(
                    continuation_bbox[2] - continuation_bbox[0],
                    line_bbox[2] - line_bbox[0],
                ),
            )
            previous_height = max(1.0, previous_bbox[3] - previous_bbox[1])
            maximum_line_gap = min(9.0, max(6.0, previous_height * 1.1))
            if not (-4.0 <= vertical_gap <= maximum_line_gap):
                break
            if start_alignment > 28.0 and horizontal_overlap / minimum_width < 0.45:
                break
            parts.append(normalized)
            boxes.append(continuation_bbox)
            previous_bbox = continuation_bbox
        caption_bbox = _union_bbox(boxes)
        caption_text = re.sub(
            r"\s+",
            " ",
            _strip_unicode_noncharacters(" ".join(parts)),
        ).strip()
        captions.append(
            _CaptionRegion(
                label=re.sub(r"\s+", " ", match.group("label")).strip(),
                text=caption_text,
                bbox=caption_bbox,
            )
        )
    return captions


def _caption_image_score(
    image_bbox: tuple[float, float, float, float],
    caption_bbox: tuple[float, float, float, float],
) -> float | None:
    image_left, image_bottom, image_right, image_top = image_bbox
    caption_left, caption_bottom, caption_right, caption_top = caption_bbox
    image_width = max(1.0, image_right - image_left)
    caption_width = max(1.0, caption_right - caption_left)
    horizontal_overlap = max(
        0.0,
        min(image_right, caption_right) - max(image_left, caption_left),
    )
    overlap_ratio = horizontal_overlap / min(image_width, caption_width)
    if overlap_ratio < 0.12:
        return None

    if caption_top <= image_bottom + 10.0:
        gap = max(0.0, image_bottom - caption_top)
        if gap > 150.0:
            return None
        position_penalty = 0.0
    elif caption_bottom >= image_top - 8.0:
        gap = max(0.0, caption_bottom - image_top)
        if gap > 80.0:
            return None
        position_penalty = 35.0
    else:
        return None
    return gap + (1.0 - overlap_ratio) * 100.0 + position_penalty


def _associate_embedded_captions(
    images: list[tuple[bytes, tuple[float, float, float, float], int]],
    captions: list[_CaptionRegion],
) -> list[_EmbeddedVisual]:
    scored_pairs: list[tuple[float, int, int]] = []
    for image_index, (_data, image_bbox, _pixel_area) in enumerate(images):
        for caption_index, caption in enumerate(captions):
            if caption.bbox is None:
                continue
            score = _caption_image_score(image_bbox, caption.bbox)
            if score is not None:
                scored_pairs.append((score, image_index, caption_index))
    scored_pairs.sort(key=lambda item: item[0])

    assigned_images: set[int] = set()
    assigned_captions: set[int] = set()
    caption_by_image: dict[int, _CaptionRegion] = {}
    for _score, image_index, caption_index in scored_pairs:
        if image_index in assigned_images or caption_index in assigned_captions:
            continue
        assigned_images.add(image_index)
        assigned_captions.add(caption_index)
        caption_by_image[image_index] = captions[caption_index]

    visuals: list[_EmbeddedVisual] = []
    has_geometric_captions = any(caption.bbox is not None for caption in captions)
    if not has_geometric_captions and len(images) == 1:
        figure_captions = [
            caption
            for caption in captions
            if any(kind != "table" for kind, _number, _suffix in caption.reference_keys)
        ]
        if len(figure_captions) == 1:
            caption_by_image[0] = figure_captions[0]
    largest_pixel_area = max((item[2] for item in images), default=0)
    for image_index, (data, bbox, pixel_area) in enumerate(images):
        matched_caption = caption_by_image.get(image_index)
        if (
            matched_caption is None
            and has_geometric_captions
            and pixel_area < largest_pixel_area * 0.45
        ):
            continue
        visuals.append(_EmbeddedVisual(image_bytes=data, bbox=bbox, caption=matched_caption))
    visuals.sort(key=lambda item: (-int(item.bbox[3] // 18.0), item.bbox[0]))
    return visuals[:_MAX_EMBEDDED_IMAGES_PER_PAGE]


def _page_object_bounds(image_object: Any) -> tuple[float, float, float, float]:
    left, bottom, right, top = (float(value) for value in image_object.get_bounds())
    bounds = (left, bottom, right, top)
    container = getattr(image_object, "container", None)
    while container is not None:
        left, bottom, right, top = (
            float(value) for value in container.get_matrix().on_rect(*bounds)
        )
        bounds = (left, bottom, right, top)
        container = getattr(container, "container", None)
    return bounds


@pdfium_locked
def _extract_page_visual_inputs(
    path: str,
    page_index: int,
    *,
    page_text: str = "",
    question: str = "",
) -> _PageVisualInputs:
    fallback_captions = _caption_regions_from_text(page_text)
    try:
        import pypdfium2 as pdfium
        from pypdfium2 import raw as pdfium_raw
    except ImportError:
        return _PageVisualInputs(embedded=(), captions=tuple(fallback_captions))

    document = None
    page = None
    textpage = None
    try:
        document = pdfium.PdfDocument(path)
        if page_index < 0 or page_index >= len(document):
            return _PageVisualInputs(embedded=(), captions=tuple(fallback_captions))
        page = document[page_index]
        page_width, page_height = (float(value) for value in page.get_size())
        try:
            textpage = page.get_textpage()
            captions = _extract_caption_regions(textpage) or fallback_captions
        except Exception:
            logger.debug("Recovered from an error in _extract_page_visual_inputs.", exc_info=True)
            captions = fallback_captions

        page_area = max(1.0, page_width * page_height)
        images: list[tuple[bytes, tuple[float, float, float, float], int]] = []
        for image_object in page.get_objects(filter=[pdfium_raw.FPDF_PAGEOBJ_IMAGE]):
            try:
                left, bottom, right, top = (
                    float(value) for value in _page_object_bounds(image_object)
                )
                if right < left:
                    left, right = right, left
                if top < bottom:
                    bottom, top = top, bottom
                displayed_area = max(0.0, right - left) * max(0.0, top - bottom)
                pixel_width, pixel_height = (
                    int(value) for value in image_object.get_px_size()
                )
                if (
                    pixel_width < 64
                    or pixel_height < 64
                    or displayed_area < page_area * 0.002
                    or displayed_area >= page_area * _SCANNED_PAGE_AREA_RATIO
                ):
                    continue
                buffer = io.BytesIO()
                image_object.extract(buffer, fb_format="png")
                image_bytes = buffer.getvalue()
                if not image_bytes:
                    continue
                images.append(
                    (
                        image_bytes,
                        (left, bottom, right, top),
                        pixel_width * pixel_height,
                    )
                )
            except Exception:
                logger.debug(
                    "Recovered from an error in _extract_page_visual_inputs.", exc_info=True
                )
                continue
        visuals = _associate_embedded_captions(images, list(captions))
        requested = _visual_reference_keys(question)
        if requested:
            targeted = [
                visual
                for visual in visuals
                if visual.caption is not None
                and visual.caption.reference_keys & requested
            ]
            visuals = targeted
        return _PageVisualInputs(
            embedded=tuple(visuals),
            captions=tuple(captions),
            page_size=(page_width, page_height),
        )
    except Exception:
        logger.debug("Recovered from an error in _extract_page_visual_inputs.", exc_info=True)
        return _PageVisualInputs(embedded=(), captions=tuple(fallback_captions))
    finally:
        if textpage is not None:
            try:
                textpage.close()
            except Exception:
                logger.debug("Unable to close the PDF text page", exc_info=True)
        if page is not None:
            try:
                page.close()
            except Exception:
                logger.debug("Unable to close the PDF page", exc_info=True)
        if document is not None:
            try:
                document.close()
            except Exception:
                logger.debug("Unable to close the PDF document", exc_info=True)


def _captions_for_question(
    captions: Iterable[_CaptionRegion],
    question: str,
) -> list[_CaptionRegion]:
    values = list(captions)
    requested = _visual_reference_keys(question)
    if not requested:
        return values
    targeted = [caption for caption in values if caption.reference_keys & requested]
    return targeted or values


def _cache_dir() -> Path:
    path = Path(config.STORAGE_DIR) / "visual_analysis_cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file_signature(path: str) -> str:
    stat = os.stat(path)
    raw = f"{os.path.abspath(path)}:{stat.st_size}:{stat.st_mtime_ns}".encode()
    return hashlib.sha256(raw).hexdigest()


def _cache_path(
    path: str,
    ocr_model: str,
    analysis_model: str,
    page_indexes: list[int],
) -> Path:
    key = json.dumps(
        {
            "file": _file_signature(path),
            "ocr_model": ocr_model,
            "analysis_model": analysis_model,
            "pages": page_indexes,
            "v": _VISUAL_PIPELINE_VERSION,
        },
        sort_keys=True,
    ).encode("utf-8")
    return _cache_dir() / f"{hashlib.sha256(key).hexdigest()}.json"


def _read_cached_visual_payload(
    path: str,
    ocr_model: str,
    analysis_model: str,
    page_indexes: list[int],
) -> dict[str, Any] | None:
    cache_path = _cache_path(path, ocr_model, analysis_model, page_indexes)
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cached, dict) or not isinstance(cached.get("results"), list):
        return None
    return {**cached, "cached": True}


def get_cached_pdf_visuals(
    path: str,
    *,
    model: str = "",
    analysis_model: str = "",
    page_texts: Iterable[str] | None = None,
    max_pages: int = 6,
    question: str = "",
) -> dict[str, Any] | None:
    """Return an existing visual analysis without invoking either visual model."""
    normalized_model = str(model or "").strip()
    normalized_analysis_model = str(analysis_model or "").strip()
    if not normalized_model and not normalized_analysis_model:
        raise VisualAnalysisError("vision_model_required")
    if not os.path.isfile(path) or Path(path).suffix.lower() != ".pdf":
        raise VisualAnalysisError("pdf_not_found")
    try:
        check_document(path)
    except DocumentRejected as exc:
        raise VisualAnalysisError("pdf_rejected") from exc
    texts = list(page_texts or [])
    if not texts:
        from pypdf import PdfReader

        reader = PdfReader(path)
        texts = [str(page.extract_text() or "") for page in reader.pages]
    pages = candidate_page_indexes(texts, max_pages=max_pages, question=question)
    if not pages:
        return None
    return _read_cached_visual_payload(
        path,
        normalized_model,
        normalized_analysis_model,
        pages,
    )


@pdfium_locked
def _render_page_png(path: str, page_index: int) -> bytes:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise VisualAnalysisError("pdf_renderer_missing") from exc
    document = pdfium.PdfDocument(path)
    try:
        if page_index < 0 or page_index >= len(document):
            raise VisualAnalysisError("invalid_page")
        page = document[page_index]
        try:
            image = page.render(scale=2.0).to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG", optimize=True)
            return buffer.getvalue()
        finally:
            page.close()
    finally:
        document.close()


def _mask_caption_regions(
    image_bytes: bytes,
    captions: Iterable[_CaptionRegion],
    page_size: tuple[float, float] | None,
) -> bytes:
    caption_boxes = [caption.bbox for caption in captions if caption.bbox is not None]
    if not caption_boxes or page_size is None:
        return image_bytes
    try:
        from PIL import Image, ImageDraw

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        page_width, page_height = page_size
        if page_width <= 0.0 or page_height <= 0.0:
            return image_bytes
        scale_x = image.width / page_width
        scale_y = image.height / page_height
        draw = ImageDraw.Draw(image)
        for left, bottom, right, top in caption_boxes:
            padding = 4
            pixel_box = (
                max(0, int(left * scale_x) - padding),
                max(0, int((page_height - top) * scale_y) - padding),
                min(image.width, int(right * scale_x) + padding),
                min(image.height, int((page_height - bottom) * scale_y) + padding),
            )
            draw.rectangle(pixel_box, fill="white")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()
    except Exception:
        logger.debug("Recovered from an error in _mask_caption_regions.", exc_info=True)
        return image_bytes


def _vision_prompt(model: str, page_number: int, *, isolated: bool = False) -> str:
    target = (
        "cette image de figure extraite du PDF"
        if isolated
        else f"cette page {page_number}"
    )
    if model.split(":", 1)[0].lower() == "glm-ocr":
        return (
            "Figure Recognition: Transcris toutes les figures, graphiques, tableaux et formules "
            f"visibles sur {target}. Préserve les axes, unités, séries et valeurs. "
            "Ne répète aucun bloc et arrête-toi après la dernière information lisible. "
            "Réponds en français, en Markdown, sans inventer de valeur illisible."
        )
    return (
        f"Transcris fidèlement les informations textuelles visibles sur {target} : axes, unités, "
        "graduations, noms des séries, annotations, formules et valeurs lisibles. "
        "Ne décris pas les tendances et n'interprète pas la figure. Ignore le texte courant sans "
        "rapport avec elle. Ne répète aucun bloc. Réponds en français, en Markdown, sans inventer "
        "de valeur illisible."
    )


def _visual_description_prompt(page_number: int, *, isolated: bool = False) -> str:
    target = (
        "cette image de figure extraite du PDF"
        if isolated
        else f"cette page {page_number}"
    )
    schema = json.dumps(_VISUAL_DESCRIPTION_SCHEMA, ensure_ascii=False)
    return (
        f"Observe directement les pixels de {target} et décris la figure scientifique. "
        "Ce travail est une interprétation visuelle, pas une transcription OCR. Identifie le type "
        "de figure, les axes et leurs échelles, les séries et leurs encodages, puis les tendances, "
        "maxima, minima, plateaux, ruptures, croisements, dispersion, barres d'erreur et comparaisons "
        "effectivement visibles. Pour un schéma, une photographie ou un tableau, adapte l'analyse à "
        "ce qui est observable. N'infère aucun mécanisme causal et n'utilise aucune légende externe. "
        "Ne calcule et n'extrapole aucune valeur. Recopie les bornes, exposants et lectures "
        "quantitatives uniquement s'ils sont visuellement non ambigus ; sinon omets-les. Ne conclus "
        "à un croisement, un ajustement ou une divergence que si cela est clairement visible. Place "
        "toute limite ou ambiguïté dans uncertainties. Réponds en français avec exactement ce "
        "schéma JSON : "
        f"{schema}"
    )


def _encode_vision_image(image_bytes: bytes) -> str:
    """Base64 image for Ollama, downscaled so full-page scans stay tractable."""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        if max(image.size) > _MAX_VISION_IMAGE_SIDE:
            rgb_image = image.convert("RGB")
            rgb_image.thumbnail((_MAX_VISION_IMAGE_SIDE, _MAX_VISION_IMAGE_SIDE))
            buffer = io.BytesIO()
            rgb_image.save(buffer, format="PNG", optimize=True)
            image_bytes = buffer.getvalue()
    except Exception:
        logger.debug("Unable to downscale the visual input", exc_info=True)
    return base64.b64encode(image_bytes).decode("ascii")


def _echo_key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`#>|:;.,()\[\]-]+", " ", value)).strip().lower()


def _is_prompt_echo(value: str, prompt: str) -> bool:
    key = _echo_key(value)
    return len(key) >= 12 and key in _echo_key(prompt)


def _strip_prompt_echo(value: str, prompt: str) -> str:
    """Drop lines a blind or weak model copied from its own instructions."""
    lines = str(value or "").splitlines()
    kept = [line for line in lines if not _is_prompt_echo(line, prompt)]
    return "\n".join(kept).strip()


def _collapse_repeated_cycles(lines: list[str]) -> list[str]:
    """Keep one copy of a block of lines that loops three times or more in a row."""
    keys = [re.sub(r"\s+", " ", line).strip().lower() for line in lines]
    collapsed: list[str] = []
    index = 0
    while index < len(lines):
        cycle: tuple[int, int] | None = None
        for period in range(1, min(40, (len(lines) - index) // 3) + 1):
            block = keys[index : index + period]
            if not any(block):
                continue
            repetitions = 1
            while keys[
                index + repetitions * period : index + (repetitions + 1) * period
            ] == block:
                repetitions += 1
            if repetitions >= 3:
                cycle = (period, repetitions)
                break
        if cycle is None:
            collapsed.append(lines[index])
            index += 1
            continue
        period, repetitions = cycle
        collapsed.extend(lines[index : index + period])
        index += period * repetitions
    return collapsed


def _dedupe_ocr_repetitions(value: str) -> str:
    text = _strip_unicode_noncharacters(str(value or "")).strip()
    if not text:
        return ""
    frequencies = Counter(
        line.lower()
        for line in (re.sub(r"\s+", " ", raw).strip() for raw in text.splitlines())
        if len(line) >= 8 and not line.startswith("```")
    )
    lines = _collapse_repeated_cycles(text.splitlines())
    normalized_lines = [re.sub(r"\s+", " ", line).strip() for line in lines]
    repeated = {line for line, count in frequencies.items() if count >= 3}
    seen_repeated: set[str] = set()
    cleaned: list[str] = []
    blank_pending = False
    for original, normalized in zip(lines, normalized_lines, strict=True):
        if normalized.startswith("```"):
            continue
        key = normalized.lower()
        if key in repeated:
            if key in seen_repeated:
                continue
            seen_repeated.add(key)
        if not normalized:
            blank_pending = bool(cleaned and cleaned[-1].strip())
            continue
        if blank_pending:
            cleaned.append("")
            blank_pending = False
        cleaned.append(original.rstrip())
    result = "\n".join(cleaned).strip()
    return result or text


def _compose_visual_text(
    ocr_text: str,
    captions: Iterable[_CaptionRegion],
    visual_analysis: str = "",
) -> str:
    parts: list[str] = []
    normalized_ocr = _dedupe_ocr_repetitions(ocr_text)
    if normalized_ocr:
        parts.append(f"### Lecture OCR de la figure\n\n{normalized_ocr}")
    normalized_analysis = str(visual_analysis or "").strip()
    if normalized_analysis:
        parts.append(f"### Analyse visuelle de la figure\n\n{normalized_analysis}")
    caption_values = [caption for caption in captions if caption.text.strip()]
    if caption_values:
        caption_lines = [f"- {caption.text.strip()}" for caption in caption_values]
        parts.append(
            "### Légende PDF ajoutée après les analyses\n\n" + "\n".join(caption_lines)
        )
    return "\n\n".join(parts).strip()


def _visual_description_markdown(response_text: str, prompt: str = "") -> str:
    text = str(response_text or "").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _dedupe_ocr_repetitions(text)
    if not isinstance(payload, dict):
        return _dedupe_ocr_repetitions(text)

    parts: list[str] = []
    figure_type = str(payload.get("figure_type") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    if figure_type:
        parts.append(f"**Type de figure :** {figure_type}")
    if summary:
        parts.append(summary)
    sections = (
        ("Axes et échelles", "axes"),
        ("Séries et encodages", "series"),
        ("Observations visuelles", "observations"),
        ("Lectures quantitatives", "quantitative_readings"),
        ("Limites et incertitudes", "uncertainties"),
    )
    for label, key in sections:
        raw_values = payload.get(key)
        values = (
            [str(value).strip() for value in raw_values if str(value).strip()]
            if isinstance(raw_values, list)
            else []
        )
        # Weak models pad lists with "Série 1…Série 20" or recite the prompt.
        values = [
            value
            for value in dict.fromkeys(values)
            if not _PLACEHOLDER_ITEM_PATTERN.match(value.rstrip(" .:"))
            and not (prompt and _is_prompt_echo(value, prompt))
        ][:_MAX_DESCRIPTION_ITEMS]
        if values:
            parts.append(f"**{label} :**\n" + "\n".join(f"- {value}" for value in values))
    return "\n\n".join(parts).strip() or _dedupe_ocr_repetitions(text)


def _invoke_vision(
    model: str,
    image_bytes: bytes,
    page_number: int,
    *,
    isolated: bool = False,
) -> str:
    prompt = _vision_prompt(model, page_number, isolated=isolated)
    payload = _ollama_json(
        "/api/generate",
        method="POST",
        payload={
            "model": model,
            "prompt": prompt,
            "images": [_encode_vision_image(image_bytes)],
            "stream": False,
            # Thinking models would spend the whole budget before transcribing.
            "think": False,
            "keep_alive": "2m",
            "options": {"temperature": 0, "num_predict": 900 if isolated else 1400},
        },
        timeout=600,
    )
    text = _strip_prompt_echo(str(payload.get("response") or ""), prompt)
    if not text:
        raise VisualAnalysisError("empty_vision_response")
    return _dedupe_ocr_repetitions(text)


def _invoke_visual_description(
    model: str,
    image_bytes: bytes,
    page_number: int,
    *,
    isolated: bool = False,
) -> str:
    prompt = _visual_description_prompt(page_number, isolated=isolated)
    payload = _ollama_json(
        "/api/generate",
        method="POST",
        payload={
            "model": model,
            "prompt": prompt,
            "images": [_encode_vision_image(image_bytes)],
            "format": _VISUAL_DESCRIPTION_SCHEMA,
            "stream": False,
            "think": False,
            "keep_alive": "2m",
            "options": {"temperature": 0, "num_predict": 1200},
        },
        timeout=600,
    )
    text = str(payload.get("response") or "").strip()
    if not text:
        raise VisualAnalysisError("empty_visual_description_response")
    return _visual_description_markdown(text, prompt)


def analyze_pdf_visuals(
    path: str,
    *,
    model: str = "",
    analysis_model: str = "",
    page_texts: Iterable[str] | None = None,
    max_pages: int = 6,
    question: str = "",
) -> dict[str, Any]:
    normalized_model = str(model or "").strip()
    normalized_analysis_model = str(analysis_model or "").strip()
    if not normalized_model and not normalized_analysis_model:
        raise VisualAnalysisError("vision_model_required")
    if not os.path.isfile(path) or Path(path).suffix.lower() != ".pdf":
        raise VisualAnalysisError("pdf_not_found")
    try:
        check_document(path)
    except DocumentRejected as exc:
        raise VisualAnalysisError("pdf_rejected") from exc
    texts = list(page_texts or [])
    if not texts:
        from pypdf import PdfReader

        reader = PdfReader(path)
        texts = [str(page.extract_text() or "") for page in reader.pages]
    pages = candidate_page_indexes(texts, max_pages=max_pages, question=question)
    if not pages:
        return {
            "ok": True,
            "model": normalized_model or normalized_analysis_model,
            "ocr_model": normalized_model,
            "visual_analysis_model": normalized_analysis_model,
            "cached": False,
            "pages": [],
            "results": [],
        }
    cache_path = _cache_path(path, normalized_model, normalized_analysis_model, pages)
    cached = _read_cached_visual_payload(
        path,
        normalized_model,
        normalized_analysis_model,
        pages,
    )
    if cached is not None:
        return cached

    jobs: list[_VisualJob] = []
    for page_index in pages:
        page_number = page_index + 1
        page_text = texts[page_index] if page_index < len(texts) else ""
        visual_inputs = _extract_page_visual_inputs(
            path,
            page_index,
            page_text=page_text,
            question=question,
        )
        if visual_inputs.embedded:
            for visual in visual_inputs.embedded:
                job_captions = (visual.caption,) if visual.caption is not None else ()
                jobs.append(
                    _VisualJob(
                        page_number=page_number,
                        image_bytes=visual.image_bytes,
                        captions=job_captions,
                        figure_label=visual.caption.label if visual.caption else "",
                        visual_source="embedded_image",
                        isolated=True,
                    )
                )
            continue

        page_captions = tuple(_captions_for_question(visual_inputs.captions, question))
        image_bytes = _render_page_png(path, page_index)
        jobs.append(
            _VisualJob(
                page_number=page_number,
                image_bytes=_mask_caption_regions(
                    image_bytes,
                    page_captions,
                    visual_inputs.page_size,
                ),
                captions=page_captions,
                figure_label=", ".join(caption.label for caption in page_captions),
                visual_source="page_render_fallback",
                isolated=False,
            )
        )

    errors: list[dict[str, Any]] = []
    with _VISION_LOCK:
        if jobs:
            _ensure_models_can_see((normalized_model, normalized_analysis_model))
        if normalized_model:
            for job in jobs:
                try:
                    if job.isolated:
                        job.ocr_text = _invoke_vision(
                            normalized_model,
                            job.image_bytes,
                            job.page_number,
                            isolated=True,
                        )
                    else:
                        job.ocr_text = _invoke_vision(
                            normalized_model,
                            job.image_bytes,
                            job.page_number,
                        )
                except Exception as exc:
                    logger.debug("Recovered from an error in analyze_pdf_visuals.", exc_info=True)
                    job.ocr_error = str(exc) or type(exc).__name__
                    errors.append(
                        {"page": job.page_number, "stage": "ocr", "error": job.ocr_error}
                    )
            if normalized_model != normalized_analysis_model:
                unload_ollama_model(normalized_model, timeout_seconds=2, base_url=_ollama_url())

        if normalized_analysis_model:
            for job in jobs:
                try:
                    if job.isolated:
                        job.visual_analysis = _invoke_visual_description(
                            normalized_analysis_model,
                            job.image_bytes,
                            job.page_number,
                            isolated=True,
                        )
                    else:
                        job.visual_analysis = _invoke_visual_description(
                            normalized_analysis_model,
                            job.image_bytes,
                            job.page_number,
                        )
                except Exception as exc:
                    logger.debug("Recovered from an error in analyze_pdf_visuals.", exc_info=True)
                    job.visual_analysis_error = str(exc) or type(exc).__name__
                    errors.append(
                        {
                            "page": job.page_number,
                            "stage": "visual_analysis",
                            "error": job.visual_analysis_error,
                        }
                    )
            unload_ollama_model(normalized_analysis_model, timeout_seconds=2, base_url=_ollama_url())

    results: list[dict[str, Any]] = []
    for job in jobs:
        text = _compose_visual_text(job.ocr_text, job.captions, job.visual_analysis)
        if not text:
            continue
        results.append(
            {
                "page": job.page_number,
                "text": text,
                "ocr_text": job.ocr_text,
                "visual_analysis": job.visual_analysis,
                "caption": "\n".join(caption.text for caption in job.captions),
                "figure_label": job.figure_label,
                "visual_source": job.visual_source,
                "ocr_model": normalized_model,
                "visual_analysis_model": normalized_analysis_model,
                "ocr_error": job.ocr_error,
                "visual_analysis_error": job.visual_analysis_error,
            }
        )
    payload = {
        "ok": True,
        "model": normalized_model or normalized_analysis_model,
        "ocr_model": normalized_model,
        "visual_analysis_model": normalized_analysis_model,
        "cached": False,
        "partial": bool(errors),
        "errors": errors,
        "pages": [index + 1 for index in pages],
        "results": results,
    }
    if not errors:
        try:
            cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass
    return payload


def results_as_markdown(payload: dict[str, Any]) -> str:
    results = payload.get("results") if isinstance(payload, dict) else []
    if not isinstance(results, list) or not results:
        return "Aucune figure légendée n'a été détectée dans les pages analysables."
    model_labels = [
        str(payload.get("ocr_model") or "").strip(),
        str(payload.get("visual_analysis_model") or "").strip(),
    ]
    models = " + ".join(dict.fromkeys(model for model in model_labels if model))
    parts = [f"## Analyse visuelle — {models or payload.get('model', '')}"]
    for item in results:
        if not isinstance(item, dict):
            continue
        parts.append(f"### Page {item.get('page', '?')}\n\n{str(item.get('text') or '').strip()}")
    return "\n\n".join(parts).strip()
