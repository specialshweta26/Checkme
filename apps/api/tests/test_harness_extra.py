import json
from types import SimpleNamespace

from app.agent.deep import RunContext, harness_tools
from app.agent.scratchpad import add, complete, listing, remove
from app.agent.task_catalog import execute, search
from app.agent.web_tools import parse_ddg_html, unwrap_ddg
from app.skills.service import match_skills, skills_prompt


def _ctx(**kwargs):
    user = SimpleNamespace(id="u1", timezone="Asia/Kolkata")
    return RunContext(db=None, user=user, connectors={}, **kwargs)


def test_parse_ddg_html_unwraps_uddg():
    hits = parse_ddg_html(
        '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa">Example Title</a>'
        '<td class="result__snippet">Hello from DDG</td>'
    )
    assert unwrap_ddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Fnews.example%2Fx") == "https://news.example/x"
    assert hits
    assert hits[0]["url"] == "https://example.com/a"
    assert "Example Title" in hits[0]["title"]


async def test_scratchpad_add_complete_remove():
    ctx = _ctx()
    added = await add(ctx, "Clone the repo")
    assert added["item"]["id"] == "s1"
    assert added["item"]["status"] == "open"
    listed = await listing(ctx)
    assert listed["open"] == 1
    done = await complete(ctx, "s1")
    assert done["item"]["status"] == "done"
    gone = await remove(ctx, "s1")
    assert gone["items"] == []


def test_task_catalog_search_finds_schedule_and_image():
    names = {row["name"] for row in search("remind me every morning")}
    assert "schedule_task" in names
    media = {row["name"] for row in search("generate an image")}
    assert "generate_image" in media


async def test_task_catalog_execute_video_is_not_configured():
    ctx = _ctx()
    out = await execute(ctx, "generate_video", {"prompt": "a cat"})
    assert "not configured" in out["error"].lower()


def test_match_skills_ranks_name_hits():
    skills = [
        {"name": "Invoice chase", "instruction": "Open Gmail, find unpaid invoices, draft a reminder."},
        {"name": "Standup notes", "instruction": "Summarize yesterday's commits."},
    ]
    hits = match_skills(skills, "chase the unpaid invoices")
    assert hits[0]["name"] == "Invoice chase"
    prompt = skills_prompt(hits)
    assert "Invoice chase" in prompt


async def test_remember_without_db_errors():
    ctx = _ctx()
    tool = next(t for t in harness_tools(ctx) if t.name == "remember")
    data = json.loads(await tool.coroutine(text="I prefer dark mode", label="prefs"))
    assert data.get("error")


async def test_launch_app_browser_does_not_exec():
    calls = []

    async def fake_call(slug, tool, args):
        calls.append(tool)
        return {"ok": True}

    computer = SimpleNamespace(
        id="1", name="Computer", alias="computer", slug="computer", kind="builtin", provider="daytona",
        description="", server_key="", health="ok",
    )
    ctx = RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={"computer": computer}, call_impl=fake_call)
    tool = next(t for t in harness_tools(ctx) if t.name == "launch_app")
    data = json.loads(await tool.coroutine(name="chrome"))
    assert data["ok"] is True
    assert calls == []
