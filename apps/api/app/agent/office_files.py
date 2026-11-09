"""Excel, Word and PowerPoint files made on the server and attached to the chat, so no office app is needed."""

from __future__ import annotations

import io
import re
from typing import Any

MAX_ROWS = 20_000
MAX_SLIDES = 200
_CELL_RANGE = re.compile(r"^[A-Za-z]{1,3}\d+:[A-Za-z]{1,3}\d+$")
_MEDIA = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


class SpecError(ValueError):
    pass


def _safe_cell(value: Any) -> Any:
    return value if isinstance(value, (int, float, bool, str)) or value is None else str(value)


def _add_chart(sheet: Any, chart: dict) -> None:
    from openpyxl.chart import BarChart, LineChart, PieChart, Reference

    kinds = {"bar": BarChart, "line": LineChart, "pie": PieChart}
    kind = str(chart.get("type") or "bar").lower()
    if kind not in kinds:
        raise SpecError(f"chart type must be bar, line or pie, not {kind}")
    labels, values = str(chart.get("labels") or ""), str(chart.get("values") or "")
    if not _CELL_RANGE.match(labels) or not _CELL_RANGE.match(values):
        raise SpecError("chart labels and values must be ranges such as A2:A4")
    made = kinds[kind]()
    made.title = str(chart.get("title") or "")[:200]
    made.add_data(Reference(sheet, range_string=f"'{sheet.title}'!{values}"), titles_from_data=False)
    made.set_categories(Reference(sheet, range_string=f"'{sheet.title}'!{labels}"))
    sheet.add_chart(made, str(chart.get("at") or "E2"))


def build_xlsx(spec: dict) -> bytes:
    from openpyxl import Workbook

    sheets = spec.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise SpecError("xlsx needs sheets: [{name, rows}]")
    book = Workbook()
    book.remove(book.active)
    for index, row_set in enumerate(sheets, 1):
        sheet = book.create_sheet(re.sub(r"[\[\]:*?/\\]", " ", str(row_set.get("name") or f"Sheet{index}"))[:31])
        rows = row_set.get("rows") or []
        if len(rows) > MAX_ROWS:
            raise SpecError(f"at most {MAX_ROWS} rows in a sheet")
        for row in rows:
            sheet.append([_safe_cell(cell) for cell in row])
        if isinstance(row_set.get("chart"), dict):
            _add_chart(sheet, row_set["chart"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def build_docx(spec: dict) -> bytes:
    from docx import Document

    blocks = spec.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise SpecError("docx needs blocks: [{type, text}]")
    doc = Document()
    for block in blocks:
        kind = str(block.get("type") or "paragraph")
        if kind == "heading":
            doc.add_heading(str(block.get("text") or ""), level=min(max(int(block.get("level") or 1), 0), 9))
        elif kind == "paragraph":
            doc.add_paragraph(str(block.get("text") or ""))
        elif kind == "bullets":
            for item in block.get("items") or []:
                doc.add_paragraph(str(item), style="List Bullet")
        elif kind == "table":
            rows = block.get("rows") or []
            if rows:
                table = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
                table.style = "Table Grid"
                for r, row in enumerate(rows):
                    for c, cell in enumerate(row):
                        table.cell(r, c).text = str(cell)
        else:
            raise SpecError(f"unknown block type {kind}")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def build_pptx(spec: dict) -> bytes:
    from pptx import Presentation

    slides = spec.get("slides")
    if not isinstance(slides, list) or not slides:
        raise SpecError("pptx needs slides: [{title, bullets}]")
    if len(slides) > MAX_SLIDES:
        raise SpecError(f"at most {MAX_SLIDES} slides")
    deck = Presentation()
    layout = deck.slide_layouts[1]
    for item in slides:
        slide = deck.slides.add_slide(layout)
        slide.shapes.title.text = str(item.get("title") or "")
        frame = slide.placeholders[1].text_frame
        bullets = [str(b) for b in item.get("bullets") or []]
        frame.text = bullets[0] if bullets else ""
        for bullet in bullets[1:]:
            frame.add_paragraph().text = bullet
        if item.get("notes"):
            slide.notes_slide.notes_text_frame.text = str(item["notes"])
    out = io.BytesIO()
    deck.save(out)
    return out.getvalue()


_BUILDERS = {"xlsx": build_xlsx, "docx": build_docx, "pptx": build_pptx}


def file_name(filename: str, kind: str) -> str:
    stem = re.sub(r"[^\w .-]+", "_", str(filename or "document")).strip(" .") or "document"
    return stem if stem.lower().endswith(f".{kind}") else f"{stem}.{kind}"


async def create(ctx: Any, kind: str, filename: str, spec: dict) -> dict:
    from app.agent.attachments import store_blob
    from connector_core import BinaryResult

    kind = str(kind or "").lower().lstrip(".")
    if kind not in _BUILDERS:
        return {"ok": False, "error": "kind must be xlsx, docx or pptx"}
    try:
        data = _BUILDERS[kind](spec if isinstance(spec, dict) else {})
    except (SpecError, TypeError, ValueError, AttributeError) as exc:
        return {"ok": False, "error": f"Couldn't build the file: {str(exc)[:300]}"}
    name = file_name(filename, kind)
    card = await store_blob(ctx.user.id, BinaryResult(filename=name, content_type=_MEDIA[kind], data=data))
    ctx.attachments.append(card)
    return {"ok": True, "filename": name, "size": len(data), "download_url": card.get("download_url")}
