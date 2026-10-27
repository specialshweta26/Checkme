from types import SimpleNamespace

import pytest

from app.agent.deep import system_prompt
from app.agents.service import (
    DEFAULT_AGENT_NAME,
    GROUP_MEMBER_MAX,
    GROUP_MEMBER_MIN,
    AgentError,
    agent_may_use_connector,
    filter_connectors,
    pick_color,
    serialize_agent,
    serialize_group,
    session_belongs,
    validate_group_members,
)


def test_session_belongs_to_one_agent():
    johnny = SimpleNamespace(agent_id="jj", group_id=None)
    chief = SimpleNamespace(agent_id="cc", group_id=None)
    group = SimpleNamespace(agent_id=None, group_id="gg")
    assert session_belongs(johnny, agent_id="jj")
    assert not session_belongs(johnny, agent_id="cc")
    assert not session_belongs(chief, agent_id="jj")
    assert session_belongs(group, group_id="gg")
    assert not session_belongs(johnny, group_id="gg")
    assert not session_belongs(group, agent_id="jj")
    orphan = SimpleNamespace(agent_id=None, group_id=None)
    assert not session_belongs(orphan, agent_id="jj")
    assert not session_belongs(orphan, agent_id=None)
    assert not session_belongs(johnny, agent_id=None)


def test_group_member_limits():
    with pytest.raises(AgentError):
        validate_group_members(["11111111-1111-1111-1111-111111111111"])
    ids = [f"11111111-1111-1111-1111-{i:012d}" for i in range(GROUP_MEMBER_MIN)]
    assert len(validate_group_members(ids)) == GROUP_MEMBER_MIN
    too_many = [f"11111111-1111-1111-1111-{i:012d}" for i in range(GROUP_MEMBER_MAX + 1)]
    with pytest.raises(AgentError):
        validate_group_members(too_many)


def test_serialize_agent_and_group():
    agent = SimpleNamespace(
        id="a1",
        name="Chief",
        title="chief of staff",
        instructions="",
        color="#f97316",
        kind="default",
    )
    row = serialize_agent(agent)
    assert row["name"] == DEFAULT_AGENT_NAME
    assert row["kind"] == "default"
    assert row["pinned"] is False
    assert row["archived"] is False
    assert row["unread"] is False
    group = serialize_group(SimpleNamespace(id="g1", name="Ops", outcome="Close the brief", owner_id="a1"), [agent])
    assert group["name"] == "Ops"
    assert group["outcome"] == "Close the brief"
    assert group["owner_id"] == "a1"
    assert group["members"][0]["id"] == "a1"
    assert pick_color(0).startswith("#")


def test_system_prompt_uses_agent_and_group():
    analyst = SimpleNamespace(id="aa", name="Analyst", title="data analyst", instructions="Prefer tables.")
    researcher = SimpleNamespace(id="bb", name="Researcher", title="web researcher", instructions="")
    group = SimpleNamespace(id="gg", name="Insights")
    prompt = system_prompt(
        SimpleNamespace(timezone="Asia/Kolkata"),
        [],
        "",
        agent=analyst,
        group=group,
        teammates=[analyst, researcher],
    )
    lowered = prompt.lower()
    assert "you are analyst" in lowered
    assert "prefer tables" in lowered
    assert "insights" in lowered
    assert "ask_teammate" in lowered
    assert "one owner per stage" in lowered
    assert "separate per bot" in lowered
    assert "researcher" in lowered
    assert "one8" not in lowered
    assert "lotto" not in lowered
    assert "combine teammate" not in lowered


def test_kickoff_system_prompt_skips_tools_and_names_creator():
    analyst = SimpleNamespace(id="aa", name="Analyst", title="data analyst", instructions="Prefer tables.")
    researcher = SimpleNamespace(id="bb", name="Researcher", title="web researcher", instructions="")
    group = SimpleNamespace(id="gg", name="Insights", outcome="Ship the brief", owner_id="aa")
    user = SimpleNamespace(timezone="Asia/Kolkata", display_name="Shweta", email="s@x.com")
    prompt = system_prompt(
        user,
        [],
        "",
        agent=analyst,
        group=group,
        teammates=[analyst, researcher],
        mentions={"kickoff": True, "roll_call": True},
    )
    lowered = prompt.lower()
    assert "shweta" in lowered
    assert "set this group up" in lowered
    assert "list_connectors" in lowered
    assert "do not call list_connectors" in lowered
    assert "search_tools" in lowered
    assert "one8" not in lowered
    assert "lotto" not in lowered


def test_connector_assignments_restrict_specific_agents():
    gmail = SimpleNamespace(id="c-mail", slug="gmail")
    computer = SimpleNamespace(id="c-box", slug="computer")
    mapping = {"c-mail": ["jj"]}
    assert agent_may_use_connector("jj", "c-mail", mapping)
    assert not agent_may_use_connector("cc", "c-mail", mapping)
    assert agent_may_use_connector("cc", "c-box", mapping)
    kept = filter_connectors([gmail, computer], agent_id="cc", assignments=mapping)
    assert [c.slug for c in kept] == ["computer"]
    group_kept = filter_connectors([gmail, computer], agent_id="cc", teammate_ids=["jj"], assignments=mapping)
    assert {c.slug for c in group_kept} == {"gmail", "computer"}
