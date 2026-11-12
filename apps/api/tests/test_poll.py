"""Refreshing a page until something appears, done by code."""

from types import SimpleNamespace

from app.agent import computer_harness, fast_browser, poll


def _site(monkeypatch, ready_after):
    state = {"clicks": 0}

    async def fake(ctx, tool, args=None, **k):
        if tool == "browser_act":
            state["clicks"] += 1
            return {"ok": True}
        ready = state["clicks"] >= ready_after
        rows = [{"ref": "e1", "role": "button", "name": "Refresh"}] + ([{"ref": "e2", "role": "button", "name": "Download"}] if ready else [])
        return {"ok": True, "url": "u", "text": "Completed" if ready else "In progress", "elements": rows}

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(computer_harness, "run_computer", fake)
    monkeypatch.setattr(poll.asyncio, "sleep", no_sleep)
    return state


async def test_refreshes_until_the_download_shows(monkeypatch):
    state = _site(monkeypatch, ready_after=3)
    ctx = SimpleNamespace()
    out = await poll.poll(ctx, "Refresh", "Download", every_s=5, max_s=300)
    assert out["found"] is True and out["how"] == "control" and state["clicks"] == 3 and ctx._gathering is False


async def test_gives_up_at_the_limit_and_says_so(monkeypatch):
    times = iter(range(0, 10000, 10))
    _site(monkeypatch, ready_after=10**6)
    monkeypatch.setattr(poll.asyncio, "get_running_loop", lambda: SimpleNamespace(time=lambda: next(times)))
    out = await poll.poll(SimpleNamespace(), "Refresh", "Download", every_s=10, max_s=60)
    assert out["found"] is False and "isn't ready" in out["note"]


async def test_a_missing_refresh_control_is_reported(monkeypatch):
    async def fake(ctx, tool, args=None, **k):
        return {"ok": True, "text": "x", "elements": []}

    monkeypatch.setattr(computer_harness, "run_computer", fake)
    out = await poll.poll(SimpleNamespace(), "Refresh", "Download")
    assert out["ok"] is False and "No control" in out["error"]


def test_generate_and_export_count_once():
    page = {"elements": [{"ref": "e1", "name": "Generate Report"}, {"ref": "e2", "name": "Export CSV"}, {"ref": "e3", "name": "Request Status"}]}
    labels = fast_browser.once_labels([{"kind": "click", "ref": r} for r in ("e1", "e2", "e3")], page)
    assert labels == ["Generate Report", "Export CSV"]


def test_a_download_counts_once_but_each_row_has_its_own():
    page = {"elements": [{"ref": "e1", "name": "download icon"}, {"ref": "e2", "name": "Request Status"}]}
    assert fast_browser.once_labels([{"kind": "click", "ref": "e1"}, {"kind": "click", "ref": "e2"}], page) == ["download icon"]

