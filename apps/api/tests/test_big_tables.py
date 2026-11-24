"""Big tables (docs/LLD.md "Big tables"): a sample and the columns first, narrowed at the source; a big fetched table
goes to a file on the computer for a script, never row by row through the model."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.agent import computer_harness, deep
from app.config import get_settings


def _row(i: int) -> list:
    return [f"2026-11-{1 + i % 24:02d}", "Online" if i % 2 else "Store", "BrandA", "SKU-%d" % (i % 50), 1, 1999.0]


def _page(offset: int, limit: int, total: int) -> dict:
    rows = [_row(i) for i in range(offset, min(offset + limit, total))]
    return {"ok": True, "totalRows": total, "returnedRows": len(rows), "offset": offset, "limit": limit,
            "hasMore": offset + len(rows) < total, "data": rows}  # fmt: skip


def _setup(monkeypatch, total, computer=True):
    calls, files = [], {}

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        calls.append(dict(args))
        return _page(int(args.get("offset", 0)), min(int(args.get("limit", 500)), 500), total)

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
    hit = SimpleNamespace(tool_name="srv__get_sku_daily", read_only=True, destructive=False, connector_name="Gateway",
                          schema={"type": "object", "properties": {"brand": {"type": "string"}, "channel": {"type": "string"},
                                                                   "from_date": {"type": "string"}, "to_date": {"type": "string"},
                                                                   "offset": {"type": "integer"}, "limit": {"type": "integer"}}})  # fmt: skip

    async def load(db, conns, wanted):
        return [hit] if ("gw", hit.tool_name) in wanted else []

    monkeypatch.setattr(deep, "load_named", load)
    conn = SimpleNamespace(kind="mcp", server_url="https://gw.example.test/mcp", alias="", name="Gateway", server_key="mcp:gw",
                           id="c1", health="ok", slug="gw", provider="mcp")  # fmt: skip
    conns = {"gw": conn}
    if computer:
        conns["computer"] = SimpleNamespace(slug="computer", kind="builtin", provider="daytona", server_url="", name="Computer", alias="")
    ctx = deep.RunContext(db=object(), user=SimpleNamespace(id="u1", timezone="Asia/Kolkata"), connectors=conns, auto_confirm=True,
                          query="product wise sales across channels nov mtd")  # fmt: skip
    return ctx, calls, files


async def test_a_big_table_comes_back_as_a_sample_with_its_filters(monkeypatch):
    ctx, calls, files = _setup(monkeypatch, total=104255)
    out = await deep.call_user_tool(ctx, "gw", "srv__get_sku_daily", {"from_date": "2026-11-01", "to_date": "2026-11-24"})
    assert out["big_table"] and out["total_rows"] == 104255 and len(out["sample_rows"]) == 8
    assert {"brand", "channel"} <= set(out["parameters"]["properties"]) and "filters" in out["next"]
    assert len(calls) == 1 and not files  # nothing read beyond the first page


async def test_a_narrowed_call_reads_every_page_into_a_file_on_the_computer(monkeypatch):
    ctx, calls, files = _setup(monkeypatch, total=1200)
    out = await deep.call_user_tool(ctx, "gw", "srv__get_sku_daily", {"brand": "BrandA", "from_date": "2026-11-01"})
    assert out["rows"] == 1200 and out["saved_to_computer"].startswith("/home/user/lumio_data/")
    pages = [files[k] for k in sorted(files)]
    assert sum(len(p) for p in pages) == 1200 and all(k.rsplit("/", 1)[1].startswith("part_") for k in files)
    assert [r for p in pages for r in p] == [_row(i) for i in range(1200)]  # every row, in order
    assert "run_code" in out["next"] and "glob" in out["next"] and len(out["sample_rows"]) == 8
    assert len(json.dumps(out)) < 5000  # the model gets the path and a sample, not the rows


async def test_the_same_call_again_reads_the_whole_table(monkeypatch):
    ctx, calls, files = _setup(monkeypatch, total=2600)
    args = {"from_date": "2026-11-01"}
    first = await deep.call_user_tool(ctx, "gw", "srv__get_sku_daily", dict(args))
    assert first["big_table"]
    again = await deep.call_user_tool(ctx, "gw", "srv__get_sku_daily", dict(args))
    assert again["rows"] == 2600 and again["saved_to_computer"]


async def test_without_a_computer_the_table_comes_back_as_before(monkeypatch):
    ctx, calls, files = _setup(monkeypatch, total=1200, computer=False)
    out = await deep.call_user_tool(ctx, "gw", "srv__get_sku_daily", {"brand": "BrandA"})
    assert "saved_to_computer" not in out and not files


async def test_a_small_table_is_answered_whole(monkeypatch):
    ctx, calls, files = _setup(monkeypatch, total=120)
    out = await deep.call_user_tool(ctx, "gw", "srv__get_sku_daily", {"brand": "BrandA"})
    assert len(out["data"]) == 120 and not files


async def test_a_few_very_wide_rows_go_to_a_file_too(monkeypatch):
    ctx, calls, files = _setup(monkeypatch, total=0)

    async def invoke(db, user, conn, tool_name, args, attachments=None):
        return {"rows": [{"sku": f"S{i}", "notes": "x" * 2000} for i in range(60)]}  # small count, too big to read inline

    monkeypatch.setattr(deep, "invoke_named", invoke)
    out = await deep.call_user_tool(ctx, "gw", "srv__get_sku_daily", {"brand": "BrandA"})
    assert out["rows"] == 60 and out["saved_to_computer"] and out["columns"] == ["sku", "notes"]


async def test_the_server_keeps_only_a_sample_once_a_table_spills_to_files():
    from app.agent import tables

    written = {}

    async def write(part, rows):
        written[part] = rows
        return {"ok": True}

    spool = tables.SpoolRows(write, keep=300)
    for start in range(0, 5000, 500):
        spool.extend([[i] for i in range(start, start + 500)])
    assert await spool.flush() and spool.spilled and len(spool) == 5000
    assert spool.buffer == [] and len(spool.sample) == 8 and len(list(spool)) == 8  # nothing else held in memory
    assert [r for p in sorted(written) for r in written[p]] == [[i] for i in range(5000)]


async def test_a_small_table_stays_in_memory_and_writes_nothing():
    from app.agent import tables

    async def write(part, rows):
        raise AssertionError("must not write")

    spool = tables.SpoolRows(write, keep=300)
    spool.extend([[1], [2], [3]])
    assert not spool.spilled and list(spool) == [[1], [2], [3]] and await spool.flush()
