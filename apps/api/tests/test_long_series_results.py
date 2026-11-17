"""Long connector answers reach the model whole (docs/LLD.md "Connector results").

The agent harness moves a tool message over 80,000 characters to a file the agent reads in parts; a 90-day daily sales
series then came back as "only 30 days" or as zeros, depending on the part it read. Lists of records go out as tables
with column totals over every row, and stay under the inline limit."""

from __future__ import annotations

import json
import random

from app.agent import approval, deep

METRICS = ["fwd_orders", "fwd_sales", "fwd_aov", "fwd_units", "net_orders", "net_sales", "net_aov", "net_units", "revenue",
           "revenue_aov", "gross_orders", "gross_sales", "gross_aov", "discount", "cancelled_orders", "cancelled_value",
           "refunded_orders", "refund_value"]  # fmt: skip


def _daily(days: int, seed: int = 7) -> dict:
    """Shaped like the gateway's get_daily_sales answer: a record per day with day-over-day deltas."""
    rnd = random.Random(seed)
    rows = []
    for i in range(days):
        row = {"date": f"2026-{8 + (i + 19) // 31:02d}-{(i + 19) % 31 + 1:02d}"}
        for m in METRICS:
            row[m] = rnd.randint(100, 900) if "orders" in m or "units" in m else rnd.randint(100_000, 900_000)
        row["dod"] = {m: (round(rnd.uniform(-20, 20), 1) if i else None) for m in METRICS}
        row["contrib"] = {}
        rows.append(row)
    return {"brand": "acme", "window": {"start": rows[0]["date"], "end": rows[-1]["date"]},
            "daily": {"brand": "acme", "trend": "dod", "metrics": METRICS, "days": rows}}  # fmt: skip


def test_a_120_day_series_reaches_the_model_whole_as_a_table():
    raw = _daily(120)
    assert len(json.dumps(raw)) > 80_000  # it used to be moved to a file
    out = deep.fit_payload(raw)
    text = json.dumps(out, default=str)
    assert len(text) <= deep.INLINE_RESULT_CHARS and "partial" not in out
    table = out["daily"]["days"]
    assert table["row_count"] == 120 and len(table["rows"]) == 120 and table["columns"][0] == "date"
    i = table["columns"].index("net_sales")
    assert [r[i] for r in table["rows"]] == [d["net_sales"] for d in raw["daily"]["days"]]  # nothing changed or lost
    assert table["column_totals"]["net_sales"] == sum(d["net_sales"] for d in raw["daily"]["days"])
    assert table["column_totals"]["net_orders"] == sum(d["net_orders"] for d in raw["daily"]["days"])
    for not_a_total in ("date", "net_aov", "fwd_aov", "dod", "contrib"):
        assert not_a_total not in table["column_totals"]  # dates, averages and nested values aren't added up


def test_a_series_too_long_even_as_a_table_keeps_totals_over_every_row():
    raw = _daily(400)
    out = deep.fit_payload(raw)
    assert out["partial"] and len(json.dumps(out, default=str)) <= deep.INLINE_RESULT_CHARS
    table = out["data"]["daily"]["days"]
    assert table["row_count"] == 400 and len(table["rows"]) < 400 and table["rows_shown"] == len(table["rows"])
    assert len(table["columns"]) == len(METRICS) + 3  # rows are cut, never columns
    assert table["column_totals"]["net_sales"] == sum(d["net_sales"] for d in raw["daily"]["days"])


def test_small_answers_and_mixed_lists_are_unchanged():
    small = {"brand": "acme", "days": [{"date": "2026-11-17", "net_sales": 5}]}
    assert deep.fit_payload(small) == small
    mixed = deep.tabulate({"items": [{"a": 1}, "text", {"a": 2}]})
    assert mixed == {"items": [{"a": 1}, "text", {"a": 2}]}
    different = deep.tabulate([{"a": 1}, {"b": 2}, {"c": 3}])
    assert different == [{"a": 1}, {"b": 2}, {"c": 3}]  # records too different to share columns


def test_a_read_named_under_the_gateways_own_key_needs_no_approval():
    for key in ("tool_name", "public_name", "public_tool_name"):
        args = {key: "funnel-analytics-prod__get_daily_sales", "arguments": {"brand": "acme"}}
        assert approval.nested_actions("gateway__execute", args) == ["funnel-analytics-prod__get_daily_sales"]
        assert approval.gateway_needs_approval("gateway__execute", args) is False
    write = {"public_tool_name": "erp__create_order", "arguments": {}}
    assert approval.gateway_needs_approval("gateway__execute", write) is True  # a write still asks
