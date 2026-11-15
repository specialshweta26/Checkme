"""A separate browser per agent (docs/AGENT_BROWSERS.md).

By default every agent drives the one shared browser on the cloud computer, so a site you signed into by hand there
is open to all of them. An agent set to its own browser gets a separate Chrome profile on the same computer, with
its own cookies and sign-ins. The browser daemon calls each profile a "space"; ``space_for`` names it from the agent
id and a reset counter, so "Sign out of everything" just moves the agent to a new, empty profile.

``BROWSER_SPACE`` holds (space, agent id) for the current run: ``run_deep_agent`` sets it, and the computer's browser
calls (``connectors/daytona_box._browser_rpc``) add the space to every command. Cookies of an agent's browser are
saved encrypted per agent (``AgentBrowserProfile``) so a recycled computer keeps its sign-ins.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

BROWSER_SPACE: ContextVar[tuple[str, str]] = ContextVar("agent_browser_space", default=("", ""))
MODES = ("shared", "own")


def settings(agent: Any) -> dict:
    raw = getattr(agent, "browser", None) or {}
    raw = raw if isinstance(raw, dict) else {}
    mode = raw.get("mode") if raw.get("mode") in MODES else "shared"
    try:
        epoch = max(0, int(raw.get("epoch") or 0))
    except (TypeError, ValueError):
        epoch = 0
    return {"mode": mode, "epoch": epoch}


def space_for(agent: Any) -> str:
    """The daemon's name for this agent's own browser, or "" when it uses the shared one."""
    agent_id = getattr(agent, "id", None)
    s = settings(agent)
    if agent_id is None or s["mode"] != "own":
        return ""
    hexid = getattr(agent_id, "hex", None) or str(agent_id).replace("-", "")
    return f"a{hexid}-{s['epoch']}"


def current() -> tuple[str, str]:
    return BROWSER_SPACE.get()


def use(agent: Any):
    """Set the browser for this run (a shared-browser teammate inside an own-browser agent's turn goes back to shared)."""
    agent_id = getattr(agent, "id", None)
    return BROWSER_SPACE.set((space_for(agent), str(agent_id) if agent_id else ""))


def serialize(agent: Any) -> dict:
    s = settings(agent)
    return {"mode": s["mode"]}
