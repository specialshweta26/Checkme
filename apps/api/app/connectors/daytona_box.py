"""Daytona cloud sandbox — the agent's own computer."""

from __future__ import annotations

import json
import logging
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
    "browser_snapshot",
    "browser_act",
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
            "Read the live computer browser: URL, title, page text, and up to 80 interactive refs (e0, e1). "
            "Call this before browser_act. If it returns fallback computer_act, observe the screen instead."
        ),
        input_schema={"type": "object", "properties": {}},
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
                }
            },
            "required": ["actions"],
        },
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
        remember_screen(await _file_bytes(fs, proc, "/tmp/checkme-browser.png"))
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
                await fs.upload_file(local, dest)
            except TypeError:
                await fs.upload_file(data, dest)
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


async def _ensure_daemon(proc, fs=None) -> None:
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


async def _browser_rpc(proc, payload: dict, timeout: int = 90, fs=None) -> dict:
    url = str(payload.get("url") or "")
    try:
        await _ensure_daemon(proc, fs)
        await _put_sandbox_file(proc, fs, "/tmp/checkme_browser_cmd.json", json.dumps(payload).encode())
        response = await proc.exec(
            "curl -sS -m "
            + str(max(15, timeout - 5))
            + " -H 'Content-Type: application/json' --data-binary @/tmp/checkme_browser_cmd.json http://127.0.0.1:18765/",
            timeout=timeout,
        )
        raw = _out(response).strip()
        if getattr(response, "exit_code", 0) not in {0, None}:
            raise ToolError("Computer browser action failed. " + _clip(raw, 600))
        data = json.loads(raw.splitlines()[-1])
        if not isinstance(data, dict):
            raise ToolError("Computer browser returned no page data.")
        data.setdefault("screenshot_name", "page.png")
        await _cache_shot(proc, fs)
        return data
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": _clip(str(exc), 500),
            "fallback": "computer_act",
            "url": url,
            "title": "",
            "text": "",
            "screenshot_name": "page.png",
        }


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
    # The file is rewritten by Chromium after each action. Always read it so live
    # screen polling advances instead of pinning the first cached frame forever.
    try:
        raw = await _file_bytes(fs, proc, "/tmp/checkme-browser.png")
    except Exception:
        cached = live_screen_png()
        if cached:
            return cached
        raise
    if not isinstance(raw, (bytes, bytearray)) or len(raw) < 32 or not raw.startswith(b"\x89PNG"):
        cached = live_screen_png()
        if cached:
            return cached
        raise ToolError("No live computer screen yet. Wake the computer first.")
    remember_screen(raw)
    return bytes(raw)


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


async def _open_in_browser(proc, fs, url: str, wait_ms: int = 1200) -> dict:
    from app.connectors.computer_daemon import annotate_page
    from app.connectors.web_fetch import browser_failed, fetch_public_page

    data = await _browser_cmd(proc, {"action": "goto", "url": url, "wait_ms": wait_ms}, 180, fs)
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
        proc, {"command": "load_html", "html": page.get("html") or "", "url": page.get("url") or url}, 60, fs
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
        return await _browser_rpc(proc, {"command": "navigate", "url": payload.get("url"), "wait_ms": payload.get("wait_ms")}, timeout, fs)
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


async def call_daytona(row, name: str, args: dict[str, Any]) -> Any:
    agent_id, agent_name = _COMPUTER_AGENT.get()
    user_key = str(getattr(row, "user_id", "") or "")
    acquired = False
    if name in _COMPUTER_USE:
        from app.skills.service import record_computer_step

        record_computer_step(getattr(row, "user_id", None), name, args)
        if agent_id:
            _LIVE_AGENT[user_key or "default"] = agent_name or agent_id
        if user_key:
            holder = _COMPUTER_BUSY.get(user_key)
            if holder and holder != (agent_id or "shared"):
                raise ToolError("The shared computer is already in use. One computer-use task at a time.")
            if not holder:
                _COMPUTER_BUSY[user_key] = agent_id or "shared"
                acquired = True
    screen_user_token = _SCREEN_USER.set(user_key or "default")
    try:
        return await _invoke_daytona(row, name, args)
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
            raise ToolError("No live computer screen yet. Wake the computer first.") from vis_exc
        finally:
            close = getattr(_client_obj, "close", None)
            if callable(close):
                await close()
    client, sandbox = await _ensure_sandbox(row)
    try:
        proc = sandbox.process
        fs = getattr(sandbox, "fs", None)
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
        if name in {"browse_open", "browser_navigate"}:
            return await _open_in_browser(proc, fs, safe_http_url(args.get("url") or ""), int(args.get("wait_ms") or 1200))
        if name == "web_fetch":
            from app.connectors.web_fetch import fetch_public_page

            url = safe_http_url(args.get("url") or "")
            try:
                page = await fetch_public_page(url)
            except Exception as fetch_exc:  # noqa: BLE001 - e.g. httpx.ReadTimeout; never crash the turn
                return {
                    "ok": False,
                    "url": url,
                    "error": f"Could not fetch the page ({type(fetch_exc).__name__}). Try another source; do not invent its content.",
                }
            shown = await _browser_rpc(
                proc, {"command": "load_html", "html": page.get("html") or "", "url": page.get("url") or url}, 60, fs
            )
            shown["ok"] = page.get("ok", True)
            shown["url"] = page.get("url") or url
            shown["title"] = page.get("title") or shown.get("title") or ""
            shown["text"] = page.get("text") or shown.get("text") or ""
            shown["fetched_via"] = "network"
            return shown
        if name == "browser_snapshot":
            return await _browser_rpc(proc, {"command": "snapshot"}, 60, fs)
        if name == "browser_act":
            actions = args.get("actions")
            if isinstance(actions, str):
                actions = json.loads(actions)
            if not isinstance(actions, list) or not actions:
                raise ToolError("browser_act requires actions")
            return await _browser_rpc(proc, {"command": "act", "actions": actions}, 90, fs)
        if name == "computer_observe":
            return await _browser_rpc(proc, {"command": "observe"}, 60, fs)
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
