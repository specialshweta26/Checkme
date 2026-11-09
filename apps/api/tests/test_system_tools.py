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


def _ui_local(monkeypatch, seen, controls=None):
    async def fake(ctx, tool, args=None):
        seen.append(args)
        return {"ok": True, "controls": controls or []}

    monkeypatch.setattr(computer, "_try_local", fake)


async def test_password_and_security_apps_are_off_limits(monkeypatch):
    seen = []
    _ui_local(monkeypatch, seen)
    for app in ("System Settings", "1Password 8", "Keychain Access"):
        out = await computer.computer_ui(SimpleNamespace(query="open it"), "read", app)
        assert out["ok"] is False
    assert seen == []


async def test_a_control_must_be_read_before_it_is_pressed(monkeypatch):
    seen = []
    _ui_local(monkeypatch, seen)
    out = await computer.computer_ui(SimpleNamespace(query="press it"), "press", "Calculator", id="1.2")
    assert out["ok"] is False and seen == []


async def test_a_final_control_needs_the_users_own_word(monkeypatch):
    seen = []
    _ui_local(monkeypatch, seen, [{"id": "1.2", "name": "Send"}, {"id": "1.3", "name": "Bold"}])
    ctx = SimpleNamespace(query="Reply to Priya saying I'll be late")
    await computer.computer_ui(ctx, "read", "Mail")
    refused = await computer.computer_ui(ctx, "press", "Mail", id="1.2")
    assert refused["needs_approval"] is True and len(seen) == 1
    assert (await computer.computer_ui(ctx, "press", "Mail", id="1.3"))["ok"] is True
    ctx.query = "yes, send it"
    assert (await computer.computer_ui(ctx, "press", "Mail", id="1.2"))["ok"] is True


async def test_a_final_menu_item_needs_the_users_own_word(monkeypatch):
    seen = []
    _ui_local(monkeypatch, seen)
    ctx = SimpleNamespace(query="tidy up my mail")
    out = await computer.computer_ui(ctx, "menu", "Mail", menu="Edit > Delete")
    assert out["needs_approval"] is True and seen == []
    assert (await computer.computer_ui(ctx, "menu", "Mail", menu="View > Show Sidebar"))["ok"] is True
