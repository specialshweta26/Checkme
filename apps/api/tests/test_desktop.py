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


async def test_desktop_shell_falls_back_when_sidecar_offline(monkeypatch, as_desktop):
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"exit_code": 0, "stdout": "cloud"}

    async def online(_uid):
        return False

    monkeypatch.setattr("app.desktop.agent.is_online", online)
    ctx = _ctx([_computer(), _local()], fake)
    out = await computer_harness.shell(ctx, "ls")
    assert calls == [("computer", "exec")]
    assert out["ran_on"] == "cloud_computer"


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
