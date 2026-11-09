"""First-class computer/browser/file verbs. They wrap this user's computer.

The website always uses the cloud computer (E2B Desktop, or Daytona). The Lumio desktop app runs files,
search, shell, the browser, and teaching on this machine. Those tools do not fall through to the cloud
machine. Browser, boot, live screen, and teaching never open E2B from the desktop app.
Toolchain installs stay on the cloud computer. See docs/COMPUTER.md."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from contextvars import ContextVar
from typing import Any

from app.agent import browser_guard, browser_tabs
from app.agent.coding import COMPUTER_SLUG
from app.desktop.agent import LOCAL_TOOLS

log = logging.getLogger(__name__)
_FORCE_CLOUD: ContextVar[bool] = ContextVar("lumio_force_cloud", default=False)

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


DESKTOP_SAVE_MAX = 5
DESKTOP_SAVE_TIMEOUT_S = 75
DOWNLOADS_SEEN_TTL_S = 30 * 86400


async def save_on_desktop(ctx, items: list[dict]) -> int:
    """A file the cloud computer downloaded also goes to the user's own Downloads folder when their desktop app is
    connected (docs/BROWSING.md "Downloads on your computer"). The app fetches the signed file link itself and only
    accepts Lumio file links. Best effort: each item gets saved_on_your_computer (the path) or desktop_save_error."""
    from app.config import get_settings

    if not getattr(get_settings(), "downloads_to_desktop", True):
        return 0
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    if user_id is None:
        return 0
    from app.desktop.agent import LocalOffline, call_local, is_online

    try:
        if not await is_online(user_id):
            return 0
    except Exception:  # noqa: BLE001 - Redis down: skip, the chat attachment is still there
        return 0
    saved = 0
    for item in [i for i in items if i.get("download_url")][:DESKTOP_SAVE_MAX]:
        args = {"url": item["download_url"], "filename": str(item.get("filename") or ""), "timeout": 60}
        try:
            out = await asyncio.wait_for(call_local(user_id, "save_download", args), DESKTOP_SAVE_TIMEOUT_S)
        except (LocalOffline, asyncio.TimeoutError) as exc:
            item["desktop_save_error"] = str(exc)[:120] or "the desktop app did not answer"
            continue
        except Exception as exc:  # noqa: BLE001
            item["desktop_save_error"] = type(exc).__name__
            continue
        if isinstance(out, dict) and out.get("ok") and out.get("path"):
            item["saved_on_your_computer"] = str(out["path"])
            saved += 1
        else:
            item["desktop_save_error"] = str((out or {}).get("error") or "not saved")[:160]
    return saved


async def _new_downloads(ctx, items: list) -> list[dict]:
    """Only files not handled before. The browser lists its recent downloads with every result, so without this each step
    would attach them again and put another copy in the user's Downloads folder."""
    from app.cache import get_redis

    seen = getattr(ctx, "_seen_downloads", None)
    if seen is None:
        seen = set()
        if ctx is not None:
            ctx._seen_downloads = seen
    user_id = getattr(getattr(ctx, "user", None), "id", None)
    fresh = []
    for item in items:
        if not isinstance(item, dict) or not item.get("path"):
            continue
        mark = str(item.get("id") or item["path"])
        if mark in seen:
            continue
        seen.add(mark)
        if user_id is not None:
            try:
                key = f"browser:downloads_seen:{user_id}"
                if not await get_redis().sadd(key, mark):
                    continue
                await get_redis().expire(key, DOWNLOADS_SEEN_TTL_S)
            except Exception:  # noqa: BLE001 - Redis down: this turn's own record above still stops repeats
                pass
        fresh.append(item)
    return fresh


async def _publish_downloads(ctx, result: dict) -> dict:
    published = []
    fresh = await _new_downloads(ctx, result.get("downloads") or [])
    if not fresh:
        result.pop("downloads", None)
        return result
    for item in fresh:
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
    if published and result.get("ran_on") != "local_computer":
        await save_on_desktop(ctx, published)
    if published:
        result["downloads"] = published
        result.setdefault(
            "hint",
            "Read each download's extracted_text and analyze it in your role and skills (get_skill if one matches). "
            "If the user asked for a deep dive, or this file is tabular/numeric and extracted_text is not enough, "
            "run_code on path with pandas and print real figures. Post that analysis in chat and cite download_url. "
            "Never cite a sandbox path. If extracted_text is empty, still attach the file and say what kind it is. "
            "When a download has saved_on_your_computer, also tell the user it is in their Downloads folder at that path.",
        )
    return result


# Indian sites (and others that refuse foreign data centres) work far better from the user's own browser: their home
# IP and sign-ins. A web chat switches its browser work to the Lumio desktop app for the rest of the turn when the app
# is online, before opening such a site or after the cloud browser is blocked.
INDIA_SITES = (
    "makemytrip.com", "goibibo.com", "cleartrip.com", "ixigo.com", "yatra.com", "easemytrip.com", "redbus.in",
    "irctc.co.in", "goindigo.in", "airindia.com", "akasaair.com", "spicejet.com", "bookmyshow.com", "swiggy.com",
    "zomato.com", "flipkart.com", "myntra.com", "ajio.com", "nykaa.com", "bigbasket.com", "blinkit.com", "zeptonow.com",
    "paytm.com", "phonepe.com", "practo.com", "1mg.com", "pharmeasy.in", "naukri.com", "magicbricks.com", "99acres.com",
)


def indian_site(url: str) -> bool:
    from urllib.parse import urlparse

    try:
        host = (urlparse(url if "://" in (url or "") else f"https://{url}").hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and (host.endswith(".in") or any(host == s or host.endswith("." + s) for s in INDIA_SITES))


def browser_local(ctx) -> bool:
    """Browser and screen tools go to the user's own computer: a desktop-app chat, or a turn switched there."""
    from app.desktop.client import prefer_local

    return prefer_local() or bool(getattr(ctx, "_browser_on_desktop", False))


async def desktop_online(ctx) -> bool:
    """The user's Lumio desktop app is connected and not set to "cloud only"."""
    try:
        from app.desktop.agent import is_online, stored_prefer

        slug = local_slug(ctx)
        conn = ctx.connectors.get(slug) if slug and hasattr(ctx.connectors, "get") else None
        if conn is None or stored_prefer(conn) == "cloud":
            return False
        user_id = getattr(getattr(ctx, "user", None), "id", None) or getattr(conn, "user_id", None)
        return bool(user_id) and await is_online(user_id)
    except Exception:  # noqa: BLE001
        return False


def switch_to_desktop(ctx, why: str) -> None:
    try:
        ctx._browser_on_desktop = True
        ctx._browser_switch_reason = why
    except AttributeError:
        log.info("browser: could not switch this turn to the desktop app")


async def _use_local(ctx, tool_name: str) -> bool:
    from app.desktop.agent import is_online, stored_prefer
    from app.desktop.client import prefer_local

    if not browser_local(ctx) or tool_name not in LOCAL_TOOLS:
        return False
    slug = local_slug(ctx)
    if not slug:
        return False
    conn = ctx.connectors.get(slug) if hasattr(ctx.connectors, "get") else None
    if conn is None:
        return False
    try:
        if stored_prefer(conn) == "cloud":
            return False
    except Exception:  # noqa: BLE001
        pass
    user_id = getattr(getattr(ctx, "user", None), "id", None) or getattr(conn, "user_id", None)
    return bool(user_id) and await is_online(user_id)


def desktop_tool_slug(ctx, slug: str, tool_name: str) -> str:
    """A desktop-app chat uses this Mac for file and computer tools, not the cloud machine."""
    from app.desktop.agent import LOCAL_SLUG
    from app.desktop.client import prefer_local

    if _FORCE_CLOUD.get() or slug != COMPUTER_SLUG or not browser_local(ctx) or tool_name not in LOCAL_TOOLS:
        return slug
    connectors = getattr(ctx, "connectors", None) or {}
    if LOCAL_SLUG in connectors:
        return LOCAL_SLUG
    return slug


def _stay_on_this_computer(tool_name: str) -> bool:
    """Browser, boot, and live-screen tools must not open E2B from the desktop app."""
    return tool_name.startswith(("browser", "browse_", "computer_")) or tool_name in {
        "boot",
        "status",
        "touch",
        "screen_png",
        "open_path",
        "launch_app",
        "app_discover",
        "computer_use",
        "grant_folder",
        "list_grants",
        "list_folder_grants",
        "search_files",
        "copy_file",
        "move_file",
        "rename_file",
        "delete_file",
        "list_dir",
        "read_file",
        "write_file",
        "publish_file",
    }


def _local_unavailable(tool_name: str, reason: str) -> dict:
    return {
        "ok": False,
        "error": reason,
        "computer_unavailable": True,
        "ran_on": "local_computer",
        "tool": tool_name,
    }


async def _try_local(ctx, tool_name: str, args: dict | None = None) -> Any | None:
    """Run on the paired laptop. None means the caller should use the cloud computer."""
    from app.agent.deep import call_user_tool
    from app.desktop.agent import LocalOffline
    from app.desktop.client import prefer_local
    from connector_core import ToolError

    if not browser_local(ctx) or tool_name not in LOCAL_TOOLS:
        return None
    slug = local_slug(ctx)
    conn = ctx.connectors.get(slug) if slug and hasattr(ctx.connectors, "get") else None
    if conn is not None:
        try:
            from app.desktop.agent import stored_prefer

            if stored_prefer(conn) == "cloud":
                return None
        except Exception:  # noqa: BLE001
            pass
    if not await _use_local(ctx, tool_name):
        if _stay_on_this_computer(tool_name):
            return _local_unavailable(tool_name, "The Lumio desktop app is not connected.")
        return None
    try:
        result = await call_user_tool(ctx, slug, tool_name, args or {})
    except (LocalOffline, ToolError) as vis_exc:
        log.info("desktop local %s missed sidecar: %s", tool_name, vis_exc)
        if _stay_on_this_computer(tool_name):
            return _local_unavailable(tool_name, str(vis_exc)[:240])
        return None
    err = str((result or {}).get("error") or "").lower() if isinstance(result, dict) else ""
    if isinstance(result, dict) and (
        result.get("fallback_cloud") or "not connected" in err or "did not return" in err or "not paired" in err
    ):
        log.info("desktop local %s offline; %s", tool_name, "stay local" if _stay_on_this_computer(tool_name) else "using cloud")
        if _stay_on_this_computer(tool_name):
            result["fallback_cloud"] = False
            result["computer_unavailable"] = True
            result.setdefault("ran_on", "local_computer")
            return result
        return None
    if isinstance(result, dict):
        result.setdefault("ran_on", "local_computer")
    return result


_NAVIGATE_TOOLS = {"browser_navigate", "browse_open"}
_ERROR_PAGE = re.compile(r"^(chrome-error|about:neterror|about:blank)", re.I)


def deep_link_fallback(tool_name: str, args: dict | None, result: Any) -> Any:
    """A deep link (a search-results URL the agent built, with a path or query) that did not open: point the agent at
    the site's home page and its own search form instead of giving up (docs/BROWSING.md "Deep links"). Generic: no
    site names. A home page that does not open is left alone."""
    if tool_name not in _NAVIGATE_TOOLS or not isinstance(result, dict):
        return result
    from urllib.parse import urlparse

    url = str((args or {}).get("url") or "")
    try:
        parts = urlparse(url)
    except ValueError:
        return result
    if parts.scheme not in {"http", "https"} or not parts.netloc or (parts.path in {"", "/"} and not parts.query):
        return result
    landed = str(result.get("url") or "")
    text = str(result.get("text") or "").strip()
    status = result.get("status")
    failed = (
        result.get("ok") is False
        or bool(result.get("bot_wall"))
        or (isinstance(status, int) and status >= 400)
        or bool(_ERROR_PAGE.match(landed))
        or (len(text) < 40 and not str(result.get("tree") or "").strip())
    )
    if not failed:
        return result
    home = f"{parts.scheme}://{parts.netloc}/"
    result["deep_link_failed"] = True
    result["fallback_url"] = home
    result["hint"] = (
        "This exact address did not open (the site may refuse direct links, or its link format changed). Don't give up "
        f"or fall back to web search yet: browser_navigate to {home}, then use the site's own search form the way a "
        "person would (fill the from / to / date fields, pick the matching suggestion, press Search; browser_task does "
        "this quickly), apply its filters (time of day, price, stops) and read the results. Only if the home page is "
        "blocked too, say plainly which site blocked it and use another source."
    )
    return result


async def run_computer(ctx, tool_name: str, args: dict | None = None, *, force_cloud: bool = False) -> Any:
    if tool_name in browser_guard.ACT_TOOLS:
        args = browser_guard.normalize(args)
    refused = browser_guard.before(ctx, tool_name, args)
    if refused is not None:
        return refused
    where = "desktop" if browser_local(ctx) else "cloud"

    async def plain(tool: str, tool_args: dict) -> Any:
        return await _run_computer(ctx, tool, tool_args, force_cloud=force_cloud)

    with_tab = await browser_tabs.tab_for(ctx, tool_name, args, where, plain)
    result = await _run_computer(ctx, tool_name, with_tab if with_tab is not None else args, force_cloud=force_cloud)
    if browser_tabs.tab_gone(result) and tool_name in browser_tabs.PAGE_TOOLS:
        # The tab was closed, or the browser restarted: use this chat's tab, or a new one, and try once more.
        await browser_tabs.forget(ctx, where)
        args = {k: v for k, v in (args or {}).items() if k != "tab"}
        with_tab = await browser_tabs.tab_for(ctx, tool_name, args, where, plain)
        result = await _run_computer(ctx, tool_name, with_tab if with_tab is not None else args, force_cloud=force_cloud)
    sent = with_tab if with_tab is not None else args
    out = browser_guard.after(ctx, tool_name, sent, result)
    if tool_name in browser_guard.ACT_TOOLS:
        out = await _after_failed_step(ctx, tool_name, sent, out, plain)
    return out


MAX_FAILURE_SHOTS = 5


async def _after_failed_step(ctx, tool_name: str, sent: dict | None, out: Any, plain) -> Any:
    """A control that moved gets one more try on the fresh page. A step that still failed leaves a screenshot as evidence."""
    if not isinstance(out, dict) or out.get("ok") is not False or out.get("repeated") or out.get("already_done") or out.get("stopped"):
        return out
    if not getattr(ctx, "_retargeting", False):
        ctx._retargeting = True
        try:
            page = await plain("browser_snapshot", {"tab": sent["tab"]} if (sent or {}).get("tab") else {})
            moved = browser_guard.stale_retry(ctx, sent, out, page.get("elements") or [] if isinstance(page, dict) else [])
            if moved is not None:
                retried = await _run_computer(ctx, tool_name, moved)
                retried = browser_guard.after(ctx, tool_name, moved, retried)
                if isinstance(retried, dict) and retried.get("ok") is not False:
                    retried.setdefault("warnings", []).append("A control had moved when the page changed, so it was found again by name and the step retried.")
                    return retried
        except Exception as exc:  # noqa: BLE001 - the original failure still gets reported
            log.info("browser retarget skipped: %s", type(exc).__name__)
        finally:
            ctx._retargeting = False
    taken = int(getattr(ctx, "_failure_shots", 0) or 0)
    if taken < MAX_FAILURE_SHOTS and not getattr(ctx, "_taking_failure_shot", False):
        ctx._taking_failure_shot = True
        ctx._failure_shots = taken + 1
        try:
            from app.agent.page_shots import capture

            shot = await capture(ctx, f"failed step {taken + 1}")
            if isinstance(shot, dict) and shot.get("ok"):
                out["failure_screenshot"] = shot.get("filename")
        except Exception as exc:  # noqa: BLE001 - a missing picture must not hide the failure
            log.info("failure screenshot skipped: %s", type(exc).__name__)
        finally:
            ctx._taking_failure_shot = False
    return out


async def _run_computer(ctx, tool_name: str, args: dict | None = None, *, force_cloud: bool = False) -> Any:
    from app.agent.deep import call_user_tool
    from app.connectors.computer_daemon import annotate_page

    switched_now = False
    if not force_cloud and not browser_local(ctx) and tool_name in LOCAL_TOOLS and tool_name in ("browser_navigate", "browse_navigate", "browser_task") \
            and indian_site(str((args or {}).get("url") or "")) and await desktop_online(ctx):
        switch_to_desktop(ctx, "Indian site: using your own browser")
        switched_now = True
    if not force_cloud:
        local = await _try_local(ctx, tool_name, args or {})
        if local is not None and switched_now and isinstance(local, dict):
            local.setdefault("note", "Opened in your Lumio desktop app's browser (Indian sites often refuse the cloud browser's foreign address).")
        if local is not None:
            return deep_link_fallback(tool_name, args, local)
        from app.desktop.client import prefer_local

        if browser_local(ctx) and tool_name in LOCAL_TOOLS:
            return _local_unavailable(tool_name, "The Lumio desktop app is not connected. This chat stays on this computer.")

    slug = computer_slug(ctx)
    if not slug:
        return computer_missing()
    token = _FORCE_CLOUD.set(True) if force_cloud else None
    try:
        result = await call_user_tool(ctx, slug, tool_name, args or {})
    finally:
        if token is not None:
            _FORCE_CLOUD.reset(token)
    if isinstance(result, dict) and result.get("ran_on") is None:
        result["ran_on"] = "cloud_computer"
    if (isinstance(result, dict) and result.get("bot_wall") and not force_cloud and tool_name in LOCAL_TOOLS
            and not browser_local(ctx) and await desktop_online(ctx)):
        # The site refused the cloud browser: do the same on the user's own browser, and stay there this turn.
        switch_to_desktop(ctx, "the site blocked the cloud browser")
        local = await _try_local(ctx, tool_name, args or {})
        if isinstance(local, dict):
            local.setdefault("note", "The site blocked the cloud browser, so this continues in your Lumio desktop app's browser.")
            return local
    if not isinstance(result, dict):
        return result
    annotate_page(result)
    deep_link_fallback(tool_name, args, result)
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
        return await run_computer(ctx, "exec", args, force_cloud=True)
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
    if not (command or "").strip():
        return {"error": "command is required"}
    args: dict[str, Any] = {"command": command, "timeout": max(10, min(int(timeout or 60), 900))}
    if cwd:
        args["cwd"] = cwd
    local = await _try_local(ctx, "exec", args)
    if local is not None:
        return local
    from app.desktop.client import prefer_local

    if prefer_local():
        return _local_unavailable("exec", "The Lumio desktop app is not connected. This chat stays on this computer.")
    return await cloud_exec(ctx, command, cwd, timeout)


async def user_computer_shell(ctx, command: str, cwd: str | None = None, timeout: int = 60) -> Any:
    from app.agent.confirm import sign_confirm
    from app.agent.deep import call_user_tool
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
    from app.agent import approval
    from app.agent.deep import may_self_approve

    # This command, once: approving `ls` never lets a different command through, nor the same one twice.
    approved = approval.find_approval(ctx.confirmations, slug, "exec", args)
    if approved is not None:
        approval.consume(ctx.confirmations, approved)
    elif not await may_self_approve(ctx, "exec", args):
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


async def search_files(ctx, query: str, path: str = "") -> Any:
    text = (query or "").strip()
    if not text:
        return {"error": "query is required"}
    args: dict[str, Any] = {"query": text}
    if (path or "").strip():
        args["path"] = path.strip()
    return await run_computer(ctx, "search_files", args)


async def list_files(ctx, path: str = "") -> Any:
    args: dict[str, Any] = {}
    if path:
        args["path"] = path
    return await run_computer(ctx, "list_dir", args)


async def read_file(ctx, path: str) -> Any:
    if not (path or "").strip():
        return {"error": "path is required"}
    return await run_computer(ctx, "read_file", {"path": path})


async def write_bytes(ctx, path: str, data: bytes) -> Any:
    """Write file bytes. Used when mail attachments must land in a folder, not a chat link."""
    import base64

    if not (path or "").strip():
        return {"error": "path is required"}
    return await run_computer(
        ctx,
        "write_file",
        {"path": path, "content_base64": base64.b64encode(data or b"").decode("ascii"), "overwrite": False},
    )


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
    local = await _try_local(ctx, "open_path", {"path": target})
    if local is not None:
        return local
    looks_dir = target.endswith("/") or "." not in target.rsplit("/", 1)[-1]
    if looks_dir:
        listed = await run_computer(ctx, "list_dir", {"path": target})
        if not (isinstance(listed, dict) and listed.get("error")):
            return listed
    if _TEXT_FILES.search(target):
        return await run_computer(ctx, "read_file", {"path": target})
    published = await run_computer(ctx, "publish_file", {"path": target})
    return published


_ASKED_FOR_CLIPBOARD = re.compile(r"clipboard|copied|paste", re.I)
DESKTOP_ONLY = "This works only in the Lumio desktop app, on the user's own computer."


async def system_tool(ctx, tool: str, args: dict | None = None) -> Any:
    """Windows, screens and clipboard on the user's computer. There is no cloud version."""
    local = await _try_local(ctx, tool, args or {})
    if local is None:
        return {"ok": False, "error": DESKTOP_ONLY}
    return local


async def clipboard_read(ctx) -> Any:
    """The clipboard can hold passwords and anything else the user copied, and a web page could ask the agent to read
    it, so it is read only when the user's own message is about it."""
    if not _ASKED_FOR_CLIPBOARD.search(str(getattr(ctx, "query", "") or "")):
        return {"ok": False, "error": "Not read: only read the clipboard when the user asks you to in their own message."}
    return await system_tool(ctx, "computer_clipboard", {"action": "read"})


# Apps that hold passwords or control the computer's security. The user uses these themselves.
_OFF_LIMITS_APPS = re.compile(r"system settings|system preferences|keychain|passwords|1password|bitwarden|lastpass|dashlane", re.I)


def _user_asked_for(label: str, query: str) -> bool:
    """A final control (Send, Delete, Pay) may be pressed only if the user's own message uses that word: text inside an
    app or a document can't change what the user wrote."""
    words = [w for w in re.findall(r"[^\W\d_]{3,}", label.casefold())]
    wrote = str(query or "").casefold()
    return any(re.search(rf"\b{re.escape(w)}", wrote) for w in words)


async def computer_ui(ctx, action: str, app: str, title: str = "", id: str = "", text: str = "", menu: str = "", depth: int = 6) -> Any:
    """Read and operate the controls inside an app, through the operating system's accessibility tools."""
    from app.agent.fast_browser import _is_final_label

    if _OFF_LIMITS_APPS.search(app or ""):
        return {"ok": False, "error": f"{app} holds passwords or security settings. Ask the user to do this themselves."}
    names = getattr(ctx, "_ui_names", None)
    if names is None:
        names = {}
        ctx._ui_names = names
    label = ""
    if action == "press":
        if (app, id) not in names:
            return {"ok": False, "error": "Read the window first with action=read, and use an id from that result."}
        label, role = names[(app, id)]
        if role == "AXCloseButton":
            return {"ok": False, "error": "Closing windows isn't offered, because it can lose unsaved work. Ask the user to close it."}
        if not label:
            return {"ok": False, "error": "This control has no name, so what it does can't be known. Look with computer_observe, and use computer_act at its position if you can see what it is."}
    elif action == "menu":
        label = str(menu or "").split(">")[-1].strip()
    if label and _is_final_label(label) and not _user_asked_for(label, getattr(ctx, "query", "")):
        return {"ok": False, "needs_approval": True,
                "error": f"Not pressed: '{label}' can't be undone. Ask the user to confirm, and press it when they say so."}  # fmt: skip
    args = {"action": action, "app": app, "title": title, "id": id, "text": text, "menu": menu, "depth": depth}
    out = await system_tool(ctx, "computer_ui", args)
    if action == "read" and isinstance(out, dict):
        for row in out.get("controls") or []:
            if isinstance(row, dict):
                names[(app, str(row.get("id")))] = (str(row.get("name") or ""), str(row.get("role") or ""))
    return out


OFFICE_WRITES = {"set_cells", "set_text", "add_slide"}
OFFICE_NAMED = OFFICE_WRITES | {"read", "export"}


async def office_app(ctx, app: str, action: str, document: str = "", cells: list | None = None, text: str = "", title: str = "",
                     notes: str = "", path: str = "", format: str = "") -> Any:  # noqa: A002
    """Work in an office app by script. A document must be one this turn created, or one the user named in their own
    message: the user's other open files are never touched."""
    made = getattr(ctx, "_office_documents", None)
    if made is None:
        made = set()
        ctx._office_documents = made
    if action in OFFICE_NAMED:
        name = str(document or "").strip()
        wrote = str(getattr(ctx, "query", "") or "").casefold()
        if not name or (name.casefold() not in made and name.casefold() not in wrote):
            return {"ok": False, "error": f"Not done: '{name}' wasn't created in this turn or named by the user. Create a new document, or ask the user which one to use."}
    args = {"app": app, "action": action, "document": document, "text": text, "title": title, "notes": notes, "path": path, "format": format}
    if cells is not None:
        args["cells"] = cells
    out = await system_tool(ctx, "office_app", args)
    if action == "new" and isinstance(out, dict) and out.get("ok") and out.get("document"):
        made.add(str(out["document"]).casefold())
    return out


async def launch_app(ctx, name: str) -> Any:
    key = (name or "").strip().lower()
    if not key:
        return {"error": "app name is required"}
    local = await _try_local(ctx, "launch_app", {"name": name.strip()})
    if local is not None:
        return local
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
