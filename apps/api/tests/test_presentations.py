"""Designed decks from a pptxgenjs script, shared by the website and the desktop app."""

import io

import pytest

from app.agent import presentations as decks


DESIGNED = """
const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
const cover = pres.addSlide();
cover.background = { color: "1E2761" };
cover.addShape(pres.ShapeType.ellipse, { x: 10.6, y: 0.6, w: 1.8, h: 1.8, fill: { color: "CADCFC" } });
cover.addText("Soil plan", { x: 0.7, y: 2.4, w: 10, h: 1, fontSize: 40, fontFace: "Cambria", color: "FFFFFF", bold: true, margin: 0 });
cover.addNotes("Open with the harvest.");
const stats = pres.addSlide();
stats.background = { color: "FFFFFF" };
stats.addText("This season", { x: 0.6, y: 0.45, w: 8, h: 0.6, fontSize: 32, fontFace: "Cambria", color: "1E2761", margin: 0 });
stats.addShape(pres.ShapeType.roundRect, { x: 0.6, y: 1.6, w: 3.6, h: 2.1, fill: { color: "E7EEF9" }, rectRadius: 0.1 });
stats.addText("12%", { x: 0.6, y: 1.9, w: 3.6, h: 0.8, fontSize: 36, fontFace: "Calibri", bold: true, color: "1E2761", align: "center", margin: 0 });
stats.addText("Yield", { x: 0.6, y: 2.8, w: 3.6, h: 0.4, fontSize: 14, fontFace: "Calibri", color: "36454F", align: "center", margin: 0 });
stats.addChart(pres.ChartType.bar, [{ name: "Yield", labels: ["Wheat", "Beans"], values: [12, 9] }], {
  x: 5.0, y: 1.5, w: 7.4, h: 4.8, showValue: true, chartColors: ["1E2761"], showLegend: false,
  catAxisLabelColor: "36454F", valAxisLabelColor: "36454F",
  valGridLine: { color: "E6E6E6", size: 1 }, catGridLine: { style: "none" }
});
await pres.writeFile({ fileName: OUT });
"""

PLAIN = """
const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";
const slide = pres.addSlide();
slide.addText("Plan", { x: 0.5, y: 0.4, w: 10, h: 0.8, fontSize: 32, fontFace: "Calibri", margin: 0 });
slide.addText("One", { x: 0.5, y: 1.5, w: 10, h: 1, fontSize: 16, fontFace: "Calibri", margin: 0 });
await pres.writeFile({ fileName: OUT });
"""


def test_scripts_that_leave_the_deck_builder_are_refused():
    with pytest.raises(decks.PresentationError):
        decks.render('require("child_process")')


def test_a_designed_deck_keeps_shapes_notes_and_a_chart():
    if not decks.node_binary() or not (decks.modules_dir() / "pptxgenjs").is_dir():
        pytest.skip("node and pptxgenjs are required")
    from pptx import Presentation

    data = decks.render(DESIGNED)
    info = decks.review(data)
    deck = Presentation(io.BytesIO(data))
    assert info["slides"] == 2 and info["plain_slides"] == []
    assert deck.slides[0].notes_slide.notes_text_frame.text == "Open with the harvest."
    assert deck.slide_width > 11_000_000  # LAYOUT_WIDE, not the default 10in canvas
    assert any(shape.has_chart for shape in deck.slides[1].shapes)


def test_a_text_only_slide_is_reported():
    if not decks.node_binary() or not (decks.modules_dir() / "pptxgenjs").is_dir():
        pytest.skip("node and pptxgenjs are required")
    info = decks.review(decks.render(PLAIN))
    assert info["plain_slides"] == [1]
