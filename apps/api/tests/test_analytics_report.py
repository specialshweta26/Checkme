from app.connectors.analytics_report import render_report


def test_report_html_has_charts_and_filterable_tables():
    html = render_report(
        {
            "title": "Daily sales",
            "period": "1–10 Oct 2026",
            "summary": "Sales rose on two peak days.",
            "tables": [
                {
                    "title": "Daily net sales",
                    "columns": ["Date", "Net sales"],
                    "rows": [["2026-10-01", 100], ["2026-10-02", 250], ["2026-10-03", 180]],
                }
            ],
            "charts": [
                {
                    "title": "Daily net sales",
                    "type": "line",
                    "labels": ["2026-10-01", "2026-10-02", "2026-10-03"],
                    "series": [{"name": "Net sales", "values": [100, 250, 180]}],
                }
            ],
            "findings": ["Two peak days drove the period."],
            "actions": ["Separate event-day sales from baseline."],
        }
    )
    body = html.data.decode("utf-8")
    assert html.filename.endswith(".html")
    assert html.content_type == "text/html"
    assert "canvas" in body
    assert "type='search'" in body or 'type="search"' in body
    assert "data-filter" in body
    assert "drawLine" in body
    assert "Filter table" in body


def test_report_auto_charts_from_tables():
    html = render_report(
        {
            "title": "Mix",
            "tables": [
                {
                    "title": "Channel mix",
                    "columns": ["Channel", "Share"],
                    "rows": [["Paid", 40], ["Direct", 35], ["Other", 25]],
                }
            ],
        }
    )
    body = html.data.decode("utf-8")
    assert "chart-0" in body
    assert "data-table='0'" in body
