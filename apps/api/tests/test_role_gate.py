"""A specialist points requests outside its role to the right agent (docs/GUARDRAILS.md "Roles").

Live: asked for Lotto's sales, the HR agent fetched them; the role rule in the prompt alone didn't hold."""

from __future__ import annotations

from types import SimpleNamespace

from app.agents import role_gate

HR = SimpleNamespace(id="hr", name="HR", title="HR", focus=None,
                     instructions="Support HR work: recruitment, candidate reviews, onboarding, employee questions.")  # fmt: skip
OPS = SimpleNamespace(id="ops", name="Ops Analyst", title="Operations Analyst", focus=None,
                      instructions="Analyze business and operational performance across sales, production, supply chain.")  # fmt: skip
CHIEF = SimpleNamespace(id="c", name="Chief", title="chief of staff", instructions="", focus=None)
OTHERS = [HR, OPS, CHIEF]


def _model(answer: str, calls: list):
    async def complete(messages, max_tokens=0):
        calls.append(messages[0]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))])

    return complete


async def test_a_request_outside_the_role_is_pointed_to_the_agent_that_fits():
    calls: list = []
    out = await role_gate.check(HR, "What were Lotto's sales in the last 3 days?", [], OTHERS,
                                _model('{"in_role": false, "fits": "Ops Analyst"}', calls))  # fmt: skip
    assert out and out["fits"] == "Ops Analyst"
    assert "outside my role as HR" in out["text"] and "**Ops Analyst**" in out["text"] and "go ahead" in out["text"]
    assert "recruitment" in calls[0] and "Ops Analyst: Operations Analyst" in calls[0]  # the model saw the roles


async def test_requests_in_role_small_talk_and_the_all_round_agent_go_through():
    calls: list = []
    assert await role_gate.check(HR, "Shortlist candidates for the designer role", [], OTHERS,
                                 _model('{"in_role": true, "fits": ""}', calls)) is None  # fmt: skip
    assert await role_gate.check(HR, "go ahead", [], OTHERS, _model("{}", calls)) is None
    assert await role_gate.check(HR, "Thanks!", [], OTHERS, _model("{}", calls)) is None
    assert await role_gate.check(CHIEF, "What were Lotto's sales?", [], OTHERS, _model("{}", calls)) is None
    assert len(calls) == 1  # small talk and Chief cost no model call


async def test_an_unknown_fit_points_to_the_all_round_agent_and_failures_never_block():
    out = await role_gate.check(HR, "Book me a flight to Goa", [], OTHERS, _model('{"in_role": false, "fits": "Travel Bot"}', []))
    assert "**Chief**" in out["text"]

    async def broken(messages, max_tokens=0):
        raise RuntimeError("model down")

    assert await role_gate.check(HR, "Book me a flight to Goa", [], OTHERS, broken) is None
    assert await role_gate.check(HR, "Book me a flight", [], OTHERS, _model("not json", [])) is None


async def test_a_role_redirect_ends_the_turn_without_tools(monkeypatch):
    from app.agent import deep
    from app.agents import role_gate as gate

    async def outside(agent, query, history, others):
        return {"text": "That's outside my role as HR.", "fits": "Ops Analyst"}

    monkeypatch.setattr(gate, "check", outside)
    out = await deep._role_block(None, SimpleNamespace(id="u"), HR, "Lotto sales?", [])
    assert out["text"].startswith("That's outside my role") and out["used_tools"] == [] and out["role_redirect"]["fits"] == "Ops Analyst"
