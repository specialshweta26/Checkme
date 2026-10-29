"""Fast answers: turn routing, recency-aware search with inline page text, parallel server-side reads, and
quick-web turns that never touch the cloud computer."""

import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.agent import fast_path, web_tools
from app.agent.fast_path import COMPUTER, DEEP, DIRECT, QUICK_WEB, classify_turn
from app.connectors import browsing

ARTICLE = "A long enough article body about the topic with dates and facts. " * 12


def _settings(**kw):
    base = {"tavily_api_key": "", "brave_search_api_key": "", "serper_api_key": "", "exa_api_key": ""}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "text,kind",
    [
        ("latest news on the national cricket team captain", QUICK_WEB),
        ("what happened in the election today", QUICK_WEB),
        ("price of the new phone model", QUICK_WEB),
        ("who won the match yesterday", QUICK_WEB),
        ("weather forecast for tomorrow", QUICK_WEB),
        ("log in to my bank dashboard and download the statement", COMPUTER),
        ("book a table for two tonight", COMPUTER),
        ("add the cheapest charger to my cart", COMPUTER),
        ("compare quotes for term insurance on the comparison site", COMPUTER),
        ("fill out the visa application form", COMPUTER),
        ("hi", DIRECT),
        ("thanks!", DIRECT),
        ("rewrite this paragraph to sound friendlier: we are late", DIRECT),
        ("write a poem about monsoon", DIRECT),
        ("write a detailed report on the EV market across all sources", DEEP),
        ("fix the bug in the repo and open a pull request", DEEP),
    ],
)
def test_classify_turn(text, kind):
    assert classify_turn(text).kind == kind


def test_classify_overrides():
    assert classify_turn("latest news", confirmations=True).kind == COMPUTER
    assert classify_turn("latest news", coding_agent=True).kind == DEEP
    assert classify_turn("latest news", has_uploads=True).kind == DEEP
    assert classify_turn("").kind == DIRECT


def test_recency_is_generic():
    assert fast_path.recency_for("news about any person") == ("week", True)
    assert fast_path.recency_for("score right now")[0] == "day"
    assert fast_path.recency_for("what changed this year")[0] == "year"
    assert fast_path.recency_for("history of the roman empire") == ("", False)
    plan = classify_turn("latest news about a company")
    assert plan.recency == "week" and plan.news and plan.quick
    assert "web_search" in fast_path.prompt_hint(plan) and 'recency="week"' in fast_path.prompt_hint(plan)
    assert fast_path.prompt_hint(classify_turn("log in to my portal")) == ""


@pytest.fixture
def tavily(monkeypatch):
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append(body)
        rows = [
            {"title": f"Story {i}", "url": f"https://news{i}.example/a", "content": "snip", "raw_content": ARTICLE, "published_date": "2026-10-29"}
            for i in range(5)
        ]
        if body.get("time_range") == "day" and "empty" in body["query"]:
            rows = []
        return httpx.Response(200, json={"answer": "Short answer.", "results": rows})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(web_tools, "_search_client", lambda: client)
    monkeypatch.setattr(web_tools, "get_settings", lambda: _settings(tavily_api_key="tv"))
    return calls


async def test_recent_search_uses_news_topic_and_returns_text_and_answer(tavily):
    out = await web_tools.web_search("latest news on the team", 5)
    body = tavily[-1]
    assert body["topic"] == "news" and body["time_range"] == "week" and body["include_answer"] == "basic"
    assert body["include_raw_content"] == "markdown"
    assert out["answer"] == "Short answer." and out["recency"] == "week"
    texts = [r.get("text") for r in out["results"]]
    assert all(texts[:3]) and not any(texts[3:])  # top 3 carry page text, capped
    assert all("_raw" not in r for r in out["results"])
    assert out["results"][0]["published"] == "2026-10-29"


async def test_plain_search_has_no_recency_filter(tavily):
    await web_tools.web_search("history of the roman empire", 5)
    assert "time_range" not in tavily[-1] and "topic" not in tavily[-1]


async def test_model_can_force_or_turn_off_recency(tavily):
    await web_tools.web_search("roman empire", 5, recency="month")
    assert tavily[-1]["time_range"] == "month" and "topic" not in tavily[-1]
    await web_tools.web_search("news today", 5, recency="none")
    assert "time_range" not in tavily[-1]


async def test_too_recent_retries_without_the_date_filter(tavily):
    out = await web_tools.web_search("empty thing today", 5)
    assert [b.get("time_range") for b in tavily] == ["day", None]
    assert out["results"]


async def test_identical_search_is_cached(tavily):
    a = await web_tools.web_search("latest news on the team", 5)
    b = await web_tools.web_search("latest news on the team", 5)
    assert len(tavily) == 1 and b.get("cached") and b["results"][0]["url"] == a["results"][0]["url"]
    assert b["results"][0]["text"]


def _no_computer(monkeypatch):
    """A connected (cold) computer that must never be used."""

    async def boom(*a, **k):
        raise AssertionError("quick web must not call the computer")

    import app.agent.deep as deep

    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "computer")
    monkeypatch.setattr(web_tools, "computer_warm", lambda ctx: False)
    monkeypatch.setattr(web_tools, "run_computer", boom)
    monkeypatch.setattr(deep, "call_user_tool", boom)


def _server(monkeypatch, delay=0.0, blocked=()):
    seen = []

    async def server(url, limit, state):
        seen.append(url)
        await asyncio.sleep(delay)
        if any(b in url for b in blocked):
            return {"ok": False, "url": url, "status": 403, "title": "Access Denied", "text": "Access Denied", "html": ""}
        return {"ok": True, "url": url, "title": "Page", "text": ARTICLE, "status": 200, "fetched_via": "server"}

    monkeypatch.setitem(browsing._STEPS, "server", server)
    monkeypatch.setitem(browsing._STEPS, "proxy", lambda *a: asyncio.sleep(0, None))
    return seen


async def test_web_fetch_reads_server_side_even_with_a_computer(monkeypatch):
    _no_computer(monkeypatch)
    _server(monkeypatch)
    page = await web_tools.web_fetch(SimpleNamespace(), "https://site.example/story")
    assert page["ok"] and page["fetched_via"] == "server"


async def test_blocked_page_never_boots_a_cold_computer(monkeypatch):
    _no_computer(monkeypatch)
    _server(monkeypatch, blocked=("blocked",))
    page = await web_tools.web_fetch(SimpleNamespace(), "https://blocked.example/story")
    assert page["ok"] is False and "browser_navigate" in page["hint"]


async def test_blocked_page_uses_an_already_running_computer(monkeypatch):
    calls = []

    async def run(ctx, name, args):
        calls.append(name)
        return {"ok": True, "url": args["url"], "text": ARTICLE, "title": "Shown"}

    monkeypatch.setattr(web_tools, "computer_slug", lambda ctx: "computer")
    monkeypatch.setattr(web_tools, "computer_warm", lambda ctx: True)
    monkeypatch.setattr(web_tools, "run_computer", run)
    _server(monkeypatch, blocked=("blocked",))
    page = await web_tools.web_fetch(SimpleNamespace(), "https://blocked.example/story")
    assert calls == ["browser_navigate"] and page["fetched_via"] == "browser"


async def test_web_read_is_parallel_capped_and_computer_free(monkeypatch):
    _no_computer(monkeypatch)
    seen = _server(monkeypatch, delay=0.3, blocked=("bad",))
    urls = [f"https://s{i}.example/p" for i in range(7)] + ["https://bad.example/x"]
    t0 = time.monotonic()
    out = await web_tools.web_read(SimpleNamespace(), urls)
    took = time.monotonic() - t0
    assert len(out["pages"]) == 5 and len(seen) == 5 and out["skipped"]
    assert took < 1.0, took  # 5 reads of 0.3s each ran together
    assert all(p["ok"] and p["text"] for p in out["pages"])


async def test_web_read_reports_blocked_pages(monkeypatch):
    _no_computer(monkeypatch)
    _server(monkeypatch, blocked=("bad",))
    out = await web_tools.web_read(SimpleNamespace(), ["https://good.example/a", "https://bad.example/b"])
    good, bad = out["pages"]
    assert good["ok"] and bad["ok"] is False and "hint" in out


async def test_quick_web_turn_tools_never_touch_the_computer(monkeypatch, tavily):
    """The whole quick-web tool path (search with inline text, then a parallel read) with a cold computer."""
    _no_computer(monkeypatch)
    _server(monkeypatch)
    found = await web_tools.web_search("latest news on the team", 5, ctx=SimpleNamespace())
    assert found["source"] == "tavily"
    urls = [r["url"] for r in found["results"][:3]]
    read = await web_tools.web_read(SimpleNamespace(), urls)
    # Tavily already returned these pages: they come from the cache, with no request to the sites.
    assert all(p["fetched_via"] == "tavily_cache" for p in read["pages"])


async def test_draft_streams_tokens_and_clears_narration():
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    from app.agent import progress as P

    class R:
        def __init__(self):
            self.drafts = []
            self.lines = []

        async def emit(self, text, kind="step", tool=""):
            self.lines.append(text)

        async def draft(self, text, force=False):
            self.drafts.append(text)

    rep = R()
    cb = P.ProgressCallback(rep, query="q")
    await cb.on_chat_model_start({}, [])
    for t in ("Let me ", "search."):
        await cb.on_llm_new_token(t)
    msg = AIMessage(content="Let me search.", tool_calls=[{"name": "web_search", "args": {"query": "q"}, "id": "1"}])
    await cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=msg)]]))
    assert rep.drafts[-1] == ""  # narration became a status line, not reply text
    await cb.on_chat_model_start({}, [])
    for t in ("The ", "answer."):
        await cb.on_llm_new_token(t)
    await cb.on_llm_end(LLMResult(generations=[[ChatGeneration(message=AIMessage(content="The answer."))]]))
    assert rep.drafts[-1] == "The answer."


async def test_reporter_draft_is_throttled_and_readable():
    from app.agent import progress as P

    class FakeRedis:
        def __init__(self):
            self.kv = {}

        async def set(self, k, v, ex=None):
            self.kv[k] = v

        async def get(self, k):
            return self.kv.get(k)

    r = FakeRedis()
    rep = P.ProgressReporter("u1", "turn_abcdefgh", redis_client=r)
    await rep.draft("Hel")
    await rep.draft("Hello")  # within the throttle window: not written yet
    assert await P.read_progress_draft("u1", "turn_abcdefgh", redis_client=r) == "Hel"
    await rep.draft("Hello there", force=True)
    assert await P.read_progress_draft("u1", "turn_abcdefgh", redis_client=r) == "Hello there"


async def test_timing_callback_records_stages():
    from uuid import uuid4

    timer = fast_path.TurnTimer(kind=QUICK_WEB)
    cb = fast_path.TimingCallback(timer)
    rid = uuid4()
    await cb.on_chat_model_start({}, [], run_id=rid)
    await cb.on_llm_new_token("x", run_id=rid)
    await cb.on_llm_end(None, run_id=rid)
    tid = uuid4()
    await cb.on_tool_start({"name": "web_search"}, "", run_id=tid)
    await cb.on_tool_end("", run_id=tid)
    s = timer.summary()
    assert s["llm_calls"] == 1 and s["first_token_ms"] is not None and s["tools"][0]["tool"] == "web_search"
    assert s["computer_used"] is False
    await cb.on_tool_start({"name": "browser_navigate"}, "", run_id=uuid4())
    assert timer.summary()["computer_used"] is True


async def test_quick_web_turn_end_to_end(monkeypatch):
    """A news-style turn: instant 'Looking this up' status, the fast-answer hint, timing, and no computer."""
    import uuid

    from langchain_core.messages import AIMessage

    import app.connectors.platform as platform
    from app.agent import deep
    from app.agent import progress as P
    from tests.test_progress import FakeRedis

    _no_computer(monkeypatch)

    async def noop(*a, **k):
        return None

    async def conns(*a, **k):
        return [SimpleNamespace(slug="computer", name="Computer", id=1)]

    async def empty(*a, **k):
        return {}

    async def memory(*a, **k):
        return ""

    seen = {}

    class Graph:
        async def ainvoke(self, payload, config=None):
            return {"messages": [AIMessage(content="Here is the news.")]}

    def build(ctx, prompt):
        seen["prompt"] = prompt
        return Graph()

    monkeypatch.setattr(platform, "ensure_platform_connectors", noop)
    monkeypatch.setattr(deep, "user_connectors", conns)
    monkeypatch.setattr(deep, "connector_assignment_map", empty)
    monkeypatch.setattr(deep, "toolkit_assignment_map", empty)
    monkeypatch.setattr(deep, "filter_connectors", lambda c, *a: c)
    monkeypatch.setattr(deep, "load_memory", memory)
    monkeypatch.setattr(deep, "_maybe_start_connect", noop)
    monkeypatch.setattr(deep, "build_agent", build)
    monkeypatch.setattr(deep, "system_prompt", lambda *a, **k: "")

    r = FakeRedis()
    rep = P.ProgressReporter("u1", "turn_fast0001", redis_client=r)
    out = await deep.run_deep_agent(
        None, SimpleNamespace(id="u1", timezone="Asia/Kolkata"), uuid.uuid4(), "r1", [], "latest news on the team captain", progress=rep
    )
    assert out["text"] == "Here is the news."
    assert "FAST ANSWER" in seen["prompt"]
    assert out["timing"]["kind"] == QUICK_WEB and out["timing"]["computer_used"] is False
    assert out["timing"]["first_status_ms"] is not None and out["timing"]["first_status_ms"] < 1000
    texts = [s["text"] for s in await P.read_progress("u1", "turn_fast0001", redis_client=r)]
    assert texts == ["Looking this up on the web"]
