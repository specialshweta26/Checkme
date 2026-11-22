"""Prompt slimming (app/agent/prompt_rules.py): a tool group left out of a turn takes its rules out of the system
prompt too, and use_tools hands them back when the model loads the group."""

from __future__ import annotations

from types import SimpleNamespace

from app.agent import prompt_rules, tool_scope
from app.agent.deep import system_prompt
from app.agent.turn_context import split_prompt


def _fixed(hidden=None, query="hi") -> str:
    user = SimpleNamespace(id="u1", timezone="Asia/Kolkata", email="a@b.test")
    agent = SimpleNamespace(id="a1", name="Chief", title="chief of staff", instructions="", focus="", kind="default")
    prompt = system_prompt(user, [], "", agent=agent, skills=[], matched_skills=[], query=query, hidden=hidden)
    return split_prompt(prompt)[0]


def test_no_scoping_keeps_every_rule():
    full = _fixed()
    for words in ("pick_date", "browser_collect", "computer_windows", "create_presentation and design every slide"):
        assert words in full
    assert _fixed() == _fixed(hidden=[])


def test_a_left_out_group_takes_its_rules_with_it():
    full = _fixed()
    slim = _fixed(hidden=["browser", "computer", "office"])
    assert "pick_date" not in slim and "browser_collect again" not in slim and "computer_windows" not in slim
    assert "create_presentation and design every slide" not in slim
    # The safety rules and routing stay whatever is hidden.
    assert "Do not click Submit, Pay, Book, Send, Publish, or Delete" in slim
    assert "Do not invent personal, address, financial, or ID data" in slim
    assert "Blocked sites:" in slim and "Pick a source in this order" in slim
    saved = len(full) - len(slim)
    assert saved > 6000 and saved / len(full) > 0.2, (len(full), len(slim))


def test_only_the_hidden_group_is_dropped():
    office_only = _fixed(hidden=["office"])
    assert "pick_date" in office_only and "computer_windows" in office_only
    assert "create_presentation and design every slide" not in office_only


def test_the_same_scope_gives_the_same_prompt():  # the provider caches the fixed part only if it doesn't change
    assert _fixed(hidden=["browser"], query="sales last week") == _fixed(hidden=["browser"], query="what's on today")


async def test_use_tools_returns_the_groups_rules():
    tool = tool_scope.load_tool(["browser", "media"])
    out = await tool.ainvoke({"group": "browser"})
    assert out["loaded"] == "browser" and "pick_date" in out["rules"]
    assert "rules" not in await tool.ainvoke({"group": "media"})  # no extra rules for that group
    assert prompt_rules.for_group("Office") == prompt_rules.office_rules()
