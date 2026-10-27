"""Built-in chat tools for managing Checkme's own agents and groups."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agent import deep
from app.agent.confirm import read_confirm
from app.agent.deep import RunContext, harness_tools, system_prompt
from app.agents import chat_tools, service
from app.agents.service import GROUP_MEMBER_MAX, GROUP_MEMBER_MIN, AgentError


class FakeWorkspace:
    """In-memory stand-in for app.agents.service persistence."""

    def __init__(self, names=("Chief", "Test", "Ori")):
        self.agents: dict[str, SimpleNamespace] = {}
        self.groups: dict[str, dict] = {}
        self.deleted: list[tuple[str, str]] = []
        for i, name in enumerate(names):
            self._add(name, kind="default" if i == 0 else "custom")

    def _add(self, name, title="", instructions="", kind="custom", archived=False):
        row = SimpleNamespace(
            id=uuid.uuid4(), name=name, title=title, instructions=instructions, color="#000",
            kind=kind, pinned=False, archived=archived, unread=False,
        )
        self.agents[str(row.id)] = row
        return row

    def install(self, monkeypatch):
        async def list_workspace(db, user):
            return {
                "agents": [service.serialize_agent(a) for a in self.agents.values()],
                "groups": [
                    service.serialize_group(g["row"], [self.agents[i] for i in g["members"] if i in self.agents])
                    for g in self.groups.values()
                ],
            }

        async def create_agent(db, user, body):
            name = service._clean_name(body.get("name") or "")
            if any(a.name == name for a in self.agents.values()):
                raise AgentError("An agent with that name already exists.")
            return self._add(name, body.get("title") or "", body.get("instructions") or "")

        async def update_agent(db, user, agent_id, body):
            row = self.agents[str(agent_id)]
            for key in ("name", "title", "instructions", "archived"):
                if body.get(key) is not None:
                    setattr(row, key, body[key])
            return row

        async def create_group(db, user, body):
            ids = [str(i) for i in service.validate_group_members(body["member_ids"])]
            row = SimpleNamespace(id=uuid.uuid4(), name=service._clean_name(body["name"]))
            self.groups[str(row.id)] = {"row": row, "members": ids}
            return row, [self.agents[i] for i in ids]

        async def update_group(db, user, group_id, body):
            g = self.groups[str(group_id)]
            if body.get("name"):
                g["row"].name = body["name"]
            if body.get("member_ids") is not None:
                g["members"] = [str(i) for i in service.validate_group_members(body["member_ids"])]
            return g["row"], [self.agents[i] for i in g["members"]]

        async def delete_agent(db, user, agent_id):
            if self.agents[str(agent_id)].kind == "default":
                raise AgentError("The default agent cannot be deleted.")
            self.deleted.append(("agent", str(agent_id)))
            self.agents.pop(str(agent_id))

        async def delete_group(db, user, group_id):
            self.deleted.append(("group", str(group_id)))
            self.groups.pop(str(group_id))

        for name, fn in locals().items():
            if callable(fn) and name != "self":
                monkeypatch.setattr(service, name, fn)
        return self

    def by_name(self, name):
        return next(a for a in self.agents.values() if a.name == name)


def _ctx(**kwargs):
    return RunContext(db=None, user=SimpleNamespace(id="u1", timezone="Asia/Kolkata"), connectors={}, **kwargs)


def _tool(ctx, name):
    return next(t for t in harness_tools(ctx) if t.name == name)


@pytest.fixture
def ws(monkeypatch):
    return FakeWorkspace().install(monkeypatch)


def test_workspace_tools_are_built_in():
    names = {t.name for t in harness_tools(_ctx())}
    for name in (
        "list_agents_and_groups",
        "create_agent",
        "update_agent",
        "create_agent_group",
        "add_group_members",
        "remove_group_members",
        "rename_agent_group",
        "delete_agent",
        "delete_agent_group",
    ):
        assert name in names
    assert {"list_connectors", "search_tools", "call_tool", "schedule_task", "run_staff_tool"} <= names


def test_prompt_routes_agent_groups_to_built_in_tools():
    prompt = system_prompt(SimpleNamespace(timezone="Asia/Kolkata"), [], "")
    assert "create_agent_group" in prompt
    assert "not a connector" in prompt
    assert "LiveChat" in prompt
    assert "sidebar under Personal" in prompt


async def test_list_agents_and_groups(ws):
    ctx = _ctx()
    data = json.loads(await _tool(ctx, "list_agents_and_groups").ainvoke({}))
    assert [a["name"] for a in data["agents"]] == ["Chief", "Test", "Ori"]
    assert data["agents"][0]["default"] is True
    assert data["limits"]["group_max_members"] == GROUP_MEMBER_MAX
    assert ctx.workspace_changed is False


async def test_create_agent_marks_workspace_changed(ws):
    ctx = _ctx()
    data = json.loads(
        await _tool(ctx, "create_agent").ainvoke({"name": "Dev", "title": "tech work", "instructions": "GitHub and Render"})
    )
    assert data["ok"] and data["agent"]["name"] == "Dev"
    assert "sidebar" in data["where"]
    assert ws.by_name("Dev").title == "tech work"
    assert ctx.workspace_changed is True


async def test_create_agent_duplicate_name_is_clear_error(ws):
    ctx = _ctx()
    data = json.loads(await _tool(ctx, "create_agent").ainvoke({"name": "Ori"}))
    assert "already exists" in data["error"]
    assert ctx.workspace_changed is False


async def test_rename_agent_by_name(ws):
    ctx = _ctx()
    data = json.loads(await _tool(ctx, "update_agent").ainvoke({"agent": "test", "name": "Tester"}))
    assert data["agent"]["name"] == "Tester"
    assert ctx.workspace_changed


async def test_create_group_with_all_agents(ws):
    ctx = _ctx()
    data = json.loads(
        await _tool(ctx, "create_agent_group").ainvoke({"name": "Software developers", "all_agents": True})
    )
    assert data["ok"]
    assert data["group"]["name"] == "Software developers"
    assert sorted(m["name"] for m in data["group"]["members"]) == ["Chief", "Ori", "Test"]
    assert ctx.workspace_changed


async def test_create_group_all_words_in_members(ws):
    data = await chat_tools.create_group(_ctx(), "Everyone", ["all my agents"])
    assert data["ok"] and len(data["group"]["members"]) == 3


async def test_create_group_skips_archived_agents_for_all(monkeypatch):
    ws = FakeWorkspace().install(monkeypatch)
    ws._add("Old", archived=True)
    data = await chat_tools.create_group(_ctx(), "Team", None, all_agents=True)
    assert "Old" not in [m["name"] for m in data["group"]["members"]]


async def test_create_group_named_members(ws):
    data = await chat_tools.create_group(_ctx(), "Pair", ["Test", "ori"])
    assert sorted(m["name"] for m in data["group"]["members"]) == ["Ori", "Test"]


async def test_create_group_too_few(monkeypatch):
    FakeWorkspace(names=("Chief",)).install(monkeypatch)
    ctx = _ctx()
    data = await chat_tools.create_group(ctx, "Solo", None, all_agents=True)
    assert f"needs {GROUP_MEMBER_MIN} to {GROUP_MEMBER_MAX}" in data["error"]
    assert "Create another agent" in data["error"]
    assert ctx.workspace_changed is False


async def test_create_group_too_many(monkeypatch):
    FakeWorkspace(names=["Chief"] + [f"A{i}" for i in range(GROUP_MEMBER_MAX)]).install(monkeypatch)
    data = await chat_tools.create_group(_ctx(), "Huge", None, all_agents=True)
    assert f"at most {GROUP_MEMBER_MAX}" in data["error"]
    assert str(GROUP_MEMBER_MAX + 1) in data["error"]


async def test_create_group_unknown_member(ws):
    data = await chat_tools.create_group(_ctx(), "X", ["Test", "Nobody"])
    assert "No agent named 'Nobody'" in data["error"]


async def test_add_and_remove_members(ws):
    ws._add("Dev")
    ctx = _ctx()
    made = await chat_tools.create_group(ctx, "Team", ["Test", "Ori"])
    added = json.loads(await _tool(ctx, "add_group_members").ainvoke({"group": "team", "members": ["Dev"]}))
    assert sorted(m["name"] for m in added["group"]["members"]) == ["Dev", "Ori", "Test"]
    removed = await chat_tools.remove_group_members(ctx, made["group"]["id"], ["Ori"])
    assert sorted(m["name"] for m in removed["group"]["members"]) == ["Dev", "Test"]
    too_small = await chat_tools.remove_group_members(ctx, "Team", ["Dev"])
    assert f"needs {GROUP_MEMBER_MIN} to {GROUP_MEMBER_MAX}" in too_small["error"]


async def test_rename_group(ws):
    ctx = _ctx()
    await chat_tools.create_group(ctx, "Team", ["Test", "Ori"])
    data = json.loads(await _tool(ctx, "rename_agent_group").ainvoke({"group": "Team", "name": "Builders"}))
    assert data["group"]["name"] == "Builders"


async def test_delete_agent_needs_confirmation(ws):
    ctx = _ctx()
    data = json.loads(await _tool(ctx, "delete_agent").ainvoke({"target": "Ori"}))
    assert data["status"] == "awaiting_user_confirmation"
    assert ws.deleted == []
    assert len(ctx.pending) == 1
    signed = read_confirm(ctx.pending[0]["token"])
    assert signed["kind"] == "tool"
    assert signed["payload"]["slug"] == chat_tools.WORKSPACE_SLUG
    assert signed["payload"]["tool"] == "delete_agent"
    assert signed["payload"]["args"]["id"] == str(ws.by_name("Ori").id)

    done = await chat_tools.run_confirmed(_ctx(), "delete_agent", signed["payload"]["args"])
    assert done["ok"] and ("agent", signed["payload"]["args"]["id"]) in ws.deleted


async def test_delete_default_agent_refused(ws):
    ctx = _ctx()
    data = await chat_tools.request_delete(ctx, "agent", "Chief")
    assert "cannot be deleted" in data["error"]
    assert ctx.pending == []


async def test_delete_refused_when_unattended(ws):
    ctx = _ctx(auto_confirm=True)
    data = await chat_tools.request_delete(ctx, "agent", "Ori")
    assert "approval" in data["error"]
    assert ws.deleted == [] and ctx.pending == []


async def test_delete_group_confirmation(ws):
    ctx = _ctx()
    made = await chat_tools.create_group(ctx, "Team", ["Test", "Ori"])
    data = json.loads(await _tool(ctx, "delete_agent_group").ainvoke({"target": "Team"}))
    assert data["status"] == "awaiting_user_confirmation"
    payload = read_confirm(ctx.pending[0]["token"])["payload"]
    assert payload["tool"] == "delete_group"
    done = await chat_tools.run_confirmed(_ctx(), "delete_group", payload["args"])
    assert done["ok"] and ("group", made["group"]["id"]) in ws.deleted


async def test_already_confirmed_is_not_requeued(ws):
    target = str(ws.by_name("Ori").id)
    ctx = _ctx(confirmations=[{"slug": chat_tools.WORKSPACE_SLUG, "tool": "delete_agent", "args": {"id": target}}])
    data = await chat_tools.request_delete(ctx, "agent", "Ori")
    assert data["status"] == "already_confirmed"
    assert ctx.pending == []


async def test_run_deep_agent_executes_confirmed_workspace_delete(ws, monkeypatch):
    target = str(ws.by_name("Ori").id)
    called = {}

    async def fake_call_user_tool(*args, **kwargs):
        called["connector"] = True
        return {}

    async def noop(*args, **kwargs):
        return None

    async def no_conns(*args, **kwargs):
        return []

    async def no_assign(*args, **kwargs):
        return {}

    async def memory(*args, **kwargs):
        return ""

    class Graph:
        async def ainvoke(self, payload, config=None):
            called["messages"] = [m.content for m in payload["messages"]]
            return {"messages": []}

    import app.connectors.platform as platform

    monkeypatch.setattr(platform, "ensure_platform_connectors", noop)
    monkeypatch.setattr(deep, "user_connectors", no_conns)
    monkeypatch.setattr(deep, "connector_assignment_map", no_assign)
    monkeypatch.setattr(deep, "load_memory", memory)
    monkeypatch.setattr(deep, "remember", noop)
    monkeypatch.setattr(deep, "call_user_tool", fake_call_user_tool)
    monkeypatch.setattr(deep, "_maybe_start_connect", noop)
    monkeypatch.setattr(deep, "build_agent", lambda ctx, prompt: Graph())

    out = await deep.run_deep_agent(
        None,
        SimpleNamespace(id="u1", timezone="Asia/Kolkata"),
        uuid.uuid4(),
        "r1",
        [],
        "Confirmed. Continue.",
        [{"slug": chat_tools.WORKSPACE_SLUG, "tool": "delete_agent", "args": {"id": target, "name": "Ori"}}],
    )
    assert ("agent", target) in ws.deleted
    assert "connector" not in called
    assert out["workspace_changed"] is True
    assert any("confirmed delete_agent" in m for m in called["messages"])
