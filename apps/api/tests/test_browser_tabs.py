"""Daemon tab bookkeeping (fake Playwright pages): open, target, switch, close, popups."""

from __future__ import annotations

import pytest

from app.connectors import computer_daemon as d


class FakePage:
    def __init__(self, ctx, url="about:blank"):
        self.ctx, self.url, self.handlers, self.front = ctx, url, {}, False

    def on(self, ev, fn):
        self.handlers[ev] = fn

    def title(self):
        return f"title {self.url}"

    def goto(self, url, **kw):
        self.url = url

    def wait_for_timeout(self, ms):
        return None

    def bring_to_front(self):
        for p in self.ctx.pages:
            p.front = False
        self.front = True

    def close(self):
        self.ctx.pages.remove(self)
        if "close" in self.handlers:
            self.handlers["close"](self)


class FakeContext:
    def __init__(self):
        self.pages: list[FakePage] = []
        self.handlers = {}

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def on(self, ev, fn):
        self.handlers[ev] = fn

    def popup(self, url):
        page = self.new_page()
        page.url = url
        self.handlers["page"](page)
        return page


@pytest.fixture
def browser(monkeypatch):
    ctx = FakeContext()
    first = ctx.new_page()
    monkeypatch.setattr(d, "_tabs", {})
    monkeypatch.setattr(d, "_tab_refs", {})
    monkeypatch.setattr(d, "_tab_seq", 0)
    monkeypatch.setattr(d, "_active_tab", "")
    monkeypatch.setattr(d, "_pending_pages", [])
    monkeypatch.setattr(d, "_pending_downloads", [])
    monkeypatch.setattr(d, "_browser_context", ctx)
    monkeypatch.setattr(d, "_browser_page", first)
    d._register_tab(first, activate=True)
    ctx.on("page", d._on_new_page)
    monkeypatch.setattr(d, "_needs_desktop", lambda page: False)
    monkeypatch.setattr(d, "_snapshot", lambda page: {"ok": True, "url": page.url})
    monkeypatch.setattr(d, "_dump", lambda page: {"ok": True, "url": page.url})
    return ctx


def test_new_tab_switch_and_list(browser):
    out = d.handle({"command": "new_tab", "url": "https://a.test/"})
    assert out["tab"] == "t2" and out["url"] == "https://a.test/"
    assert d._active_tab == "t2"
    bg = d.handle({"command": "new_tab", "url": "https://b.test/", "background": True})
    assert bg["tab"] == "t3" and d._active_tab == "t2"
    listed = d.handle({"command": "tabs"})
    assert [t["tab"] for t in listed["tabs"]] == ["t1", "t2", "t3"]
    assert [t["active"] for t in listed["tabs"]] == [False, True, False]
    d.handle({"command": "switch_tab", "tab": "t1"})
    assert d._active_tab == "t1" and d._tabs["t1"].front


def test_commands_target_a_tab_without_switching(browser):
    d.handle({"command": "new_tab", "url": "https://a.test/", "background": True})
    out = d.handle({"command": "navigate", "url": "https://c.test/", "tab": "t2"})
    assert out["tab"] == "t2" and d._tabs["t2"].url == "https://c.test/"
    assert d._active_tab == "t1" and d._tabs["t1"].url == "about:blank"
    with pytest.raises(RuntimeError, match="no tab t9"):
        d.handle({"command": "snapshot", "tab": "t9"})


def test_popups_become_tabs_and_close_falls_back(browser):
    browser.popup("https://popup.test/")
    d.drain_browser_events()
    assert "t2" in d._tabs and d._active_tab == "t1"
    d.handle({"command": "switch_tab", "tab": "t2"})
    out = d.handle({"command": "close_tab"})
    assert out["closed"] == "t2" and d._active_tab == "t1"
    last = d.handle({"command": "close_tab", "tab": "t1"})
    assert "cleared" in last["note"] and "t1" in d._tabs


def test_tab_limit(browser, monkeypatch):
    monkeypatch.setattr(d, "MAX_TABS", 2)
    d.handle({"command": "new_tab"})
    with pytest.raises(RuntimeError, match="at most 2 tabs"):
        d.handle({"command": "new_tab"})


def test_page_event_does_not_call_playwright_until_drained(browser):
    page = browser.popup("https://popup.test/")
    assert "close" not in page.handlers
    assert "t2" not in d._tabs
    d.drain_browser_events()
    assert "close" in page.handlers and "t2" in d._tabs
