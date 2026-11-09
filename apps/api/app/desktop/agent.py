"""Pair the desktop sidecar and RPC tools to it through Redis (works behind NAT)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache import get_redis
from app.connectors.registry import server_key_for
from app.models import Connector, User
from app.security import decrypt_json, encrypt

log = logging.getLogger(__name__)

LOCAL_SLUG = "laptop"
LOCAL_PROVIDER = "trusted_local"
ONLINE_TTL_S = 90
RESULT_TTL_S = 180
LOCAL_TOOLS = {
    "exec",
    "save_download",
    "list_dir",
    "read_file",
    "write_file",
    "publish_file",
    "run_code",
    "boot",
    "status",
    "touch",
    "screen_png",
    "browser_navigate",
    "browse_open",
    "browser_snapshot",
    "browser_act",
    "browser_collect",
    "browse_act",
    "browser_tabs",
    "browser_new_tab",
    "browser_switch_tab",
    "browser_close_tab",
    "browser_search",
    "browser_handoff",
    "browse_screenshot",
    "computer_observe",
    "computer_act",
    "computer_control",
    "open_path",
    "launch_app",
    "app_discover",
    "list_grants",
    "list_folder_grants",
    "grant_folder",
    "search_files",
    "copy_file",
    "move_file",
    "rename_file",
    "delete_file",
}


class LocalOffline(Exception):
    """The desktop sidecar is not connected or did not answer in time."""


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _online_key(user_id) -> str:
    return f"desktop:online:{user_id}"


def _jobs_key(user_id) -> str:
    return f"desktop:jobs:{user_id}"


def _result_key(job_id: str) -> str:
    return f"desktop:result:{job_id}"


async def is_online(user_id) -> bool:
    try:
        return bool(await get_redis().get(_online_key(user_id)))
    except Exception:  # noqa: BLE001
        return False


async def mark_online(user_id, token_hash: str) -> None:
    try:
        await get_redis().set(_online_key(user_id), token_hash.encode(), ex=ONLINE_TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.warning("desktop online mark skipped: %s", type(exc).__name__)


async def laptop_row(db: AsyncSession, user: User) -> Connector | None:
    return (
        await db.execute(select(Connector).where(Connector.user_id == user.id, Connector.slug == LOCAL_SLUG))
    ).scalar_one_or_none()


async def pair(db: AsyncSession, user: User, *, hostname: str = "", os_name: str = "") -> dict:
    """Create/rotate the laptop connector and return a one-time agent token for the sidecar."""
    token = secrets.token_urlsafe(32)
    digest = _token_hash(token)
    host = (hostname or "Lumio desktop")[:80]
    row = await laptop_row(db, user)
    creds = {"token_hash": digest, "prefer": "local", "hostname": host, "os": (os_name or "")[:40]}
    if row is None:
        row = Connector(
            user_id=user.id,
            kind="builtin",
            provider=LOCAL_PROVIDER,
            name="This computer",
            slug=LOCAL_SLUG,
            alias=host,
            description="The Lumio desktop app on this machine: granted folders, apps, and the Lumio browser.",
            auth_type="platform",
            credentials_enc=encrypt(creds),
            server_key=server_key_for("builtin", LOCAL_PROVIDER, None),
            tags=["computer", "local", "desktop"],
            default_for=["local"],
            enabled=True,
            health="ok",
        )
        db.add(row)
    else:
        prev = decrypt_json(row.credentials_enc)
        creds["prefer"] = prev.get("prefer") or "local"
        row.provider = LOCAL_PROVIDER
        row.kind = "builtin"
        row.enabled = True
        row.health = "ok"
        row.alias = host or row.alias
        row.credentials_enc = encrypt(creds)
        row.server_key = row.server_key or server_key_for("builtin", LOCAL_PROVIDER, None)
    from app.connectors.platform import LOCAL_COMPUTER_TOOLS
    from app.connectors.registry import upsert_tool_index

    await upsert_tool_index(db, row.server_key, row.name or "This computer", LOCAL_COMPUTER_TOOLS)
    await db.commit()
    await mark_online(user.id, digest)
    return {
        "ok": True,
        "agent_token": token,
        "slug": LOCAL_SLUG,
        "prefer": creds["prefer"],
        "jail": "grants",
    }


def stored_hash(row: Connector) -> str:
    return str(decrypt_json(row.credentials_enc).get("token_hash") or "")


def stored_prefer(row: Connector | None) -> str:
    if row is None:
        return "local"
    return str(decrypt_json(row.credentials_enc).get("prefer") or "local")


async def set_prefer(db: AsyncSession, user: User, target: str) -> dict:
    want = "cloud" if (target or "").strip().lower() == "cloud" else "local"
    row = await laptop_row(db, user)
    if row is None:
        raise LookupError("desktop app is not paired")
    creds = decrypt_json(row.credentials_enc)
    creds["prefer"] = want
    row.credentials_enc = encrypt(creds)
    await db.commit()
    return {"ok": True, "prefer": want}


async def authorize_token(db: AsyncSession, token: str) -> tuple[User, Connector]:
    if not (token or "").strip():
        raise LookupError("missing agent token")
    digest = _token_hash(token.strip())
    rows = (await db.execute(select(Connector).where(Connector.slug == LOCAL_SLUG, Connector.enabled.is_(True)))).scalars().all()
    for row in rows:
        if stored_hash(row) == digest:
            user = await db.get(User, row.user_id)
            if user is None:
                break
            return user, row
    raise LookupError("unknown agent token")


async def heartbeat(user_id, token_hash: str) -> None:
    await mark_online(user_id, token_hash)


async def enqueue(user_id, tool: str, args: dict) -> str:
    job_id = str(uuid.uuid4())
    payload = json.dumps({"id": job_id, "tool": tool, "args": args or {}, "ts": time.time()})
    r = get_redis()
    await r.rpush(_jobs_key(user_id), payload.encode())
    await r.expire(_jobs_key(user_id), 300)
    return job_id


async def take_job(user_id, wait_s: int = 20) -> dict | None:
    try:
        hit = await get_redis().blpop(_jobs_key(user_id), timeout=max(1, min(int(wait_s), 25)))
    except Exception:  # noqa: BLE001
        return None
    if not hit:
        return None
    raw = hit[1] if isinstance(hit, (list, tuple)) else hit
    try:
        job = json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
    except Exception:  # noqa: BLE001
        return None
    log.info("desktop.job picked tool=%s waited_ms=%d", job.get("tool"), int((time.time() - float(job.get("ts") or time.time())) * 1000))
    return job


async def put_result(job_id: str, result: dict) -> None:
    try:
        await get_redis().set(_result_key(job_id), json.dumps(result).encode(), ex=RESULT_TTL_S)
    except Exception as exc:  # noqa: BLE001
        log.warning("desktop result store skipped: %s", type(exc).__name__)


async def wait_result(job_id: str, timeout_s: float) -> dict:
    deadline = time.monotonic() + max(5.0, timeout_s)
    r = get_redis()
    while time.monotonic() < deadline:
        raw = await r.get(_result_key(job_id))
        if raw:
            try:
                await r.delete(_result_key(job_id))
            except Exception:  # noqa: BLE001
                pass
            return json.loads(raw.decode() if isinstance(raw, (bytes, bytearray)) else raw)
        await asyncio.sleep(0.05)
    raise LocalOffline("desktop agent did not return a result")


async def call_local(user_id, tool: str, args: dict | None = None) -> dict:
    """Send one tool to the sidecar. Raises LocalOffline if it is not connected."""
    if tool not in LOCAL_TOOLS:
        return {"ok": False, "fallback_cloud": True, "error": f"{tool} runs on the cloud computer"}
    if not await is_online(user_id):
        raise LocalOffline("desktop app is not connected")
    timeout = 90 if tool.startswith("browser") or tool in {"computer_observe", "computer_act", "boot", "screen_png"} else 60
    try:
        timeout = int((args or {}).get("timeout") or timeout)
    except (TypeError, ValueError):
        pass
    timeout = max(10, min(timeout, 300))
    sent = time.monotonic()
    job_id = await enqueue(user_id, tool, args or {})
    try:
        out = await wait_result(job_id, timeout + 15)
    except LocalOffline:
        raise
    except Exception as vis_exc:  # noqa: BLE001
        raise LocalOffline(str(vis_exc)[:200]) from vis_exc
    if not isinstance(out, dict):
        return {"ok": False, "error": "invalid desktop result"}
    log.info("desktop.job done tool=%s total_ms=%d screenshot_chars=%s", tool, int((time.monotonic() - sent) * 1000),
             len(out.get("screenshot_b64") or ""))  # fmt: skip
    out.setdefault("ran_on", "local_computer")
    if tool == "browse_screenshot":
        # Kept apart from the live panel's frame, which can be the whole desktop rather than this page.
        out = _keep_page_shot(out)
    return _ingest_screen(user_id, out)


_PAGE_SHOTS: dict[str, bytes] = {}


def _keep_page_shot(result: dict) -> dict:
    raw = result.pop("screenshot_b64", None)
    if raw:
        import base64

        try:
            png = base64.b64decode(raw)
        except Exception:  # noqa: BLE001
            png = b""
        if png:
            while len(_PAGE_SHOTS) >= 20:
                _PAGE_SHOTS.pop(next(iter(_PAGE_SHOTS)))
            key = uuid.uuid4().hex
            _PAGE_SHOTS[key] = png
            result["page_shot_key"] = key
    return result


def take_page_shot(key: str) -> bytes:
    return _PAGE_SHOTS.pop(key, b"")


def _ingest_screen(user_id, result: dict) -> dict:
    raw_b64 = result.pop("screenshot_b64", None)
    if raw_b64:
        import base64

        from app.connectors.daytona_box import remember_screen

        try:
            png = base64.b64decode(raw_b64)
        except Exception:  # noqa: BLE001
            png = b""
        if png:
            remember_screen(png, str(user_id))
            result["screenshot"] = True
    url = str(result.get("url") or "")
    if url.startswith(("http://", "https://")):
        from app.connectors.daytona_box import remember_page

        remember_page(str(user_id), url, str(result.get("title") or ""))
    return result


_SCREEN_TRY: dict[str, float] = {}
_SCREEN_ERROR: dict[str, str] = {}
_SCREEN_STALE_S = 30
DESKTOP_CAPTURE_EVERY_S = 10
SCREEN_UNAVAILABLE = "Can't show this computer's screen yet. Check that Lumio has Screen Recording permission."


async def local_browser_frame(user_id) -> bytes:
    """A picture for the live screen panel: Lumio's browser if it's open, otherwise this computer's desktop, since the
    agent also works in other apps. Reuses a recent picture for a couple of seconds while the panel is open."""
    from app.connectors.daytona_box import live_screen_age, live_screen_png

    key = str(user_id)
    png = live_screen_png(key)
    if png and live_screen_age(key) < 2:
        return png
    now = time.monotonic()
    if now - _SCREEN_TRY.get(key, 0) < 2:
        if png and live_screen_age(key) < _SCREEN_STALE_S:
            return png
        raise LocalOffline(_SCREEN_ERROR.get(key) or SCREEN_UNAVAILABLE)
    _SCREEN_TRY[key] = now
    problem = SCREEN_UNAVAILABLE
    for tool in ("browse_screenshot", "screen_png"):
        try:
            result = await asyncio.wait_for(call_local(user_id, tool, {}), timeout=8)
        except Exception as vis_exc:  # noqa: BLE001
            problem = str(vis_exc)[:200] or SCREEN_UNAVAILABLE
            continue
        if isinstance(result, dict) and result.get("screenshot"):
            _SCREEN_ERROR.pop(key, None)
            if tool == "screen_png":
                # Capturing the whole screen in a fast loop has frozen machines, so it runs far less often than the
                # browser picture. The panel keeps showing the last one in between.
                _SCREEN_TRY[key] = now + DESKTOP_CAPTURE_EVERY_S - 2
            return live_screen_png(key)
        if isinstance(result, dict) and result.get("error"):
            problem = str(result["error"])[:200]
    _SCREEN_ERROR[key] = problem
    # A picture this old could show something that's no longer true, so say what's wrong instead.
    if png and live_screen_age(key) < _SCREEN_STALE_S:
        return png
    raise LocalOffline(problem)


async def wants_local_computer(db: AsyncSession, user: User) -> bool:
    """Desktop app: use this machine even before the sidecar is marked online. Never E2B."""
    from app.desktop.client import prefer_local

    if not prefer_local():
        return False
    row = await laptop_row(db, user)
    if row is not None and (not row.enabled or stored_prefer(row) == "cloud"):
        return False
    return True


async def wait_online(user_id, timeout_s: float = 20.0) -> bool:
    deadline = time.monotonic() + max(1.0, timeout_s)
    while time.monotonic() < deadline:
        if await is_online(user_id):
            return True
        await asyncio.sleep(0.5)
    return await is_online(user_id)


async def use_local_computer(db: AsyncSession, user: User) -> bool:
    """True when this request is the desktop app and the sidecar is online (not prefer-cloud)."""
    if not await wants_local_computer(db, user):
        return False
    return await is_online(user.id)


async def status(db: AsyncSession, user: User) -> dict:
    row = await laptop_row(db, user)
    online = await is_online(user.id) if row is not None else False
    return {
        "paired": row is not None and row.enabled,
        "online": online,
        "prefer": stored_prefer(row),
        "hostname": (decrypt_json(row.credentials_enc).get("hostname") if row else "") or "",
        "client": __import__("app.desktop.client", fromlist=["client_kind"]).client_kind(),
    }
