"""Office files made on the server, and scripted office apps."""

import io
from types import SimpleNamespace

import pytest

from app.agent import computer_harness as computer
from app.agent import office_files as files


def test_a_spreadsheet_keeps_values_formulas_and_its_chart():
    from openpyxl import load_workbook

    data = files.build_xlsx({"sheets": [{"name": "Sales/Q1", "rows": [["Item", "Amount"], ["Tea", 40], ["Milk", 60], ["Total", "=SUM(B2:B3)"]],
                                         "chart": {"type": "bar", "title": "Amounts", "labels": "A2:A3", "values": "B2:B3"}}]})  # fmt: skip
    sheet = load_workbook(io.BytesIO(data))["Sales Q1"]
    assert sheet["B2"].value == 40 and sheet["B4"].value == "=SUM(B2:B3)"
    assert len(sheet._charts) == 1


def test_bad_chart_ranges_are_refused():
    with pytest.raises(files.SpecError):
        files.build_xlsx({"sheets": [{"rows": [["a"]], "chart": {"labels": "A1", "values": "B1:B2"}}]})


def test_a_document_keeps_its_parts():
    from docx import Document

    data = files.build_docx({"blocks": [{"type": "heading", "text": "Report"}, {"type": "paragraph", "text": "Hello"},
                                        {"type": "bullets", "items": ["One"]}, {"type": "table", "rows": [["a", "b"], ["c", "d"]]}]})  # fmt: skip
    doc = Document(io.BytesIO(data))
    assert [p.text for p in doc.paragraphs][:3] == ["Report", "Hello", "One"]
    assert doc.tables[0].cell(1, 1).text == "d"


def test_a_presentation_keeps_slides_and_notes():
    from pptx import Presentation

    data = files.build_pptx({"slides": [{"title": "Plan", "bullets": ["One", "Two"], "notes": "Say this"}]})
    slide = Presentation(io.BytesIO(data)).slides[0]
    assert slide.shapes.title.text == "Plan"
    assert [p.text for p in slide.placeholders[1].text_frame.paragraphs] == ["One", "Two"]
    assert slide.notes_slide.notes_text_frame.text == "Say this"


def test_file_names_are_safe():
    assert "/" not in files.file_name("../../etc/passwd", "xlsx")
    assert files.file_name("report.XLSX", "xlsx") == "report.XLSX"


async def test_unknown_kinds_are_refused():
    assert (await files.create(SimpleNamespace(), "exe", "a", {}))["ok"] is False


def _apps(monkeypatch, seen):
    async def fake(ctx, tool, args=None):
        seen.append(args)
        return {"ok": True, "document": "Untitled 2"}

    monkeypatch.setattr(computer, "_try_local", fake)


async def test_the_users_other_documents_are_never_touched(monkeypatch):
    seen = []
    _apps(monkeypatch, seen)
    ctx = SimpleNamespace(query="make me a budget spreadsheet")
    out = await computer.office_app(ctx, "numbers", "set_cells", "india_footwear_founders", [{"cell": "A1", "value": 1}])
    assert out["ok"] is False and seen == []
    assert (await computer.office_app(ctx, "numbers", "read", "india_footwear_founders"))["ok"] is False


async def test_a_document_made_in_this_turn_can_be_used(monkeypatch):
    seen = []
    _apps(monkeypatch, seen)
    ctx = SimpleNamespace(query="make me a budget spreadsheet")
    await computer.office_app(ctx, "numbers", "new")
    assert (await computer.office_app(ctx, "numbers", "set_cells", "Untitled 2", [{"cell": "A1", "value": 1}]))["ok"] is True


async def test_a_document_the_user_named_can_be_used(monkeypatch):
    seen = []
    _apps(monkeypatch, seen)
    ctx = SimpleNamespace(query="Read india_footwear_founders in Numbers")
    assert (await computer.office_app(ctx, "numbers", "read", "india_footwear_founders"))["ok"] is True
