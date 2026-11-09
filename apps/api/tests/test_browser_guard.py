"""A browser turn must not repeat actions that change nothing (one real turn ran a dropdown action about 80 times)."""

from types import SimpleNamespace

from app.agent import browser_guard as guard


def _ctx():
    return SimpleNamespace()


def _page(text="catalogue"):
    return {"ok": True, "url": "https://shop.example/items", "title": "Shop", "text": text, "tree": "- e1 button Add"}


def _act(ctx, args, result):
    refused = guard.before(ctx, "browser_act", args)
    return refused if refused is not None else guard.after(ctx, "browser_act", args, result)


SORT = {"actions": [{"kind": "select", "ref": "e5", "text": "Price"}]}


def test_the_same_action_is_refused_when_it_keeps_changing_nothing():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _page())
    assert _act(ctx, SORT, _page())["ok"] is True
    assert _act(ctx, SORT, _page())["ok"] is True
    third = _act(ctx, SORT, _page())
    assert third["ok"] is False and third["repeated"] is True
    assert "2 times" in third["error"]
    assert not guard.is_stuck(ctx)


def test_an_action_that_works_can_be_repeated():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _page())
    add = {"actions": [{"kind": "click", "ref": "e1"}]}
    for n in range(6):
        assert _act(ctx, add, _page(f"cart has {n}"))["ok"] is True
    assert not guard.is_stuck(ctx)


def test_many_different_actions_that_change_nothing_end_the_browser_work():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _page())
    results = [_act(ctx, {"actions": [{"kind": "click", "ref": f"e{i}"}]}, _page()) for i in range(guard.NO_CHANGE_STOP)]
    assert any("changed nothing" in " ".join(r.get("warnings") or []) for r in results)
    assert results[-1]["stopped"] is True
    assert guard.is_stuck(ctx)
    later = guard.before(ctx, "browser_act", {"actions": [{"kind": "click", "ref": "e99"}]})
    assert later["stopped"] is True


def test_a_change_of_page_resets_the_count():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _page())
    for i in range(guard.NO_CHANGE_WARN):
        _act(ctx, {"actions": [{"kind": "click", "ref": f"e{i}"}]}, _page())
    assert _act(ctx, {"actions": [{"kind": "click", "ref": "e50"}]}, _page("next page"))["ok"] is True
    next_result = _act(ctx, {"actions": [{"kind": "click", "ref": "e51"}]}, _page("next page"))
    assert not next_result.get("warnings")


def test_logging_in_again_does_not_clear_the_repeat_count():
    # The real turn navigated back, logged in again and resumed the same dropdown action.
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _page())
    _act(ctx, SORT, _page())
    _act(ctx, SORT, _page())
    guard.after(ctx, "browser_navigate", {"url": "https://shop.example/"}, _page("login"))
    guard.after(ctx, "browser_snapshot", {}, _page())
    assert _act(ctx, SORT, _page())["repeated"] is True


def test_scrolling_and_waiting_are_never_counted():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _page())
    scroll = {"actions": [{"kind": "scroll", "dy": 800}]}
    for _ in range(guard.NO_CHANGE_STOP + 2):
        assert _act(ctx, scroll, _page())["ok"] is True
    assert not guard.is_stuck(ctx)


def test_failures_without_a_page_count_as_no_change():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _page())
    bad = {"actions": [{"kind": "click", "ref": "e?"}]}
    failure = {"ok": False, "error": "ref not found: e?"}
    _act(ctx, bad, failure)
    _act(ctx, bad, failure)
    assert _act(ctx, bad, failure)["repeated"] is True


def test_it_never_keeps_what_was_typed():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _page())
    secret = {"actions": [{"kind": "fill", "ref": "e2", "text": "hunter2-secret"}]}
    _act(ctx, secret, _page())
    assert "hunter2-secret" not in str(ctx._browser_guard)


def test_other_tools_pass_through():
    ctx = _ctx()
    assert guard.before(ctx, "computer_observe", {}) is None
    assert guard.after(ctx, "computer_observe", {}, {"ok": True}) == {"ok": True}


def test_a_big_page_reaches_the_browser_tools_whole():
    # An outline of a big page has no text or elements, and the agent read such pages as blank.
    from app.agent.deep import BROWSER_PAGE_CHARS, BROWSER_PAGE_TOOLS, MAX_TOOL_CHARS, compact_payload

    assert {"browser_navigate", "browser_snapshot", "browser_act"} <= BROWSER_PAGE_TOOLS
    page = {"url": "https://x.example/", "text": "a" * 12000, "tree": "- e1 button Go\n" * 500, "elements": ["e"] * 3000}
    assert compact_payload(page, MAX_TOOL_CHARS).get("too_large") is True
    assert compact_payload(page, BROWSER_PAGE_CHARS)["text"] == page["text"]


def test_a_dropdown_already_on_that_option_does_not_count_as_stuck():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _page())
    done = {**_page(), "warnings": ["Price was already chosen in this dropdown, so nothing needed to change."]}
    for _ in range(4):
        assert _act(ctx, SORT, dict(done))["ok"] is True
    assert ctx._browser_guard["idle"] == 0


def test_a_choice_sent_as_value_reaches_the_browser_as_text():
    args = {"actions": [{"kind": "select", "ref": "e2", "value": "lohi"}, {"kind": "click", "ref": "e3"}], "tab": "t1"}
    out = guard.normalize(args)
    assert out["actions"][0]["text"] == "lohi" and "text" not in out["actions"][1] and out["tab"] == "t1"
    keep = {"actions": [{"kind": "fill", "ref": "e1", "text": "a", "value": "b"}]}
    assert guard.normalize(keep)["actions"][0]["text"] == "a"


def test_logged_errors_drop_quoted_text():
    text = guard._safe_error({"error": 'this dropdown has no option "hunter2". Its options are: A | B'})
    assert "hunter2" not in text and "no option" in text


def _checkout_page():
    return {**_page(), "elements": [{"ref": "e1", "role": "button", "name": "Finish"}, {"ref": "e2", "role": "button", "name": "Add to cart"}]}


def test_finish_cannot_be_clicked_twice_in_a_turn():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _checkout_page())
    finish = {"actions": [{"kind": "click", "ref": "e1"}]}
    assert _act(ctx, finish, {**_checkout_page(), "text": "thanks"})["ok"] is True
    assert guard.final_step_done(ctx)
    guard.after(ctx, "browser_navigate", {}, _checkout_page())
    second = _act(ctx, finish, _checkout_page())
    assert second["already_done"] is True and "Finish" in second["error"]


def test_a_finish_that_did_not_run_can_still_be_tried():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _checkout_page())
    batch = {"actions": [{"kind": "click", "ref": "e2"}, {"kind": "click", "ref": "e1"}]}
    _act(ctx, batch, {"ok": False, "completed": 1, "error": "step 2 failed"})
    assert not guard.final_step_done(ctx)


def test_ordinary_buttons_can_repeat():
    ctx = _ctx()
    guard.after(ctx, "browser_snapshot", {}, _checkout_page())
    add = {"actions": [{"kind": "click", "ref": "e2"}]}
    assert _act(ctx, add, {**_checkout_page(), "text": "one"})["ok"] is True
    assert _act(ctx, add, {**_checkout_page(), "text": "two"})["ok"] is True
    assert not guard.final_step_done(ctx)
