"""A reply quoting totals Lumio worked out (aggregate_result) or a report's own figures kept them: the figure check
only knew raw connector numbers, and its rewrite saw just the first 60,000 characters of the data, so the SIS totals
the report had were replaced with "not available in the supplied data excerpt"."""

from __future__ import annotations

from app.agent import fact_check

ROWS = [{"channel": "SIS" if i % 3 else "Online", "net_sales": 1000 + i, "units": 2} for i in range(5000)]
SIS = sum(r["net_sales"] for r in ROWS if r["channel"] == "SIS")


def test_an_aggregate_lumio_worked_out_counts_as_data():
    # Rows the check never saw whole (a stored table read in pages), and the total Lumio worked out over all of them.
    raw = [{"source_tool": "ssa__get_data_daily", "data": {"rows": ROWS[:10]}}]
    reply = f"SIS net sales for November MTD were ₹{SIS:,}."
    assert fact_check.unverified(reply, raw)
    derived = [{"source_tool": "aggregate_result", "data": {"groups": [{"channel": "SIS", "net_sales": SIS}]}}]
    assert fact_check.unverified(reply, derived + raw) == []


def test_a_reports_scaled_figures_count():
    report = [{"source_tool": "write_analytics_report", "data": {"extracted_text": "SIS net revenue: ₹6.51 crore across 8,921 units"}}]
    assert fact_check.unverified("SIS net revenue was ₹6.51 crore.", report) == []


async def test_with_too_much_data_the_figures_are_marked_not_removed():
    raw = [{"source_tool": "ssa__get_data_daily", "data": {"rows": ROWS}}]
    called = []

    async def complete(*a, **k):
        called.append(1)
        raise AssertionError("no rewrite from cut data")

    text, report = await fact_check.verify_reply("SIS net sales were ₹9,99,99,999.", raw, complete=complete)
    assert not called and "9,99,99,999" in text and "Check these figures" in text and report["too_much_data"]
