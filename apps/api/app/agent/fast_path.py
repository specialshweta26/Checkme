"""Cheap per-turn routing and timing for fast answers.

Every chat turn is classified without a model call into one of:

- ``direct``: chit-chat, rewriting, maths, or anything the model can answer from what it knows.
- ``quick_web``: a question that needs a few current pages (news, prices, scores, "what happened").
  Answered with web_search (page text and an answer come back with the results) plus at most a few
  parallel server-side reads. The cloud computer is never booted or waited on for these turns.
- ``computer``: the task needs a real browser session: logging in, clicking around, forms, carts,
  bookings, dashboards, or a site the user wants to watch on the live screen.
- ``deep``: long multi-source research, reports, coding.

The class only steers budgets and the prompt; the model still picks its own tools, and every computer tool
stays available (the computer is started lazily, only when such a tool is actually called).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

DIRECT = "direct"
QUICK_WEB = "quick_web"
COMPUTER = "computer"
DEEP = "deep"

# Generic signals only: nothing here is about one topic, site or person.
_COMPUTER = re.compile(
    r"\b(log ?in|sign ?in|sign ?up|logged in|my account|password|otp|2fa|captcha|"
    r"add to (?:my )?cart|cart|checkout|check out|buy|purchase|order (?:it|this|that|one|me)|place (?:an |the )?order|"
    r"book(?:ing)?|reserve|reservation|check[- ]?in|"
    r"fill (?:in|out)|form|submit|apply (?:for|to)|upload|download (?:the|my|from)|"
    r"dashboard|portal|admin|click|scroll|tap|screenshot|live screen|on (?:the|my|your) (?:computer|screen|browser)|"
    r"open (?:the )?browser|use (?:the )?browser|browse to|navigate to|go to (?:the )?(?:site|website|page)|"
    r"compare (?:quotes|plans|policies)|get (?:a )?quote)\b",
    re.I,
)
_DEEP = re.compile(
    r"\b(deep(?:ly)? research|in[- ]depth|thorough(?:ly)?|comprehensive|literature review|detailed report|"
    r"write (?:a |an )?(?:report|essay|article|paper)|analy[sz]e (?:all|every|the market)|across (?:many|all) (?:sites|sources)|"
    r"every source|crawl|scrape|spreadsheet of|"
    r"pull request|refactor|fix (?:the |this )?bug|repo(?:sitory)?|deploy|implement)\b",
    re.I,
)
_RECENT = re.compile(
    r"\b(news|headlines?|latest|recent(?:ly)?|today|tonight|yesterday|this (?:week|month|morning|evening|year)|"
    r"right now|currently|current|live|breaking|update[sd]?|just (?:happened|announced)|score[sd]?|result[sd]?|"
    r"trending|happening|now)\b",
    re.I,
)
_WEB = re.compile(
    r"\b(news|latest|price (?:of|for)|how much (?:is|does|are)|weather|forecast|score|match|stock|share price|"
    r"exchange rate|who (?:is|won|are)|what (?:is|are|happened)|when (?:is|does|did|will)|where (?:is|can)|"
    r"release date|launch(?:ed)?|announce[ds]?|review[s]?|rank(?:ing)?s?|schedule|fixtures?|election|"
    r"search|look up|find (?:out|me)|google)\b",
    re.I,
)
_DIRECT = re.compile(
    r"^\s*(hi|hey|hello|thanks|thank you|ok(?:ay)?|cool|great|good (?:morning|night|evening)|bye|yo)\b[\s!.?]*$|"
    r"\b(rewrite|rephrase|translate|summari[sz]e (?:this|the following)|proofread|fix (?:the )?grammar|"
    r"explain (?:the concept|like)|write (?:a )?(?:poem|joke|haiku|story|caption|tweet)|calculate|convert \d)\b",
    re.I,
)
_DAY = re.compile(r"\b(today|tonight|right now|live|breaking|just (?:happened|announced)|this (?:morning|evening))\b", re.I)
_MONTH = re.compile(r"\b(this month|recently|recent)\b", re.I)
_YEAR = re.compile(r"\b(this year)\b", re.I)
_NEWS = re.compile(r"\b(news|headlines?|breaking|announce[ds]?|happened|update[sd]?)\b", re.I)


@dataclass
class TurnPlan:
    kind: str
    reason: str = ""
    recency: str = ""  # "", "day", "week", "month", "year"
    news: bool = False

    @property
    def quick(self) -> bool:
        return self.kind in {DIRECT, QUICK_WEB}


def recency_for(query: str) -> tuple[str, bool]:
    """(Tavily time_range or "", news topic?) from generic recency words in the query."""
    q = query or ""
    news = bool(_NEWS.search(q))
    if _DAY.search(q):
        return "day", news
    if _YEAR.search(q):
        return "year", news
    if _MONTH.search(q):
        return "month", news
    if _RECENT.search(q):
        return "week", news
    return "", news


def classify_turn(query: str, *, has_uploads: bool = False, confirmations: bool = False, coding_agent: bool = False) -> TurnPlan:
    """Heuristic class of one chat turn. No I/O, microseconds."""
    q = (query or "").strip()
    if confirmations:
        return TurnPlan(COMPUTER, "confirmed tool call")
    if coding_agent:
        return TurnPlan(DEEP, "coding agent")
    if has_uploads:
        return TurnPlan(DEEP, "uploaded files")
    if not q:
        return TurnPlan(DIRECT, "empty")
    first = q.split("\n\n", 1)[0]
    if _COMPUTER.search(first):
        return TurnPlan(COMPUTER, "needs a real browser session")
    if _DEEP.search(first) or len(first) > 600:
        return TurnPlan(DEEP, "long or multi-source work")
    recency, news = recency_for(first)
    if recency or news or _WEB.search(first):
        return TurnPlan(QUICK_WEB, "current facts", recency or ("week" if news else ""), news)
    if _DIRECT.search(first) or len(first.split()) <= 3:
        return TurnPlan(DIRECT, "answerable directly")
    return TurnPlan(DIRECT, "no web or computer signal")


def prompt_hint(plan: TurnPlan) -> str:
    """Short system-prompt addendum that keeps quick turns quick."""
    if plan.kind == QUICK_WEB:
        when = f" Use recency=\"{plan.recency}\"." if plan.recency else ""
        return (
            "\n\nFAST ANSWER: this is a quick web question. Call web_search once" + when + " Its results already include "
            "each top page's text and a short answer, so answer straight from them. Only if that is not enough, read up "
            "to 3 more pages with ONE web_read call (parallel). Do not use browser_* or computer tools, research_crawl, "
            "write_todos or start_background_task for this, unless a page you truly need requires login or clicking. "
            "Reply concisely with the key facts, dates and source links."
        )
    if plan.kind == DIRECT:
        return (
            "\n\nFAST ANSWER: answer directly and concisely. Only use a tool if the request really needs one; "
            "for current facts use web_search (it returns page text), not the browser."
        )
    return ""


@dataclass
class TurnTimer:
    """Per-stage wall-clock timings for one turn, logged as one line at the end."""

    kind: str = ""
    started: float = field(default_factory=time.monotonic)
    stages: dict[str, int] = field(default_factory=dict)
    tools: list[dict] = field(default_factory=list)
    first_status_ms: int | None = None
    first_token_ms: int | None = None
    llm_calls: int = 0
    llm_ms: int = 0
    computer_used: bool = False

    def ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def mark(self, stage: str, since: float) -> None:
        self.stages[stage] = self.stages.get(stage, 0) + int((time.monotonic() - since) * 1000)

    def status(self) -> None:
        if self.first_status_ms is None:
            self.first_status_ms = self.ms()

    def token(self) -> None:
        if self.first_token_ms is None:
            self.first_token_ms = self.ms()

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "total_ms": self.ms(),
            "first_status_ms": self.first_status_ms,
            "first_token_ms": self.first_token_ms,
            "llm_calls": self.llm_calls,
            "llm_ms": self.llm_ms,
            "stages": self.stages,
            "tools": self.tools[:20],
            "computer_used": self.computer_used,
        }

    def log(self) -> None:
        log.info("turn timing %s", json.dumps(self.summary(), default=str))


try:
    from langchain_core.callbacks import AsyncCallbackHandler
except Exception:  # pragma: no cover
    AsyncCallbackHandler = object  # type: ignore[misc,assignment]

COMPUTER_TOOLS = re.compile(r"^(browser_|browse_|computer_|launch_app$|open_path$|request_takeover$|exec$|run_code$)")


class TimingCallback(AsyncCallbackHandler):  # type: ignore[misc]
    """Feeds TurnTimer from LangChain events (model calls, first token, each tool)."""

    raise_error = False

    def __init__(self, timer: TurnTimer):
        self.timer = timer
        self._llm: dict[Any, float] = {}
        self._tool: dict[Any, tuple[str, float]] = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        self._llm[run_id] = time.monotonic()

    async def on_llm_start(self, serialized, prompts, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        self._llm[run_id] = time.monotonic()

    async def on_llm_new_token(self, token, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        if token:
            self.timer.token()

    async def on_llm_end(self, response, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        t0 = self._llm.pop(run_id, None)
        self.timer.llm_calls += 1
        if t0 is not None:
            self.timer.llm_ms += int((time.monotonic() - t0) * 1000)

    async def on_tool_start(self, serialized, input_str, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        name = (serialized or {}).get("name") or kwargs.get("name") or ""
        if COMPUTER_TOOLS.match(name):
            self.timer.computer_used = True
        self._tool[run_id] = (name, time.monotonic())

    async def _tool_done(self, run_id, ok: bool) -> None:  # noqa: ANN001
        name, t0 = self._tool.pop(run_id, ("", time.monotonic()))
        self.timer.tools.append({"tool": name, "ms": int((time.monotonic() - t0) * 1000), "ok": ok})

    async def on_tool_end(self, output, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        await self._tool_done(run_id, True)

    async def on_tool_error(self, error, *, run_id=None, **kwargs) -> None:  # noqa: ANN001
        await self._tool_done(run_id, False)
