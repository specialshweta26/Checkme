"""Daytona cloud sandbox — the agent's own computer."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlparse

from connector_core import ToolError, ToolSpec
from app.config import get_settings
from app.security import decrypt_json, encrypt

log = logging.getLogger(__name__)
_LIVE_PNG: dict[str, bytes] = {}
_LIVE_AT: dict[str, float] = {}
_LIVE_AGENT: dict[str, str] = {}
_COMPUTER_BUSY: dict[str, str] = {}
_COMPUTER_AGENT: ContextVar[tuple[str, str]] = ContextVar("computer_agent", default=("", ""))
_SCREEN_USER: ContextVar[str] = ContextVar("computer_screen_user", default="")
_COMPUTER_USE = {
    "exec",
    "boot",
    "browse_open",
    "browser_navigate",
    "web_fetch",
    "browser_search",
    "browser_snapshot",
    "browser_act",
    "browser_tabs",
    "browser_new_tab",
    "browser_switch_tab",
    "browser_close_tab",
    "computer_observe",
    "computer_act",
    "computer_control",
    "browse_act",
}


def set_computer_agent(agent_id: str, agent_name: str = "") -> None:
    _COMPUTER_AGENT.set((agent_id or "", agent_name or ""))


def remember_screen(raw: bytes, user_id: str | None = None) -> None:
    """Keep the latest Chromium PNG so the chat rail does not wait on Daytona."""
    if isinstance(raw, (bytes, bytearray)) and len(raw) >= 32 and raw.startswith(b"\x89PNG"):
        user_key = str(user_id or _SCREEN_USER.get() or "default")
        _LIVE_PNG[user_key] = bytes(raw)
        _LIVE_AT[user_key] = time.monotonic()


def live_screen_png(user_id: str | None = None) -> bytes:
    return _LIVE_PNG.get(str(user_id or _SCREEN_USER.get() or "default"), b"")


def live_screen_agent(user_id: str | None = None) -> str:
    return _LIVE_AGENT.get(str(user_id or _SCREEN_USER.get() or "default"), "")

DAYTONA_TOOLS = [
    ToolSpec(
        name="exec",
        description="Run a shell command in this user's Daytona cloud sandbox (their computer).",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run."},
                "cwd": {"type": "string", "description": "Working directory inside the sandbox."},
                "timeout": {"type": "integer", "description": "Seconds to wait. Default 60."},
            },
            "required": ["command"],
        },
        group="computer",
        read_only=False,
        tags=("computer", "shell"),
    ),
    ToolSpec(
        name="run_code",
        description=(
            "Run a Python snippet in this user's Daytona sandbox. "
            "For charts or HTML reports, write files under /tmp then call publish_file so the user gets a download link."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python source to execute."},
                "timeout": {"type": "integer", "description": "Seconds to wait. Default 60."},
            },
            "required": ["code"],
        },
        group="computer",
        read_only=False,
        tags=("computer", "python"),
    ),
    ToolSpec(
        name="read_file",
        description="Read a UTF-8 file from the sandbox filesystem.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute or workspace-relative path."}},
            "required": ["path"],
        },
        group="computer",
        tags=("computer", "files"),
    ),
    ToolSpec(
        name="write_file",
        description="Write a UTF-8 file in the sandbox filesystem, creating parent directories.",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        group="computer",
        read_only=False,
        tags=("computer", "files"),
    ),
    ToolSpec(
        name="list_dir",
        description="List files and folders in a sandbox directory.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory path. Default workspace."}},
        },
        group="computer",
        tags=("computer", "files"),
    ),
    ToolSpec(
        name="sandbox_info",
        description="Return this user's sandbox id, state, and working directory.",
        input_schema={"type": "object", "properties": {}},
        group="computer",
        tags=("computer",),
    ),
    ToolSpec(
        name="browser_navigate",
        description=(
            "Open an http(s) URL in the live Chromium tab on this user's computer. "
            "Same visible browser the chat screen shows. Prefer the destination site over a Google Search URL "
            "when the user needs a live product page (bookings, inventory, forms). "
            "If the page is a CAPTCHA or verification wall, stop and hand off — do not invent results. "
            "If a site fails, try another source — do not stop after one error."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http or https URL to open."},
                "wait_ms": {"type": "integer", "description": "Extra wait after load. Default 1200."},
                "tab": {"type": "string", "description": "Optional tab id (t1, t2 from browser_tabs). Works on that tab without switching the screen; default is the active tab."},
            },
            "required": ["url"],
        },
        group="computer",
        read_only=False,
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="browser_snapshot",
        description=(
            "Read the live computer browser: URL, title, page text, and up to 150 visible interactive refs (e0, e1), "
            "in-view elements first. Refs stay valid until the page changes. browser_navigate and browser_act already "
            "return a fresh snapshot, so call this only when you need to re-read. If it returns fallback computer_act, "
            "observe the screen instead."
        ),
        input_schema={"type": "object", "properties": {"tab": {"type": "string", "description": "Optional tab id (t1, t2 from browser_tabs). Works on that tab without switching the screen; default is the active tab."},}},
        group="computer",
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="browser_act",
        description=(
            "Act on the live computer browser using snapshot refs (e0, e1) or a CSS/label selector. "
            "actions is a list of {kind, ref, text}. kind: click, fill, type, select, check, uncheck, hover, press, scroll, wait, reload, back. "
            "fill/type/select need text. Complete whole forms: snapshot, fill each field, click submit. "
            "If fallback is computer_act, use computer_observe then computer_act."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "actions": {
                    "type": "array",
                    "description": "Ordered steps, e.g. [{kind: click, ref: e3}].",
                },
                "tab": {"type": "string", "description": "Optional tab id (t1, t2 from browser_tabs). Works on that tab without switching the screen; default is the active tab."},
            },
            "required": ["actions"],
        },
        group="computer",
        read_only=False,
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="browser_tabs",
        description="List the computer browser's open tabs: tab id (t1, t2), URL, title, and which one is active (shown on the live screen).",
        input_schema={"type": "object", "properties": {}},
        group="computer",
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="browser_new_tab",
        description=(
            "Open a new browser tab, optionally at a URL, and return its tab id and snapshot. Use tabs to keep several "
            "pages open at once (e.g. the same search on different sites) and come back to each with its state intact. "
            "background=true opens it without switching the live screen. Links that open a new window also become tabs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Optional http(s) URL."},
                "background": {"type": "boolean", "description": "Open without making it the active tab."},
            },
        },
        group="computer",
        read_only=False,
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="browser_switch_tab",
        description="Make a tab active (shown on the live screen) and return its snapshot.",
        input_schema={"type": "object", "properties": {"tab": {"type": "string", "description": "Tab id from browser_tabs."}}, "required": ["tab"]},
        group="computer",
        read_only=False,
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="browser_close_tab",
        description="Close a tab (default: the active one). The last tab is cleared instead of closed.",
        input_schema={"type": "object", "properties": {"tab": {"type": "string", "description": "Tab id from browser_tabs."}}},
        group="computer",
        read_only=False,
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="computer_observe",
        description=(
            "Screenshot the live computer screen (the same Chromium tab). "
            "Use when page tools fail or you need coordinates for computer_act."
        ),
        input_schema={"type": "object", "properties": {}},
        group="computer",
        tags=("computer", "desktop"),
    ),
    ToolSpec(
        name="computer_act",
        description=(
            "Desktop actions on the live computer screen: click/move at x,y, type, key, scroll, wait. "
            "Fallback when browser_act cannot use refs. actions is a list of {kind, x, y, text, key, direction, amount, ms}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "actions": {"type": "array", "description": "Ordered desktop actions."},
            },
            "required": ["actions"],
        },
        group="computer",
        read_only=False,
        tags=("computer", "desktop"),
    ),
    ToolSpec(
        name="browse_open",
        description=(
            "Open an http(s) URL in this user's Daytona computer browser. "
            "Alias for browser_navigate. Returns page title, visible text, and a screenshot."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "http or https URL to open."},
                "wait_ms": {"type": "integer", "description": "Extra wait after load. Default 1500."},
            },
            "required": ["url"],
        },
        group="computer",
        read_only=False,
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="browse_act",
        description=(
            "Act on the open Daytona browser tab: click, fill, type, select, check, press, scroll, reload, or back. "
            "Prefer CSS selectors or visible labels."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "click, fill, type, select, check, uncheck, hover, press, scroll, reload, back, wait.",
                },
                "selector": {"type": "string", "description": "CSS selector or visible text."},
                "text": {"type": "string", "description": "Text to type, or key name for press (Enter, Tab)."},
                "dx": {"type": "integer", "description": "Horizontal scroll delta for scroll."},
                "dy": {"type": "integer", "description": "Vertical scroll delta for scroll. Default 800."},
                "wait_ms": {"type": "integer"},
            },
            "required": ["action"],
        },
        group="computer",
        read_only=False,
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="browser_handoff",
        description=(
            "Pause and give the user control of the live computer screen. "
            "Use for login, 2FA, captcha, payment, file pickers, or any step you cannot complete. "
            "Pass a short instruction describing what they should do. After they finish, snapshot and continue."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why the user must take over."},
                "instruction": {"type": "string", "description": "What the user should do on the screen."},
            },
            "required": ["reason"],
        },
        group="computer",
        read_only=False,
        tags=("computer", "browser", "handoff"),
    ),
    ToolSpec(
        name="browser_search",
        description=(
            "Search the web inside the computer's real browser (Bing, DuckDuckGo, then Google) and return result "
            "links read from the page. Use when web_search comes back empty or blocked."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "count": {"type": "integer", "description": "Max results (1-12).", "default": 8},
            },
            "required": ["query"],
        },
        group="computer",
        tags=("computer", "browser", "search"),
    ),
    ToolSpec(
        name="web_fetch",
        description=(
            "Fetch a public http(s) page over the network and show it on the computer screen. "
            "Use for news and when browser_navigate returns chrome-error or connection reset."
        ),
        input_schema={
            "type": "object",
            "properties": {"url": {"type": "string", "description": "http or https URL to fetch."}},
            "required": ["url"],
        },
        group="computer",
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="browse_screenshot",
        description="Screenshot the current Daytona browser tab and return a downloadable Checkme image.",
        input_schema={"type": "object", "properties": {}},
        group="computer",
        tags=("computer", "browser"),
    ),
    ToolSpec(
        name="write_analytics_report",
        description=(
            "Build a downloadable analytics HTML report with suitable charts (line/bar/pie) "
            "and tables that sort and filter. Pass title, period, summary, tables, and optional charts "
            "from fetched data. Returns download_url after the file is stored — do not write HTML yourself."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "period": {"type": "string"},
                "summary": {"type": "string"},
                "source": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "string"}},
                "actions": {"type": "array", "items": {"type": "string"}},
                "tables": {
                    "type": "array",
                    "description": "[{title, columns: [str], rows: [[value]]}]",
                },
                "charts": {
                    "type": "array",
                    "description": "[{title, type: line|bar|pie, labels: [str], series: [{name, values: [number]}]}]. "
                    "line = time series, bar = comparison, pie = mix/share. Optional if tables can be charted.",
                },
            },
            "required": ["title", "tables"],
        },
        group="computer",
        tags=("computer", "report"),
    ),
    ToolSpec(
        name="publish_file",
        description=(
            "Copy a sandbox file into a Checkme download link for the user. "
            "Use after run_code writes a chart PNG, HTML report, CSV, or PDF. "
            "Returns download_url and preview_url — never give the user a sandbox or /tmp path."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path inside the sandbox, e.g. /tmp/report.html"},
                "filename": {"type": "string", "description": "Download name shown to the user."},
            },
            "required": ["path"],
        },
        group="computer",
        tags=("computer", "files"),
    ),
]


def _config():
    from daytona import DaytonaConfig

    s = get_settings()
    if not s.daytona_api_key:
        raise ToolError("DAYTONA_API_KEY is not configured.")
    return DaytonaConfig(
        api_key=s.daytona_api_key,
        api_url=s.daytona_api_url or "https://app.daytona.io/api",
        target=s.daytona_target or "us",
        use_deprecated_polling=True,
    )


async def _file_bytes(fs, proc, path: str) -> bytes:
    if fs is not None:
        downloader = getattr(fs, "download_file", None) or getattr(fs, "download", None)
        if callable(downloader):
            data = await downloader(path)
            if isinstance(data, bytes):
                return data
            if isinstance(data, str):
                return data.encode("utf-8")
            read = getattr(data, "read", None)
            if callable(read):
                raw = read()
                return raw if isinstance(raw, bytes) else str(raw).encode("utf-8")
    response = await proc.exec(f"base64 {path}", timeout=30)
    blob = getattr(response, "result", None) or getattr(response, "stdout", "") or ""
    if isinstance(blob, bytes):
        blob = blob.decode("ascii", "ignore")
    import base64

    return base64.b64decode("".join(str(blob).split()))


async def _cache_shot(proc, fs) -> None:
    try:
        remember_screen(await asyncio.wait_for(_file_bytes(fs, proc, "/tmp/checkme-browser.png"), timeout=20))
    except Exception:  # noqa: BLE001
        pass


def _clip(text: str, limit: int = 24000) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… truncated {len(text) - limit} chars"


def safe_http_url(url: str) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ToolError("Only http(s) URLs are allowed for the computer browser.")
    lowered = raw.lower()
    if lowered.startswith(("javascript:", "data:", "file:", "vbscript:", "about:")):
        raise ToolError("Only http(s) URLs are allowed for the computer browser.")
    if raw.startswith("//"):
        raw = "https:" + raw
    elif "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        raise ToolError("Only http(s) URLs are allowed for the computer browser.")
    return raw


async def _exec(proc, command: str, timeout: int = 60):
    return await proc.exec(command, timeout=timeout)


def _out(response) -> str:
    return str(getattr(response, "result", None) or getattr(response, "stdout", "") or "")


PLAYWRIGHT_INSTALL = (
    "python3 -m pip install -q playwright && python3 -m playwright install chromium"
    " && python3 -c 'from playwright.sync_api import sync_playwright' && touch /tmp/checkme-pw-ready"
)
# E2B Desktop runs as a non-root user with passwordless sudo and lacks Chromium's system libraries.
PLAYWRIGHT_INSTALL_DESKTOP = (
    "(python3 -m pip install -q --user playwright || python3 -m pip install -q --user --break-system-packages playwright)"
    " && (python3 -m playwright install --with-deps chromium || python3 -m playwright install chromium)"
    " && python3 -c 'from playwright.sync_api import sync_playwright' && touch /tmp/checkme-pw-ready"
)


async def _ensure_playwright(proc) -> None:
    marker = await _exec(proc, "test -f /tmp/checkme-pw-ready && echo ready || echo missing", 15)
    if "ready" in _out(marker):
        return
    desktop = bool(getattr(proc, "browser_deps", False))
    install = await _exec(proc, PLAYWRIGHT_INSTALL_DESKTOP if desktop else PLAYWRIGHT_INSTALL, 480 if desktop else 240)
    if getattr(install, "exit_code", 1) not in {0, None}:
        raise ToolError("Could not install a browser in the computer sandbox. " + _clip(_out(install), 400))


async def _put_sandbox_file(proc, fs, dest: str, data: bytes) -> None:
    import base64
    import tempfile
    from pathlib import Path as FilePath

    if fs is not None and hasattr(fs, "upload_file"):
        local = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as tmp:
                tmp.write(data)
                local = tmp.name
            try:
                await asyncio.wait_for(fs.upload_file(local, dest), timeout=30)
            except TypeError:
                await asyncio.wait_for(fs.upload_file(data, dest), timeout=30)
            return
        except Exception:  # noqa: BLE001
            pass
        finally:
            if local:
                FilePath(local).unlink(missing_ok=True)
    await _exec(proc, f"rm -f {dest}", 8)
    raw = base64.b64encode(data).decode("ascii")
    for i in range(0, len(raw), 1800):
        chunk = raw[i : i + 1800]
        await _exec(
            proc,
            "python3 -c \"import base64; open("
            + json.dumps(dest)
            + ",'ab').write(base64.b64decode("
            + json.dumps(chunk)
            + "))\"",
            15,
        )


async def _install_daemon(proc, fs=None) -> None:
    from pathlib import Path as FilePath

    from app.connectors import computer_daemon

    await _put_sandbox_file(proc, fs, "/tmp/checkme_browser_daemon.py", FilePath(computer_daemon.__file__).read_bytes())


async def _daemon_up(proc) -> bool:
    ping = await _exec(proc, "curl -sf http://127.0.0.1:18765/health && echo OK || echo DOWN", 8)
    out = _out(ping)
    return daemon_current(out)


def daemon_current(health_output: str) -> bool:
    """True when the running daemon is healthy and is this release (a stale one is restarted)."""
    from app.connectors.computer_daemon import VERSION

    return "OK" in health_output and f'"version": "{VERSION}"' in health_output


_DAEMON_LOCKS: dict[str, asyncio.Lock] = {}


async def _ensure_daemon(proc, fs=None) -> None:
    # One install/start at a time per user: the E2B boot warms the browser in the background while the
    # agent's first browser tool may ask for it too.
    lock = _DAEMON_LOCKS.setdefault(_SCREEN_USER.get() or "default", asyncio.Lock())
    async with lock:
        await _ensure_daemon_locked(proc, fs)


async def _ensure_daemon_locked(proc, fs=None) -> None:
    await _ensure_playwright(proc)
    if await _daemon_up(proc):
        return
    await _exec(proc, "pkill -f checkme_browser_daemon.py >/dev/null 2>&1 || true", 8)
    await _install_daemon(proc, fs)
    present = await _exec(proc, "test -s /tmp/checkme_browser_daemon.py && echo HAS || echo MISSING", 8)
    if "HAS" not in _out(present):
        raise ToolError("Computer browser did not start. Daemon file missing in sandbox.")
    boot = await _exec(
        proc,
        "bash -lc '"
        + str(getattr(proc, "daemon_env", "") or "")
        + "nohup python3 /tmp/checkme_browser_daemon.py >/tmp/checkme-browser-daemon.log 2>&1 & disown; "
        "for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do "
        "curl -sf http://127.0.0.1:18765/health && exit 0; sleep 2; done; "
        "tail -n 40 /tmp/checkme-browser-daemon.log; exit 1'",
        80,
    )
    if getattr(boot, "exit_code", 1) not in {0, None} and not await _daemon_up(proc):
        raise ToolError("Computer browser did not start. " + _clip(_out(boot), 500))


_RPC_LOCKS: dict[str, asyncio.Lock] = {}


def _rpc_error(url: str, message: str) -> dict:
    return {
        "ok": False,
        "error": _clip(message, 500),
        "fallback": "computer_act",
        "url": url,
        "title": "",
        "text": "",
        "screenshot_name": "page.png",
    }


async def _browser_rpc(proc, payload: dict, timeout: int = 90, fs=None) -> dict:
    """One command to the browser daemon. Never hangs past ``timeout`` (plus queueing) and never raises:
    failures come back as ``ok: false`` with the reason so the agent can retry or switch approach."""
    url = str(payload.get("url") or "")
    try:
        await _ensure_daemon(proc, fs)
    except Exception as exc:  # noqa: BLE001
        return _rpc_error(url, str(exc) or type(exc).__name__)
    # Browser commands for one user's computer run one at a time (the daemon drives a single tab), and
    # each one ships its payload in its own file so parallel tool calls can never swap commands.
    lock = _RPC_LOCKS.setdefault(_SCREEN_USER.get() or "default", asyncio.Lock())
    try:
        async with lock:
            try:
                return await asyncio.wait_for(_browser_rpc_once(proc, payload, timeout, fs), timeout=timeout + 30)
            except _NotDelivered as first:
                # The command never reached the browser (upload hiccup, daemon restarting): safe to retry once,
                # even for clicks. Anything that may have run is not retried, so actions never double up.
                log.info("e2b: browser %s not delivered (%s); retrying once", payload.get("command"), first)
                await asyncio.sleep(1.0)
                await _ensure_daemon(proc, fs)
                return await asyncio.wait_for(_browser_rpc_once(proc, payload, timeout, fs), timeout=timeout + 30)
    except (asyncio.TimeoutError, TimeoutError):
        log.warning("e2b: browser %s timed out after %ss", payload.get("command"), timeout)
        return _rpc_error(
            url,
            f"The computer browser did not answer '{payload.get('command')}' within {timeout}s. "
            "Take a browser_snapshot or computer_observe to see the page, then continue or try another way.",
        )
    except Exception as exc:  # noqa: BLE001
        return _rpc_error(url, str(exc) or type(exc).__name__)


class _NotDelivered(Exception):
    """A browser command that provably did not reach the daemon."""


# curl exit codes meaning the request never got to the daemon: 7 = connection refused, 26 = cannot read the
# payload file.
_CURL_NOT_SENT = {7, 26}


async def _browser_rpc_once(proc, payload: dict, timeout: int, fs=None) -> dict:
    import secrets as _secrets

    cmd_path = f"/tmp/checkme_cmd_{_secrets.token_hex(6)}.json"
    try:
        await _put_sandbox_file(proc, fs, cmd_path, json.dumps(payload).encode())
    except Exception as exc:  # noqa: BLE001
        raise _NotDelivered(f"upload failed: {type(exc).__name__}") from exc
    response = await proc.exec(
        "curl -sS -m "
        + str(max(15, timeout - 5))
        + f" -H 'Content-Type: application/json' --data-binary @{cmd_path} http://127.0.0.1:18765/"
        + f"; rc=$?; rm -f {cmd_path}; exit $rc",
        timeout=timeout,
    )
    raw = _out(response).strip()
    code = getattr(response, "exit_code", 0)
    if code in _CURL_NOT_SENT:
        raise _NotDelivered(f"curl exit {code}")
    if code not in {0, None}:
        raise ToolError("Computer browser action failed. " + _clip(raw, 600))
    lines = raw.splitlines()
    if not lines:
        raise ToolError("Computer browser returned no page data.")
    data = json.loads(lines[-1])
    if not isinstance(data, dict):
        raise ToolError("Computer browser returned no page data.")
    data.setdefault("screenshot_name", "page.png")
    await _cache_shot(proc, fs)
    return data


def decode_screen_b64(raw: str) -> bytes:
    import base64

    blob = "".join(str(raw or "").split())
    if not blob:
        raise ToolError("No live computer screen yet. Wake the computer first.")
    try:
        data = base64.b64decode(blob)
    except Exception as exc:  # noqa: BLE001
        raise ToolError("No live computer screen yet. Wake the computer first.") from exc
    if len(data) < 32 or not data.startswith(b"\x89PNG"):
        raise ToolError("No live computer screen yet. Wake the computer first.")
    return data


async def _pull_screen(proc, fs=None) -> bytes:
    # Ask the browser daemon to capture a new frame over its dedicated screen
    # endpoint first. This avoids relying on Daytona file downloads for live video.
    try:
        response = await _exec(
            proc,
            "bash -o pipefail -c 'curl -fsS --max-time 12 http://127.0.0.1:18765/screen | base64 -w0'",
            20,
        )
        raw = decode_screen_b64(_out(response))
        remember_screen(raw)
        return raw
    except Exception as capture_exc:  # noqa: BLE001
        log.warning("Live screen endpoint capture failed: %s", _clip(str(capture_exc), 300))

    # Keep the sandbox file as a fallback if the daemon's screenshot endpoint is
    # briefly unavailable, then fall back to this user's last good frame.
    try:
        raw = await _file_bytes(fs, proc, "/tmp/checkme-browser.png")
    except Exception as file_exc:  # noqa: BLE001
        cached = live_screen_png()
        if cached:
            return cached
        raise ToolError("Could not capture the live computer screen.") from file_exc
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 32 or not raw.startswith(b"\x89PNG"):
        cached = live_screen_png()
        if cached:
            return cached
        raise ToolError("The computer returned an invalid screen image.")
    remember_screen(raw)
    return bytes(raw)


_WARMING: set = set()


def _warm_browser_later(proc, fs=None) -> None:
    async def run() -> None:
        try:
            await _boot_computer(proc, fs)
            log.info("e2b: browser ready on the desktop")
        except Exception as exc:  # noqa: BLE001 - the agent's next browser tool retries and reports
            log.warning("e2b: background browser start failed: %s: %s", type(exc).__name__, str(exc)[:300])

    task = asyncio.get_running_loop().create_task(run())
    _WARMING.add(task)
    task.add_done_callback(_WARMING.discard)


async def _boot_computer(proc, fs=None) -> dict:
    await _ensure_daemon(proc, fs)
    data = await _browser_rpc(proc, {"command": "wake"}, 40, fs)
    data["state"] = "running" if data.get("ok") is not False else "error"
    data["screenAvailable"] = True
    return data


async def _computer_status(proc) -> dict:
    ping = await _exec(proc, "curl -sf http://127.0.0.1:18765/health || echo DOWN", 8)
    raw = _out(ping).strip()
    if "DOWN" in raw or '"version"' not in raw:
        return {"state": "suspended", "screenAvailable": False, "url": "", "title": "", "screen_agent": live_screen_agent()}
    try:
        health = json.loads(raw.splitlines()[-1])
    except Exception:  # noqa: BLE001
        health = {}
    return {
        "state": "running" if health.get("ok") else "suspended",
        "screenAvailable": True,
        "url": health.get("url") or "",
        "title": health.get("title") or "",
        "version": health.get("version") or "",
        "screen_agent": live_screen_agent(),
    }


BROWSER_SEARCH_ENGINES = ("bing", "duckduckgo", "google")


def unwrap_result_url(url: str) -> str:
    """Resolve search-engine redirect links (Bing /ck/a, DuckDuckGo /l/?uddg=, Google /url?q=) to the target."""
    import base64
    from urllib.parse import parse_qs, unquote, urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    host = (parsed.hostname or "").lower()
    qs = parse_qs(parsed.query)
    if host.endswith("bing.com") and parsed.path.startswith("/ck/"):
        raw = (qs.get("u") or [""])[0]
        if raw.startswith("a1"):
            raw = raw[2:]
            try:
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return url
    if "duckduckgo.com" in host and parsed.path.startswith("/l/"):
        return unquote((qs.get("uddg") or [url])[0])
    if host.endswith("google.com") and parsed.path == "/url":
        return (qs.get("q") or qs.get("url") or [url])[0]
    return url


_STOP = frozenset("the and for with from that this what when where which near best how are you your www com".split())


def _query_terms(query: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", (query or "").lower()) if t not in _STOP}


def _clean_serp(rows: list, count: int, query: str = "") -> list[dict]:
    """Real result links only. Drops engine-internal links and rows that share no word with the query
    (search engines sometimes serve unrelated filler results to automated browsers)."""
    from urllib.parse import urlparse

    terms = _query_terms(query)
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        url = unwrap_result_url(str(row.get("url") or ""))
        host = (urlparse(url).hostname or "").lower()
        if not url.startswith(("http://", "https://")) or not host:
            continue
        if any(host.endswith(d) for d in ("bing.com", "duckduckgo.com", "google.com")) and "/maps" not in url:
            continue
        if url in seen:
            continue
        blob = f"{row.get('title') or ''} {row.get('snippet') or ''} {url}".lower()
        if terms and not any(t in blob for t in terms):
            continue
        seen.add(url)
        out.append({"title": str(row.get("title") or "")[:200], "url": url, "snippet": str(row.get("snippet") or "")[:400]})
        if len(out) >= count:
            break
    return out


async def browser_search(proc, fs, query: str, count: int = 8, engines=None) -> dict:
    """Search in the cloud browser. A CAPTCHA/verification page is reported, never solved; then the next engine."""
    query = (query or "").strip()
    if not query:
        raise ToolError("browser_search requires query")
    count = max(1, min(int(count or 8), 12))
    order = [e for e in (engines or BROWSER_SEARCH_ENGINES) if e in BROWSER_SEARCH_ENGINES] or list(BROWSER_SEARCH_ENGINES)
    tried: list[str] = []
    for engine in order:
        try:
            data = await _browser_rpc(proc, {"command": "serp", "engine": engine, "query": query, "count": count}, 35, fs)
        except Exception as exc:  # noqa: BLE001 - one engine failing must not end the search
            tried.append(f"browser:{engine}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        if not isinstance(data, dict):
            tried.append(f"browser:{engine}: no response")
            continue
        if data.get("blocked"):
            tried.append(f"browser:{engine}: blocked ({data.get('blocked')} page; not bypassed)")
            continue
        results = _clean_serp(data.get("results") or [], count, query)
        if results:
            return {"ok": True, "query": query, "results": results, "source": f"browser:{engine}", "tried": tried}
        raw = len(data.get("results") or [])
        detail = f" ({raw} unrelated links dropped)" if raw else ""
        tried.append(f"browser:{engine}: no results{detail}" + (f" (error: {data.get('error')})" if data.get("error") else ""))
    return {"ok": False, "query": query, "results": [], "tried": tried, "error": "Browser search found nothing. " + "; ".join(tried)}


async def _open_in_browser(proc, fs, url: str, wait_ms: int = 1200, tab: str = "") -> dict:
    from app.connectors.computer_daemon import annotate_page
    from app.connectors.web_fetch import browser_failed, fetch_public_page

    data = await _browser_cmd(proc, {"action": "goto", "url": url, "wait_ms": wait_ms, "tab": tab}, 180, fs)
    annotate_page(data)
    if data.get("bot_wall"):
        return data
    if not browser_failed(data, url):
        return data
    try:
        page = await fetch_public_page(url)
    except Exception as vis_exc:  # noqa: BLE001
        data["error"] = (data.get("error") or str(vis_exc))[:500]
        data["hint"] = "Try another source URL, or request_takeover if a CAPTCHA is showing. Do not invent results."
        return data
    shown = await _browser_rpc(
        proc, {"command": "load_html", "html": page.get("html") or "", "url": page.get("url") or url, "tab": tab}, 60, fs
    )
    shown["ok"] = True
    shown["url"] = page.get("url") or url
    shown["title"] = page.get("title") or shown.get("title") or ""
    shown["text"] = page.get("text") or shown.get("text") or ""
    shown["fetched_via"] = "network"
    shown.pop("error", None)
    shown.pop("fallback", None)
    return shown


async def _browser_cmd(proc, payload: dict, timeout: int = 90, fs=None) -> dict:
    action = str(payload.get("action") or "goto")
    if action == "goto":
        return await _browser_rpc(
            proc,
            {"command": "navigate", "url": payload.get("url"), "wait_ms": payload.get("wait_ms"), "tab": payload.get("tab") or ""},
            timeout,
            fs,
        )
    if action == "screenshot":
        return await _browser_rpc(proc, {"command": "observe"}, timeout, fs)
    kind = action
    step: dict[str, Any] = {"kind": kind}
    if payload.get("selector"):
        step["ref"] = str(payload.get("selector"))
        step["text"] = payload.get("text") or ""
        return await _browser_rpc(proc, {"command": "act", "actions": [step]}, timeout, fs)
    if kind in {"type", "press", "fill", "select"}:
        step["text"] = payload.get("text") or ""
    if kind == "scroll":
        step["dx"] = payload.get("dx") or 0
        step["dy"] = payload.get("dy") or 800
    return await _browser_rpc(proc, {"command": "act", "actions": [step]}, timeout, fs)



async def _client():
    from daytona import AsyncDaytona

    return AsyncDaytona(_config())


def computer_warm(row) -> bool | None:
    """Whether the user's computer is already running in this process (no cold boot needed). None if unknown."""
    if row is None:
        return None
    if _use_e2b():
        from app.connectors import e2b_box

        return e2b_box._user_key(row) in e2b_box._LIVE
    return None


def _use_e2b() -> bool:
    return get_settings().computer_backend == "e2b"


async def _ensure_sandbox(row) -> tuple[Any, Any]:
    if _use_e2b():
        from app.connectors import e2b_box

        return None, await e2b_box.ensure_sandbox(row)
    from daytona import CreateSandboxFromSnapshotParams

    client = await _client()
    creds = decrypt_json(row.credentials_enc)
    sandbox_id = creds.get("sandbox_id")
    sandbox = None
    if sandbox_id:
        try:
            sandbox = await client.get(sandbox_id)
        except Exception:  # noqa: BLE001
            log.warning("stored daytona sandbox %s missing; creating a new one", sandbox_id)
            sandbox = None
    if sandbox is None:
        try:
            sandbox = await client.create(
                CreateSandboxFromSnapshotParams(labels={"checkme_user": str(row.user_id), "app": "checkme"})
            )
        except TypeError:
            sandbox = await client.create()
        except Exception:
            sandbox = await client.create()
        creds["sandbox_id"] = getattr(sandbox, "id", None)
        row.credentials_enc = encrypt(creds)
    state = str(getattr(sandbox, "state", "") or getattr(sandbox, "status", "")).lower()
    if state and state not in {"started", "running", "ready"}:
        start = getattr(sandbox, "start", None)
        if callable(start):
            await start()
    return client, sandbox


async def _existing_sandbox(row):
    if _use_e2b():
        from app.connectors import e2b_box

        return None, await e2b_box.existing_sandbox(row)
    creds = decrypt_json(row.credentials_enc)
    sandbox_id = creds.get("sandbox_id")
    if not sandbox_id:
        return None, None
    client = await _client()
    try:
        return client, await client.get(sandbox_id)
    except Exception:  # noqa: BLE001
        return None, None


async def _e2b_screen_or_status(row, name: str) -> Any:
    """E2B: the screen is the real desktop; status never resumes a paused sandbox."""
    from app.connectors import e2b_box

    if name == "status":
        data = await e2b_box.status(row)
        data.setdefault("url", "")
        data.setdefault("title", "")
        if data.get("state") == "running":
            sandbox = await e2b_box.existing_sandbox(row)
            if sandbox is not None:
                try:
                    page = await _computer_status(sandbox.process)
                    data["url"] = page.get("url") or ""
                    data["title"] = page.get("title") or ""
                except Exception:  # noqa: BLE001
                    pass
        data["screen_agent"] = live_screen_agent()
        return data
    sandbox = await e2b_box.existing_sandbox(row)
    if sandbox is None:
        raise ToolError("No live computer screen yet. Wake the computer first.")
    try:
        raw = await sandbox.screenshot()
    except Exception as exc:  # noqa: BLE001
        cached = live_screen_png()
        if cached:
            return cached
        raise ToolError("No live computer screen yet. Wake the computer first.") from exc
    if not raw.startswith(b"\x89PNG"):
        raise ToolError("No live computer screen yet. Wake the computer first.")
    remember_screen(raw)
    return raw


# Upper bound for one computer tool call, so a stuck sandbox call can never freeze a chat turn. Browser
# tools also bound each daemon command; this is the outer backstop, sized to allow a first-run browser
# install (several minutes) on a fresh computer.
_BROWSER_CAP_S = 600.0
_NO_CAP = {"screen_png", "status", "write_analytics_report", "browser_handoff", "computer_handoff"}


def tool_time_cap(name: str, args: dict[str, Any] | None) -> float | None:
    if name in _NO_CAP or name == "boot":
        return None
    if name in {"exec", "run_code"}:
        try:
            own = int((args or {}).get("timeout") or (120 if name == "run_code" else 60))
        except (TypeError, ValueError):
            own = 60
        return float(max(30, own) + 90)
    if name in {"read_bytes", "publish_file", "read_file", "write_file", "list_dir", "sandbox_info"}:
        return 300.0
    return _BROWSER_CAP_S


async def call_daytona(row, name: str, args: dict[str, Any]) -> Any:
    agent_id, agent_name = _COMPUTER_AGENT.get()
    user_key = str(getattr(row, "user_id", "") or "")
    acquired = False
    if name in _COMPUTER_USE:
        from app.skills.service import record_computer_step

        record_computer_step(getattr(row, "user_id", None), name, args)
        if agent_id:
            _LIVE_AGENT[user_key or "default"] = agent_name or agent_id
        if user_key and name != "boot":
            # The UI's boot (no agent) must not lock the agent out of its own computer.
            holder = _COMPUTER_BUSY.get(user_key)
            if holder and holder != (agent_id or "shared"):
                raise ToolError("The shared computer is already in use. One computer-use task at a time.")
            if not holder:
                _COMPUTER_BUSY[user_key] = agent_id or "shared"
                acquired = True
    screen_user_token = _SCREEN_USER.set(user_key or "default")
    cap = tool_time_cap(name, args)
    try:
        if cap is None:
            return await _invoke_daytona(row, name, args)
        try:
            return await asyncio.wait_for(_invoke_daytona(row, name, args), timeout=cap)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            log.warning("e2b: computer tool %s timed out after %ss", name, cap)
            raise ToolError(
                f"The computer did not finish {name} within {int(cap)}s, so it was stopped. "
                "Check the screen with browser_snapshot or computer_observe, then continue or try another way."
            ) from exc
    finally:
        _SCREEN_USER.reset(screen_user_token)
        if acquired and user_key:
            _COMPUTER_BUSY.pop(user_key, None)


async def _invoke_daytona(row, name: str, args: dict[str, Any]) -> Any:
    if name == "write_analytics_report":
        from app.connectors.analytics_report import render_report

        return render_report(args)
    if name in {"screen_png", "status"} and _use_e2b():
        return await _e2b_screen_or_status(row, name)
    if name in {"screen_png", "status"}:
        _client_obj, sandbox = await _existing_sandbox(row)
        if sandbox is None:
            if name == "status":
                return {"state": "stopped", "screenAvailable": False, "url": "", "title": "", "screen_agent": live_screen_agent()}
            raise ToolError("No live computer screen yet. Wake the computer first.")
        try:
            if name == "status":
                data = await _computer_status(sandbox.process)
                data["screen_agent"] = live_screen_agent()
                return data
            return await _pull_screen(sandbox.process, getattr(sandbox, "fs", None))
        except Exception as vis_exc:  # noqa: BLE001
            if name == "status":
                return {"state": "suspended", "screenAvailable": False, "url": "", "title": "", "screen_agent": live_screen_agent()}
            log.warning("Screen capture failed for user %s: %s", getattr(row, "user_id", "unknown"), vis_exc, exc_info=True)
            raise ToolError(f"Live computer screen capture failed ({type(vis_exc).__name__}). Try reconnecting the computer.") from vis_exc
        finally:
            close = getattr(_client_obj, "close", None)
            if callable(close):
                await close()
    client, sandbox = await _ensure_sandbox(row)
    try:
        proc = sandbox.process
        fs = getattr(sandbox, "fs", None)
        if name == "boot" and getattr(sandbox, "provider", "") == "e2b":
            # The desktop and its stream are up: answer now so the UI can show the screen, and start the
            # browser (first run installs Playwright, which can take minutes) in the background.
            _warm_browser_later(proc, fs)
            data = {"ok": True, "state": "running", "screenAvailable": True, "url": "", "title": "Computer"}
            data["sandbox_id"] = getattr(sandbox, "id", None)
            data["provider"] = "e2b"
            data.update(sandbox.stream_urls())
            return data
        if name == "boot":
            data = await _boot_computer(proc, fs)
            data["sandbox_id"] = getattr(sandbox, "id", None)
            data["provider"] = getattr(sandbox, "provider", "daytona")
            urls = getattr(sandbox, "stream_urls", None)
            if callable(urls):
                data.update(urls())
            return data
        if name == "sandbox_info":
            return {
                "provider": getattr(sandbox, "provider", "daytona"),
                "sandbox_id": getattr(sandbox, "id", None),
                "state": str(getattr(sandbox, "state", None) or getattr(sandbox, "status", "")),
                "cwd": getattr(sandbox, "user_cwd", None) or "/home/daytona",
            }
        if name == "exec":
            env = args.get("env")
            env = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) and env else None
            response = await proc.exec(
                args["command"],
                cwd=args.get("cwd") or None,
                env=env,
                timeout=int(args.get("timeout") or 60),
            )
            return {
                "exit_code": getattr(response, "exit_code", None),
                "stdout": _clip(getattr(response, "result", None) or getattr(response, "stdout", "") or ""),
            }
        tab = str(args.get("tab") or "").strip()
        if name in {"browse_open", "browser_navigate"}:
            return await _open_in_browser(proc, fs, safe_http_url(args.get("url") or ""), int(args.get("wait_ms") or 1200), tab)
        if name == "browser_tabs":
            return await _browser_rpc(proc, {"command": "tabs"}, 20, fs)
        if name == "browser_new_tab":
            url = str(args.get("url") or "").strip()
            payload = {"command": "new_tab", "url": safe_http_url(url) if url else "", "background": bool(args.get("background"))}
            return await _browser_rpc(proc, payload, 180, fs)
        if name == "browser_switch_tab":
            if not tab:
                raise ToolError("browser_switch_tab requires tab (see browser_tabs)")
            return await _browser_rpc(proc, {"command": "switch_tab", "tab": tab}, 60, fs)
        if name == "browser_close_tab":
            return await _browser_rpc(proc, {"command": "close_tab", "tab": tab}, 30, fs)
        if name == "web_fetch":
            from app.connectors.web_fetch import fetch_public_page, needs_real_browser

            url = safe_http_url(args.get("url") or "")
            try:
                page = await fetch_public_page(url)
            except Exception as fetch_exc:  # noqa: BLE001 - e.g. httpx.ReadTimeout; never crash the turn
                # The server could not reach it; the cloud browser often can (different network path).
                shown = await _open_in_browser(proc, fs, url, tab=tab)
                shown.setdefault("fetched_via", "browser")
                if not shown.get("ok", True) and not shown.get("bot_wall"):
                    shown["error"] = (
                        f"Could not fetch the page ({type(fetch_exc).__name__}) and the browser could not load it. "
                        "Try another source; do not invent its content."
                    )
                return shown
            if needs_real_browser(page):
                # JS apps (Google Maps, Search, listing sites) and blocked fetches: painting the server's static
                # HTML shows an empty shell, so load the page for real and wait for it to render.
                shown = await _open_in_browser(proc, fs, url, tab=tab)
                shown.setdefault("fetched_via", "browser")
                return shown
            shown = await _browser_rpc(
                proc, {"command": "load_html", "html": page.get("html") or "", "url": page.get("url") or url}, 60, fs
            )
            shown["ok"] = page.get("ok", True)
            shown["url"] = page.get("url") or url
            shown["title"] = page.get("title") or shown.get("title") or ""
            shown["text"] = page.get("text") or shown.get("text") or ""
            shown["fetched_via"] = "network"
            return shown
        if name == "browser_search":
            return await browser_search(proc, fs, str(args.get("query") or ""), int(args.get("count") or 8), args.get("engines"))
        if name == "browser_snapshot":
            return await _browser_rpc(proc, {"command": "snapshot", "tab": tab}, 60, fs)
        if name == "browser_act":
            actions = args.get("actions")
            if isinstance(actions, str):
                actions = json.loads(actions)
            if not isinstance(actions, list) or not actions:
                raise ToolError("browser_act requires actions")
            return await _browser_rpc(proc, {"command": "act", "actions": actions, "tab": tab}, 90, fs)
        if name == "computer_observe":
            return await _browser_rpc(proc, {"command": "observe", "tab": tab}, 60, fs)
        if name in {"computer_act", "computer_control"}:
            actions = args.get("actions")
            if isinstance(actions, str):
                actions = json.loads(actions)
            if not isinstance(actions, list) or not actions:
                raise ToolError("computer_act requires actions")
            return await _browser_rpc(proc, {"command": "desktop_act", "actions": actions}, 90, fs)
        if name == "browser_status":
            return await _browser_rpc(proc, {"command": "status"}, 20, fs)
        if name in {"browser_handoff", "computer_handoff"}:
            return {
                "ok": True,
                "handoff": True,
                "reason": args.get("reason") or "",
                "instruction": args.get("instruction") or args.get("reason") or "",
            }
        if name == "browse_act":
            action = str(args.get("action") or "").strip().lower()
            if action not in {
                "click",
                "fill",
                "type",
                "select",
                "check",
                "uncheck",
                "hover",
                "press",
                "scroll",
                "reload",
                "back",
                "wait",
            }:
                raise ToolError("action must be click, fill, type, select, check, press, scroll, reload, back, or wait")
            payload = {
                "action": action,
                "selector": args.get("selector") or "",
                "text": args.get("text") or "",
                "dx": int(args.get("dx") or 0),
                "dy": int(args.get("dy") or 800),
                "wait_ms": int(args.get("wait_ms") or 1500),
            }
            return await _browser_cmd(proc, payload, 90, fs)
        if name == "browse_screenshot":
            return await _browser_cmd(proc, {"action": "screenshot"}, 60, fs)
        if name == "run_code":
            code = args["code"]
            runner = getattr(proc, "code_run", None)
            if callable(runner):
                response = await runner(code, timeout=int(args.get("timeout") or 120))
            else:
                response = await proc.exec(f"python3 - <<'PY'\n{code}\nPY", timeout=int(args.get("timeout") or 120))
            return {
                "exit_code": getattr(response, "exit_code", None),
                "stdout": _clip(getattr(response, "result", None) or getattr(response, "stdout", "") or ""),
            }
        path = args.get("path") or "."
        if name == "read_bytes":
            # Internal (not in DAYTONA_TOOLS): unclipped base64 of one sandbox file, e.g. a git push bundle.
            limit = int(args.get("max_bytes") or 64 * 1024 * 1024)
            if fs is not None and hasattr(fs, "download_file"):
                data = await fs.download_file(path)
                raw = data if isinstance(data, bytes) else str(data).encode()
            else:
                import base64 as _b64

                response = await proc.exec(f"base64 -w0 {shlex.quote(path)}", timeout=120)
                raw = _b64.b64decode(getattr(response, "result", "") or "")
            if len(raw) > limit:
                raise ToolError(f"{path} is larger than {limit} bytes")
            import base64 as _b64

            return {"path": path, "size": len(raw), "base64": _b64.b64encode(raw).decode()}
        if name == "read_file":
            if fs is not None and hasattr(fs, "download_file"):
                data = await fs.download_file(path)
                if isinstance(data, bytes):
                    return {"path": path, "content": _clip(data.decode("utf-8", "replace"))}
                return {"path": path, "content": _clip(str(data))}
            response = await proc.exec(f"cat {path!s}", timeout=30)
            return {"path": path, "content": _clip(getattr(response, "result", "") or "")}
        if name == "write_file":
            content = args.get("content") or ""
            if fs is not None and hasattr(fs, "upload_file"):
                await fs.upload_file(content.encode("utf-8"), path)
                return {"ok": True, "path": path, "bytes": len(content.encode("utf-8"))}
            response = await proc.exec(
                f"mkdir -p \"$(dirname {path})\" && cat > {path} <<'CHECKME_EOF'\n{content}\nCHECKME_EOF",
                timeout=30,
            )
            return {"ok": getattr(response, "exit_code", 1) == 0, "path": path}
        if name == "list_dir":
            if fs is not None and hasattr(fs, "list_files"):
                entries = await fs.list_files(path)
                names = []
                for item in entries or []:
                    names.append(getattr(item, "name", None) or str(item))
                return {"path": path, "entries": names}
            response = await proc.exec(f"ls -la {path}", timeout=30)
            return {"path": path, "listing": _clip(getattr(response, "result", "") or "")}
        if name == "publish_file":
            from pathlib import Path as FilePath

            from connector_core import BinaryResult
            from app.storage import guess_media_type

            if not path or path in {".", "/"}:
                raise ToolError("path is required")
            data = await _file_bytes(fs, proc, path)
            if not data:
                raise ToolError(f"sandbox file is empty: {path}")
            filename = str(args.get("filename") or FilePath(path).name or "file")
            return BinaryResult(filename=filename, content_type=guess_media_type(filename), data=data)
        raise ToolError(f"Unknown computer tool '{name}'")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            await close()
