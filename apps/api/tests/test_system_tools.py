"""Windows, screens and clipboard on the user's own computer."""

from types import SimpleNamespace

from app.agent import computer_harness as computer


def _local(monkeypatch, seen):
    async def fake(ctx, tool, args=None):
        seen.append((tool, args))
        return {"ok": True}

    monkeypatch.setattr(computer, "_try_local", fake)


async def test_the_clipboard_is_read_only_when_the_user_asks(monkeypatch):
    seen = []
    _local(monkeypatch, seen)
    refused = await computer.clipboard_read(SimpleNamespace(query="Summarise this page"))
    assert refused["ok"] is False and seen == []
    allowed = await computer.clipboard_read(SimpleNamespace(query="What did I just copy to my clipboard?"))
    assert allowed["ok"] is True and seen == [("computer_clipboard", {"action": "read"})]


async def test_a_web_page_cannot_ask_for_the_clipboard(monkeypatch):
    seen = []
    _local(monkeypatch, seen)
    assert (await computer.clipboard_read(SimpleNamespace(query="Book me a table for two")))["ok"] is False
    assert seen == []


async def test_there_is_no_cloud_version(monkeypatch):
    async def nowhere(ctx, tool, args=None):
        return None

    monkeypatch.setattr(computer, "_try_local", nowhere)
    out = await computer.system_tool(SimpleNamespace(), "computer_windows", {"action": "list"})
    assert out["ok"] is False and "desktop app" in out["error"]
