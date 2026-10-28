"""First-class computer/browser/file verbs. They wrap this user's cloud computer (E2B Desktop or Daytona)."""

from __future__ import annotations

import re
from typing import Any

from app.agent.coding import COMPUTER_SLUG

LOCAL_PROVIDERS = {"local", "trusted_local", "computer_local"}
CLOUD_PROVIDERS = {"daytona"}
_BROWSER_APPS = {
    "browser",
    "chrome",
    "chromium",
    "google-chrome",
    "google chrome",
    "safari",
    "firefox",
}
_EDITOR_APPS = {"code", "vscode", "vs code", "cursor", "visual studio code"}
_APP_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._+-]{0,40}$")
_TEXT_FILES = re.compile(r"\.(txt|md|py|js|ts|tsx|json|yml|yaml|toml|csv|html|css|xml|sh|log)$", re.I)


def computer_slug(ctx) -> str | None:
    if COMPUTER_SLUG in ctx.connectors:
        return COMPUTER_SLUG
    for slug, conn in ctx.connectors.items():
        if getattr(conn, "provider", "") in CLOUD_PROVIDERS:
            return slug
    return None


def local_slug(ctx) -> str | None:
    for slug, conn in ctx.connectors.items():
        if getattr(conn, "provider", "") in LOCAL_PROVIDERS:
            return slug
    return None


def computer_missing() -> dict:
    """Only reached when the server has no computer backend, or the user's computer row could not be loaded."""
    from app.config import get_settings

    if not get_settings().computer_backend:
        reason = (
            "No cloud computer is configured on the server: E2B_API_KEY (or DAYTONA_API_KEY) is not set. "
            "Tell the user exactly this so the operator can add the key."
        )
    else:
        reason = (
            "The cloud computer is configured on the server, but this user's computer connector could not be loaded "
            "for this run. Tell the user this exact reason and suggest opening the Computer panel once to set it up."
        )
    return {"error": reason, "computer_unavailable": True}


async def run_computer(ctx, tool_name: str, args: dict | None = None) -> Any:
    from app.agent.deep import call_user_tool
    from app.connectors.computer_daemon import annotate_page

    slug = computer_slug(ctx)
    if not slug:
        return computer_missing()
    result = await call_user_tool(ctx, slug, tool_name, args or {})
    if not isinstance(result, dict):
        return result
    annotate_page(result)
    if tool_name in {"browser_handoff", "computer_handoff"}:
        return result
    if result.get("bot_wall"):
        return await _handoff_wall(ctx, result)
    return result


async def _handoff_wall(ctx, result: dict) -> dict:
    if any(isinstance(p, dict) and p.get("kind") == "handoff" for p in (ctx.pending or [])):
        result["note"] = "The live screen is already waiting for you. Do not invent results from this page."
        return result
    handoff = await request_takeover(
        ctx,
        str(result.get("reason") or "This site is blocking the cloud browser (CAPTCHA or verification)."),
        "Complete the CAPTCHA or verification on this screen, then tell Lumio to continue.",
    )
    if isinstance(handoff, dict):
        result.update(handoff)
    result["bot_wall"] = True
    result["note"] = "The live screen is waiting for you. Do not invent results. End your turn."
    return result


async def shell(ctx, command: str, cwd: str | None = None, timeout: int = 60) -> Any:
    if not (command or "").strip():
        return {"error": "command is required"}
    args: dict[str, Any] = {"command": command, "timeout": max(10, min(int(timeout or 60), 900))}
    if cwd:
        args["cwd"] = cwd
    return await run_computer(ctx, "exec", args)


async def user_computer_shell(ctx, command: str, cwd: str | None = None, timeout: int = 60) -> Any:
    from app.agent.confirm import sign_confirm
    from app.agent.deep import call_user_tool
    from app.agent.invoke import confirmed

    if not (command or "").strip():
        return {"error": "command is required"}
    slug = local_slug(ctx)
    if not slug:
        return {
            "error": (
                "No trusted local computer is connected. user_computer_shell runs on the user's real machine, "
                "not the cloud sandbox. Connect a local computer agent first. Use shell for the cloud computer."
            )
        }
    args: dict[str, Any] = {"command": command, "timeout": max(10, min(int(timeout or 60), 900))}
    if cwd:
        args["cwd"] = cwd
    if not ctx.auto_confirm and not confirmed(ctx.confirmations, slug, "exec"):
        token = sign_confirm("tool", {"slug": slug, "tool": "exec", "args": args})
        pending = {
            "needs_confirm": True,
            "token": token,
            "title": "Run this on your computer",
            "preview": {"kind": "shell", "command": command, "cwd": cwd or ""},
        }
        ctx.pending.append(pending)
        return {"status": "awaiting_user_confirmation", "preview": pending["preview"]}
    return await call_user_tool(ctx, slug, "exec", args)


async def list_files(ctx, path: str = "") -> Any:
    args: dict[str, Any] = {}
    if path:
        args["path"] = path
    return await run_computer(ctx, "list_dir", args)


async def read_file(ctx, path: str) -> Any:
    if not (path or "").strip():
        return {"error": "path is required"}
    return await run_computer(ctx, "read_file", {"path": path})


async def write_file(ctx, path: str, content: str) -> Any:
    if not (path or "").strip():
        return {"error": "path is required"}
    return await run_computer(ctx, "write_file", {"path": path, "content": content or ""})


async def attach_file(ctx, path: str, filename: str = "") -> Any:
    if not (path or "").strip():
        return {"error": "path is required"}
    args: dict[str, Any] = {"path": path}
    if filename:
        args["filename"] = filename
    return await run_computer(ctx, "publish_file", args)


async def open_path(ctx, path: str) -> Any:
    target = (path or "").strip()
    if not target:
        return {"error": "path is required"}
    looks_dir = target.endswith("/") or "." not in target.rsplit("/", 1)[-1]
    if looks_dir:
        listed = await run_computer(ctx, "list_dir", {"path": target})
        if not (isinstance(listed, dict) and listed.get("error")):
            return listed
    if _TEXT_FILES.search(target):
        return await run_computer(ctx, "read_file", {"path": target})
    published = await run_computer(ctx, "publish_file", {"path": target})
    return published


async def launch_app(ctx, name: str) -> Any:
    key = (name or "").strip().lower()
    if not key:
        return {"error": "app name is required"}
    if key in _BROWSER_APPS:
        return {
            "ok": True,
            "app": "browser",
            "note": "The live computer screen is already the browser. Call browser_navigate with a URL.",
        }
    if key in _EDITOR_APPS:
        cmd = (
            "if command -v code >/dev/null 2>&1; then "
            "nohup code --no-sandbox /home/daytona/work >/tmp/lumio-app.log 2>&1 & echo started=code; "
            "else echo started=missing; fi"
        )
        return await run_computer(ctx, "exec", {"command": cmd, "timeout": 30})
    if not _APP_NAME.match(key.replace(" ", "-")):
        return {"error": "Use a short app name (letters, digits, dash)."}
    binary = key.replace(" ", "-")
    cmd = (
        f"if command -v {binary} >/dev/null 2>&1; then "
        f"nohup {binary} >/tmp/lumio-app.log 2>&1 & echo started={binary}; "
        "else echo started=missing; fi"
    )
    return await run_computer(ctx, "exec", {"command": cmd, "timeout": 30})


async def request_takeover(ctx, reason: str, instruction: str = "") -> Any:
    args = {"reason": reason or "Finish this step on the live screen."}
    if instruction:
        args["instruction"] = instruction
    return await run_computer(ctx, "browser_handoff", args)


async def render_plot(
    ctx,
    title: str,
    labels: list[str] | None = None,
    values: list | None = None,
    chart_type: str = "bar",
    series_name: str = "",
) -> Any:
    labs = [str(x) for x in (labels or [])]
    nums: list[float] = []
    for item in values or []:
        try:
            nums.append(float(item))
        except (TypeError, ValueError):
            nums.append(0.0)
    if not labs or not nums:
        return {"error": "labels and values are required"}
    n = min(len(labs), len(nums))
    labs, nums = labs[:n], nums[:n]
    kind = (chart_type or "bar").lower()
    if kind not in {"line", "bar", "pie"}:
        kind = "bar"
    return await run_computer(
        ctx,
        "write_analytics_report",
        {
            "title": title or "Chart",
            "tables": [{"title": title or "Chart", "columns": ["label", "value"], "rows": [[labs[i], nums[i]] for i in range(n)]}],
            "charts": [
                {
                    "title": title or "Chart",
                    "type": kind,
                    "labels": labs,
                    "series": [{"name": series_name or title or "value", "values": nums}],
                }
            ],
        },
    )
