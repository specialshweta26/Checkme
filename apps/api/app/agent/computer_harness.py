"""First-class computer/browser/file verbs. They wrap this user's cloud computer (E2B Desktop, or Daytona).

shell always runs on the cloud computer; user_computer_shell (the user's own machine) is only offered when a
local computer is connected. See docs/COMPUTER.md."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from typing import Any

from app.agent.coding import COMPUTER_SLUG

log = logging.getLogger(__name__)

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
_DELIVERABLE = re.compile(r"\.(html?|pdf|png|jpe?g|gif|webp|svg|csv|md|txt|docx|xlsx)$", re.I)
_BROWSER_START_FAIL = re.compile(r"module|did not start|ImportError|playwright|had to restart", re.I)


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


async def _publish_downloads(ctx, result: dict) -> dict:
    published = []
    for item in result.get("downloads") or []:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        if item.get("download_url"):
            published.append(item)
            continue
        att = await attach_file(ctx, str(item["path"]), str(item.get("filename") or ""))
        if isinstance(att, dict) and att.get("download_url"):
            published.append(
                {
                    **item,
                    "download_url": att["download_url"],
                    "preview_url": att.get("preview_url"),
                    "filename": att.get("filename") or item.get("filename"),
                    "extracted_text": att.get("extracted_text") or "",
                    "kind": att.get("kind") or item.get("kind"),
                }
            )
        else:
            published.append(item)
    if published:
        result["downloads"] = published
        result.setdefault(
            "hint",
            "Read each download's extracted_text and analyze it in your role and skills (get_skill if one matches). "
            "If the user asked for a deep dive, or this file is tabular/numeric and extracted_text is not enough, "
            "run_code on path with pandas and print real figures. Post that analysis in chat and cite download_url. "
            "Never cite a sandbox path. If extracted_text is empty, still attach the file and say what kind it is.",
        )
    return result


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
    if tool_name in {"browser_act", "browse_act"} and result.get("downloads"):
        result = await _publish_downloads(ctx, result)
    if tool_name in {"browser_handoff", "computer_handoff"}:
        return result
    if result.get("ok") is False and _BROWSER_START_FAIL.search(str(result.get("error") or "")):
        result["continue"] = True
        result.setdefault(
            "note",
            "The page browser restarted. The live desktop still has the user's work. "
            "Call browser_snapshot or computer_observe and continue; do not treat this as the task failing.",
        )
        return result
    if result.get("bot_wall") and result.get("handoff", True) is not False:
        return await _handoff_wall(ctx, result)  # a CAPTCHA only a person can solve
    if result.get("bot_wall"):
        host = result.get("blocked_source") or (result.get("block") or {}).get("host") or "This site"
        result["note"] = (
            f"{host} blocked automated access and no read-only copy worked. Say so plainly and continue with other "
            "sources; do not invent what the page says."
        )
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


def _exec_parts(result: Any) -> tuple[Any, str]:
    if not isinstance(result, dict):
        return None, str(result or "")
    inner = result.get("data") if isinstance(result.get("data"), dict) else result
    return inner.get("exit_code"), str(inner.get("stdout") or inner.get("output") or inner.get("result") or "")


async def cloud_exec(ctx, command: str, cwd: str | None = None, timeout: int = 60) -> Any:
    """Run a command on the cloud computer with the toolchain PATH, healing a missing known tool once.

    On exit 127 ("command not found") for a tool in ``toolchain.TOOLS`` (node/npm, pnpm, yarn, python/pip, git,
    gh, vercel, render, uv, ...), install it non-interactively in user space and re-run the command once.
    """
    from app.agent import toolchain
    from app.config import get_settings

    if not (command or "").strip():
        return {"error": "command is required"}
    s = get_settings()
    tag = secrets.token_hex(6)
    wrapped = toolchain.wrap(command, tag) if getattr(s, "computer_toolchain_env", True) else command
    args: dict[str, Any] = {"command": wrapped, "timeout": max(10, min(int(timeout or 60), 900))}
    if cwd:
        args["cwd"] = cwd
    result = await _exec_stoppable(ctx, args, tag)
    code, out = _exec_parts(result)
    if code != 127 or not getattr(s, "computer_auto_install", True):
        return result
    tool = toolchain.missing_tool(out, command)
    if not tool:
        if isinstance(result, dict):
            result["note"] = (
                "A command was not found on the cloud computer. Install it yourself (ensure_toolchain for known tools, "
                "else apt via sudo -n, pip --user, npm -g or a release download into ~/.local/bin) and run again. "
                "Do not ask the user to install anything on their own machine."
            )
        return result
    installed = await toolchain.ensure_tools(ctx, [tool])
    if not installed.get("ok"):
        if isinstance(result, dict):
            result["auto_install"] = {"tool": tool, "ok": False, "failed": installed.get("failed"), "error": installed.get("error", "")}
            result["note"] = installed.get("note") or (
                f"{tool} is missing on the cloud computer and the automatic install failed. Try another install route with "
                "shell; do not ask the user to install it on their own machine."
            )
        return result
    retry = await _exec_stoppable(ctx, args, tag)
    if isinstance(retry, dict):
        retry["auto_installed"] = [t for t in installed.get("installed", [])] or [tool]
        retry["note"] = f"{tool} was missing on the cloud computer, so it was installed and the command was run again."
    return retry


async def _exec_stoppable(ctx, args: dict, tag: str) -> Any:
    """``exec`` on the computer; if the turn is stopped mid-command, kill that command's processes there."""
    try:
        return await run_computer(ctx, "exec", args)
    except asyncio.CancelledError:
        await _kill_tagged(ctx, tag)
        raise


async def _kill_tagged(ctx, tag: str) -> None:
    from app.agent import toolchain

    slug = computer_slug(ctx)
    if not slug or not tag:
        return
    conns = getattr(ctx, "connectors", None) or {}
    conn = conns.get(slug) if hasattr(conns, "get") else None
    if conn is None:
        return
    try:
        # Shielded and short: the turn is being cancelled, but this cleanup must still reach the computer.
        await asyncio.shield(asyncio.wait_for(_raw_exec(ctx, conn, toolchain.kill_script(tag)), timeout=20))
    except BaseException as exc:  # noqa: BLE001
        log.debug("could not stop the cloud command: %s", type(exc).__name__)


async def _raw_exec(ctx, conn, command: str) -> Any:
    # Bypass call_user_tool's stop check (the run is already marked stopped) but keep the same connector path.
    args = {"command": command, "timeout": 15}
    impl = getattr(ctx, "call_impl", None)
    if impl is not None:
        return await impl(conn.slug if hasattr(conn, "slug") else COMPUTER_SLUG, "exec", args)
    from app.connectors.runtime import RuntimeConnector

    return await RuntimeConnector(conn).call("exec", args)


async def shell(ctx, command: str, cwd: str | None = None, timeout: int = 60) -> Any:
    return await cloud_exec(ctx, command, cwd, timeout)


async def user_computer_shell(ctx, command: str, cwd: str | None = None, timeout: int = 60) -> Any:
    from app.agent.confirm import sign_confirm
    from app.agent.deep import call_user_tool
    from app.agent.invoke import confirmed
    from app.config import get_settings

    if not (command or "").strip():
        return {"error": "command is required"}
    slug = local_slug(ctx)
    if not slug:
        if getattr(get_settings(), "local_shell_fallback_to_cloud", True) and computer_slug(ctx):
            result = await cloud_exec(ctx, command, cwd, timeout)
            if isinstance(result, dict):
                result["ran_on"] = "cloud_computer"
                result["note"] = (
                    "No local computer is connected, so this ran on the user's cloud computer instead (use shell for "
                    "cloud commands). Only if the user explicitly wanted their own machine, say it ran in the cloud "
                    "and that their own machine needs the Checkme local agent connected."
                )
            return result
        return {
            "error": (
                "No trusted local computer is connected. user_computer_shell runs on the user's real machine, "
                "not the cloud computer. Use shell instead: it runs on the cloud computer and installs missing tools."
            ),
            "use_instead": "shell",
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


async def ensure_toolchain(ctx, tools: list[str] | None = None, force: bool = False) -> Any:
    from app.agent import toolchain

    if not computer_slug(ctx):
        return computer_missing()
    return await toolchain.ensure_tools(ctx, list(tools or ["dev"]), force=force)


async def run_code(ctx, code: str, timeout: int = 120) -> Any:
    if not (code or "").strip():
        return {"error": "code is required"}
    return await run_computer(ctx, "run_code", {"code": code, "timeout": max(10, min(int(timeout or 120), 300))})


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
    result = await run_computer(ctx, "write_file", {"path": path, "content": content or ""})
    if isinstance(result, dict) and result.get("ok") and _DELIVERABLE.search(path):
        published = await attach_file(ctx, path)
        if isinstance(published, dict) and published.get("download_url"):
            result["download_url"] = published["download_url"]
            result["preview_url"] = published.get("preview_url")
            result["filename"] = published.get("filename") or path.rsplit("/", 1)[-1]
            result["hint"] = "File is saved to durable storage. Give the user download_url; it stays valid. Never cite a sandbox path."
    return result


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
            'mkdir -p "$HOME/work"; nohup code --no-sandbox "$HOME/work" >/tmp/lumio-app.log 2>&1 & echo started=code; '
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
