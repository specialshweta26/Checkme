"""Plan-and-build helpers for agents whose focus is Coding and deployments.

Everything runs through existing infrastructure: the `computer` connector (Daytona
sandbox `exec`), whatever GitHub connector or Composio GitHub toolkit the user has,
and any deploy connector (Render MCP, Vercel, ...). Nothing new is provisioned here.
"""

from __future__ import annotations

from typing import Any

COMPUTER_SLUG = "computer"
SHELL_TIMEOUT_DEFAULT = 300
SHELL_TIMEOUT_MAX = 900
DEPLOY_HINTS = ("render", "vercel", "netlify", "fly", "railway", "heroku", "cloudflare")

# One shell round-trip that reports the toolchain and whether git can push.
PROBE_COMMAND = (
    "echo git=$(git --version 2>/dev/null | head -1); "
    "echo gh=$(gh --version 2>/dev/null | head -1); "
    "echo gh_auth=$(gh auth status >/dev/null 2>&1 && echo ok || echo none); "
    "echo git_cred=$(git config --global credential.helper 2>/dev/null || echo none); "
    "echo node=$(node --version 2>/dev/null); echo python=$(python3 --version 2>/dev/null); "
    "echo home=$HOME; mkdir -p ~/work && echo work=$(cd ~/work && pwd)"
)


def _text(conn) -> str:
    bits = [getattr(conn, a, "") or "" for a in ("slug", "provider", "name", "alias", "description")]
    tags = getattr(conn, "tags", None) or []
    return " ".join([*map(str, bits), *map(str, tags)]).lower()


def classify_connectors(connectors: dict[str, Any]) -> dict:
    computer = connectors.get(COMPUTER_SLUG)
    github = [c for c in connectors.values() if "github" in _text(c)]
    deploy = [c for c in connectors.values() if any(h in _text(c) for h in DEPLOY_HINTS) and c is not computer]
    composio = connectors.get("composio")
    return {
        "computer": computer,
        "github": github,
        "deploy": deploy,
        "composio": composio,
    }


def parse_probe(stdout: str) -> dict:
    out: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def readiness(connectors: dict[str, Any], probe: dict | None = None, probe_error: str = "") -> dict:
    """What this coding agent can do right now, and exactly what is missing."""
    found = classify_connectors(connectors)
    computer = found["computer"]
    missing: list[str] = []
    can: list[str] = ["plan the work (write_todos)"]
    probe = probe or {}
    if computer is None:
        missing.append(
            "The computer (Daytona sandbox) is not available for this agent, so I cannot clone, edit or run code. "
            "It needs DAYTONA_API_KEY on the server, or the computer connector assigned to this agent."
        )
    elif probe_error:
        missing.append(f"The computer did not respond: {probe_error[:200]}")
    else:
        can.append("clone repos, edit files, install dependencies and run tests on the computer (sandbox_shell)")
        if not probe.get("git"):
            missing.append("git is not installed on the computer; install it with sandbox_shell (apt-get install -y git) first.")
    git_push = probe.get("gh_auth") == "ok" or probe.get("git_cred", "none") not in {"", "none"}
    if found["github"]:
        can.append("push branches and open pull requests through " + ", ".join(sorted({c.name for c in found["github"]})))
    elif git_push:
        can.append("push and open pull requests with git/gh credentials already on the computer")
    elif found["composio"] is not None:
        missing.append(
            "GitHub may not be connected. Check call_tool(composio, list_connections); if github is not listed, "
            "connect it with connect_app github so I can push branches and open pull requests. "
            "Public repos can still be cloned and worked on."
        )
    else:
        missing.append(
            "GitHub is not connected, so I cannot push or open pull requests. Connect a GitHub connector "
            "(or connect_app github). Public repos can still be cloned and worked on."
        )
    if found["deploy"]:
        can.append("trigger and check deploys through " + ", ".join(sorted({c.name for c in found["deploy"]})))
    else:
        missing.append("No deploy connector (Render, Vercel, Netlify, ...) is connected, so I cannot trigger or check deploys.")
    return {
        "computer": "ready" if computer is not None and not probe_error else "missing",
        "toolchain": {k: v for k, v in probe.items() if k in {"git", "gh", "node", "python", "work"} and v},
        "git_push_credentials": git_push,
        "github_connectors": [{"slug": c.slug, "name": c.name} for c in found["github"]],
        "deploy_connectors": [{"slug": c.slug, "name": c.name} for c in found["deploy"]],
        "can": can,
        "missing": missing,
        "workdir": probe.get("work") or "~/work",
    }


def clamp_timeout(value: Any) -> int:
    try:
        n = int(value or SHELL_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        n = SHELL_TIMEOUT_DEFAULT
    return max(10, min(n, SHELL_TIMEOUT_MAX))


def is_coding_agent(agent) -> bool:
    return (getattr(agent, "focus", "") or "") == "coding"
