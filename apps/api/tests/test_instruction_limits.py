"""An agent's own instructions take tools away (app/agents/instruction_limits.py).

Live: Baba's instructions say "always use only skill to answer dont use browser", yet it ran web_search for One8's
return policy."""

from __future__ import annotations

from types import SimpleNamespace

from app.agents import instruction_limits as il

BABA = "always use only skill to answer dont use browser, never ask for permission"
TOOLS = [SimpleNamespace(name=n) for n in (
    "read_file", "edit_file", "list_files", "web_search", "web_read", "web_fetch", "research_crawl",
    "browser_navigate", "browser_act", "browse_open", "browser_search", "get_skill",
)]  # fmt: skip


def names(tools):
    return {t.name for t in tools}


def test_skill_only_takes_away_browser_and_web_but_keeps_files_and_skills():
    kept, note = il.apply(TOOLS, BABA, "return policy for one8")
    assert names(kept) == {"read_file", "edit_file", "list_files", "get_skill"}
    assert "not available" in note and "skills" in note


def test_a_link_in_the_request_keeps_page_fetching_for_the_skills_ingest():
    kept, _ = il.apply(TOOLS, BABA, "ingest https://agilitas.com/pages/our-story into the wiki")
    assert {"web_read", "web_fetch"} <= names(kept)
    assert not names(kept) & {"web_search", "browser_navigate", "browse_open", "research_crawl"}


def test_no_browser_alone_keeps_web_search():
    kept, note = il.apply(TOOLS, "Be brief. Don't use the browser.", "weather in Pune")
    assert "web_search" in names(kept) and "web_read" in names(kept)
    assert not any(n.startswith(("browser_", "browse_")) for n in names(kept))
    assert "browser" in note


def test_no_web_turns_off_search_and_fetch():
    kept, _ = il.apply(TOOLS, "Never use the internet; answer from my files.", "what is in my notes")
    assert not names(kept) & {"web_search", "web_read", "web_fetch", "browser_navigate"}


def test_ordinary_instructions_change_nothing():
    for text in ("Support HR work: recruitment and onboarding.", "Use the browser for sites that need login.",
                 "Never ask for permission.", ""):  # fmt: skip
        kept, note = il.apply(TOOLS, text, "anything")
        assert kept == TOOLS and note == ""


def test_the_agent_is_built_without_the_ruled_out_tools(monkeypatch):
    from app.agent import deep

    seen = {}

    def fake_create(**kw):
        seen.update(kw)
        return "graph"

    import deepagents

    monkeypatch.setattr(deepagents, "create_deep_agent", fake_create)
    monkeypatch.setattr(deep, "harness_tools", lambda ctx: list(TOOLS))
    monkeypatch.setattr(deep, "_chat_model", lambda ctx: "model")
    ctx = SimpleNamespace(agent=SimpleNamespace(instructions=BABA), route_query="return policy for one8", query="go ahead",
                          hidden_tools=[], turn_context="", job=None, teammate_depth=0)  # fmt: skip
    assert deep.build_agent(ctx, "PROMPT") == "graph"
    assert "web_search" not in names(seen["tools"]) and "browser_navigate" not in names(seen["tools"])
    assert seen["system_prompt"].startswith("PROMPT") and "not available" in seen["system_prompt"]
