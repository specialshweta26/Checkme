"""Before/after latency for a typical "latest news on X" turn, with mocked network latencies taken from the
production logs of a slow news turn (2026-10-29). No keys or network needed.

    cd apps/api && uv run --frozen python scripts/bench_fast_answers.py [--scale 1.0]

AFTER runs the real fast-path code (classify_turn, web_search with inline text, parallel web_read, the
computer-free web_fetch) against mocked Tavily, sites and LLM. BEFORE replays the recorded serial timeline.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import fast_path, web_tools  # noqa: E402
from app.connectors import browsing  # noqa: E402

# Recorded latencies (seconds), from the Render logs of the slow turn.
LAT = {
    "pre_llm_setup": 1.0,  # memory + skill routing (now concurrent)
    "llm_first_token": 0.8,
    "llm_step": 2.5,
    "tavily_search": 2.0,
    "server_page": 0.5,
    "blocked_page": 0.3,
    "tavily_extract": 1.0,
    "computer_read": 36.0,  # E2B render + daemon reinstall per page, as logged
}

BEFORE = {  # replay of the recorded turn
    "pre_llm_setup_serial": 3.0,
    "llm_steps": 8 * LAT["llm_step"],
    "tavily_search": LAT["tavily_search"],
    "web_fetch_on_computer_x2": 36.0,
    "blocked_pages_on_computer_x3": 3 * 36.0,
}
BEFORE_RECORDED_TOTAL = 182.0
ARTICLE = "Article text with facts and dates. " * 40


async def after(scale: float) -> dict:
    s = lambda k: LAT[k] * scale  # noqa: E731
    marks = {}
    t0 = time.monotonic()

    async def handler(request: httpx.Request):
        await asyncio.sleep(s("tavily_search"))
        rows = [
            {"title": f"S{i}", "url": f"https://site{i}.example/a", "content": "snip", "raw_content": ARTICLE if i != 1 else ""}
            for i in range(5)
        ]
        return httpx.Response(200, json={"answer": "Short answer.", "results": rows})

    async def server(url, limit, state):
        if "site1" in url:
            await asyncio.sleep(s("blocked_page"))
            return {"ok": False, "url": url, "status": 403, "title": "Access Denied", "text": "Access Denied"}
        await asyncio.sleep(s("server_page"))
        return {"ok": True, "url": url, "title": "P", "text": ARTICLE, "fetched_via": "server"}

    async def tavily_extract(url, limit, state):
        await asyncio.sleep(s("tavily_extract"))
        return None

    async def nothing(*a):
        return None

    async def no_computer(*a, **k):
        raise AssertionError("quick web touched the computer")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    web_tools._search_client = lambda: client
    settings = SimpleNamespace(tavily_api_key="x", brave_search_api_key="", serper_api_key="", exa_api_key="")
    web_tools.get_settings = lambda: settings
    web_tools.computer_slug = lambda ctx: "computer"
    web_tools.computer_warm = lambda ctx: False
    web_tools.run_computer = no_computer
    browsing._STEPS.update(server=server, proxy=nothing, tavily=tavily_extract, alternates=nothing, reader=nothing)

    query = "latest news on the team captain"
    plan = fast_path.classify_turn(query)
    marks["first_status"] = time.monotonic() - t0  # emitted right after classification
    await asyncio.sleep(s("pre_llm_setup"))
    await asyncio.sleep(s("llm_step"))  # step 1: model decides to call web_search
    found = await web_tools.web_search(query, 5, ctx=SimpleNamespace())
    marks["search_done"] = time.monotonic() - t0
    have_text = sum(1 for r in found["results"] if r.get("text"))
    if have_text < 3:  # the hint allows one parallel read
        await web_tools.web_read(SimpleNamespace(), [r["url"] for r in found["results"][:3]])
    await asyncio.sleep(s("llm_first_token"))  # step 2: the answer streams
    marks["first_token"] = time.monotonic() - t0
    await asyncio.sleep(s("llm_step") - s("llm_first_token"))
    marks["answer_done"] = time.monotonic() - t0
    # Separately: a forced parallel read of 3 pages including a blocked one.
    r0 = time.monotonic()
    read = await web_tools.web_read(SimpleNamespace(), [f"https://site{i}.example/a?x" for i in range(3)])
    marks["web_read_3_pages_incl_blocked"] = time.monotonic() - r0
    await client.aclose()
    return {
        "kind": plan.kind,
        "recency": plan.recency,
        "pages_with_inline_text": have_text,
        "blocked_pages_in_read": sum(1 for p in read["pages"] if not p.get("ok")),
        **{k: round(v / scale, 2) for k, v in marks.items()},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=float, default=1.0, help="Shrink every latency (0.1 runs 10x faster).")
    args = ap.parse_args()
    out = asyncio.run(after(args.scale))
    print(json.dumps({"before_recorded_total_s": BEFORE_RECORDED_TOTAL, "before_breakdown_s": BEFORE, "after_s": out}, indent=2))


if __name__ == "__main__":
    main()
