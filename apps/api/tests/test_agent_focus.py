"""Focus areas from the new-agent onboarding card, and the coding plan-and-build setup."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agent import coding, deep
from app.agent.deep import RunContext, harness_tools, system_prompt
from app.agent.progress import describe_tool
from app.agents import focus as F
from app.agents import service
from app.agents.service import AgentError


def _agent(**kw):
    base = dict(id=uuid.uuid4(), name="Abhy", title="", instructions="", color="#000", kind="custom", focus="")
    base.update(kw)
    return SimpleNamespace(**base)


class FakeDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1

    async def refresh(self, row):
        return None


def _user():
    return SimpleNamespace(id="u1", timezone="Asia/Kolkata", name="Shweta Kumari")


# ---- focus catalogue -------------------------------------------------------


def test_options_match_the_reference_card_order():
    assert [(o["letter"], o["label"]) for o in F.options()] == [
        ("A", "Email and calendar"),
        ("B", "Coding and deployments"),
        ("C", "Research and writing"),
        ("D", "Errands and bookings"),
    ]


@pytest.mark.parametrize(
    "raw,key",
    [
        ("coding", "coding"),
        ("Coding and deployments", "coding"),
        ("dev", "coding"),
        ("research", "research"),
        ("Research and writing", "research"),
        ("email", "email_calendar"),
        ("bookings", "errands"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_focus(raw, key):
    assert F.normalize_focus(raw) == key


def test_unknown_focus_is_rejected():
    with pytest.raises(ValueError):
        F.normalize_focus("astrology")


# ---- service: setting the focus --------------------------------------------


async def test_update_agent_sets_focus_and_default_title(monkeypatch):
    row = _agent()

    async def get_agent(db, user, agent_id):
        return row

    monkeypatch.setattr(service, "get_agent", get_agent)
    db = FakeDB()
    out = await service.update_agent(db, _user(), row.id, {"focus": "Coding and deployments"})
    assert out.focus == "coding"
    assert out.title == "software engineer"
    assert db.commits == 1
    data = service.serialize_agent(out)
    assert data["focus"] == "coding" and data["focus_label"] == "Coding and deployments"


async def test_update_agent_keeps_a_custom_title(monkeypatch):
    row = _agent(title="frontend dev")

    async def get_agent(db, user, agent_id):
        return row

    monkeypatch.setattr(service, "get_agent", get_agent)
    out = await service.update_agent(FakeDB(), _user(), row.id, {"focus": "research"})
    assert out.focus == "research" and out.title == "frontend dev"


async def test_update_agent_rejects_bad_focus(monkeypatch):
    row = _agent()

    async def get_agent(db, user, agent_id):
        return row

    monkeypatch.setattr(service, "get_agent", get_agent)
    with pytest.raises(AgentError):
        await service.update_agent(FakeDB(), _user(), row.id, {"focus": "nope"})


def test_patch_route_accepts_focus():
    from app.agents.routes import AgentIn, AgentPatch

    assert AgentPatch(focus="coding").model_dump()["focus"] == "coding"
    assert AgentIn(name="x", focus="research").model_dump()["focus"] == "research"
    assert AgentPatch().model_dump()["focus"] is None


# ---- system prompt ---------------------------------------------------------


def test_coding_focus_prompt_is_plan_then_build():
    p = system_prompt(_user(), [], "", agent=_agent(focus="coding", title="software engineer"))
    for needle in (
        "Focus: Coding and deployments",
        "write_todos",
        "coding_workspace_status",
        "sandbox_shell",
        "git_checkout",
        "agent/<short-name>",
        "ensure_dev_toolchain",
        "repo_overview",
        "find_symbol",
        "run_checks",
        "push_branch",
        "approval card",
        "do not merge by default",
        "'Merge it?'",
        "check_merge",
        "merge commit sha",
        "ask_user",
        "remember_coding",
        "waiting_for='github_app'",
        "Explore remotely first",
        "review_pull_request",
        "never put a token",
        "detect_deploy_targets",
        "deploy_rollback",
        "never see",
        "waiting_for='deploy_<platform>'",
        "exactly what is missing",
    ):
        assert needle in p, needle
    assert "Never merge" not in p


def test_research_focus_prompt_cites_sources():
    p = system_prompt(_user(), [], "", agent=_agent(focus="research"))
    assert "Focus: Research and writing" in p and "Sources list" in p and "at least three" in p


def test_unset_custom_agent_is_told_to_set_focus_from_a_free_answer():
    p = system_prompt(_user(), [], "", agent=_agent())
    assert "set_my_focus" in p and "Coding and deployments" in p


def test_chief_and_configured_agents_get_no_onboarding_prompt():
    chief = _agent(name="Chief", kind="default", title="chief of staff")
    assert "set_my_focus" not in system_prompt(_user(), [], "", agent=chief)
    briefed = _agent(instructions="You review PRs.")
    assert "set_my_focus" not in system_prompt(_user(), [], "", agent=briefed)


# ---- tools -----------------------------------------------------------------


def _ctx(agent, connectors=None, **kw):
    return RunContext(db=None, user=_user(), connectors=connectors or {}, agent=agent, **kw)


def _names(ctx):
    return {t.name for t in harness_tools(ctx)}


def test_coding_agent_gets_plan_and_build_tools():
    names = _names(_ctx(_agent(focus="coding")))
    assert {"coding_workspace_status", "sandbox_shell", "set_my_focus", "call_tool", "search_tools"} <= names


def test_other_agents_do_not_get_coding_tools():
    for agent in (_agent(focus="research"), _agent(), _agent(kind="default", name="Chief")):
        names = _names(_ctx(agent))
        assert "sandbox_shell" not in names and "coding_workspace_status" not in names
    assert "set_my_focus" not in _names(_ctx(_agent(kind="default", name="Chief")))


async def test_set_my_focus_tool_updates_the_agent(monkeypatch):
    row = _agent()
    seen = {}

    async def update_agent(db, user, agent_id, body):
        seen.update(body)
        row.focus = F.normalize_focus(body["focus"])
        return row

    monkeypatch.setattr(service, "update_agent", update_agent)
    ctx = _ctx(row)
    ctx.db = FakeDB()
    tool = next(t for t in harness_tools(ctx) if t.name == "set_my_focus")
    out = json.loads(await tool.ainvoke({"focus": "coding"}))
    assert out == {"ok": True, "agent": "Abhy", "focus": "coding", "label": "Coding and deployments"}
    assert ctx.workspace_changed is True and seen == {"focus": "coding"}


async def test_sandbox_shell_runs_exec_on_the_computer(monkeypatch):
    calls = []

    async def fake_call(ctx, slug, tool, args=None):
        calls.append((slug, tool, args))
        return {"exit_code": 0, "stdout": "ok"}

    monkeypatch.setattr(deep, "call_user_tool", fake_call)
    computer = SimpleNamespace(slug="computer", name="Computer", provider="daytona", tags=["computer"])
    ctx = _ctx(_agent(focus="coding"), {"computer": computer})
    tool = next(t for t in harness_tools(ctx) if t.name == "sandbox_shell")
    out = json.loads(await tool.ainvoke({"command": "pytest -q", "cwd": "~/work/app", "timeout": 5000}))
    assert out["exit_code"] == 0
    assert calls == [("computer", "exec", {"command": "pytest -q", "timeout": coding.SHELL_TIMEOUT_MAX, "cwd": "~/work/app"})]


async def test_sandbox_shell_says_what_is_missing_without_a_computer():
    ctx = _ctx(_agent(focus="coding"), {})
    tool = next(t for t in harness_tools(ctx) if t.name == "sandbox_shell")
    out = json.loads(await tool.ainvoke({"command": "ls"}))
    assert "Daytona" in out["error"]


def _conn(slug, name, provider="", tags=()):
    return SimpleNamespace(slug=slug, name=name, provider=provider, alias="", description="", tags=list(tags))


async def test_coding_status_reports_ready_setup(monkeypatch):
    async def fake_call(ctx, slug, tool, args=None):
        assert (slug, tool) == ("computer", "exec")
        return {"exit_code": 0, "stdout": "git=git version 2.43\ngh=\ngh_auth=none\ngit_cred=none\nnode=v20\npython=Python 3.12\nwork=/home/daytona/work"}

    monkeypatch.setattr(deep, "call_user_tool", fake_call)
    conns = {
        "computer": _conn("computer", "Computer", "daytona", ["computer"]),
        "github": _conn("github", "GitHub", "mcp"),
        "render": _conn("render", "Render", "mcp"),
    }
    out = await deep.coding_workspace_status(_ctx(_agent(focus="coding"), conns))
    assert out["computer"] == "ready"
    assert out["missing"] == []
    assert out["github_connectors"] == [{"slug": "github", "name": "GitHub"}]
    assert out["deploy_connectors"] == [{"slug": "render", "name": "Render"}]
    assert out["workdir"] == "/home/daytona/work"


def test_readiness_names_exactly_what_is_missing():
    out = coding.readiness({})
    joined = " ".join(out["missing"])
    assert out["computer"] == "missing"
    assert "Daytona" in joined and "GitHub is not connected" in joined and "deploy connector" in joined


def test_readiness_with_composio_points_at_connect_app_github():
    out = coding.readiness({"composio": _conn("composio", "Composio", "composio")}, {"git": "git version 2"})
    assert any("connect_app github" in m for m in out["missing"])


def test_readiness_uses_sandbox_git_credentials():
    conns = {"computer": _conn("computer", "Computer", "daytona")}
    out = coding.readiness(conns, {"git": "git 2", "gh_auth": "ok"})
    assert out["git_push_credentials"] is True
    assert not any("GitHub" in m for m in out["missing"])


async def test_coding_agent_gets_a_bigger_step_budget(monkeypatch):
    import app.connectors.platform as platform

    seen = {}

    async def noop(*a, **k):
        return None

    async def empty(*a, **k):
        return []

    async def no_assign(*a, **k):
        return {}

    async def memory(*a, **k):
        return ""

    class Graph:
        async def ainvoke(self, payload, config=None):
            seen["limit"] = config["recursion_limit"]
            return {"messages": []}

    monkeypatch.setattr(platform, "ensure_platform_connectors", noop)
    monkeypatch.setattr(deep, "user_connectors", empty)
    monkeypatch.setattr(deep, "connector_assignment_map", no_assign)
    monkeypatch.setattr(deep, "load_memory", memory)
    monkeypatch.setattr(deep, "remember", noop)
    monkeypatch.setattr(deep, "_maybe_start_connect", noop)
    monkeypatch.setattr(deep, "build_agent", lambda ctx, prompt: Graph())
    await deep.run_deep_agent(None, _user(), uuid.uuid4(), "r", [], "fix the bug", workspace_agent=_agent(focus="coding"))
    assert seen["limit"] >= 200


def test_status_lines_for_coding_tools():
    assert describe_tool("sandbox_shell", {"command": "git clone https://github.com/a/b"}) == (
        "Running `git clone https://github.com/a/b` on the computer"
    )
    assert describe_tool("set_my_focus", {"focus": "coding"}) == "Setting my focus to Coding and deployments"
