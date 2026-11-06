"""Runs one scenario through the real agent: the real model, system prompt and tools. Files, the computer and the web
are fakes, so every run starts the same way and nothing touches real files or accounts."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest import mock

NOT_IN_EVAL = "This tool isn't available in this check."


@dataclass
class Scenario:
    name: str
    message: str
    files: dict[str, str | bytes] = field(default_factory=dict)  # path -> content; bytes are binary
    web: dict[str, str] = field(default_factory=dict)  # search words -> result text
    history: list[dict] = field(default_factory=list)
    images: list[bytes] = field(default_factory=list)
    must_call: list[str] = field(default_factory=list)  # each must be called; "a|b" means either
    must_not_call: list[str] = field(default_factory=list)  # fnmatch patterns, e.g. "browser_*"
    reply_has: list[str] = field(default_factory=list)  # regexes, case-insensitive
    reply_lacks: list[str] = field(default_factory=list)
    max_seconds: float = 60
    max_tool_calls: int = 12
    note: str = ""
    known_issue: str = ""  # a failure we know about and haven't fixed yet: reported, but doesn't fail the run


@dataclass
class Result:
    name: str
    passed: bool
    known_issue: str = ""
    seconds: float = 0.0
    calls: list[tuple[str, dict]] = field(default_factory=list)
    reply: str = ""
    problems: list[str] = field(default_factory=list)


class FakeComputer:
    """A small filesystem behind the agent's computer tools. Records every call."""

    def __init__(self, files: dict[str, str | bytes]):
        self.files = {self._norm(k): v for k, v in files.items()}
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _norm(path: str) -> str:
        path = (path or "").strip().replace("\\", "/")
        path = re.sub(r"^(/Users/[^/]+|/home/[^/]+|\$HOME|~)(?=/|$)", "~", path)
        if path and not path.startswith("~") and not path.startswith("/"):
            path = "~/" + path
        return path.rstrip("/") or "~"

    def _children(self, folder: str) -> list[dict]:
        folder = self._norm(folder)
        seen: dict[str, dict] = {}
        for path in self.files:
            if not path.startswith(folder + "/"):
                continue
            head = path[len(folder) + 1 :].split("/", 1)
            kind = "dir" if len(head) > 1 else "file"
            seen.setdefault(head[0], {"name": head[0], "type": kind, "path": f"{folder}/{head[0]}"})
        return sorted(seen.values(), key=lambda e: e["name"])

    async def run(self, ctx, tool: str, args: dict | None = None, **_: Any) -> Any:
        args = dict(args or {})
        self.calls.append((tool, args))
        if tool in {"list_dir", "list_files"}:
            path = args.get("path") or "~"
            entries = self._children(path)
            if not entries and self._norm(path) != "~":
                return {"ok": False, "error": f"{path} does not exist", "ran_on": "local_computer"}
            if self._norm(path) == "~":
                entries = [{"name": n, "type": "dir", "path": f"~/{n}"} for n in ("Desktop", "Documents", "Downloads")]
            return {"ok": True, "path": path, "entries": entries, "ran_on": "local_computer"}
        if tool == "read_file":
            data = self.files.get(self._norm(args.get("path", "")))
            if data is None:
                return {"ok": False, "error": f"{args.get('path')} does not exist", "ran_on": "local_computer"}
            if isinstance(data, bytes):
                return {"ok": False, "error": f"{args.get('path')} is a binary file, so its text can't be read here. Say so and ask the user to attach it to the chat.", "ran_on": "local_computer"}
            return {"ok": True, "path": args.get("path"), "content": data, "ran_on": "local_computer"}
        if tool == "search_files":
            words = [w for w in re.findall(r"\w+", str(args.get("query") or "").lower()) if len(w) > 2]
            hits = [p for p in self.files if any(w in p.lower() for w in words)]
            return {"ok": True, "matches": [{"path": p} for p in hits[:20]], "ran_on": "local_computer"}
        if tool == "write_file":
            self.files[self._norm(args.get("path", ""))] = str(args.get("content") or "")
            return {"ok": True, "path": args.get("path"), "verified": True, "ran_on": "local_computer"}
        if tool in {"app_discover"}:
            return {"ok": True, "apps": []}
        return {"ok": False, "error": NOT_IN_EVAL, "tool": tool}


def _png_message(images: list[bytes]):
    if not images:
        return None
    import base64

    from langchain_core.messages import HumanMessage

    parts = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b).decode()}} for b in images]
    return HumanMessage(content=[{"type": "text", "text": "The images the user attached to their message:"}, *parts])


def _matches(name: str, pattern: str) -> bool:
    return any(fnmatch.fnmatch(name, p) for p in pattern.split("|"))


def check(s: Scenario, calls: list[tuple[str, dict]], reply: str, seconds: float) -> list[str]:
    names = [n for n, _ in calls]
    reply = reply.replace("\u2019", "'").replace("\u2018", "'")  # models use curly apostrophes
    problems = []
    for want in s.must_call:
        if not any(_matches(n, want) for n in names):
            problems.append(f"didn't call {want}")
    for bad in s.must_not_call:
        hit = [n for n in names if _matches(n, bad)]
        if hit:
            problems.append(f"called {hit[0]}, which it shouldn't")
    for rx in s.reply_has:
        if not re.search(rx, reply, re.I):
            problems.append(f"reply lacks /{rx}/")
    for rx in s.reply_lacks:
        if re.search(rx, reply, re.I):
            problems.append(f"reply has /{rx}/")
    if seconds > s.max_seconds:
        problems.append(f"took {seconds:.0f}s, limit {s.max_seconds:.0f}s")
    if len(names) > s.max_tool_calls:
        problems.append(f"{len(names)} tool calls, limit {s.max_tool_calls}")
    return problems


async def run_scenario(s: Scenario) -> Result:
    import app.connectors.platform as platform
    from app.agent import computer_harness, deep, fast_browser, web_tools

    computer = FakeComputer(s.files)
    tool_names: list[tuple[str, dict]] = []

    async def none(*a, **k):
        return None

    async def empty(*a, **k):
        return {}

    async def conns(*a, **k):
        from app.models import Connector

        return [Connector(id=uuid.uuid4(), user_id=uuid.uuid4(), slug="computer", name="Computer", alias=None, kind="builtin",
                          provider="daytona", description="", tags=["computer"], default_for=[], enabled=True, health="ok")]

    async def web_search(query, count=8, ctx=None, **k):
        tool_names.append(("web_search", {"query": query}))
        words = set(re.findall(r"\w+", str(query).lower()))
        hits = [{"title": key, "url": f"https://example.test/{i}", "text": text} for i, (key, text) in enumerate(s.web.items()) if words & set(key.lower().split())]
        return {"ok": True, "results": hits or [{"title": "No results", "url": "", "text": "Nothing relevant was found."}]}

    async def web_read(ctx, urls):
        tool_names.append(("web_read", {"urls": urls}))
        return {"ok": True, "pages": [{"url": u, "text": next(iter(s.web.values()), "")} for u in urls]}

    async def refused(*a, **k):
        name = k.get("_name", "tool")
        tool_names.append((name, {}))
        return {"ok": False, "error": NOT_IN_EVAL}

    async def calls_user_tool(ctx, slug, tool, args=None):
        tool_names.append((tool, dict(args or {})))
        return {"ok": False, "error": NOT_IN_EVAL}

    shown = _png_message(s.images)
    original_build = deep.build_agent

    async def image_message(keys):
        return shown

    with ExitStack() as stack:
        patch = lambda target, value: stack.enter_context(mock.patch.object(*target, value))  # noqa: E731
        patch((platform, "ensure_platform_connectors"), none)
        patch((deep, "user_connectors"), conns)
        patch((deep, "with_computer"), lambda db, user, c: _same(c))
        patch((deep, "connector_assignment_map"), empty)
        patch((deep, "toolkit_assignment_map"), empty)
        patch((deep, "filter_connectors"), lambda c, *a: c)
        patch((deep, "_maybe_start_connect"), none)
        patch((deep, "call_user_tool"), calls_user_tool)
        patch((computer_harness, "run_computer"), computer.run)
        patch((computer_harness, "_try_local"), lambda *a, **k: _same(None))
        patch((fast_browser, "prewarm"), lambda ctx: None)
        patch((web_tools, "web_search"), web_search)
        patch((web_tools, "web_read"), web_read)
        patch((web_tools, "run_computer"), computer.run)
        stack.enter_context(mock.patch("app.agent.attachments.image_message", image_message))
        started = time.monotonic()
        try:
            out = await asyncio.wait_for(
                deep.run_deep_agent(
                    None,
                    SimpleNamespace(id=uuid.uuid4(), email="eval@example.test", name="Eval", timezone="Asia/Kolkata"),
                    uuid.uuid4(),
                    f"eval-{s.name}",
                    list(s.history),
                    s.message,
                    client_kind="desktop",
                    images=["eval-image"] if shown is not None else None,
                ),
                timeout=s.max_seconds * 3,
            )
            reply = str(out.get("text") or "")
            used = list(out.get("used_tools") or [])
        except Exception as exc:  # noqa: BLE001 - a crash is a failed check, not a crashed run
            reply, used = f"<error: {type(exc).__name__}: {exc}>", []
        seconds = time.monotonic() - started
    _ = original_build
    calls = [(n, a) for n, a in computer.calls] + tool_names
    names_from_ctx = [(n, {}) for n in used if n not in {c for c, _ in calls}]
    calls += names_from_ctx
    problems = check(s, calls, reply, seconds)
    if reply.startswith("<error"):
        problems.insert(0, reply)
    return Result(s.name, not problems, s.known_issue, seconds, calls, reply, problems)


async def _same(value):
    return value


def summary(results: list[Result]) -> str:
    lines = []
    for r in results:
        mark = "PASS" if r.passed else ("KNOWN" if r.known_issue else "FAIL")
        tools = ", ".join(n for n, _ in r.calls) or "-"
        lines.append(f"{mark}  {r.name:<34} {r.seconds:5.1f}s  tools: {tools}")
        for p in r.problems:
            lines.append(f"      - {p}")
        if r.known_issue and not r.passed:
            lines.append(f"      known issue: {r.known_issue}")
    passed = sum(r.passed for r in results)
    known = sum(1 for r in results if not r.passed and r.known_issue)
    lines.append(f"\n{passed}/{len(results)} passed" + (f", {known} known issue(s)" if known else ""))
    return "\n".join(lines)


def failed(results: list[Result]) -> bool:
    """Known issues are reported but don't fail the run."""
    return any(not r.passed and not r.known_issue for r in results)


def to_json(results: list[Result]) -> str:
    return json.dumps(
        [{"name": r.name, "passed": r.passed, "known_issue": r.known_issue, "seconds": round(r.seconds, 1), "tools": [n for n, _ in r.calls], "problems": r.problems, "reply": r.reply[:2000]} for r in results],
        indent=2,
    )
