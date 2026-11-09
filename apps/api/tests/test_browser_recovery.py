"""The cloud browser service gets going again after its page or the whole browser has gone away."""

from app.connectors import computer_daemon as d


class Page:
    def __init__(self, closed=False):
        self.closed = closed

    def is_closed(self):
        if self.closed == "error":
            raise RuntimeError("browser has been closed")
        return self.closed


class Context:
    def __init__(self, pages, dead=False):
        self._pages, self.dead, self.closed = pages, dead, False

    @property
    def pages(self):
        if self.dead:
            raise RuntimeError("Target page, context or browser has been closed")
        return self._pages

    def new_page(self):
        page = Page()
        self._pages.append(page)
        return page

    def close(self):
        self.closed = True


def _quiet(monkeypatch):
    monkeypatch.setattr(d, "_register_tab", lambda page, activate=False: "t1")
    monkeypatch.setattr(d, "_needs_desktop", lambda page: False)
    monkeypatch.setattr(d, "drain_browser_events", lambda: None)


def test_a_closed_page_is_swapped_for_an_open_one(monkeypatch):
    _quiet(monkeypatch)
    open_page = Page()
    monkeypatch.setattr(d, "_browser_page", Page(closed=True))
    monkeypatch.setattr(d, "_browser_context", Context([Page(closed=True), open_page]))
    monkeypatch.setattr(d, "_pw", object())
    assert d._ensure_page() is open_page


def test_a_new_page_is_opened_when_every_tab_is_closed(monkeypatch):
    _quiet(monkeypatch)
    context = Context([Page(closed=True)])
    monkeypatch.setattr(d, "_browser_page", Page(closed="error"))
    monkeypatch.setattr(d, "_browser_context", context)
    monkeypatch.setattr(d, "_pw", object())
    page = d._ensure_page()
    assert page is context._pages[-1] and not page.is_closed()


def test_a_dead_browser_is_dropped_and_started_again(monkeypatch):
    _quiet(monkeypatch)
    dead = Context([], dead=True)
    fresh = Page()

    class Playwright:
        stopped = False

        def stop(self):
            self.stopped = True

    pw = Playwright()
    monkeypatch.setattr(d, "_browser_page", Page(closed=True))
    monkeypatch.setattr(d, "_browser_context", dead)
    monkeypatch.setattr(d, "_pw", pw)
    calls = []
    real = d._ensure_page

    def ensure():
        calls.append(d._pw)
        if d._pw is None:
            return fresh  # stands in for launching a new browser
        return real()

    monkeypatch.setattr(d, "_ensure_page", ensure)
    assert d._ensure_page() is fresh
    assert dead.closed and pw.stopped and d._browser_context is None and calls[-1] is None


def test_a_live_page_is_kept(monkeypatch):
    _quiet(monkeypatch)
    page = Page()
    monkeypatch.setattr(d, "_browser_page", page)
    assert d._ensure_page() is page
