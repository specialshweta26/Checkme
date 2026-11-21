"""Figures in a reply must come from the data (docs/GUARDRAILS.md "Facts and figures")."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import deep, fact_check

DAYS = [
    {"date": "2026-11-14", "net_sales": 711406, "net_orders": 303, "net_units": 321},
    {"date": "2026-11-15", "net_sales": 835670, "net_orders": 345, "net_units": 374},
    {"date": "2026-11-16", "net_sales": 716013, "net_orders": 286, "net_units": 329},
    {"date": "2026-11-17", "net_sales": 643673, "net_orders": 263, "net_units": 278},
]
BIG = {"brand": "one8", "kpis": {"net_sales": 667722469, "net_orders": 92423, "net_units": 102465}}
SMALL = {"brand": "lotto", "kpis": {"net_sales": 58014669, "net_orders": 21501, "net_units": 23163}}
RESULTS = [{"source_tool": "gw.get_daily_sales", "args": {"brand": "lotto"}, "data": {"days": DAYS}},
           {"source_tool": "gw.get_sales_dashboard", "args": {"brand": "one8"}, "data": BIG},
           {"source_tool": "gw.get_sales_dashboard", "args": {"brand": "lotto"}, "data": SMALL}]  # fmt: skip

GOOD = """## Lotto daily sales — last 4 days (14–17 November 2026)
| Date | Net sales | Orders | Units |
|---|---:|---:|---:|
| Total | ₹29,06,762 | 1,197 | 1,302 |
| 14 Nov | ₹7,11,406 | 303 | 321 |
| 15 Nov | ₹8,35,670 | 345 | 374 |
Sales peaked on 15 Nov. One8 recorded ₹66.77 crore (92,423 orders), about 11.5× Lotto's ₹5.80 crore, and One8's
net AOV was ₹7,225. Together the two brands sold ₹72.57 crore; One8 was ₹60.97 crore ahead. Growth was 13.7% over
the period, over 110 days, from 2026-08-01, and in 2026."""


def test_figures_from_the_data_and_simple_combinations_pass():
    assert fact_check.unverified(GOOD, RESULTS) == []


def test_invented_figures_are_found():
    bad = GOOD + "\nLotto's best day was ₹9,40,000 and One8 made ₹70 crore."
    assert fact_check.unverified(bad, RESULTS) == ["₹9,40,000", "₹70 crore"]


def test_percentages_multiples_durations_dates_and_years_are_not_figures():
    text = "Up 13.7%, 11.4× more, over 110 days, 3 hours, on 2026-11-17 at 10:30, in 2026, the 21st."
    assert fact_check.reply_figures(text) == []
    assert [v for _, v, _ in fact_check.reply_figures("₹5.80 crore, 1.2 lakh, 3.5M, 92,423")] == [58_000_000.0, 120_000.0, 3_500_000.0, 92_423.0]


async def test_a_reply_with_invented_figures_is_rewritten_from_the_data():
    bad = "Lotto sold ₹31,00,000 over the four days (1,197 orders)."
    calls = []

    async def complete(messages, max_tokens=0):
        calls.append(messages[0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Lotto sold ₹29,06,762 over the four days (1,197 orders)."))])

    out, report = await fact_check.verify_reply(bad, RESULTS, complete)
    assert out == "Lotto sold ₹29,06,762 over the four days (1,197 orders)." and report["rewritten"] and not report["unverified"]
    assert "₹31,00,000" in calls[0] and "net_sales" in calls[0]  # the model got the figure and the data


async def test_a_figure_the_rewrite_cannot_support_is_marked_not_hidden():
    bad = "Lotto sold ₹31,00,000 over the four days."

    async def stubborn(messages, max_tokens=0):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=bad))])

    out, report = await fact_check.verify_reply(bad, RESULTS, stubborn)
    assert report["unverified"] == ["₹31,00,000"] and "Check these figures" in out and "₹31,00,000" in out


async def test_replies_without_data_are_left_alone():
    async def never(*a, **k):
        raise AssertionError("no model call without data")

    text = "Sure — I've scheduled it for 10:30 tomorrow, and the invoice of ₹4,500 is attached."
    assert (await fact_check.verify_reply(text, [], never))[0] == text
    assert (await fact_check.verify_reply(GOOD, RESULTS, never))[0] == GOOD  # all verified: no rewrite call


def test_snapshot_columns_are_not_totalled_and_totals_say_who_added_them():
    rows = [{"sku": f"S{i}", "units": i, "stock": 100 + i, "on_hand": 5, "net_sales": 10 * i} for i in range(5)]
    table = deep.tabulate({"rows": rows})["rows"]
    assert table["column_totals"] == {"units": 10, "net_sales": 100}
    assert "Lumio" in table["column_totals_note"]
