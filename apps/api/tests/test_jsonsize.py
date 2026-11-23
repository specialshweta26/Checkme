"""Big results (docs/LLD.md "Big results"): size checks stop at the limit and give the same answer as encoding all."""

from __future__ import annotations

import json
import time

from app.agent import deep
from app.agent.jsonsize import bounded_dumps, fits, size_or_more


def test_same_answer_as_encoding_everything():
    for value in ({"a": [1, 2, 3]}, {"d": "x" * 500}, [{"n": i, "t": "₹"} for i in range(50)], {"when": __import__("datetime").date(2026, 11, 22)}):
        full = json.dumps(value, default=str)
        assert bounded_dumps(value, len(full)) == full and bounded_dumps(value, len(full) - 1) is None
        assert fits(value, len(full)) and not fits(value, len(full) - 1)
    assert size_or_more({"d": "x" * 5000}, 100) == 100


def test_a_huge_table_is_judged_in_a_moment():
    rows = [[i, "One8", "Seam", 1500.5, 1300.25] * 5 for i in range(100_000)]
    started = time.monotonic()
    assert not fits({"data": rows}, 60_000)
    assert time.monotonic() - started < 0.05
    out = deep.fit_payload({"data": rows, "hasMore": False})
    cap = min(deep.CONNECTOR_RESULT_CHARS, deep.INLINE_RESULT_CHARS)
    assert out["partial"] and out["chars"] > cap and len(json.dumps(out["data"])) <= cap


def test_a_breakdown_keyed_by_name_becomes_a_table_that_can_be_cut():
    from app.agent.deep import fit_payload, tabulate

    by_state = {f"State {i}": {"units": i, "net_sales": i * 10.0} for i in range(40)}
    table = tabulate({"summary": {"by_state": by_state, "total": 1}})["summary"]["by_state"]
    assert table["columns"] == ["name", "units", "net_sales"] and table["row_count"] == 40
    assert table["column_totals"]["units"] == sum(range(40))
    skus = tabulate({f"SKU-{i}": i for i in range(25)})
    assert skus["columns"] == ["name", "value"]
    # An ordinary object is left alone.
    assert tabulate({"brand": {"id": "x"}, "period": {"from": "a"}, "totals": {"units": 3}}) == {"brand": {"id": "x"}, "period": {"from": "a"}, "totals": {"units": 3}}
    big = {"summary": {"by_sku": {f"SKU-{i}": {"units": i, "net_sales": float(i), "title": "Seam XVIII Signature " * 4} for i in range(3000)}}}
    out = fit_payload(big, 20000)
    assert out.get("partial") and out["data"]["summary"]["by_sku"]["column_totals"]["units"] == sum(range(3000))
