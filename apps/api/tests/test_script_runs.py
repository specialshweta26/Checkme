"""Script runs (docs/LLD.md "Script runs"): a month-plus-product-breakdown data question ("a brand's sales in October
and product-wise details") reads its tables together, analyses them in one script, and never runs a script again just
because its output was cut off or because it already ran."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.agent import computer_harness, deep
from app.agent.harness_extra import _dumps, extra_harness_tools
from app.config import get_settings


def _ctx(**extra):
    return deep.RunContext(db=None, user=SimpleNamespace(id="u1", timezone="Asia/Kolkata"), connectors={}, auto_confirm=True,
                           query="brand sales in october month and product wise sales details", **extra)  # fmt: skip


def _product_report(products: int = 900) -> str:
    rows = [f"SKU-{i:05d} | Product name number {i} | units {i * 3} | revenue {i * 101.5:.2f}" for i in range(products)]
    return "\n".join(["OCTOBER TOTAL revenue 41,067,247.50 units 1,213,650", *rows, "TOTAL products 900 revenue 41,067,247.50"])


def _fake_computer(monkeypatch, stdout: str = "ok", delay: float = 0.0):
    runs: list[tuple[str, dict]] = []
    state = {"now": 0, "max": 0}

    async def checked(ctx, tool_name, args, *, force_cloud=False, gated=False):
        runs.append((tool_name, dict(args or {})))
        state["now"] += 1
        state["max"] = max(state["max"], state["now"])
        await asyncio.sleep(delay)
        state["now"] -= 1
        if tool_name == "run_code":
            return {"ok": True, "exit_code": 0, "stdout": stdout, "ran_on": "local_computer"}
        return {"ok": True, "path": "/home/user/" + str((args or {}).get("path") or "")}

    monkeypatch.setattr(computer_harness, "_run_computer_checked", checked)
    return runs, state


def _scripts(runs) -> list[str]:
    return [a["code"] for t, a in runs if t == "run_code"]


async def _run_code(ctx, code: str) -> dict:
    tool = next(t for t in extra_harness_tools(ctx) if t.name == "run_code")
    return json.loads(await tool.coroutine(code=code))


async def test_a_long_product_table_reaches_the_model_instead_of_an_empty_outline(monkeypatch):
    report = _product_report()
    assert len(report) > 40000
    before = json.loads(_dumps({"ok": True, "exit_code": 0, "stdout": report}, 40000))
    assert before.get("too_large") and not isinstance(before["map"]["stdout"], str) and "result_id" not in before
    # ^ how it was: no text at all, and the hint named a result_id it didn't give, so the agent ran the script again.

    ctx = _ctx()
    runs, _ = _fake_computer(monkeypatch, report)
    out = await _run_code(ctx, "print(report)")
    assert not out.get("too_large") and isinstance(out["stdout"], str)
    assert out["stdout"].startswith("OCTOBER TOTAL revenue 41,067,247.50")  # the month's total, first line
    assert out["stdout"].rstrip().endswith("TOTAL products 900 revenue 41,067,247.50")  # and the last
    cut = out["stdout_cut"]
    assert cut["lines"] == 902 and cut["first_lines"] > 300 and cut["result_id"]
    assert "Don't run it again" in out["note"]
    # The lines in between are stored: inspect_result reads them, no second run.
    page = deep.inspect_user_result(ctx, cut["result_id"], "stdout_lines", None, cut["first_lines"], 50)
    assert page["data"] and page["data"]["items"][0].startswith(f"SKU-{cut['first_lines'] - 1:05d}")
    assert len(_scripts(runs)) == 1


async def test_the_same_script_twice_runs_once(monkeypatch):
    ctx = _ctx()
    runs, _ = _fake_computer(monkeypatch, "total 42")
    first = await _run_code(ctx, "print('total', 42)")
    again = await _run_code(ctx, "print('total', 42)\n")
    assert first["stdout"] == again["stdout"] == "total 42" and again["not_run_again"]
    assert len(_scripts(runs)) == 1


async def test_a_script_runs_again_after_the_computer_changed(monkeypatch):
    ctx = _ctx()
    runs, _ = _fake_computer(monkeypatch, "total 42")
    await _run_code(ctx, "print(open('lumio_data/x.json').read())")
    await computer_harness.write_bytes(ctx, "lumio_data/x.json", b"[]")  # a new table landed
    out = await _run_code(ctx, "print(open('lumio_data/x.json').read())")
    assert not out.get("not_run_again") and len(_scripts(runs)) == 2


async def test_a_failed_script_can_be_run_again(monkeypatch):
    ctx = _ctx()
    calls = []

    async def checked(ctx_, tool_name, args, *, force_cloud=False, gated=False):
        calls.append(tool_name)
        return {"ok": False, "exit_code": 1, "stdout": "ModuleNotFoundError: pandas"}

    monkeypatch.setattr(computer_harness, "_run_computer_checked", checked)
    await _run_code(ctx, "import pandas")
    await _run_code(ctx, "import pandas")
    assert calls == ["run_code", "run_code"]


async def test_scripts_asked_for_at_once_take_turns(monkeypatch):
    """The desktop app runs every script from one file: two at once could run each other's code."""
    ctx = _ctx()
    runs, state = _fake_computer(monkeypatch, "ok", delay=0.02)
    await asyncio.gather(_run_code(ctx, "print(1)"), _run_code(ctx, "print(2)"))
    assert state["max"] == 1 and sorted(_scripts(runs)) == ["print(1)", "print(2)"]


# --- the connector side: both tables together, each read once, one script for both -----------------------------------


def _row(i: int) -> list:
    return [f"2026-10-{1 + i % 31:02d}", "Online", "BrandA", "SKU-%d" % (i % 400), 1, 1999.0]


def _setup(monkeypatch, totals: dict[str, int], delays: dict[str, float]):
    calls: list[tuple[str, int]] = []
    files: dict[str, list] = {}

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        offset, limit = int(args.get("offset", 0)), min(int(args.get("limit", 500)), 500)
        calls.append((tool_name, offset))
        await asyncio.sleep(delays.get(tool_name, 0))
        total = totals[tool_name]
        rows = [_row(i) for i in range(offset, min(offset + limit, total))]
        return {"ok": True, "totalRows": total, "returnedRows": len(rows), "offset": offset, "limit": limit,
                "hasMore": offset + len(rows) < total, "data": rows}  # fmt: skip

    async def write_bytes(ctx, path, data):
        files[path] = json.loads(data)
        return {"ok": True, "path": "/home/user/" + path}

    async def no_usage(*a, **k):
        return None

    monkeypatch.setattr(deep, "invoke_named", invoke)
    monkeypatch.setattr(deep, "bump_usage", no_usage)
    monkeypatch.setattr(computer_harness, "write_bytes", write_bytes)
    monkeypatch.setattr(get_settings(), "big_table_rows", 2000)
    monkeypatch.setattr(get_settings(), "table_file_rows", 300)
    props = {k: {"type": "string"} for k in ("brand", "from_date", "to_date")} | {"offset": {"type": "integer"}, "limit": {"type": "integer"}}
    hits = {name: SimpleNamespace(tool_name=name, read_only=True, destructive=False, connector_name="Gateway",
                                  schema={"type": "object", "properties": props}) for name in totals}  # fmt: skip

    async def load(db, conns, wanted):
        return [hits[t] for (s, t) in wanted if s == "gw" and t in hits]

    monkeypatch.setattr(deep, "load_named", load)
    conn = SimpleNamespace(kind="mcp", server_url="https://gw.example.test/mcp", alias="", name="Gateway", server_key="mcp:gw",
                           id="c1", health="ok", slug="gw", provider="mcp")  # fmt: skip
    computer = SimpleNamespace(slug="computer", kind="builtin", provider="daytona", server_url="", name="Computer", alias="")
    ctx = deep.RunContext(db=object(), user=SimpleNamespace(id="u1", timezone="Asia/Kolkata"), connectors={"gw": conn, "computer": computer},
                          auto_confirm=True, query="brand sales in october month and product wise sales details")  # fmt: skip
    tool = next(t for t in deep.harness_tools(ctx) if t.name == "table_to_computer")
    return ctx, tool, calls, files


async def test_two_tables_read_at_once_both_come_back_whole_and_point_to_one_script(monkeypatch):
    """October's daily sales and the product-wise table, asked for in one step. The flag that says 'read it whole' was
    on the turn's context, so the faster call cleared it while the slower one still ran: that one came back as a
    sample, and the agent had to ask for it again."""
    totals = {"srv__sales_daily": 2600, "srv__sales_by_product": 3100}
    ctx, tool, calls, files = _setup(monkeypatch, totals, {"srv__sales_daily": 0.0, "srv__sales_by_product": 0.03})
    args = {"brand": "BrandA", "from_date": "2026-10-01", "to_date": "2026-10-31"}
    daily, product = await asyncio.gather(
        tool.coroutine(connector_slug="gw", tool_name="srv__sales_daily", args=dict(args)),
        tool.coroutine(connector_slug="gw", tool_name="srv__sales_by_product", args=dict(args)),
    )
    daily, product = json.loads(daily), json.loads(product)
    assert not daily.get("big_table") and not product.get("big_table")
    assert {daily["rows"], product["rows"]} == {2600, 3100}
    newest = daily if daily.get("saved_this_turn") else product
    assert len(newest["saved_this_turn"]) == 2 and "ONE Python script" in newest["next"]
    assert sum(len(v) for v in files.values()) == 2600 + 3100
    assert not deep._full_table(ctx)  # nothing left behind for the next ordinary call


async def test_a_full_read_after_a_sample_starts_from_the_page_it_already_has(monkeypatch):
    ctx, tool, calls, files = _setup(monkeypatch, {"srv__sales_by_product": 2600}, {})
    args = {"from_date": "2026-10-01", "to_date": "2026-10-31"}
    sample = await deep.call_user_tool(ctx, "gw", "srv__sales_by_product", dict(args))
    assert sample["big_table"]
    whole = json.loads(await tool.coroutine(connector_slug="gw", tool_name="srv__sales_by_product", args=dict(args)))
    assert whole["rows"] == 2600
    assert [o for _, o in calls].count(0) == 1  # the first page was asked for once, not twice
    assert ctx.call_timings[-1].get("first_page_reused")


async def test_the_prompt_asks_for_one_script_and_calls_together():
    text = deep.system_prompt(SimpleNamespace(timezone="Asia/Kolkata"), [], "")
    assert "never a script per month, product or breakdown" in text
    assert "per-product or per-month" in text
