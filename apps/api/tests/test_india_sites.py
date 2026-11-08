"""Indian sites and blocked pages: recognised, and moved to the user's own browser (desktop app) when it is online."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agent import computer_harness as ch
from app.connectors.block_detect import detect_block


@pytest.mark.parametrize(
    "url, indian",
    [("https://www.makemytrip.com/flights/", True), ("irctc.co.in", True), ("https://www.amazon.in/x", True),
     ("https://shop.flipkart.com", True), ("https://www.google.com/travel/flights", False), ("https://example.com", False),
     ("https://fakemakemytrip.com", False)],
)
def test_indian_sites(url, indian):
    assert ch.indian_site(url) is indian


def test_a_fake_offline_page_is_a_block():
    wall = detect_block(200, "https://www.makemytrip.com/", "MakeMyTrip", "You are offline\nPlease check your connection")
    assert wall and wall["kind"] == "denied"
    assert detect_block(200, "https://news.test/a", "Offline-first apps", "x" * 3000 + " you are offline ") is None  # long article


@pytest.fixture
def desktop(monkeypatch):
    calls = []

    async def online(ctx):
        return ctx.desktop_up

    async def local(ctx, tool, args=None):
        if not ch.browser_local(ctx):
            return None
        calls.append(("local", tool))
        return {"ok": True, "url": (args or {}).get("url", ""), "ran_on": "local_computer"}

    async def cloud(ctx, slug, tool, args):
        calls.append(("cloud", tool))
        return ctx.cloud_answer

    monkeypatch.setattr(ch, "desktop_online", online)
    monkeypatch.setattr(ch, "_try_local", local)
    monkeypatch.setattr(ch, "computer_slug", lambda ctx: "computer")
    monkeypatch.setattr("app.agent.deep.call_user_tool", cloud)
    return calls


def _ctx(up=True, cloud=None):
    return SimpleNamespace(user=SimpleNamespace(id="u1"), connectors={}, desktop_up=up,
                           cloud_answer=cloud or {"ok": True, "url": "https://example.com", "title": "x", "text": "y" * 100})


async def test_an_indian_site_opens_in_the_desktop_browser_when_online(desktop):
    ctx = _ctx()
    out = await ch.run_computer(ctx, "browser_navigate", {"url": "https://www.makemytrip.com/"})
    assert out["ran_on"] == "local_computer" and "desktop app" in out["note"]
    await ch.run_computer(ctx, "browser_snapshot", {})  # the rest of the turn stays there
    assert desktop == [("local", "browser_navigate"), ("local", "browser_snapshot")]


async def test_other_sites_and_an_offline_desktop_stay_in_the_cloud(desktop):
    await ch.run_computer(_ctx(), "browser_navigate", {"url": "https://example.com"})
    await ch.run_computer(_ctx(up=False), "browser_navigate", {"url": "https://www.makemytrip.com/"})
    assert desktop == [("cloud", "browser_navigate"), ("cloud", "browser_navigate")]


async def test_a_blocked_cloud_page_moves_to_the_desktop(desktop):
    ctx = _ctx(cloud={"ok": True, "url": "https://shop.example.com", "bot_wall": True, "title": "Access Denied", "text": "Access Denied"})
    out = await ch.run_computer(ctx, "browser_navigate", {"url": "https://shop.example.com"})
    assert out["ran_on"] == "local_computer" and "blocked the cloud browser" in out["note"]
    assert desktop == [("cloud", "browser_navigate"), ("local", "browser_navigate")]
