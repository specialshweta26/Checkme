"""A cloud-computer download also lands in the user's own Downloads folder when the desktop app is connected
(docs/BROWSING.md "Downloads on your computer")."""

from types import SimpleNamespace

import pytest

from app.agent import computer_harness as ch
from app.config import get_settings
from app.desktop import agent as desk

CTX = SimpleNamespace(user=SimpleNamespace(id="u1"))
ITEM = {"filename": "Order Status.csv", "download_url": "https://app.test/api/files?t=abc", "path": "/home/user/Downloads/o.csv"}


@pytest.fixture
def desktop(monkeypatch):
    calls = []
    state = {"online": True, "result": {"ok": True, "path": "/Users/me/Downloads/Order Status.csv"}}

    async def online(user_id):
        return state["online"]

    async def call_local(user_id, tool, args=None):
        calls.append((tool, dict(args or {})))
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(desk, "is_online", online)
    monkeypatch.setattr(desk, "call_local", call_local)
    return SimpleNamespace(calls=calls, state=state)


async def test_saves_each_download_on_the_desktop(desktop):
    items = [dict(ITEM)]
    assert await ch.save_on_desktop(CTX, items) == 1
    assert desktop.calls == [("save_download", {"url": ITEM["download_url"], "filename": "Order Status.csv", "timeout": 60})]
    assert items[0]["saved_on_your_computer"] == "/Users/me/Downloads/Order Status.csv"
    assert "save_download" in desk.LOCAL_TOOLS


async def test_offline_desktop_or_switch_off_does_nothing(desktop, monkeypatch):
    desktop.state["online"] = False
    assert await ch.save_on_desktop(CTX, [dict(ITEM)]) == 0
    desktop.state["online"] = True
    monkeypatch.setattr(get_settings(), "downloads_to_desktop", False)
    assert await ch.save_on_desktop(CTX, [dict(ITEM)]) == 0
    assert desktop.calls == []


async def test_failures_are_noted_not_raised(desktop):
    desktop.state["result"] = desk.LocalOffline("desktop app is not connected")
    items = [dict(ITEM)]
    assert await ch.save_on_desktop(CTX, items) == 0 and "not connected" in items[0]["desktop_save_error"]
    desktop.state["result"] = {"ok": False, "error": "only Lumio file links are saved"}
    items = [dict(ITEM)]
    await ch.save_on_desktop(CTX, items)
    assert items[0]["desktop_save_error"] == "only Lumio file links are saved"


async def test_publish_saves_cloud_downloads_but_not_local_ones(desktop):
    cloud = {"ran_on": "cloud_computer", "downloads": [dict(ITEM)]}
    out = await ch._publish_downloads(CTX, cloud)
    assert out["downloads"][0]["saved_on_your_computer"] and "Downloads folder" in out["hint"]
    desktop.calls.clear()
    await ch._publish_downloads(CTX, {"ran_on": "local_computer", "downloads": [dict(ITEM)]})
    assert desktop.calls == []


async def test_at_most_five_files_per_result(desktop):
    items = [dict(ITEM, filename=f"f{i}.csv") for i in range(8)]
    assert await ch.save_on_desktop(CTX, items) == ch.DESKTOP_SAVE_MAX
