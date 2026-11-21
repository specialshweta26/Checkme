"""Agents keep to their role, instructions and skills (docs/GUARDRAILS.md "Roles")."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

from app.agent import deep
from app.agent.deep import system_prompt
from app.agents.focus import is_general, role_contract

USER = SimpleNamespace(timezone="Asia/Kolkata", display_name="Gaurav", email="g@x.test")
CHIEF = SimpleNamespace(id="c", name="Chief", title="chief of staff", instructions="", focus=None)
ANALYST = SimpleNamespace(id="a", name="Funnel", title="sales analyst", instructions="Report One8 and Lotto sales from the gateway.",
                          focus=None)  # fmt: skip


def test_a_specialist_keeps_to_its_role_and_chief_takes_anything():
    assert is_general(CHIEF) and not is_general(ANALYST)
    analyst = role_contract(ANALYST)
    assert "sales analyst" in analyst and "outside your role" in analyst and "ask_teammate" in analyst
    chief = role_contract(CHIEF)
    assert "take any request" in chief and "outside your role" not in chief
    for text in (analyst, chief):
        assert "only the user changes your role" in text and "safety rule wins" in text


def test_the_role_rule_is_in_every_agents_prompt_after_its_instructions():
    prompt = system_prompt(USER, [], "", agent=ANALYST)
    assert "Report One8 and Lotto sales" in prompt and "## Your role" in prompt
    assert prompt.index("Report One8 and Lotto sales") < prompt.index("## Your role")
    assert "outside your role" in prompt
    assert "take any request" in system_prompt(USER, [], "", agent=CHIEF)


def test_a_run_uses_only_the_agents_own_connectors():
    src = inspect.getsource(deep._run_deep_agent)
    assert "filter_connectors(conns, getattr(workspace_agent, \"id\", None), [], assignments)" in src
