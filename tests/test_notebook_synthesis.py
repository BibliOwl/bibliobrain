from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import BaseModel

import src.config as config
import src.notebook_synthesis as synthesis
from src.chat_harness import plan_notebook_tools

ROOT = Path(__file__).resolve().parents[1]

PAGE_ONE = (
    "Pulsed field magnetization of bulk GdBaCuO was studied with a multi-pulse technique. "
    "The trapped field reached 2.1 T at 40 K when the second pulse was applied after "
    "stepwise cooling, which is higher than with a single pulse. " * 3
)
PAGE_TWO = (
    "Numerical simulations based on the critical state model reproduce the measured "
    "temperature rise. The peak effect in Jc(B) explains the larger trapped field. " * 3
)
MODELLING_TEXT = (
    "A macroscopic critical state model describes the magnetization loops and the "
    "levitation force of hard type-II superconductors under non-uniform fields. " * 4
)


def _load_notebooks_module(workspace_tmp_dir: Path) -> Any:
    storage_dir = workspace_tmp_dir / "storage"
    data_dir = workspace_tmp_dir / "data"
    storage_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    config.STORAGE_DIR = str(storage_dir)
    config.DATA_PATH = str(data_dir)
    module = cast(Any, importlib.import_module("src.notebooks"))
    return cast(Any, importlib.reload(module))


def _fake_llm(monkeypatch: Any, *, grouping_fails: bool = False) -> list[str]:
    """Answer extraction calls from the article text and group every point in one chapter."""
    prompts: list[str] = []

    def invoke(model_name: str, prompt: str, response_model: type[BaseModel]) -> Any:
        prompts.append(prompt)
        if response_model is synthesis._ExtractedPoints:
            if "Pulsed field magnetization" in prompt:
                return synthesis._ExtractedPoints(
                    points=[
                        synthesis._ExtractedPoint(
                            idea="La technique multi-impulsions augmente le champ piégé.",
                            quote="The trapped field reached 2.1 T at 40 K when the second pulse was applied",
                            page="p. 1",
                        ),
                        synthesis._ExtractedPoint(
                            idea="L'effet de pic explique le gain.",
                            quote="The peak effect in Jc(B) explains the larger trapped field.",
                            page="p. 1",
                        ),
                        synthesis._ExtractedPoint(
                            idea="Un point avec une citation inventée.",
                            quote="Superconductors levitate trains at room temperature.",
                            page="p. 2",
                        ),
                    ]
                )
            return synthesis._ExtractedPoints(
                points=[
                    synthesis._ExtractedPoint(
                        idea="Le modèle de l'état critique décrit la lévitation.",
                        quote="describes the magnetization loops and the levitation force",
                    )
                ]
            )
        if response_model is synthesis._ChapterPlan:
            if grouping_fails:
                raise RuntimeError("model unavailable")
            return synthesis._ChapterPlan(
                chapters=[
                    synthesis._PlannedChapter(title="Aimantation par champ pulsé", scope="PFM"),
                    synthesis._PlannedChapter(title="## Modélisation", scope="Modèles"),
                    synthesis._PlannedChapter(title="modélisation", scope="doublon"),
                ]
            )
        if response_model.__name__ == "PointChapters":
            # p1_3 is never answered, as if its batch kept failing validation.
            chapters: dict[str, Any] = {"p1_1": 1, "p1_2": 2, "p2_1": 2}
            return response_model.model_construct(
                **{key: value for key, value in chapters.items() if key in response_model.model_fields}
            )
        if "« Modélisation »" in prompt:
            return synthesis._ChapterText(
                summary="Le multi-impulsions améliore le champ piégé [S1], "
                "ce que confirme la modélisation [2, 9]."
            )
        return synthesis._ChapterText(summary='"Le champ pulsé reste la voie compacte [1][2]."}')

    monkeypatch.setattr(synthesis, "_invoke_structured", invoke)
    monkeypatch.setattr(synthesis, "_article_char_budget", lambda model_name: 8000)
    return prompts


@pytest.mark.parametrize(
    "message",
    [
        "Dans une liste d'articles, regroupe les différentes idées/points clés, classe les par "
        "thèmes et fais en des chapitres, en donnant les citations adaptées",
        "Fais une synthèse thématique du carnet",
        "Organise les idées de ces articles en chapitres",
        "Group the key findings of these papers by theme into chapters",
    ],
)
def test_thematic_synthesis_requests_are_detected(message: str) -> None:
    assert synthesis.is_thematic_synthesis_request(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "Quel est le thème principal de cet article ?",
        "Fais un résumé détaillé de cet article",
        "Classe ces articles par année",
        "Rajoutez cette synthèse dans les notes.",
    ],
)
def test_ordinary_notebook_questions_are_not_thematic_syntheses(message: str) -> None:
    assert synthesis.is_thematic_synthesis_request(message) is False


def test_notebook_plan_routes_thematic_synthesis_to_the_notes() -> None:
    plan = plan_notebook_tools(
        notebook_id="nb-1",
        message="Regroupe les points clés par thèmes en chapitres",
        append_to_notes=False,
    )

    assert [call.name for call in plan.calls] == [
        "notebook.thematic_synthesis",
        "notebook.append_notes",
    ]


def test_long_article_keeps_its_opening_and_its_conclusion() -> None:
    pages = [
        synthesis.ArticlePage("p. 1", "Introduction " + "a" * 30 + " intro words " * 400),
        synthesis.ArticlePage("p. 9", "middle words " * 800),
        synthesis.ArticlePage("p. 12", "middle " * 20 + "In conclusion the method works."),
    ]

    text = synthesis.fit_article_text(pages, 5000)

    assert len(text) <= 5100
    assert text.startswith("[p. 1]\nIntroduction")
    assert text.rstrip().endswith("In conclusion the method works.")
    assert "[…]" in text


def test_references_section_is_dropped() -> None:
    body = "Results and discussion of the trapped field. " * 40
    pages = [
        synthesis.ArticlePage("p. 1", body),
        synthesis.ArticlePage("p. 2", body + "\nReferences\n[1] A. Author, Some paper, 2001."),
        synthesis.ArticlePage("p. 3", "[2] B. Author, Other paper, 2005."),
    ]

    kept = synthesis._drop_references(pages)

    assert [page.label for page in kept] == ["p. 1", "p. 2"]
    assert "Some paper" not in kept[-1].text


def test_font_encoded_pdf_text_is_unreadable() -> None:
    assert synthesis._looks_unreadable("/C77/C101/C108/C116/C112" * 60) is True
    assert synthesis._looks_unreadable(PAGE_ONE) is False


def test_quotes_are_verified_against_the_page_they_come_from() -> None:
    pages = [synthesis.ArticlePage("p. 1", PAGE_ONE), synthesis.ArticlePage("p. 2", PAGE_TWO)]

    assert synthesis.verify_quote(
        "The peak effect in Jc(B) explains the larger trapped field.", pages, "p. 1"
    ) == (True, "p. 2")
    assert synthesis.verify_quote("Superconductors levitate trains.", pages, "p. 2") == (False, "")
    straddling = "explains the larger trapped field. Numerical simulations based on"
    assert synthesis.verify_quote(straddling, pages[1:], "[Page 2]") == (True, "p. 2")


def test_pdf_page_labels_use_the_printed_page_number() -> None:
    unit = SimpleNamespace(page=0, location_label="Page 1")

    assert synthesis._page_label(unit) == "p. 1"


def test_unverified_quote_is_dropped_but_the_point_is_kept(monkeypatch: Any) -> None:
    _fake_llm(monkeypatch)
    pages = [synthesis.ArticlePage("p. 1", PAGE_ONE), synthesis.ArticlePage("p. 2", PAGE_TWO)]

    points = synthesis.extract_key_points(
        "regroupe par thèmes",
        4,
        "Fujishiro 2009",
        pages,
        "demo",
        char_budget=8000,
        language="fr",
    )

    assert [point.point_id for point in points] == ["4.1", "4.2", "4.3"]
    assert points[0].page == "p. 1"
    assert points[1].page == "p. 2"
    assert points[2].quote == ""
    assert points[2].idea == "Un point avec une citation inventée."


def test_chapters_follow_the_plan_and_hold_every_point_once() -> None:
    points = [
        synthesis.KeyPoint("1.1", 1, "a"),
        synthesis.KeyPoint("1.2", 1, "b"),
        synthesis.KeyPoint("2.1", 2, "c"),
    ]
    plan = [
        synthesis._PlannedChapter(title="Premier"),
        synthesis._PlannedChapter(title="Vide"),
        synthesis._PlannedChapter(title="Troisième"),
    ]

    chapters = synthesis.assign_points(
        plan, {"1.1": 1, "2.1": 3}, points, other_title="Autres points"
    )

    assert [chapter.title for chapter in chapters] == ["Premier", "Troisième", "Autres points"]
    assert [[point.point_id for point in chapter.points] for chapter in chapters] == [
        ["1.1"],
        ["2.1"],
        ["1.2"],
    ]


def test_classification_asks_again_for_the_points_left_out(monkeypatch: Any) -> None:
    prompts = _fake_llm(monkeypatch)
    points = [synthesis.KeyPoint(f"1.{index}", 1, "idée") for index in range(1, 4)]
    points.append(synthesis.KeyPoint("2.1", 2, "idée"))
    plan = [synthesis._PlannedChapter(title="Un"), synthesis._PlannedChapter(title="Deux")]

    classification = synthesis.classify_points(plan, points, "demo")
    with pytest.raises(StopIteration) as stop:
        assert next(classification) == 0
        assert next(classification) == 3
        next(classification)

    assert stop.value.value == {"1.1": 1, "1.2": 2, "2.1": 2}
    assert "p1_3 : idée" in prompts[-1]
    assert "p1_1" not in prompts[-1]


def test_assignment_schema_requires_every_point_and_bounds_the_chapter() -> None:
    model = synthesis._assignment_model([synthesis.KeyPoint("3.2", 3, "a")], 4)
    schema = model.model_json_schema()

    assert schema["required"] == ["p3_2"]
    assert schema["properties"]["p3_2"]["enum"] == [1, 2, 3, 4]
    with pytest.raises(ValueError):
        model(p3_2=5)


def test_citations_are_limited_to_the_chapter_sources() -> None:
    assert synthesis.clean_citations("Voir [S1] et [1, 7] puis [7].", {1}) == "Voir [1] et [1] puis."


def test_synthesis_reads_every_source_and_reports_unreadable_ones(monkeypatch: Any) -> None:
    prompts = _fake_llm(monkeypatch)
    sources = [
        {"title": "Fujishiro 2009", "content": PAGE_ONE + PAGE_TWO},
        {"title": "Navau 2013", "content": MODELLING_TEXT},
        {"title": "Ikuta 2001", "content": "/C77/C101/C108/C116" * 80},
    ]

    events = synthesis.generate_thematic_synthesis(
        sources, "Regroupe les idées par thèmes, en chapitres, avec les citations adaptées.", "demo"
    )
    statuses: list[str] = []
    with pytest.raises(StopIteration) as stop:
        while True:
            statuses.append(next(events)["msg"])
    markdown = stop.value.value

    assert statuses[:3] == [
        "Lecture de l'article 1/3 : Fujishiro 2009",
        "Lecture de l'article 2/3 : Navau 2013",
        "Lecture de l'article 3/3 : Ikuta 2001",
    ]
    assert statuses[3:] == [
        "Plan des chapitres à partir de 4 points clés…",
        "Classement des points dans les chapitres (0/4 classés)…",
        "Classement des points dans les chapitres (3/4 classés)…",
        "Rédaction du chapitre 1/2 : Aimantation par champ pulsé",
        "Rédaction du chapitre 2/2 : Modélisation",
    ]
    # The unreadable source never reaches the model: 2 extractions, a plan,
    # a classification batch, its retry for the point left out, and one call
    # per thematic chapter.
    assert len(prompts) == 7
    assert all("Superconductors levitate" not in prompt for prompt in prompts[2:])
    assert "### 1. Aimantation par champ pulsé\n\nLe champ pulsé reste la voie compacte [1].\n" in markdown
    assert (
        "- La technique multi-impulsions augmente le champ piégé. [1] — *« The trapped field "
        "reached 2.1 T at 40 K when the second pulse was applied »*\n"
    ) in markdown
    # The chat's fallback renderer builds a list only from blocks made of "- " lines.
    for block in markdown.split("\n\n"):
        block_lines = block.strip().splitlines()
        if any(line.startswith("- ") for line in block_lines):
            assert all(line.startswith("- ") for line in block_lines), block
    assert "### 2. Modélisation" in markdown
    assert "Le multi-impulsions améliore le champ piégé [1], ce que confirme la modélisation [2]." in markdown
    assert "### 3. Autres points\n\n- Un point avec une citation inventée. [1]" in markdown
    assert "Superconductors levitate" not in markdown
    assert "- [3] Ikuta 2001 — texte illisible ou absent" in markdown
    assert "3 citation(s) vérifiée(s)" in markdown


def test_planning_failure_falls_back_to_one_chapter_per_article(monkeypatch: Any) -> None:
    prompts = _fake_llm(monkeypatch, grouping_fails=True)
    sources = [
        {"title": "Fujishiro 2009", "content": PAGE_ONE + PAGE_TWO},
        {"title": "Navau 2013", "content": MODELLING_TEXT},
    ]

    events = synthesis.generate_thematic_synthesis(sources, "synthèse thématique", "demo")
    with pytest.raises(StopIteration) as stop:
        while True:
            next(events)
    markdown = stop.value.value

    assert "### 1. Fujishiro 2009" in markdown
    assert "### 2. Navau 2013" in markdown
    assert len(prompts) == 3


def test_stream_saves_the_synthesis_to_the_notes(workspace_tmp_dir: Path, monkeypatch: Any) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    _fake_llm(monkeypatch)
    nb = notebooks.create_notebook("HTS")
    notebooks.update_notes(nb["id"], "Notes existantes.")
    notebooks.add_source(nb["id"], "Fujishiro 2009", PAGE_ONE + PAGE_TWO)
    notebooks.add_source(nb["id"], "Navau 2013", MODELLING_TEXT)

    events = list(
        notebooks.stream_notebook_thematic_synthesis(
            nb["id"], "Fais une synthèse thématique", "demo"
        )
    )

    types = [event["type"] for event in events]
    assert types.count("token") == 1
    assert types[-1] == "notes_updated"
    saved = notebooks.get_notebook(nb["id"])
    assert saved["notes"].startswith("Notes existantes.\n\n---\n\n## Synthèse thématique")
    assert [message["role"] for message in saved["messages"]] == ["user", "assistant"]


def test_read_only_stream_leaves_the_notes_alone(workspace_tmp_dir: Path, monkeypatch: Any) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    _fake_llm(monkeypatch)
    nb = notebooks.create_notebook("HTS")
    notebooks.add_source(nb["id"], "Navau 2013", MODELLING_TEXT)

    events = list(
        notebooks.stream_notebook_thematic_synthesis(
            nb["id"],
            "Fais une synthèse thématique",
            "demo",
            persist_history=False,
            read_only=True,
        )
    )

    assert "notes_updated" not in [event["type"] for event in events]
    saved = notebooks.get_notebook(nb["id"])
    assert saved["notes"] == ""
    assert saved["messages"] == []


def test_empty_notebook_reports_an_error(workspace_tmp_dir: Path) -> None:
    notebooks = _load_notebooks_module(workspace_tmp_dir)
    nb = notebooks.create_notebook("Vide")

    events = list(notebooks.stream_notebook_thematic_synthesis(nb["id"], "synthèse thématique", "demo"))

    assert events == [
        {
            "type": "error",
            "msg": "Le carnet ne contient aucune source exploitable. Ajoutez des articles ou "
            "une collection Zotero, puis relancez la synthèse.",
        }
    ]


def test_notebook_toolbar_offers_the_thematic_synthesis() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    bindings = (ROOT / "static" / "js" / "static-event-bindings.js").read_text(encoding="utf-8")
    script = (ROOT / "static" / "js" / "app" / "notebooks.js").read_text(encoding="utf-8")

    assert 'data-bb-static-onclick="run-nb-thematic-synthesis"' in html
    # The main action of the notebook header, named after what it produces.
    assert 'data-i18n="notebooks.writeSynthesis"' in html
    assert 'class="btn btn-indigo nb-primary-action"' in html
    assert "['run-nb-thematic-synthesis', 'click', function(event) { runNbThematicSynthesis() }]" in bindings
    assert "input.value = _ti('notebooks.thematicSynthesisPrompt');" in script


def test_notebook_citations_display_their_source_number() -> None:
    rendering = (ROOT / "static" / "js" / "app" / "assistant-rendering.js").read_text(encoding="utf-8")

    # Notebook sources have no label: without its number every citation showed [1].
    assert "sourceRefLabel(source, /^\\d+$/.test(label) ? label : 1) || label" in rendering
