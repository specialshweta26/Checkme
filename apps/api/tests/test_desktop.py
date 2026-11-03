"""Desktop client vs website: local computer by default, E2B fallback, web stays on E2B."""

import uuid
from types import SimpleNamespace

import pytest

from app.agent import computer_harness
from app.agent.deep import RunContext, shell_routing_hint
from app.desktop.agent import LocalOffline, call_local
from app.desktop.client import client_kind, prefer_local, reset_client_kind, set_client_kind


def _user():
    return SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Kolkata", email="a@b.c", name="A")


def _computer():
    return SimpleNamespace(
        slug="computer", name="Computer", provider="daytona", tags=["computer"], alias="", description="", kind="builtin"
    )


def _local():
    return SimpleNamespace(
        slug="laptop",
        name="This computer",
        provider="trusted_local",
        tags=["computer", "local"],
        alias="",
        description="",
        kind="builtin",
        user_id=None,
    )


def _ctx(connectors, call_impl=None, user=None):
    user = user or _user()
    for conn in connectors:
        if getattr(conn, "user_id", None) is None:
            conn.user_id = user.id
    return RunContext(db=None, user=user, connectors={c.slug: c for c in connectors}, call_impl=call_impl)


@pytest.fixture
def as_web():
    token = set_client_kind("web")
    yield
    reset_client_kind(token)


@pytest.fixture
def as_desktop():
    token = set_client_kind("desktop")
    yield
    reset_client_kind(token)


def test_unknown_client_is_web():
    token = set_client_kind("tablet")
    try:
        assert client_kind() == "web" and prefer_local() is False
    finally:
        reset_client_kind(token)


def test_desktop_header_prefers_local(as_desktop):
    assert client_kind() == "desktop" and prefer_local() is True


def test_web_never_prefers_local(as_web):
    assert prefer_local() is False


def test_website_prompt_keeps_cloud_default(as_web):
    hint = shell_routing_hint([_computer(), _local()])
    assert "cloud computer" in hint
    assert "Lumio desktop app" not in hint or "own connected computer" in hint


def test_desktop_prompt_defaults_to_this_computer(as_desktop):
    hint = shell_routing_hint([_computer(), _local()])
    assert "Lumio desktop app" in hint and "desktop" in hint.lower()
    assert "cloud computer" in hint


async def test_call_local_offline_raises(monkeypatch):
    async def offline(_uid):
        return False

    monkeypatch.setattr("app.desktop.agent.is_online", offline)
    with pytest.raises(LocalOffline):
        await call_local(uuid.uuid4(), "exec", {"command": "ls"})


async def test_call_local_unknown_tool_falls_to_cloud():
    out = await call_local(uuid.uuid4(), "ensure_toolchain", {})
    assert out["fallback_cloud"] is True


async def test_website_shell_stays_on_cloud(monkeypatch, as_web):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"exit_code": 0, "stdout": "cloud"}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.shell(ctx, "ls")
    assert calls == [("computer", "exec")]
    assert out["ran_on"] == "cloud_computer"


async def test_desktop_shell_uses_laptop_when_online(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"exit_code": 0, "stdout": "local"}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.shell(ctx, "ls")
    assert calls == [("laptop", "exec")]
    assert out["ran_on"] == "local_computer" and out["stdout"] == "local"


async def test_desktop_shell_does_not_use_the_cloud_when_offline(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"exit_code": 0, "stdout": "cloud"}

    async def online(_uid):
        return False

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.shell(ctx, "ls ~/Desktop")
    assert calls == []
    assert out["ran_on"] == "local_computer"
    assert out.get("computer_unavailable") is True


async def test_desktop_file_tools_are_not_sent_to_the_cloud(as_desktop):
    ctx = _ctx([_computer(), _local()])
    assert computer_harness.desktop_tool_slug(ctx, "computer", "list_dir") == "laptop"
    assert computer_harness.desktop_tool_slug(ctx, "computer", "search_files") == "laptop"
    assert computer_harness.desktop_tool_slug(ctx, "computer", "read_file") == "laptop"


async def test_desktop_search_stays_on_this_computer(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool, args.get("query")))
        return {"ok": True, "hits": [{"name": "OMS report.xlsx", "path": "/Users/me/Desktop/OMS report.xlsx"}]}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.search_files(ctx, "OMS report")
    assert calls == [("laptop", "search_files", "OMS report")]
    assert out["ran_on"] == "local_computer"
    assert out["hits"][0]["name"] == "OMS report.xlsx"


async def test_desktop_browser_offline_does_not_open_e2b(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"ok": True, "url": args.get("url")}

    async def online(_uid):
        return False

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.run_computer(ctx, "browser_navigate", {"url": "https://www.booking.com"})
    assert calls == []
    assert out["ran_on"] == "local_computer"
    assert out.get("computer_unavailable") is True


async def test_desktop_browser_uses_laptop(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"ok": True, "url": args.get("url"), "ran_on": "local_computer"}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.run_computer(ctx, "browser_navigate", {"url": "https://www.booking.com"})
    assert calls == [("laptop", "browser_navigate")]
    assert out["ran_on"] == "local_computer"


async def test_website_browser_stays_on_cloud(monkeypatch, as_web):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"ok": True, "url": args.get("url")}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.run_computer(ctx, "browser_navigate", {"url": "https://www.booking.com"})
    assert calls == [("computer", "browser_navigate")]
    assert out["ran_on"] == "cloud_computer"


async def test_desktop_toolchain_force_cloud(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append(slug)
        return {"exit_code": 0, "stdout": "ok"}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.run_computer(ctx, "exec", {"command": "true"}, force_cloud=True)
    assert calls == ["computer"]
    assert out["ran_on"] == "cloud_computer"


async def test_desktop_read_file_stays_local_when_offline(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"ok": True, "content": "a,b"}

    async def online(_uid):
        return False

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.read_file(ctx, "/Users/me/Desktop/orders.csv")
    assert calls == []
    assert out["ran_on"] == "local_computer"
    assert out.get("computer_unavailable") is True


async def test_desktop_files_use_laptop(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"path": args.get("path") or ".", "entries": ["notes.md"]}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.list_files(ctx, "")
    assert calls == [("laptop", "list_dir")]
    assert out["ran_on"] == "local_computer"


async def test_desktop_grant_folder_stays_local(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"ok": False, "need": "folder_grant", "path": args.get("path"), "ran_on": "local_computer"}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.run_computer(ctx, "grant_folder", {"path": "/Users/me/Downloads"})
    assert calls == [("laptop", "grant_folder")]
    assert out["need"] == "folder_grant"
    assert out["ran_on"] == "local_computer"


async def test_desktop_open_path_uses_laptop(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"ok": True, "path": args.get("path"), "ran_on": "local_computer"}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.open_path(ctx, "/Users/me/Documents")
    assert calls == [("laptop", "open_path")]
    assert out["ran_on"] == "local_computer"


async def test_desktop_launch_app_uses_laptop(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool, args.get("name")))
        return {"ok": True, "app": args.get("name"), "ran_on": "local_computer"}

    async def online(_uid):
        return True

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.launch_app(ctx, "Finder")
    assert calls == [("laptop", "launch_app", "Finder")]
    assert out["ran_on"] == "local_computer"


async def test_desktop_screen_uses_the_browser_picture_not_a_desktop_capture(monkeypatch):
    from app.connectors.daytona_box import forget_screen, remember_screen
    from app.desktop import agent

    png = b"\x89PNG" + b"\x00" * 40
    calls = []

    async def fake(user_id, tool, args=None):
        calls.append(tool)
        remember_screen(png, str(user_id))
        return {"ok": True}

    monkeypatch.setattr(agent, "call_local", fake)
    forget_screen("u-screen")
    agent._SCREEN_TRY.pop("u-screen", None)
    assert (await agent.local_browser_frame("u-screen")).startswith(b"\x89PNG")
    assert calls == ["browse_screenshot"]
    await agent.local_browser_frame("u-screen")
    assert calls == ["browse_screenshot"]
