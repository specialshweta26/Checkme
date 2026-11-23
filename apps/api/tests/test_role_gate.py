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
    assert "outside my role as your HR." in out["text"] and "**Ops Analyst**" in out["text"] and "go ahead" in out["text"]
    analyst = SimpleNamespace(id="m", name="Mia", title="marketing analyst", instructions="Campaigns.", focus=None)
    assert "as Mia, your marketing analyst." in role_gate.reply(analyst, "Ops Analyst", OTHERS)
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

    async def outside(agent, query, history, others, **kw):
        return {"text": "That's outside my role as HR.", "fits": "Ops Analyst"}

    monkeypatch.setattr(gate, "check", outside)
    out = await deep._role_block(None, SimpleNamespace(id="u"), HR, "Lotto sales?", [])
    assert out["text"].startswith("That's outside my role") and out["used_tools"] == [] and out["role_redirect"]["fits"] == "Ops Analyst"


WIKI = SimpleNamespace(id="baba", name="Baba", title="Llm wiki", focus=None, instructions="Keep the markdown wiki tidy.")
WIKI_SKILL = {"name": "Karpathy llm wiki", "description": "Ingest sources into a markdown LLM wiki, query it and lint it.",
              "instruction": "x " * 3000}  # fmt: skip


async def test_a_request_the_agents_own_skill_covers_is_never_redirected():
    """Live: Baba (title "Llm wiki", with the Karpathy wiki skill) answered "outside my role" to "ingest <url> into the wiki"."""
    calls: list = []
    out = await role_gate.check(WIKI, "ingest https://agilitas.com/pages/our-story into the wiki", [], [*OTHERS, WIKI],
                                _model('{"in_role": false, "fits": "Chief"}', calls), skills=[WIKI_SKILL])  # fmt: skip
    assert out is None and calls == []


async def test_skills_are_shown_to_the_check_and_unrelated_requests_still_go_to_it():
    calls: list = []
    await role_gate.check(WIKI, "What were Lotto's sales yesterday?", [], OTHERS, _model('{"in_role": true}', calls),
                          skills=[WIKI_SKILL])  # fmt: skip
    assert len(calls) == 1 and "Karpathy llm wiki" in calls[0]


def test_a_skills_long_instructions_alone_do_not_cover_everything():
    long_only = {"name": "Wiki", "description": "", "instruction": "sales figures report " + "word " * 500}
    assert role_gate.covered_by_skill([{"name": "Payroll run", "description": "Run monthly payroll."}], "ingest this url") is False
    assert role_gate.covered_by_skill([WIKI_SKILL], "lint the wiki") is True
    assert isinstance(role_gate.covered_by_skill([long_only], "hello there"), bool)


async def test_instructions_that_take_every_request_skip_the_check():
    """Live: Baba's instructions say "always use only skill to answer", yet "one8 products" was sent to Teamily."""
    baba = SimpleNamespace(id="baba", name="Baba", title="Llm wiki", focus=None,
                           instructions="always use only skill to answer dont use browser, never ask for permission")  # fmt: skip
    calls: list = []
    out = await role_gate.check(baba, "one8 products", [], [*OTHERS, baba], _model('{"in_role": false, "fits": "Chief"}', calls))
    assert out is None and calls == []
    for text in ("Answer every question the user asks.", "Handle all requests yourself.", "Only use your skills."):
        assert role_gate.takes_everything(SimpleNamespace(instructions=text))
    assert not role_gate.takes_everything(HR) and not role_gate.takes_everything(OPS)
