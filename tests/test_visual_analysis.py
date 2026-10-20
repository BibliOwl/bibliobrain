from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from conftest import read_app_javascript
from langchain_core.documents import Document
from PIL import Image
from pypdf import PdfWriter

import src.visual_analysis as visual_analysis


@pytest.fixture(autouse=True)
def _skip_vision_probe(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visual_analysis, "_vision_probe_memory", {})
    if not request.node.name.startswith("test_vision_probe_"):
        monkeypatch.setattr(visual_analysis, "_ensure_models_can_see", lambda _models: None)


def test_candidate_pages_are_targeted_and_limited() -> None:
    pages = [
        "Introduction without visuals",
        "Figure 1 Experimental current as a function of field",
        "body\nTable 2 Parameters",
        "Fig. 3 Comparison",
    ]

    assert visual_analysis.candidate_page_indexes(pages, max_pages=2) == [1, 2]


def test_candidate_pages_prioritize_figure_named_in_question() -> None:
    pages = [
        "Figure 1. Overview",
        "Table 2. Parameters",
        "Figure 8b. Detailed loss curve",
        "Figure 9. Comparison",
    ]

    assert visual_analysis.candidate_page_indexes(
        pages,
        max_pages=3,
        question="Que montre la figure 8b ?",
    ) == [2, 0, 1]


def test_candidate_pages_prefer_caption_over_earlier_textual_mention() -> None:
    pages = [
        "Figure 1 shows the trend discussed in the next page.",
        "Results\nFigure 1. Measured and simulated losses.",
        "Figure 2. Other results.",
    ]

    assert visual_analysis.candidate_page_indexes(
        pages,
        max_pages=2,
        question="Que montre la figure 1 ?",
    ) == [1, 0]


def test_index_visual_signature_changes_for_hybrid_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        visual_analysis,
        "get_app_settings",
        lambda: {
            "visual_analysis_mode": "index",
            "vision_model": "glm-ocr:q8_0",
            "visual_description_model": "vision:2b",
        },
    )

    assert visual_analysis.index_visual_signature() == (
        "index-v4:ocr=glm-ocr:q8_0:analysis=vision:2b"
    )


def test_embedded_images_are_ocrd_without_caption_then_combined_afterward(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = workspace_tmp_dir / "embedded.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(visual_analysis.config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    caption = visual_analysis._CaptionRegion(
        label="Figure 1",
        text="Figure 1. Courbe des pertes mesurées.",
        bbox=(20.0, 30.0, 200.0, 50.0),
    )
    monkeypatch.setattr(
        visual_analysis,
        "_extract_page_visual_inputs",
        lambda *_args, **_kwargs: visual_analysis._PageVisualInputs(
            embedded=(
                visual_analysis._EmbeddedVisual(
                    image_bytes=b"native-image",
                    bbox=(20.0, 60.0, 200.0, 180.0),
                    caption=caption,
                ),
            ),
            captions=(caption,),
            page_size=(300.0, 200.0),
        ),
    )
    monkeypatch.setattr(
        visual_analysis,
        "_render_page_png",
        lambda *_args: (_ for _ in ()).throw(AssertionError("page rendering must not run")),
    )
    captured: dict[str, Any] = {}

    def fake_invoke(
        model: str,
        image: bytes,
        page: int,
        *,
        isolated: bool = False,
    ) -> str:
        captured.update(model=model, image=image, page=page, isolated=isolated)
        return "Axe x : champ magnétique."

    monkeypatch.setattr(visual_analysis, "_invoke_vision", fake_invoke)
    monkeypatch.setattr(visual_analysis, "unload_ollama_model", lambda *args, **kwargs: True)

    payload = visual_analysis.analyze_pdf_visuals(
        str(pdf_path),
        model="vision:2b",
        page_texts=["Figure 1. Courbe des pertes mesurées."],
    )

    result = payload["results"][0]
    assert captured == {
        "model": "vision:2b",
        "image": b"native-image",
        "page": 1,
        "isolated": True,
    }
    assert result["visual_source"] == "embedded_image"
    assert result["figure_label"] == "Figure 1"
    assert result["caption"] == caption.text
    assert result["text"].index("Axe x") < result["text"].index("Légende PDF")
    assert caption.text in result["text"]


def test_page_fallback_masks_caption_before_ocr_and_appends_it_afterward(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = workspace_tmp_dir / "vector.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(visual_analysis.config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    caption = visual_analysis._CaptionRegion(
        label="Table 2",
        text="Table 2. Paramètres du modèle.",
        bbox=(20.0, 30.0, 200.0, 50.0),
    )
    monkeypatch.setattr(
        visual_analysis,
        "_extract_page_visual_inputs",
        lambda *_args, **_kwargs: visual_analysis._PageVisualInputs(
            embedded=(),
            captions=(caption,),
            page_size=(300.0, 200.0),
        ),
    )
    monkeypatch.setattr(visual_analysis, "_render_page_png", lambda *_args: b"rendered-page")
    masked: dict[str, Any] = {}

    def fake_mask(image: bytes, captions: Any, page_size: Any) -> bytes:
        masked.update(image=image, captions=list(captions), page_size=page_size)
        return b"page-without-caption"

    monkeypatch.setattr(visual_analysis, "_mask_caption_regions", fake_mask)
    captured: dict[str, Any] = {}

    def fake_invoke(
        model: str,
        image: bytes,
        page: int,
        *,
        isolated: bool = False,
    ) -> str:
        captured.update(image=image, isolated=isolated)
        return "Valeurs du tableau."

    monkeypatch.setattr(visual_analysis, "_invoke_vision", fake_invoke)
    monkeypatch.setattr(visual_analysis, "unload_ollama_model", lambda *args, **kwargs: True)

    payload = visual_analysis.analyze_pdf_visuals(
        str(pdf_path),
        model="vision:2b",
        page_texts=["Table 2. Paramètres du modèle."],
    )

    result = payload["results"][0]
    assert masked == {
        "image": b"rendered-page",
        "captions": [caption],
        "page_size": (300.0, 200.0),
    }
    assert captured == {"image": b"page-without-caption", "isolated": False}
    assert result["visual_source"] == "page_render_fallback"
    assert result["caption"] == caption.text


def test_same_caption_free_figure_is_sent_to_ocr_and_visual_understanding(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = workspace_tmp_dir / "dual-pass.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(visual_analysis.config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    caption = visual_analysis._CaptionRegion(
        label="Figure 3",
        text="Figure 3. Comparison between measurement and simulation.",
    )
    monkeypatch.setattr(
        visual_analysis,
        "_extract_page_visual_inputs",
        lambda *_args, **_kwargs: visual_analysis._PageVisualInputs(
            embedded=(
                visual_analysis._EmbeddedVisual(
                    image_bytes=b"same-native-image",
                    bbox=(10.0, 20.0, 200.0, 180.0),
                    caption=caption,
                ),
            ),
            captions=(caption,),
            page_size=(300.0, 200.0),
        ),
    )
    calls: list[tuple[str, str, bytes, bool]] = []

    def fake_ocr(
        model: str,
        image: bytes,
        _page: int,
        *,
        isolated: bool = False,
    ) -> str:
        calls.append(("ocr", model, image, isolated))
        return "Axe y : pertes (W/m)."

    def fake_description(
        model: str,
        image: bytes,
        _page: int,
        *,
        isolated: bool = False,
    ) -> str:
        calls.append(("analysis", model, image, isolated))
        return "La simulation suit les mesures puis les surestime."

    monkeypatch.setattr(visual_analysis, "_invoke_vision", fake_ocr)
    monkeypatch.setattr(visual_analysis, "_invoke_visual_description", fake_description)
    monkeypatch.setattr(visual_analysis, "unload_ollama_model", lambda *args, **kwargs: True)

    payload = visual_analysis.analyze_pdf_visuals(
        str(pdf_path),
        model="glm-ocr:q8_0",
        analysis_model="vision:9b",
        page_texts=[caption.text],
    )

    assert calls == [
        ("ocr", "glm-ocr:q8_0", b"same-native-image", True),
        ("analysis", "vision:9b", b"same-native-image", True),
    ]
    result = payload["results"][0]
    assert result["ocr_model"] == "glm-ocr:q8_0"
    assert result["visual_analysis_model"] == "vision:9b"
    assert result["text"].index("Lecture OCR") < result["text"].index("Analyse visuelle")
    assert result["text"].index("Analyse visuelle") < result["text"].index("Légende PDF")
    assert payload["partial"] is False


def test_visual_description_json_is_rendered_as_rag_markdown() -> None:
    response = """{
      "figure_type": "graphique en courbes",
      "summary": "Deux séries augmentent avec la fréquence.",
      "axes": ["x: fréquence (Hz)", "y: pertes (W/m)"],
      "series": ["mesure rouge", "simulation bleue"],
      "observations": ["la simulation surestime les hautes fréquences"],
      "quantitative_readings": ["croisement vers 40 Hz"],
      "uncertainties": ["graduations partiellement illisibles"]
    }"""

    markdown = visual_analysis._visual_description_markdown(response)

    assert "graphique en courbes" in markdown
    assert "Observations visuelles" in markdown
    assert "croisement vers 40 Hz" in markdown
    assert "graduations partiellement illisibles" in markdown


def test_visual_description_disables_thinking_and_uses_json_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_ollama_json(*_args: Any, **kwargs: Any) -> dict[str, str]:
        captured.update(kwargs["payload"])
        return {
            "response": (
                '{"figure_type":"graphique","summary":"croissant",'
                '"axes":[],"series":[],"observations":[],'
                '"quantitative_readings":[],"uncertainties":[]}'
            )
        }

    monkeypatch.setattr(visual_analysis, "_ollama_json", fake_ollama_json)

    result = visual_analysis._invoke_visual_description(
        "vision:9b",
        b"image",
        4,
        isolated=True,
    )

    assert captured["think"] is False
    assert captured["format"] == visual_analysis._VISUAL_DESCRIPTION_SCHEMA
    assert captured["options"]["temperature"] == 0
    assert "croissant" in result


def test_single_raster_uses_only_non_table_caption_without_geometry() -> None:
    figure_caption = visual_analysis._CaptionRegion(
        label="Figure 1",
        text="Figure 1. Measured losses.",
    )
    table_caption = visual_analysis._CaptionRegion(
        label="Table 1",
        text="Table 1. Machine parameters.",
    )

    visuals = visual_analysis._associate_embedded_captions(
        [(b"graph", (10.0, 20.0, 200.0, 180.0), 120_000)],
        [figure_caption, table_caption],
    )

    assert len(visuals) == 1
    assert visuals[0].caption == figure_caption


def test_repeated_ocr_lines_are_kept_once() -> None:
    repeated = "```markdown\n" + "\n```\n".join(
        ["metal | HTS | substrate\ufffe"] * 12
    )

    cleaned = visual_analysis._dedupe_ocr_repetitions(repeated)

    assert cleaned == "metal | HTS | substrate"


def test_looping_ocr_blocks_of_short_lines_are_kept_once() -> None:
    units = ["mm", "cm", "kg", "g", "mg", "µm", "nm", "Å"]

    cleaned = visual_analysis._dedupe_ocr_repetitions("\n".join(["Unités"] + units * 20))

    assert cleaned.splitlines() == ["Unités", *units]


def test_prompt_echo_is_removed_from_ocr_text() -> None:
    prompt = visual_analysis._vision_prompt("vision:2b", 4)
    response = (
        "Panneau 1 :\n"
        "axes, unités, graduations, noms des séries, annotations, formules et valeurs lisibles.\n"
        "Axe x : champ magnétique (T)"
    )

    cleaned = visual_analysis._strip_prompt_echo(response, prompt)

    assert cleaned == "Panneau 1 :\nAxe x : champ magnétique (T)"


def test_visual_description_drops_placeholder_series() -> None:
    response = json.dumps(
        {
            "figure_type": "graphique en courbes",
            "summary": "Quatre panneaux.",
            "axes": ["Axe des lignes", "horizontal axis", "Y", "x : champ magnétique (T)"],
            "series": [f"Série {index}" for index in range(1, 21)] + ["4,2 K", "20 K"],
            "observations": ["Ic décroît avec le champ."],
            "quantitative_readings": [],
            "uncertainties": [],
        }
    )

    markdown = visual_analysis._visual_description_markdown(response)

    assert "Série 1" not in markdown
    assert "Axe des lignes" not in markdown
    assert "horizontal axis" not in markdown
    assert "- Y\n" not in markdown
    assert "- 4,2 K" in markdown
    assert "- x : champ magnétique (T)" in markdown


def test_vision_probe_refuses_blind_model_and_remembers_it(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(visual_analysis.config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    calls: list[str] = []

    def fake_ollama_json(path: str, **kwargs: Any) -> dict[str, Any]:
        if path == "/api/tags":
            return {"models": [{"name": "blind:2b", "digest": "abc"}]}
        calls.append(kwargs["payload"]["model"])
        # A blind model recites its instructions instead of reading the digits.
        return {"response": "Recopie le texte écrit sur cette image."}

    monkeypatch.setattr(visual_analysis, "_ollama_json", fake_ollama_json)

    for _attempt in range(2):
        with pytest.raises(visual_analysis.VisualAnalysisError, match="vision_model_blind:blind:2b"):
            visual_analysis._ensure_models_can_see(["blind:2b"])

    assert calls == ["blind:2b", "blind:2b"]
    cached = json.loads(
        (workspace_tmp_dir / "storage" / "visual_analysis_cache" / "vision_probe.json").read_text(
            encoding="utf-8"
        )
    )
    assert cached == {"blind:2b@abc": False}


def test_vision_probe_accepts_seeing_model_and_skips_inconclusive_answers(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(visual_analysis.config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    answers = {"sees:4b": ["73"], "mute:4b": ["", ""]}

    def fake_ollama_json(path: str, **kwargs: Any) -> dict[str, Any]:
        if path == "/api/tags":
            return {"models": []}
        return {"response": answers[kwargs["payload"]["model"]].pop(0)}

    monkeypatch.setattr(visual_analysis, "_ollama_json", fake_ollama_json)

    visual_analysis._ensure_models_can_see(["sees:4b", "mute:4b", ""])

    assert visual_analysis._vision_probe_memory == {"sees:4b@": True}


def test_caption_mask_whitens_only_caption_region() -> None:
    source = Image.new("RGB", (200, 100), "black")
    buffer = io.BytesIO()
    source.save(buffer, format="PNG")
    caption = visual_analysis._CaptionRegion(
        label="Figure 1",
        text="Figure 1. Test",
        bbox=(50.0, 10.0, 150.0, 30.0),
    )

    masked = visual_analysis._mask_caption_regions(
        buffer.getvalue(),
        [caption],
        (200.0, 100.0),
    )
    image = Image.open(io.BytesIO(masked)).convert("RGB")

    assert image.getpixel((100, 80)) == (255, 255, 255)
    assert image.getpixel((10, 10)) == (0, 0, 0)


def test_list_installed_vision_models_filters_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(visual_analysis, "_ollama_url", lambda: "http://localhost:11434")

    def fake_ollama_json(
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float | int = 10,
    ) -> dict[str, Any]:
        if path == "/api/tags":
            return {
                "models": [
                    {"name": "text:1b", "size": 1_000_000_000},
                    {"name": "vision:2b", "size": 2_000_000_000},
                    {"name": "glm-ocr:q8_0", "size": 1_600_000_000},
                ]
            }
        name = str(payload.get("model") if payload else "")
        return {"capabilities": ["completion", "vision"] if name != "text:1b" else ["completion"]}

    monkeypatch.setattr(visual_analysis, "_ollama_json", fake_ollama_json)
    monkeypatch.setattr(visual_analysis, "_detect_total_gpu_vram", lambda: (8 * 1024**3, "test"))

    result = visual_analysis.list_installed_vision_models()

    assert result["ok"] is True
    assert [item["name"] for item in result["models"]] == ["glm-ocr:q8_0", "vision:2b"]
    assert result["models"][0]["specialized_ocr"] is True
    assert result["recommended_analysis_model"] == "vision:2b"
    assert result["gpu_vram_label"] == "8 Go"


def test_models_that_failed_the_vision_probe_are_never_recommended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(visual_analysis, "_ollama_url", lambda: "http://localhost:11434")
    monkeypatch.setattr(visual_analysis, "_vision_probe_memory", {"gemma4:e2b@d1": False})

    def fake_ollama_json(path: str, **kwargs: Any) -> dict[str, Any]:
        if path == "/api/tags":
            return {
                "models": [
                    {"name": "gemma4:e2b", "size": 2_000_000_000, "digest": "d1"},
                    {"name": "qwen3.5:4b", "size": 3_300_000_000, "digest": "d2"},
                ]
            }
        return {"capabilities": ["completion", "vision"]}

    monkeypatch.setattr(visual_analysis, "_ollama_json", fake_ollama_json)
    monkeypatch.setattr(visual_analysis, "_detect_total_gpu_vram", lambda: (4 * 1024**3, "test"))

    result = visual_analysis.list_installed_vision_models()

    blind = {item["name"]: item["vision_blind"] for item in result["models"]}
    assert blind == {"gemma4:e2b": True, "qwen3.5:4b": False}
    assert result["recommended_analysis_model"] == "qwen3.5:4b"


def test_visual_model_recommendation_uses_installed_models_and_vram() -> None:
    models = [
        {"name": "glm-ocr:q8_0", "size": 1_600_000_000, "specialized_ocr": True},
        {"name": "qwen3.5:4b", "size": 3_300_000_000, "specialized_ocr": False},
        {"name": "qwen3.5:9b", "size": 6_600_000_000, "specialized_ocr": False},
        {"name": "gemma4:12b", "size": 7_600_000_000, "specialized_ocr": False},
        {"name": "gemma4:26b", "size": 17_000_000_000, "specialized_ocr": False},
    ]

    assert visual_analysis._recommend_visual_analysis_model(models, 6 * 1024**3) == "qwen3.5:4b"
    assert visual_analysis._recommend_visual_analysis_model(models, 12 * 1024**3) == "qwen3.5:9b"
    assert visual_analysis._recommend_visual_analysis_model(models, 16 * 1024**3) == "gemma4:12b"
    assert visual_analysis._recommend_visual_analysis_model(models, 24 * 1024**3) == "gemma4:26b"


def test_small_gpu_recommendation_prefers_light_model_that_sees() -> None:
    models = [
        {"name": "gemma4:e2b", "size": 7_200_000_000, "specialized_ocr": False},
        {"name": "minicpm-v4.6:latest", "size": 1_600_000_000, "specialized_ocr": False},
    ]

    assert (
        visual_analysis._recommend_visual_analysis_model(models, 4 * 1024**3)
        == "minicpm-v4.6:latest"
    )


def test_visual_model_recommendation_preserves_balanced_default_when_vram_unknown() -> None:
    models = [
        {"name": "tiny-vision:2b", "size": 2_000_000_000, "specialized_ocr": False},
        {"name": "qwen3.5:9b", "size": 6_600_000_000, "specialized_ocr": False},
    ]

    assert visual_analysis._recommend_visual_analysis_model(models, 0) == "qwen3.5:9b"


def test_light_analysis_model_is_suggested_only_on_modest_machines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(visual_analysis, "_ollama_url", lambda: "http://localhost:11434")
    installed: list[dict[str, Any]] = [{"name": "glm-ocr:q8_0", "size": 1_600_000_000}]

    def fake_ollama_json(path: str, **kwargs: Any) -> dict[str, Any]:
        if path == "/api/tags":
            return {"models": installed}
        return {"capabilities": ["completion", "vision"]}

    monkeypatch.setattr(visual_analysis, "_ollama_json", fake_ollama_json)

    def listing(vram_gib: int) -> dict[str, Any]:
        monkeypatch.setattr(
            visual_analysis, "_detect_total_gpu_vram", lambda: (vram_gib * 1024**3, "test")
        )
        return visual_analysis.list_installed_vision_models()

    assert listing(4)["light_analysis_suggested"] is True
    assert listing(0)["light_analysis_suggested"] is True
    assert listing(16)["light_analysis_suggested"] is False
    installed.append({"name": "minicpm-v4.6:latest", "size": 1_600_000_000})
    modest = listing(4)
    assert modest["light_analysis_installed"] is True
    assert modest["light_analysis_suggested"] is False
    assert modest["recommended_analysis_model"] == "minicpm-v4.6:latest"


def test_light_analysis_model_can_be_installed_and_reports_listed_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pulled: list[list[str]] = []
    monkeypatch.setattr(visual_analysis, "_ollama_url", lambda: "http://localhost:11434")
    monkeypatch.setattr(visual_analysis, "find_ollama_executable", lambda: "ollama")

    def fake_run(command: list[str], **_kwargs: Any) -> Any:
        pulled.append(command)
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(visual_analysis.subprocess, "run", fake_run)

    result = visual_analysis.install_recommended_vision_model("minicpm-v4.6", confirmed=True)

    assert pulled == [["ollama", "pull", "minicpm-v4.6"]]
    assert result["model"] == "minicpm-v4.6:latest"
    with pytest.raises(visual_analysis.VisualAnalysisError, match="model_not_allowed"):
        visual_analysis.install_recommended_vision_model("gemma4:e2b", confirmed=True)


def test_install_requires_explicit_confirmation() -> None:
    with pytest.raises(visual_analysis.VisualAnalysisError, match="confirmation_required"):
        visual_analysis.install_recommended_vision_model("glm-ocr:q8_0", confirmed=False)


def test_visual_analysis_is_cached(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = workspace_tmp_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(visual_analysis.config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    monkeypatch.setattr(visual_analysis, "_render_page_png", lambda path, page: b"png")
    calls: list[int] = []

    def fake_invoke(model: str, image: bytes, page: int) -> str:
        calls.append(page)
        return f"Axes and values on page {page}"

    monkeypatch.setattr(visual_analysis, "_invoke_vision", fake_invoke)
    monkeypatch.setattr(visual_analysis, "unload_ollama_model", lambda *args, **kwargs: True)
    texts = ["Introduction", "Figure 1. Measured losses"]

    first = visual_analysis.analyze_pdf_visuals(
        str(pdf_path), model="vision:2b", page_texts=texts
    )
    second = visual_analysis.analyze_pdf_visuals(
        str(pdf_path), model="vision:2b", page_texts=texts
    )

    assert first["cached"] is False
    assert second["cached"] is True
    assert calls == [2]
    assert "Page 2" in visual_analysis.results_as_markdown(second)


def test_cached_visual_analysis_reader_never_invokes_models(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = workspace_tmp_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-test")
    monkeypatch.setattr(visual_analysis.config, "STORAGE_DIR", str(workspace_tmp_dir / "storage"))
    page_texts = ["Introduction", "Figure 1. Measured losses"]
    cache_path = visual_analysis._cache_path(str(pdf_path), "vision:2b", "", [1])
    cache_path.write_text(
        json.dumps(
            {
                "ok": True,
                "model": "vision:2b",
                "ocr_model": "vision:2b",
                "visual_analysis_model": "",
                "cached": False,
                "pages": [2],
                "results": [{"page": 2, "text": "Axes and values."}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        visual_analysis,
        "_invoke_vision",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model invoked")),
    )
    monkeypatch.setattr(
        visual_analysis,
        "_invoke_visual_description",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model invoked")),
    )

    cached = visual_analysis.get_cached_pdf_visuals(
        str(pdf_path),
        model="vision:2b",
        page_texts=page_texts,
    )
    missing = visual_analysis.get_cached_pdf_visuals(
        str(pdf_path),
        model="other:2b",
        page_texts=page_texts,
    )

    assert cached is not None
    assert cached["cached"] is True
    assert cached["results"][0]["text"] == "Axes and values."
    assert missing is None


def test_pdf_page_renderer_returns_png(workspace_tmp_dir: Path) -> None:
    pdf_path = workspace_tmp_dir / "render.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=200)
    with pdf_path.open("wb") as handle:
        writer.write(handle)

    rendered = visual_analysis._render_page_png(str(pdf_path), 0)

    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")


def test_visual_analysis_ui_is_opt_in() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "static" / "index.html").read_text(encoding="utf-8")
    javascript = (root / "static" / "js" / "settings.js").read_text(encoding="utf-8")
    library_javascript = (root / "static" / "js" / "app" / "library.js").read_text(
        encoding="utf-8"
    )

    assert '<option value="off"' in html
    assert 'id="vision-model-install-btn"' in html
    assert 'id="visual-description-model-select"' in html
    assert 'id="vision-model-discovery-row"' in html
    assert 'aria-describedby="vision-model-refresh-desc"' in html
    assert html.index('id="vision-model-refresh-btn"') < html.index('id="vision-model-select"')
    assert html.index('id="vision-model-refresh-btn"') < html.index('id="visual-description-model-select"')
    assert "window.confirm(" in javascript
    assert "confirmed: true" in javascript
    assert "visual_analysis_mode" in javascript
    assert 'id="visual-index-warning"' in html
    assert "indexWarning.style.display = mode === 'index' ? 'block' : 'none'" in javascript
    assert 'id="visual-analysis-auto-chat"' in html
    assert 'class="settings-checkbox-row" for="visual-analysis-auto-chat"' in html
    assert "/api/visual-analysis-cached" in library_javascript
    assert "bucket.visualText" in library_javascript
    assert "VISUAL_ANALYSIS_VIEW_STORAGE_KEY" in library_javascript
    assert "visual_analysis_auto_chat" in javascript
    assert "visual_description_model" in javascript
    assert "settings.visionModelsDetectedMany" in javascript
    assert 'id="chat-force-visual"' in html

    app_javascript = read_app_javascript()
    assert "force_visual_analysis: forceVisualAnalysis" in app_javascript
    assert "chatForceVisualOnce" in app_javascript


def test_index_mode_adds_visual_chunks_without_mixing_them_with_pdf_text(
    workspace_tmp_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.rag.document_loader as document_loader

    pdf_path = workspace_tmp_dir / "paper.pdf"
    pdf_path.write_bytes(b"%PDF-test")

    class FakeLoader:
        def __init__(self, _path: str) -> None:
            pass

        def load(self) -> list[Document]:
            return [Document(page_content="Figure 1. Courbe des pertes", metadata={"page": 0})]

    document_loader.state._vector_load_pause_event.clear()
    monkeypatch.setattr(document_loader, "PyPDFLoader", FakeLoader)
    monkeypatch.setattr(
        document_loader,
        "resolve_pdf_metadata",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(document_loader, "_build_note_documents", lambda *_args: [])
    monkeypatch.setattr(
        document_loader,
        "visual_analysis_model_settings",
        lambda: ("index", "vision:2b", "vision:9b"),
    )
    monkeypatch.setattr(document_loader, "index_visual_signature", lambda: "index:vision:2b")
    monkeypatch.setattr(
        document_loader,
        "analyze_pdf_visuals",
        lambda *_args, **_kwargs: {
            "results": [
                {
                    "page": 1,
                    "text": "La courbe augmente de 2 à 8 A.",
                    "visual_source": "embedded_image",
                    "figure_label": "Figure 1",
                    "caption": "Figure 1. Courbe des pertes.",
                }
            ]
        },
    )
    monkeypatch.setattr(document_loader, "save_cached_prepared_article", lambda *_args, **_kwargs: None)

    _notes, chunks, page_count = document_loader._load_and_chunk_single_pdf(
        "paper.pdf",
        str(pdf_path),
        500,
    )

    assert page_count == 1
    assert [chunk.metadata["content_type"] for chunk in chunks] == ["pdf_page", "figure_ocr"]
    assert chunks[1].metadata["vision_model"] == "vision:2b"
    assert chunks[1].metadata["ocr_model"] == "vision:2b"
    assert chunks[1].metadata["visual_analysis_model"] == "vision:9b"
    assert chunks[1].metadata["page"] == 1
    assert chunks[1].metadata["visual_source"] == "embedded_image"
    assert chunks[1].metadata["figure_label"] == "Figure 1"
    assert chunks[1].metadata["figure_caption"] == "Figure 1. Courbe des pertes."
    assert "augmente" in chunks[1].page_content
