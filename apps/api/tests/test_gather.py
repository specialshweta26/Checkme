"""Multi-page collecting and counting done in code."""

from types import SimpleNamespace

import pytest

from app.agent import computer_harness, gather


def _page(n, last=False, text=None):
    elements = [{"ref": "e1", "role": "textbox", "name": "Next"}]
    if not last:
        elements.append({"ref": "e2", "role": "link", "name": "Next →"})
    return {"ok": True, "url": f"https://x.example/page/{n}", "text": text or f"Quote {n} by Albert Einstein\nQuote {n}b by Jane Austen\nPage {n}", "elements": elements}


def _browser(monkeypatch, pages):
    state = {"at": 0, "clicks": []}

    async def fake(ctx, tool, args=None, **k):
        if tool == "browser_snapshot":
            return pages[state["at"]]
        steps = (args or {}).get("actions") or []
        state["clicks"].append(steps)
        state["at"] = min(state["at"] + 1, len(pages) - 1)
        return {**pages[state["at"]]}

    monkeypatch.setattr(computer_harness, "run_computer", fake)
    return state


async def test_every_page_is_walked_and_counted_by_code(monkeypatch):
    state = _browser(monkeypatch, [_page(1), _page(2), _page(3, last=True)])
    ctx = SimpleNamespace()
    out = await gather.gather(ctx, "next")
    assert out["pages"] == 3 and "end" in out["stopped"]
    assert state["clicks"][0] == [{"kind": "click", "ref": "e2"}]  # the link, not the text box named Next
    count = gather.query(ctx, out["dataset"], "count", ["einstein"])
    assert count["matched_lines"] == 3 and count["per_page"] == [1, 1, 1]


async def test_a_page_that_stops_changing_ends_the_walk(monkeypatch):
    _browser(monkeypatch, [_page(1), _page(1)])
    out = await gather.gather(SimpleNamespace(), "next")
    assert out["pages"] == 1 and "stopped changing" in out["stopped"]


async def test_the_page_limit_holds(monkeypatch):
    _browser(monkeypatch, [_page(n) for n in range(1, 9)])
    out = await gather.gather(SimpleNamespace(), "next", max_pages=3)
    assert out["pages"] == 3 and "limit" in out["stopped"]


async def test_a_disabled_next_is_not_clicked(monkeypatch):
    page = _page(1)
    page["elements"][1]["disabled"] = True
    state = _browser(monkeypatch, [page])
    out = await gather.gather(SimpleNamespace(), "next")
    assert out["pages"] == 1 and state["clicks"] == []


async def test_load_more_keeps_only_the_grown_page(monkeypatch):
    first = {"ok": True, "url": "u", "text": "one", "elements": [{"ref": "e1", "role": "button", "name": "Load more"}]}
    second = {"ok": True, "url": "u", "text": "one\ntwo", "elements": []}
    _browser(monkeypatch, [first, second])
    ctx = SimpleNamespace()
    out = await gather.gather(ctx, "load_more")
    assert out["pages"] == 1 and gather.query(ctx, out["dataset"], "count")["matched_lines"] == 2


async def test_cut_off_pages_are_flagged(monkeypatch):
    _browser(monkeypatch, [{**_page(1, last=True), "text_omitted": True}])
    out = await gather.gather(SimpleNamespace(), "next")
    assert "couldn't be read" in out["warning"]


def _ctx(*pages):
    ctx = SimpleNamespace()
    ctx._gathered = {"d1": [{"url": "u", "lines": lines} for lines in pages]}
    return ctx


def test_words_and_exclusions_ignore_case():
    ctx = _ctx(["Tea London", "Milk LONDON paid", "Sugar Paris"])
    assert gather.query(ctx, "d1", "count", ["london"], ["paid"])["matched_lines"] == 1


def test_sums_read_money_and_table_columns():
    ctx = _ctx(["A\t$1,200.50", "B\t₹1,00,000"], ["C\tRs. 10"])
    out = gather.query(ctx, "d1", "sum", column=2)
    assert out["sum"] == 101210.5 and out["per_page"] == [2, 1]


def test_a_row_with_several_amounts_is_not_guessed():
    ctx = _ctx(["Order 7 $10 $20", "Order 8\t$30"])
    out = gather.query(ctx, "d1", "sum", ["order"])
    assert out["sum"] == 30.0 and out["skipped_lines"] == 1 and "column" in out["warning"]


def test_unique_counts_repeated_lines_once():
    ctx = _ctx(["Header", "a"], ["Header", "b"])
    assert gather.query(ctx, "d1", "count", unique=True)["matched_lines"] == 3


def test_unknown_datasets_and_actions_are_refused():
    ctx = _ctx(["a"])
    assert gather.query(ctx, "d9")["ok"] is False
    assert gather.query(ctx, "d1", "delete")["ok"] is False


@pytest.mark.parametrize("name,found", [("Next →", True), ("  next ", True), ("Next steps", False), ("›", True)])
def test_next_controls_are_found_by_name(name, found):
    row = {"ref": "e1", "role": "link", "name": name}
    assert (gather.find_control([row], gather._NEXT_WORDS) is not None) is found


def test_a_long_page_is_stitched_without_losing_real_duplicates():
    held, whole = gather.grow([], {"text": "a\nby Einstein\nb\nby Einstein"})
    assert whole and held == ["a", "by Einstein", "b", "by Einstein"]
    more, whole = gather.grow(held, {"text": "a\n…\ncut off\nby Einstein\nb\nby Einstein\nc\nby Einstein", "text_omitted": True})
    assert whole and more == ["a", "by Einstein", "b", "by Einstein", "c", "by Einstein"]


def test_a_gap_in_a_long_page_is_flagged():
    more, whole = gather.grow(["a", "b"], {"text": "a\n…\nx\ny", "text_omitted": True})
    assert not whole and more == ["a", "b"]  # never adds text that might be counted twice


async def test_a_slow_page_gets_more_chances(monkeypatch):
    calls = []
    pages = [_page(1), _page(1), _page(2, last=True)]

    async def fake(ctx, tool, args=None, **k):
        if tool == "browser_snapshot":
            return pages[0]
        calls.append(args)
        return pages[min(len(calls), 2)]

    monkeypatch.setattr(computer_harness, "run_computer", fake)
    out = await gather.gather(SimpleNamespace(), "next")
    assert out["pages"] == 2
    assert calls[1]["actions"][0]["kind"] == "wait"  # waits rather than clicking Next twice


async def test_gathering_goes_back_to_the_first_page_first(monkeypatch):
    def page(n, last):
        rows = []
        if n > 1:
            rows.append({"ref": "p", "role": "link", "name": "Previous"})
        if not last:
            rows.append({"ref": "n", "role": "link", "name": "Next"})
        return {"ok": True, "url": f"u{n}", "text": f"row {n}", "elements": rows}

    state = {"at": 2}

    async def fake(ctx, tool, args=None, **k):
        if tool == "browser_snapshot":
            return page(state["at"], state["at"] == 3)
        ref = args["actions"][0]["ref"]
        state["at"] += 1 if ref == "n" else -1
        return page(state["at"], state["at"] == 3)

    monkeypatch.setattr(computer_harness, "run_computer", fake)
    ctx = SimpleNamespace()
    out = await gather.gather(ctx, "next")
    assert out["went_back_pages"] == 1 and out["pages"] == 3
    assert gather.query(ctx, out["dataset"], "count", ["row"])["per_page"] == [1, 1, 1]
    assert ctx._gathering is False


async def test_figures_from_an_incomplete_dataset_say_so(monkeypatch):
    _browser(monkeypatch, [{**_page(1, last=True), "text_omitted": True}])
    ctx = SimpleNamespace()
    out = await gather.gather(ctx, "next")
    assert out["incomplete"] is True
    figure = gather.query(ctx, out["dataset"], "count", ["einstein"])
    assert figure["incomplete"] is True and "Tell the user" in figure["warning"]


def test_stitching_copes_with_a_footer_that_is_always_last():
    held = ["head", "q1", "by A", "q2", "by B", "footer"]
    read = {"text": "head\n…\ncut\nq1\nby A\nq2\nby B\nq3\nby A\nfooter", "text_omitted": True}
    more, whole = gather.grow(held, read)
    assert whole and more == ["head", "q1", "by A", "q2", "by B", "q3", "by A", "footer"]


async def test_a_label_without_the_arrow_still_finds_the_control(monkeypatch):
    _browser(monkeypatch, [_page(1), _page(2, last=True)])
    out = await gather.gather(SimpleNamespace(), "next", next_label="Next")
    assert out["pages"] == 2


def test_a_wrong_label_falls_back_to_the_usual_names():
    assert gather.find_control([{"ref": "e2", "role": "link", "name": "Next →"}], gather._NEXT_WORDS, "Forward")["ref"] == "e2"
    assert gather.find_control([{"ref": "e2", "role": "link", "name": "Next »"}], gather._NEXT_WORDS, "Next")["ref"] == "e2"


def test_a_word_inside_other_text_is_called_out_when_counting_records():
    lines = [f"by Author {n}" for n in range(10)] + ["Baby steps", "Written by nobody"]
    ctx = _ctx(lines)
    loose = gather.query(ctx, "d1", "count", ["by"])
    assert loose["matched_lines"] == 12 and "starts_with ['by']" in loose["warning"]
    exact = gather.query(ctx, "d1", "count", starts_with=["by"])
    assert exact["matched_lines"] == 10 and "warning" not in exact


def test_starts_with_needs_whole_words():
    assert gather.query(_ctx(["by Ann", "bystander", "BY Bob"]), "d1", "count", starts_with=["by"])["matched_lines"] == 2


async def test_gather_says_how_lines_start(monkeypatch):
    text = "\n".join(f"“Quote {n}”\nby Person {n}\nTags: a" for n in range(5))
    _browser(monkeypatch, [{"ok": True, "url": "u", "text": text, "elements": []}])
    out = await gather.gather(SimpleNamespace(), "next")
    assert {"starts_with": "by", "lines": 5} in out["line_kinds"] and {"starts_with": "Tags:", "lines": 5} in out["line_kinds"]


def _guard(query):
    from app.agent import browser_guard

    return browser_guard, SimpleNamespace(query=query)


def test_scrolling_by_hand_towards_a_figure_is_sent_to_gather():
    guard, ctx = _guard("Load all of them. How many quotes are there?")
    state = {"elements": []}
    assert not guard._hand_collecting(ctx, state, [{"kind": "scroll"}, {"kind": "scroll"}])
    assert guard._hand_collecting(ctx, state, [{"kind": "scroll"}])


def test_a_figure_request_is_free_to_use_the_page_after_gathering():
    guard, ctx = _guard("How many quotes are there?")
    ctx._gathered = {"d1": []}
    assert not guard._hand_collecting(ctx, {"elements": []}, [{"kind": "scroll"}] * 6)


def test_scrolling_with_no_figure_wanted_is_left_alone():
    guard, ctx = _guard("Scroll down and open the pricing section")
    assert not guard._hand_collecting(ctx, {"elements": []}, [{"kind": "scroll"}] * 6)


async def test_price_lines_show_in_line_kinds_and_can_be_summed(monkeypatch):
    text = "\n".join(f"Book {n}\n£{n}.50\nIn stock\nAdd to basket" for n in range(1, 6)) + "\n£ prices are made up"
    _browser(monkeypatch, [{"ok": True, "url": "u", "text": text, "elements": []}])
    ctx = SimpleNamespace()
    out = await gather.gather(ctx, "next")
    assert {"starts_with": "£", "lines": 5} in out["line_kinds"]
    total = gather.query(ctx, out["dataset"], "sum", starts_with=["£"])
    # The note starts with "£" too but holds no amount, so it's left out of the sum and reported.
    assert total["sum"] == 17.5 and total["summed_lines"] == 5 and total["skipped_lines"] == 1

