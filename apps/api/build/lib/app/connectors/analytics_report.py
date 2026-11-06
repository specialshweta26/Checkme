"""Self-contained analytics HTML: charts plus sortable, filterable tables."""

from __future__ import annotations

import json
import re
from html import escape
from typing import Any

from connector_core import BinaryResult, ToolError


def _as_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    return []


def _as_dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").replace(",", "").replace("₹", "").replace("%", "").strip()
    if not text:
        return None
    token = text.split()[0]
    try:
        return float(token)
    except ValueError:
        return None


def _looks_date(label: str) -> bool:
    return bool(re.search(r"\d{4}|\d{1,2}[-/]\d{1,2}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", label, re.I))


def _auto_charts(tables: list[dict]) -> list[dict]:
    charts: list[dict] = []
    for table in tables[:3]:
        columns = [str(c) for c in (table.get("columns") or [])]
        rows = table.get("rows") or []
        if len(columns) < 2 or len(rows) < 2:
            continue
        numeric_cols = []
        for idx, col in enumerate(columns[1:], start=1):
            values = [_num(row[idx] if idx < len(row) else None) for row in rows]
            if sum(v is not None for v in values) >= max(2, len(rows) // 2):
                numeric_cols.append((col, [0.0 if v is None else v for v in values]))
        if not numeric_cols:
            continue
        labels = [str(row[0] if row else "") for row in rows]
        dateish = sum(_looks_date(label) for label in labels) >= max(2, len(labels) // 2)
        kind = "line" if dateish else "bar"
        if len(numeric_cols) == 1 and not dateish:
            total = sum(numeric_cols[0][1]) or 1
            shares = [v / total for v in numeric_cols[0][1]]
            if 0.85 <= sum(shares) <= 1.15 and len(labels) <= 12:
                kind = "pie"
        charts.append(
            {
                "title": table.get("title") or columns[0],
                "type": kind,
                "labels": labels,
                "series": [{"name": name, "values": values} for name, values in numeric_cols[:4]],
            }
        )
    return charts


def _coerce(args: dict) -> dict:
    tables = []
    for raw in _as_list(args.get("tables")):
        item = _as_dict(raw) if not isinstance(raw, dict) else raw
        columns = [str(c) for c in (item.get("columns") or item.get("headers") or [])]
        rows = []
        for row in item.get("rows") or []:
            if isinstance(row, dict):
                rows.append([row.get(col, "") for col in columns] if columns else list(row.values()))
            else:
                rows.append(list(row) if isinstance(row, (list, tuple)) else [row])
        if columns:
            tables.append({"title": str(item.get("title") or "Table"), "columns": columns, "rows": rows})
    charts = []
    for raw in _as_list(args.get("charts")):
        item = _as_dict(raw) if not isinstance(raw, dict) else raw
        labels = [str(x) for x in (item.get("labels") or [])]
        series = []
        for serie in item.get("series") or []:
            row = _as_dict(serie) if not isinstance(serie, dict) else serie
            values = [_num(v) or 0.0 for v in (row.get("values") or [])]
            series.append({"name": str(row.get("name") or "Series"), "values": values})
        if not series and item.get("values"):
            series = [{"name": str(item.get("name") or "Value"), "values": [_num(v) or 0.0 for v in item.get("values")]}]
        kind = str(item.get("type") or item.get("kind") or "bar").lower()
        if kind not in {"line", "bar", "pie"}:
            kind = "bar"
        if labels and series:
            charts.append({"title": str(item.get("title") or "Chart"), "type": kind, "labels": labels, "series": series})
    if not charts:
        charts = _auto_charts(tables)
    findings = args.get("findings") or []
    actions = args.get("actions") or []
    if isinstance(findings, str):
        findings = [line.strip() for line in findings.split("\n") if line.strip()]
    if isinstance(actions, str):
        actions = [line.strip() for line in actions.split("\n") if line.strip()]
    return {
        "title": str(args.get("title") or "Analytics report"),
        "period": str(args.get("period") or ""),
        "summary": str(args.get("summary") or ""),
        "source": str(args.get("source") or ""),
        "findings": [str(x) for x in _as_list(findings) or findings],
        "actions": [str(x) for x in _as_list(actions) or actions],
        "tables": tables,
        "charts": charts,
    }


def build_html(spec: dict) -> str:
    payload = json.dumps({"tables": spec["tables"], "charts": spec["charts"]}, ensure_ascii=False).replace("<", "\\u003c")
    findings = "".join(f"<li>{escape(item)}</li>" for item in spec["findings"])
    actions = "".join(f"<li>{escape(item)}</li>" for item in spec["actions"])
    table_blocks = []
    for idx, table in enumerate(spec["tables"]):
        heads = "".join(f"<th data-col='{i}'>{escape(col)}</th>" for i, col in enumerate(table["columns"]))
        table_blocks.append(
            f"<section class='block' data-table='{idx}'>"
            f"<div class='toolbar'><h2>{escape(table['title'])}</h2>"
            f"<input type='search' placeholder='Filter table…' data-filter='{idx}'></div>"
            f"<div class='table-wrap'><table><thead><tr>{heads}</tr></thead><tbody></tbody></table></div>"
            f"</section>"
        )
    chart_blocks = []
    for idx, chart in enumerate(spec["charts"]):
        chart_blocks.append(
            f"<section class='block'><h2>{escape(chart['title'])}</h2>"
            f"<canvas id='chart-{idx}' width='920' height='320'></canvas></section>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(spec["title"])}</title>
<style>
body {{ font: 15px/1.5 ui-sans-serif, system-ui, sans-serif; margin: 0; background: #f5f7fb; color: #111827; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 28px 20px 48px; }}
h1 {{ margin: 0 0 6px; font-size: 28px; }}
.period, .source {{ color: #64748b; margin: 0 0 8px; }}
.summary {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; padding: 16px 18px; }}
.grid {{ display: grid; gap: 16px; margin-top: 16px; }}
.block {{ background: #fff; border: 1px solid #e5e7eb; border-radius: 14px; padding: 16px; overflow: auto; }}
.toolbar {{ display: flex; gap: 12px; align-items: center; justify-content: space-between; margin-bottom: 10px; }}
.toolbar h2, .block h2 {{ margin: 0 0 10px; font-size: 16px; }}
input[type=search] {{ border: 1px solid #e5e7eb; border-radius: 8px; padding: 8px 10px; min-width: 200px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{ padding: 8px 10px; border-bottom: 1px solid #eef2f7; text-align: left; white-space: nowrap; }}
th {{ cursor: pointer; background: #f8faff; position: sticky; top: 0; }}
th.sort-asc::after {{ content: " \\25B2"; font-size: 10px; }}
th.sort-desc::after {{ content: " \\25BC"; font-size: 10px; }}
tr:nth-child(even) {{ background: #f8faff; }}
tr.hide {{ display: none; }}
canvas {{ width: 100%; max-width: 920px; }}
</style></head>
<body><main>
<h1>{escape(spec["title"])}</h1>
<p class="period">{escape(spec["period"])}</p>
{f'<p class="source">Source: {escape(spec["source"])}</p>' if spec["source"] else ""}
{f'<div class="summary">{escape(spec["summary"])}</div>' if spec["summary"] else ""}
<div class="grid">{"".join(chart_blocks)}</div>
<div class="grid">{"".join(table_blocks)}</div>
{f'<section class="block"><h2>Findings</h2><ul>{findings}</ul></section>' if findings else ""}
{f'<section class="block"><h2>Recommended actions</h2><ul>{actions}</ul></section>' if actions else ""}
<script>
const DATA = {payload};
function num(v) {{
  const n = Number(String(v).replace(/[^0-9.+-]/g, ""));
  return Number.isFinite(n) ? n : String(v).toLowerCase();
}}
function drawBar(ctx, labels, series, w, h) {{
  const pad = 36, max = Math.max(1, ...series.flatMap(s => s.values));
  const n = labels.length || 1, bw = (w - pad * 2) / n;
  ctx.clearRect(0,0,w,h); ctx.strokeStyle = "#e5e7eb"; ctx.beginPath(); ctx.moveTo(pad, h-pad); ctx.lineTo(w-pad, h-pad); ctx.stroke();
  series.forEach((s, si) => {{
    ctx.fillStyle = ["#2f6bff","#16a34a","#d97706","#7c3aed"][si % 4];
    s.values.forEach((v, i) => {{
      const bh = ((v || 0) / max) * (h - pad * 2);
      const x = pad + i * bw + si * (bw / (series.length + 1));
      ctx.fillRect(x, h - pad - bh, Math.max(4, bw / (series.length + 1) - 4), bh);
    }});
  }});
  ctx.fillStyle = "#64748b"; ctx.font = "11px sans-serif";
  labels.forEach((lb, i) => ctx.fillText(String(lb).slice(0,12), pad + i * bw, h - 14));
}}
function drawLine(ctx, labels, series, w, h) {{
  const pad = 36, max = Math.max(1, ...series.flatMap(s => s.values));
  const n = Math.max(1, labels.length - 1);
  ctx.clearRect(0,0,w,h); ctx.strokeStyle = "#e5e7eb"; ctx.beginPath(); ctx.moveTo(pad, h-pad); ctx.lineTo(w-pad, h-pad); ctx.stroke();
  series.forEach((s, si) => {{
    ctx.strokeStyle = ["#2f6bff","#16a34a","#d97706","#7c3aed"][si % 4]; ctx.lineWidth = 2; ctx.beginPath();
    s.values.forEach((v, i) => {{
      const x = pad + (i / n) * (w - pad * 2);
      const y = h - pad - ((v || 0) / max) * (h - pad * 2);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }});
    ctx.stroke();
  }});
}}
function drawPie(ctx, labels, series, w, h) {{
  const values = (series[0] && series[0].values) || [];
  const total = values.reduce((a,b) => a + (b || 0), 0) || 1;
  let a0 = -Math.PI / 2, cx = w/2, cy = h/2, r = Math.min(w,h)/2 - 24;
  ctx.clearRect(0,0,w,h);
  values.forEach((v, i) => {{
    const a1 = a0 + ((v || 0) / total) * Math.PI * 2;
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, r, a0, a1); ctx.closePath();
    ctx.fillStyle = ["#2f6bff","#16a34a","#d97706","#7c3aed","#dc4d5d","#0ea5e9"][i % 6]; ctx.fill();
    a0 = a1;
  }});
}}
DATA.charts.forEach((chart, i) => {{
  const canvas = document.getElementById("chart-" + i);
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const w = canvas.width, h = canvas.height;
  if (chart.type === "line") drawLine(ctx, chart.labels, chart.series, w, h);
  else if (chart.type === "pie") drawPie(ctx, chart.labels, chart.series, w, h);
  else drawBar(ctx, chart.labels, chart.series, w, h);
}});
DATA.tables.forEach((table, ti) => {{
  const root = document.querySelector("[data-table='" + ti + "']");
  if (!root) return;
  const tbody = root.querySelector("tbody");
  const state = {{ col: 0, dir: 1, q: "" }};
  function render() {{
    const q = state.q.toLowerCase();
    const rows = table.rows.slice().sort((a, b) => {{
      const av = num(a[state.col]), bv = num(b[state.col]);
      if (av < bv) return -state.dir; if (av > bv) return state.dir; return 0;
    }});
    tbody.innerHTML = rows.map(row => {{
      const hide = q && !row.join(" ").toLowerCase().includes(q);
      return "<tr class='" + (hide ? "hide" : "") + "'>" + row.map(c => "<td>" + String(c ?? "") + "</td>").join("") + "</tr>";
    }}).join("");
  }}
  root.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {{
    const col = Number(th.getAttribute("data-col"));
    state.dir = state.col === col ? -state.dir : 1;
    state.col = col;
    root.querySelectorAll("th").forEach(x => x.classList.remove("sort-asc", "sort-desc"));
    th.classList.add(state.dir === 1 ? "sort-asc" : "sort-desc");
    render();
  }}));
  const filter = root.querySelector("[data-filter]");
  if (filter) filter.addEventListener("input", e => {{ state.q = e.target.value; render(); }});
  render();
}});
</script></main></body></html>"""


def render_report(args: dict) -> BinaryResult:
    spec = _coerce(args or {})
    if not spec["tables"] and not spec["charts"]:
        raise ToolError("write_analytics_report needs tables or charts from the fetched data.")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", spec["title"])[:80] or "analytics-report"
    html = build_html(spec)
    return BinaryResult(filename=f"{slug}.html", content_type="text/html", data=html.encode("utf-8"))
