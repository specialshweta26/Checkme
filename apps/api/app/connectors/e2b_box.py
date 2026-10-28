"""E2B Desktop backend for the agent's computer.

The rest of the computer code (daytona_box.py) was written against Daytona's sandbox shape:
``sandbox.process.exec(...)`` returning ``.exit_code``/``.result`` and ``sandbox.fs.download_file`` /
``upload_file`` / ``list_files``. This module wraps an E2B Desktop sandbox in that same shape so every
tool (browser daemon, shell, files, publish) keeps working unchanged, and adds what Daytona lacked:
a real XFCE desktop with a live noVNC stream the UI can embed.

Lifecycle, per user:
- the sandbox id lives in the computer connector's encrypted credentials (``e2b_sandbox_id``) and in
  E2B metadata (``checkme_user``) as a backup lookup;
- ``ensure_sandbox`` reuses the in-process handle, else reconnects by id (which resumes a paused
  sandbox with its memory, so background processes such as the browser daemon survive), else creates;
- sandboxes are created with ``on_timeout: pause`` so idle time pauses rather than deletes, and every
  tool call extends the timeout (throttled keepalive);
- ``status`` never resumes a paused sandbox, so polling the UI does not burn sandbox minutes.

The E2B SDK is synchronous, so every call runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
import time
from dataclasses import dataclass
from typing import Any

from app.config import get_settings
from app.security import decrypt_json, encrypt
from connector_core import ToolError

log = logging.getLogger(__name__)

DISPLAY = ":0"
RESOLUTION = (1280, 900)
WORKDIR = "/home/daytona"  # tools and prompts address the computer's home as /home/daytona on both backends
STREAM_PORT = 6080
VNC_PORT = 5900
KEEPALIVE_EVERY = 60.0
STATUS_TTL = 4.0
CREDS_ID = "e2b_sandbox_id"
CREDS_STREAM = "e2b_stream_key"


@dataclass
class _Live:
    sbx: Any
    sandbox_id: str
    stream_key: str = ""
    kept_alive: float = 0.0


_LIVE: dict[str, _Live] = {}
_LOCKS: dict[str, asyncio.Lock] = {}
_STATUS: dict[str, tuple[float, dict]] = {}
# Per user: when a boot started (monotonic), and the last boot failure shown to the UI.
_BOOTING: dict[str, float] = {}
_LAST_ERROR: dict[str, str] = {}
BOOT_TIMEOUT = 90.0


def enabled() -> bool:
    return get_settings().computer_backend == "e2b"


def _sdk():
    try:
        import e2b_desktop
    except ImportError as exc:  # pragma: no cover - dependency is declared in pyproject
        raise ToolError("The e2b-desktop package is not installed on the server.") from exc
    return e2b_desktop


def _api_key() -> str:
    key = (get_settings().e2b_api_key or "").strip()
    if not key:
        raise ToolError("E2B_API_KEY is not configured.")
    return key


def _timeout() -> int:
    return max(300, int(get_settings().e2b_sandbox_timeout or 3600))


def _user_key(row) -> str:
    return str(getattr(row, "user_id", "") or "default")


# ---------------------------------------------------------------------------
# Daytona-shaped adapter
# ---------------------------------------------------------------------------


@dataclass
class ExecResult:
    exit_code: int
    result: str

    @property
    def stdout(self) -> str:
        return self.result


_NOHUP = re.compile(r"(?<![\w-])(?<!setsid )nohup\s")


def detach_background(command: str) -> str:
    """Start ``nohup`` jobs in their own session so they outlive the command that launched them."""
    return _NOHUP.sub("setsid nohup ", command or "")


def _merge(stdout: Any, stderr: Any) -> str:
    out = str(stdout or "")
    err = str(stderr or "")
    if err.strip():
        return (out + ("\n" if out and not out.endswith("\n") else "") + err) if out else err
    return out


class E2BProcess:
    """``sandbox.process`` with Daytona's ``exec`` signature."""

    # Prefix for the browser daemon launch: Chromium runs headed on the desktop so it shows in the stream.
    daemon_env = f"DISPLAY={DISPLAY} CHECKME_BROWSER_HEADED=1 "
    # Playwright on the desktop template needs its system libraries (installed via passwordless sudo).
    browser_deps = True

    def __init__(self, sbx: Any):
        self._sbx = sbx

    async def exec(self, command: str, cwd: str | None = None, env: dict | None = None, timeout: int | None = 60):
        return await asyncio.to_thread(self._run, command, cwd, env, timeout)

    def _run(self, command: str, cwd: str | None, env: dict | None, timeout: int | None) -> ExecResult:
        sdk = _sdk()
        secs = int(timeout or 60)
        envs = {"DISPLAY": DISPLAY, **{str(k): str(v) for k, v in (env or {}).items()}}
        try:
            res = self._sbx.commands.run(
                detach_background(command),
                cwd=cwd or WORKDIR,
                envs=envs,
                timeout=secs,
                request_timeout=secs + 30,
            )
        except sdk.CommandExitException as exc:
            return ExecResult(int(getattr(exc, "exit_code", 1) or 1), _merge(getattr(exc, "stdout", ""), getattr(exc, "stderr", "")))
        except sdk.TimeoutException as exc:
            return ExecResult(124, f"command timed out after {secs}s: {str(exc)[:200]}")
        return ExecResult(int(getattr(res, "exit_code", 0) or 0), _merge(getattr(res, "stdout", ""), getattr(res, "stderr", "")))


@dataclass
class _Entry:
    name: str
    is_dir: bool = False


class E2BFiles:
    """``sandbox.fs`` with Daytona's method names."""

    def __init__(self, sbx: Any):
        self._sbx = sbx

    async def download_file(self, path: str) -> bytes:
        data = await asyncio.to_thread(self._sbx.files.read, path, format="bytes")
        return bytes(data)

    async def upload_file(self, src: Any, dest: str) -> None:
        if isinstance(src, (bytes, bytearray)):
            data = bytes(src)
        else:
            from pathlib import Path

            data = Path(str(src)).read_bytes()
        await asyncio.to_thread(self._sbx.files.write, dest, data)

    async def list_files(self, path: str) -> list[_Entry]:
        items = await asyncio.to_thread(self._sbx.files.list, path)
        return [_Entry(name=getattr(i, "name", str(i)), is_dir=str(getattr(i, "type", "")).endswith("dir")) for i in items or []]


class E2BComputer:
    """Looks like a Daytona sandbox to daytona_box.py, plus desktop extras (stream URLs, screenshots)."""

    provider = "e2b"
    user_cwd = WORKDIR

    def __init__(self, live: _Live):
        self._live = live
        self.process = E2BProcess(live.sbx)
        self.fs = E2BFiles(live.sbx)

    @property
    def id(self) -> str:
        return self._live.sandbox_id

    @property
    def state(self) -> str:
        return "running"

    def stream_urls(self) -> dict:
        return stream_urls(self._live.sbx, self._live.stream_key)

    async def screenshot(self) -> bytes:
        return bytes(await asyncio.to_thread(self._live.sbx.screenshot))


def stream_urls(sbx: Any, key: str) -> dict:
    if not key:
        return {}
    base = f"https://{sbx.get_host(STREAM_PORT)}/vnc.html?autoconnect=true&resize=scale&reconnect=true"
    return {"stream_url": f"{base}&view_only=true&password={key}", "control_url": f"{base}&password={key}"}


# ---------------------------------------------------------------------------
# Sandbox lifecycle
# ---------------------------------------------------------------------------


def _new_key(length: int = 20) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _ok(sbx: Any, cmd: str, timeout: int = 20) -> bool:
    sdk = _sdk()
    try:
        res = sbx.commands.run(cmd, timeout=timeout)
        return int(getattr(res, "exit_code", 0) or 0) == 0
    except (sdk.CommandExitException, sdk.TimeoutException):
        return False


def _wait(sbx: Any, cmd: str, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _ok(sbx, cmd, 10):
            return True
        time.sleep(0.5)
    return False


def _ensure_desktop(sbx: Any) -> None:
    """X + XFCE are up (they are after create, and after a memory-restoring resume; a reboot needs a restart)."""
    sbx._display = DISPLAY
    if not _ok(sbx, f"xdpyinfo -display {DISPLAY} >/dev/null 2>&1"):
        w, h = RESOLUTION
        sbx.commands.run(
            f"Xvfb {DISPLAY} -ac -screen 0 {w}x{h}x24 -retro -dpi 96 -nolisten tcp -nolisten unix",
            background=True,
            timeout=0,
        )
        if not _wait(sbx, f"xdpyinfo -display {DISPLAY} >/dev/null 2>&1", 15):
            raise ToolError("The E2B desktop display did not start.")
    if not _ok(sbx, "pgrep -x xfce4-session >/dev/null"):
        try:
            sbx._start_xfce4()
        except Exception as exc:  # noqa: BLE001 - a bare X display still works for the browser
            log.warning("e2b xfce start failed: %s", type(exc).__name__)
    _ok(sbx, f"sudo -n mkdir -p {WORKDIR} && sudo -n chown -R $(id -u):$(id -g) {WORKDIR} || mkdir -p {WORKDIR}")


def _ensure_stream(sbx: Any, key: str) -> str:
    """x11vnc + noVNC on :6080 with a password. Reuses the running stream when the key is known."""
    vnc_up = _ok(sbx, "pgrep -x x11vnc >/dev/null")
    web_up = _ok(sbx, f"(ss -ltn 2>/dev/null || netstat -tln) | grep -q ':{STREAM_PORT} '")
    if key and vnc_up and web_up:
        return key
    key = _new_key()
    _ok(sbx, "pkill -x x11vnc; pkill -f '[n]ovnc_proxy'; true")
    sbx.commands.run(f"mkdir -p ~/.vnc && x11vnc -storepasswd {key} ~/.vnc/passwd >/dev/null 2>&1", timeout=20)
    sbx.commands.run(
        f"x11vnc -bg -display {DISPLAY} -forever -wait 50 -shared -rfbport {VNC_PORT} -usepw 2>/tmp/x11vnc_stderr.log",
        timeout=30,
    )
    sbx.commands.run(
        f"cd /opt/noVNC/utils && ./novnc_proxy --vnc localhost:{VNC_PORT} --listen {STREAM_PORT} --web /opt/noVNC > /tmp/novnc.log 2>&1",
        background=True,
        timeout=0,
    )
    if not _wait(sbx, f"(ss -ltn 2>/dev/null || netstat -tln) | grep -q ':{STREAM_PORT} '", 20):
        tail = ""
        try:
            res = sbx.commands.run("tail -n 8 /tmp/novnc.log /tmp/x11vnc_stderr.log 2>&1", timeout=10)
            tail = " ".join(str(getattr(res, "stdout", "") or "").split())[-300:]
        except Exception:  # noqa: BLE001
            pass
        log.warning("e2b: noVNC did not open port %s: %s", STREAM_PORT, _scrub(tail) or "(no log)")
        raise ToolError("E2B: the desktop stream (noVNC) did not start.")
    return key


def _create(user_key: str) -> Any:
    sdk = _sdk()
    s = get_settings()
    kwargs: dict[str, Any] = {
        "resolution": RESOLUTION,
        "timeout": _timeout(),
        "metadata": {"app": "checkme", "checkme_user": user_key},
        "envs": {"CHECKME_BROWSER_HEADED": "1"},
        "api_key": _api_key(),
    }
    if (s.e2b_template or "").strip():
        kwargs["template"] = s.e2b_template.strip()
    log.info("e2b: creating desktop sandbox user=%s template=%s", user_key, kwargs.get("template") or "desktop")
    try:
        # Pause instead of delete on idle timeout so files, logins and running processes come back.
        return sdk.Sandbox.create(lifecycle={"on_timeout": "pause", "auto_resume": False}, **kwargs)
    except TypeError:
        return sdk.Sandbox.create(**kwargs)
    except sdk.InvalidArgumentException:
        log.warning("e2b lifecycle option rejected; creating without auto-pause")
        return sdk.Sandbox.create(**kwargs)


def _connect(sandbox_id: str) -> Any | None:
    """Reconnect (resuming a paused sandbox). None when it no longer exists."""
    sdk = _sdk()
    log.info("e2b: connecting to sandbox %s", sandbox_id)
    try:
        sbx = sdk.Sandbox.connect(sandbox_id, timeout=_timeout(), api_key=_api_key())
    except sdk.NotFoundException:
        log.info("e2b: sandbox %s not found", sandbox_id)
        return None
    except sdk.SandboxException as exc:
        if "not found" in str(exc).lower() or "404" in str(exc):
            log.info("e2b: sandbox %s not found", sandbox_id)
            return None
        raise
    sbx._display = DISPLAY  # connect() skips Sandbox.create's desktop setup, which is where _display is set
    return sbx


def _find_by_metadata(user_key: str) -> str:
    sdk = _sdk()
    try:
        pager = sdk.Sandbox.list(query=sdk.SandboxQuery(metadata={"checkme_user": user_key}), api_key=_api_key())
        items = pager.next_items() if pager is not None else []
    except Exception as exc:  # noqa: BLE001 - the lookup is only a backup for a lost id
        log.info("e2b sandbox lookup by metadata failed: %s", type(exc).__name__)
        return ""
    for info in items or []:
        sid = str(getattr(info, "sandbox_id", "") or "")
        if sid:
            return sid
    return ""


def _info_state(sandbox_id: str) -> str:
    """'running', 'paused' or '' (gone). Never resumes."""
    sdk = _sdk()
    try:
        info = sdk.Sandbox.get_info(sandbox_id, api_key=_api_key())
    except sdk.NotFoundException:
        return ""
    except Exception as exc:  # noqa: BLE001
        log.warning("e2b: get_info %s failed: %s", sandbox_id, describe_error(exc))
        return "unknown"
    state = getattr(info, "state", "")
    return str(getattr(state, "value", state) or "").lower()


def _alive(live: _Live) -> bool:
    try:
        return bool(live.sbx.is_running())
    except Exception:  # noqa: BLE001
        return False


def _keepalive(live: _Live, force: bool = False) -> None:
    now = time.monotonic()
    if not force and now - live.kept_alive < KEEPALIVE_EVERY:
        return
    try:
        live.sbx.set_timeout(_timeout())
        live.kept_alive = now
    except Exception as exc:  # noqa: BLE001
        log.info("e2b keepalive failed: %s", type(exc).__name__)


def _open(user_key: str, creds: dict) -> tuple[_Live, bool]:
    """Blocking: return a ready sandbox for the user and whether creds changed."""
    cached = _LIVE.get(user_key)
    if cached is not None:
        if time.monotonic() - cached.kept_alive < KEEPALIVE_EVERY or _alive(cached):
            _keepalive(cached)
            return cached, False
        _LIVE.pop(user_key, None)
    started = time.monotonic()
    sandbox_id = str(creds.get(CREDS_ID) or "") or _find_by_metadata(user_key)
    sbx = _connect(sandbox_id) if sandbox_id else None
    if sbx is None:
        if sandbox_id:
            log.warning("e2b: stored sandbox %s is gone; creating a new one", sandbox_id)
        sbx = _create(user_key)
        creds.pop(CREDS_STREAM, None)
        log.info("e2b: sandbox %s created in %.1fs", getattr(sbx, "sandbox_id", "?"), time.monotonic() - started)
    else:
        log.info("e2b: reconnected to sandbox %s in %.1fs", sandbox_id, time.monotonic() - started)
    _ensure_desktop(sbx)
    log.info("e2b: desktop ready on %s", DISPLAY)
    key = _ensure_stream(sbx, str(creds.get(CREDS_STREAM) or ""))
    new_id = str(getattr(sbx, "sandbox_id", "") or "")
    log.info("e2b: stream ready for sandbox %s after %.1fs", new_id, time.monotonic() - started)
    changed = creds.get(CREDS_ID) != new_id or creds.get(CREDS_STREAM) != key
    creds[CREDS_ID] = new_id
    creds[CREDS_STREAM] = key
    live = _Live(sbx=sbx, sandbox_id=new_id, stream_key=key, kept_alive=time.monotonic())
    _LIVE[user_key] = live
    _STATUS.pop(user_key, None)
    return live, changed


async def _persist(row) -> None:
    """Save the new sandbox id now, so a reconnect from another request or a restart finds it."""
    try:
        from sqlalchemy import update

        from app.db import SessionLocal
        from app.models import Connector

        async with SessionLocal() as session:
            await session.execute(update(Connector).where(Connector.id == row.id).values(credentials_enc=row.credentials_enc))
            await session.commit()
    except Exception as exc:  # noqa: BLE001 - the caller's session still carries the change
        log.warning("e2b: could not persist sandbox id: %s", type(exc).__name__)


def _scrub(text: str) -> str:
    key = (get_settings().e2b_api_key or "").strip()
    text = str(text or "")
    if key and len(key) > 6:
        text = text.replace(key, "***")
    return re.sub(r"e2b_[A-Za-z0-9]{16,}", "***", text)


def describe_error(exc: BaseException) -> str:
    """A short, user-facing reason a boot failed (never contains the API key)."""
    if isinstance(exc, ToolError):
        return _scrub(str(exc))[:300]
    name = type(exc).__name__
    msg = _scrub(str(exc)).strip()
    low = msg.lower()
    if "Authentication" in name or "401" in low or "invalid api key" in low or "unauthorized" in low:
        return "E2B: invalid API key (E2B_API_KEY was rejected)."
    if "RateLimit" in name or "429" in low or "concurrent" in low:
        return "E2B: quota exceeded (rate limit or concurrent sandbox limit reached). Try again shortly."
    if any(word in low for word in ("quota", "billing", "credit", "insufficient", "payment", "blocked")):
        return f"E2B: quota exceeded ({msg[:160]})."
    if "Timeout" in name or isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "E2B: timed out starting the desktop."
    return f"E2B: could not start the computer ({name}: {msg[:200]})."


def booting(row) -> float:
    """Seconds since a boot started for this user, or 0 when none is running."""
    since = _BOOTING.get(_user_key(row))
    return (time.monotonic() - since) if since else 0.0


def last_error(row) -> str:
    return _LAST_ERROR.get(_user_key(row), "")


async def ensure_sandbox(row) -> E2BComputer:
    user_key = _user_key(row)
    lock = _LOCKS.setdefault(user_key, asyncio.Lock())
    async with lock:
        creds = decrypt_json(row.credentials_enc) if getattr(row, "credentials_enc", None) else {}
        fresh = user_key not in _LIVE
        if fresh:
            _BOOTING[user_key] = time.monotonic()
            _STATUS.pop(user_key, None)
            log.info("e2b: boot requested user=%s stored_sandbox=%s", user_key, creds.get(CREDS_ID) or "-")
        try:
            live, changed = await asyncio.to_thread(_open, user_key, creds)
        except Exception as exc:  # noqa: BLE001
            reason = describe_error(exc)
            _LAST_ERROR[user_key] = reason
            _STATUS.pop(user_key, None)
            log.warning("e2b: boot failed user=%s: %s (%s)", user_key, reason, type(exc).__name__)
            raise ToolError(reason) from exc
        finally:
            _BOOTING.pop(user_key, None)
        _LAST_ERROR.pop(user_key, None)
        if changed:
            row.credentials_enc = encrypt(creds)
            await _persist(row)
        return E2BComputer(live)


async def existing_sandbox(row) -> E2BComputer | None:
    """The user's sandbox only if it is already running in this process (never creates or resumes)."""
    live = _LIVE.get(_user_key(row))
    if live is None:
        return None
    return E2BComputer(live)


async def status(row) -> dict:
    """State for the UI. Running -> stream URLs; paused -> 'suspended'. Never resumes a sandbox."""
    user_key = _user_key(row)
    cached = _STATUS.get(user_key)
    if cached and time.monotonic() - cached[0] < STATUS_TTL:
        return dict(cached[1])
    live = _LIVE.get(user_key)
    creds = decrypt_json(row.credentials_enc) if getattr(row, "credentials_enc", None) else {}
    sandbox_id = (live.sandbox_id if live else "") or str(creds.get(CREDS_ID) or "")
    since = _BOOTING.get(user_key)
    if live is None and since:
        # A boot (from the agent's first computer tool or the Open button) is in flight.
        data = {"state": "booting", "screenAvailable": False, "booting_for": round(time.monotonic() - since, 1)}
    elif not sandbox_id:
        data = {"state": "stopped", "screenAvailable": False}
    else:
        state = await asyncio.to_thread(_info_state, sandbox_id)
        if state == "running" and live is None and creds.get(CREDS_STREAM):
            # Running but opened by another worker/process: reattach cheaply (no resume needed).
            try:
                sbx = await asyncio.to_thread(_connect, sandbox_id)
                if sbx is not None:
                    sbx._display = DISPLAY
                    live = _Live(sbx=sbx, sandbox_id=sandbox_id, stream_key=str(creds[CREDS_STREAM]), kept_alive=time.monotonic())
                    _LIVE[user_key] = live
            except Exception as exc:  # noqa: BLE001
                log.warning("e2b: reattach to running sandbox %s failed: %s", sandbox_id, type(exc).__name__)
                live = None
        if state == "running" and live is not None:
            data = {"state": "running", "screenAvailable": True, **stream_urls(live.sbx, live.stream_key)}
        elif state == "running":
            data = {"state": "running", "screenAvailable": False}
        elif state == "paused":
            _LIVE.pop(user_key, None)
            data = {"state": "suspended", "screenAvailable": False}
        elif state == "unknown":
            data = {"state": "suspended", "screenAvailable": False}
        else:
            _LIVE.pop(user_key, None)
            data = {"state": "stopped", "screenAvailable": False}
    data["provider"] = "e2b"
    data["sandbox_id"] = sandbox_id
    if data.get("state") != "running" and _LAST_ERROR.get(user_key):
        data["error"] = _LAST_ERROR[user_key]
    _STATUS[user_key] = (time.monotonic(), dict(data))
    return data


async def pause(row) -> bool:
    """Pause now (keeps memory + disk). Optional; idle sandboxes also pause on their own timeout."""
    live = _LIVE.pop(_user_key(row), None)
    if live is None:
        return False
    try:
        await asyncio.to_thread(live.sbx.pause)
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("e2b pause failed: %s", type(exc).__name__)
        return False


def forget(user_key: str | None = None) -> None:
    """Drop in-process handles (tests, or after the key changes)."""
    if user_key is None:
        _LIVE.clear()
        _STATUS.clear()
    else:
        _LIVE.pop(user_key, None)
        _STATUS.pop(user_key, None)
