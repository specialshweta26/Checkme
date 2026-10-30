"""What the user does on the computer while teaching a skill.

The live desktop is an E2B noVNC stream: the user's clicks and keys go straight from the browser to the
sandbox's X server, so the API never sees them. Recording therefore happens in two places:

- inside the sandbox, a small capture script listens to X input (``xinput test-xi2 --root``) and writes
  clicks (with pointer position and the focused window title), key presses and scrolls to a JSON-lines
  file, plus focused-window changes. It is generic: no site- or task-specific logic;
- on the API, a loop keeps the sandbox alive and grabs a screenshot every few seconds, keeping only frames
  that changed (key frames for the skill writer). Input sent through the rail's typing bar and agent tool
  calls are recorded by the routes themselves (``service.record_computer_step``).

Every event carries ``t``: seconds since the teaching session started, so actions line up with spoken
narration (``narration.py``).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

SCRIPT_PATH = "/tmp/checkme_teach.py"
# pkill -f matches full command lines, including the shell that runs pkill itself ("bash -c 'pkill -f
# /tmp/checkme_teach.py; ...'"), which then kills its own shell (exit 143). A bracket pattern still matches
# the real process but never its own command line.
KILL_CAPTURE = "pkill -f '/tmp/[c]heckme_teach.py'; pkill -f '[x]input test-xi2'; true"
CAPTURE_ATTEMPTS = 3
EVENTS_PATH = "/tmp/checkme_teach.jsonl"
SNAPSHOT_EVERY = 5.0
MAX_FRAMES = 30
FRAME_WIDTH = 640
MAX_EVENTS = 1500

# Runs inside the sandbox with python3. Writes one JSON object per line to EVENTS_PATH.
CAPTURE_SCRIPT = r'''
import json, os, re, shutil, subprocess, sys, threading, time
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/checkme_teach.jsonl"
env = dict(os.environ, DISPLAY=os.environ.get("DISPLAY") or ":0")
t0 = time.time()
lock = threading.Lock()
def emit(**row):
    row["t"] = round(time.time() - t0, 2)
    with lock, open(OUT, "a") as f:
        f.write(json.dumps(row) + "\n")
def run(*args):
    try:
        return subprocess.run(args, env=env, capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return ""
def window():
    return run("xdotool", "getactivewindow", "getwindowname").strip()[:200]
def pointer():
    vals = {}
    for line in run("xdotool", "getmouselocation", "--shell").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip()
    try:
        return int(vals.get("X", -1)), int(vals.get("Y", -1))
    except ValueError:
        return -1, -1
def watch_windows():
    last = None
    while True:
        title = window()
        if title and title != last:
            emit(kind="window", window=title)
            last = title
        time.sleep(1.0)
keymap = {}
for line in run("xmodmap", "-pke").splitlines():
    m = re.match(r"\s*keycode\s+(\d+)\s*=\s*(.*)$", line)
    if m and m.group(2).strip():
        keymap[int(m.group(1))] = m.group(2).split()
CHARS = {"space": " ", "period": ".", "comma": ",", "minus": "-", "underscore": "_", "slash": "/",
         "backslash": "\\", "at": "@", "colon": ":", "semicolon": ";", "apostrophe": "'", "quotedbl": '"',
         "question": "?", "exclam": "!", "equal": "=", "plus": "+", "numbersign": "#", "dollar": "$",
         "percent": "%", "ampersand": "&", "asterisk": "*", "parenleft": "(", "parenright": ")",
         "bracketleft": "[", "bracketright": "]", "braceleft": "{", "braceright": "}", "less": "<",
         "greater": ">", "bar": "|", "grave": "`", "asciitilde": "~", "asciicircum": "^"}
MODS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R", "Meta_L", "Meta_R", "ISO_Level3_Shift"}
held = set()
emit(kind="capture_started", window=window())
threading.Thread(target=watch_windows, daemon=True).start()
if not shutil.which("xinput"):
    # Best effort: the desktop image usually has passwordless sudo; never blocks the window watcher.
    for cmd in (["sudo", "-n", "apt-get", "install", "-y", "-qq", "xinput"],
                ["sh", "-c", "sudo -n apt-get update -qq && sudo -n apt-get install -y -qq xinput"]):
        try:
            subprocess.run(cmd, env=env, capture_output=True, timeout=120)
        except Exception:
            pass
        if shutil.which("xinput"):
            break
if not shutil.which("xinput"):
    emit(kind="capture_unavailable", reason="xinput is not installed")
    while True:
        time.sleep(60)
proc = subprocess.Popen(["xinput", "test-xi2", "--root"], env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
current = ""
for line in proc.stdout:
    head = re.match(r"EVENT type \d+ \((\w+)\)", line)
    if head:
        current = head.group(1)
        continue
    m = re.match(r"\s*detail:\s*(\d+)", line)
    if not m or current not in ("RawButtonPress", "RawKeyPress", "RawKeyRelease"):
        continue
    detail, kind, current = int(m.group(1)), current, ""
    if kind == "RawButtonPress":
        if detail in (1, 2, 3):
            x, y = pointer()
            emit(kind="click", button=detail, x=x, y=y, window=window())
        elif detail in (4, 5, 6, 7):
            emit(kind="scroll", direction={4: "up", 5: "down", 6: "left", 7: "right"}[detail])
        continue
    names = keymap.get(detail) or []
    name = names[0] if names else str(detail)
    if kind == "RawKeyRelease":
        held.discard(name)
        continue
    if name in MODS:
        held.add(name)
        continue
    shift = any(h.startswith("Shift") for h in held)
    ctrl = any(h.startswith(("Control", "Super", "Meta")) for h in held)
    sym = names[1] if shift and len(names) > 1 else name
    char = sym if len(sym) == 1 else CHARS.get(sym, "")
    if char and not ctrl:
        emit(kind="char", char=char)
    else:
        combo = "+".join(sorted({"ctrl" if h.startswith(("Control", "Super", "Meta")) else "alt" if h.startswith(("Alt", "ISO")) else "shift" for h in held}) + [name])
        emit(kind="key", key=combo)
'''


@dataclass
class Recorder:
    user_key: str
    started: float  # monotonic, same clock as the teaching session
    task: asyncio.Task | None = None
    sandbox: Any = None
    capture_offset: float | None = None  # session seconds when the in-sandbox script started
    frames: list[dict] = field(default_factory=list)
    last_hash: int | None = None
    notes: list[str] = field(default_factory=list)
    capture_attempts: int = 0
    events_text: str = ""  # latest copy of the in-sandbox capture file (read without stopping it)
    dom_on: bool = False  # the page browser's DOM recorder is running (BROWSER_TEACH_DOM)
    dom_attempts: int = 0
    dom_events: list[dict] = field(default_factory=list)  # selector-level steps for deterministic replay
    row: Any = None


_RECORDERS: dict[str, Recorder] = {}


def elapsed(started: float) -> float:
    return round(max(0.0, time.monotonic() - started), 2)


async def _computer_row(user_key: str):
    import uuid

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Connector

    async with SessionLocal() as session:
        return (
            await session.execute(
                select(Connector).where(Connector.user_id == uuid.UUID(user_key), Connector.slug == "computer")
            )
        ).scalar_one_or_none()


async def _live_sandbox(rec: Recorder):
    """The user's running E2B desktop, without booting or resuming one."""
    from app.config import get_settings

    if get_settings().computer_backend != "e2b":
        return None
    from app.connectors import e2b_box

    try:
        row = await _computer_row(rec.user_key)
        if row is None:
            return None
        box = await e2b_box.existing_sandbox(row)
        if box is None:
            data = await asyncio.wait_for(e2b_box.status(row), timeout=15)
            if data.get("state") == "running":
                box = await e2b_box.existing_sandbox(row)
        return box
    except Exception as exc:  # noqa: BLE001 - recording is best-effort
        log.info("teach: no live sandbox for user=%s: %s", rec.user_key, type(exc).__name__)
        return None


async def _start_capture(rec: Recorder, box) -> None:
    from app.connectors.e2b_box import DISPLAY

    sbx = box._live.sbx

    def launch() -> None:
        sbx.files.write(SCRIPT_PATH, CAPTURE_SCRIPT.encode())
        sbx.commands.run(f"{KILL_CAPTURE}; rm -f {EVENTS_PATH}; true", timeout=10)
        sbx.commands.run(
            f"python3 {SCRIPT_PATH} {EVENTS_PATH} > /tmp/checkme_teach.err 2>&1",
            background=True,
            envs={"DISPLAY": DISPLAY},
            timeout=0,
        )

    await asyncio.wait_for(asyncio.to_thread(launch), timeout=30)
    rec.capture_offset = elapsed(rec.started)
    log.info("teach: input capture started user=%s at %.1fs", rec.user_key, rec.capture_offset)


def frame_hash(png: bytes) -> tuple[int, bytes]:
    """128-bit perceptual hash (difference hash + average hash) and a small JPEG of a screenshot."""
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    gray = img.convert("L")
    px = list(gray.resize((9, 8)).tobytes())
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    avg = list(gray.resize((8, 8)).tobytes())
    mean = sum(avg) / len(avg)
    for value in avg:
        bits = (bits << 1) | (1 if value > mean else 0)
    if img.width > FRAME_WIDTH:
        img = img.resize((FRAME_WIDTH, max(1, round(img.height * FRAME_WIDTH / img.width))))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=60)
    return bits, out.getvalue()


def changed(a: int | None, b: int, threshold: int = 6) -> bool:
    return a is None or bin(a ^ b).count("1") >= threshold


async def _snapshot(rec: Recorder, box) -> None:
    png = await asyncio.wait_for(box.screenshot(), timeout=12)
    bits, jpeg = await asyncio.to_thread(frame_hash, png)
    if not changed(rec.last_hash, bits):
        return
    rec.last_hash = bits
    frame = {"t": elapsed(rec.started), "jpeg": jpeg}
    if len(rec.frames) >= MAX_FRAMES:
        # Keep the first frame and an even spread: drop every other later frame.
        rec.frames = rec.frames[:1] + rec.frames[2::2]
    rec.frames.append(frame)


async def _loop(rec: Recorder, limit: float) -> None:
    from app.connectors import e2b_box

    while elapsed(rec.started) < limit:
        if rec.sandbox is None:
            rec.sandbox = await _live_sandbox(rec)
        if rec.sandbox is not None and rec.capture_offset is None and rec.capture_attempts < CAPTURE_ATTEMPTS:
            rec.capture_attempts += 1
            try:
                await _start_capture(rec, rec.sandbox)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "teach: capture start failed user=%s attempt=%d: %s: %s",
                    rec.user_key, rec.capture_attempts, type(exc).__name__, str(exc)[:200],
                )  # fmt: skip
                if rec.capture_attempts >= CAPTURE_ATTEMPTS:
                    rec.notes.append("input capture could not start in the sandbox")
        if rec.sandbox is not None:
            try:
                e2b_box._keepalive(rec.sandbox._live)  # the user is driving noVNC directly, not via our API
                await _snapshot(rec, rec.sandbox)
            except Exception as exc:  # noqa: BLE001
                log.info("teach: snapshot failed user=%s: %s", rec.user_key, type(exc).__name__)
                rec.sandbox = None
        if rec.sandbox is not None:
            await _dom_tick(rec)
        if rec.sandbox is not None and rec.capture_offset is not None:
            try:
                rec.events_text = await asyncio.wait_for(asyncio.to_thread(_peek_events, rec.sandbox._live.sbx), timeout=10)
            except Exception as exc:  # noqa: BLE001 - the next tick or stop() reads it again
                log.info("teach: reading capture failed user=%s: %s", rec.user_key, type(exc).__name__)
        await asyncio.sleep(SNAPSHOT_EVERY)


DOM_ATTEMPTS = 6


def teach_dom_enabled() -> bool:
    from app.config import get_settings

    return bool(getattr(get_settings(), "browser_teach_dom", True))


async def _dom(rec: Recorder, op: str) -> dict:
    """Teach mode's DOM recorder in the page browser (start / drain / stop). Best effort; never boots anything."""
    from app.connectors.daytona_box import call_daytona

    if rec.row is None:
        rec.row = await _computer_row(rec.user_key)
    if rec.row is None:
        return {"ok": False}
    out = await asyncio.wait_for(call_daytona(rec.row, "browser_rec", {"op": op, "only_if_running": True}), timeout=40)
    return out if isinstance(out, dict) else {"ok": False}


async def _dom_tick(rec: Recorder) -> None:
    if not teach_dom_enabled():
        return
    try:
        if not rec.dom_on:
            if rec.dom_attempts >= DOM_ATTEMPTS:
                return
            rec.dom_attempts += 1
            rec.dom_on = bool((await _dom(rec, "start")).get("recording"))
            return
        out = await _dom(rec, "drain")
        rec.dom_events.extend(e for e in out.get("events") or [] if isinstance(e, dict))
        if out.get("recording") is False or out.get("skipped"):
            rec.dom_on = False  # the browser restarted: start again on the next tick
        del rec.dom_events[:-500]
    except Exception as exc:  # noqa: BLE001 - screen capture still records the session
        log.info("teach: DOM recorder %s user=%s: %s", "drain" if rec.dom_on else "start", rec.user_key, type(exc).__name__)


def start(user_key: str, started: float, limit: float) -> None:
    """Begin recording in the background (needs a running event loop; a no-op otherwise)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    old = _RECORDERS.pop(user_key, None)
    if old and old.task:
        old.task.cancel()
    rec = Recorder(user_key=user_key, started=started)
    rec.task = loop.create_task(_loop(rec, limit))
    _RECORDERS[user_key] = rec


def _peek_events(sbx) -> str:
    try:
        data = sbx.files.read(EVENTS_PATH)
    except Exception:  # noqa: BLE001 - no events file means nothing was captured yet
        return ""
    return data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data or "")


def _read_events(sbx) -> str:
    try:
        sbx.commands.run(KILL_CAPTURE, timeout=10)
    except Exception as exc:  # noqa: BLE001 - still read whatever was written
        log.info("teach: stopping capture failed: %s", type(exc).__name__)
    return _peek_events(sbx)


def parse_capture(text: str, offset: float) -> tuple[list[dict], list[str]]:
    """JSON lines from the capture script -> session-timed events (consecutive chars become one 'type')."""
    events: list[dict] = []
    notes: list[str] = []
    last_window = None
    for line in (text or "").splitlines()[: MAX_EVENTS * 4]:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        kind = row.get("kind")
        t = round(offset + float(row.get("t") or 0), 2)
        if kind == "capture_unavailable":
            notes.append(f"click/key capture unavailable ({row.get('reason') or 'unknown'}); only screenshots and window changes were recorded")
            continue
        if kind == "capture_started":
            if row.get("window"):
                last_window = str(row["window"])[:200]
                events.append({"t": t, "kind": "window", "window": last_window, "source": "screen"})
            continue
        prev = events[-1] if events else None
        if kind == "char":
            if prev and prev["kind"] == "type" and prev.get("source") == "screen" and t - prev["t_end"] < 4:
                prev["text"] += str(row.get("char") or "")
                prev["t_end"] = t
                continue
            events.append({"t": t, "t_end": t, "kind": "type", "text": str(row.get("char") or ""), "source": "screen"})
        elif kind == "scroll":
            direction = str(row.get("direction") or "down")
            if prev and prev["kind"] == "scroll" and prev.get("direction") == direction and t - prev["t_end"] < 2:
                prev["amount"] += 1
                prev["t_end"] = t
                continue
            events.append({"t": t, "t_end": t, "kind": "scroll", "direction": direction, "amount": 1, "source": "screen"})
        elif kind == "click":
            events.append(
                {"t": t, "kind": "click", "x": row.get("x"), "y": row.get("y"), "button": row.get("button", 1),
                 "window": str(row.get("window") or "")[:200], "source": "screen"}  # fmt: skip
            )
        elif kind == "key":
            events.append({"t": t, "kind": "key", "key": str(row.get("key") or "")[:40], "source": "screen"})
        elif kind == "window":
            title = str(row.get("window") or "")[:200]
            if not title or title == last_window:
                continue
            last_window = title
            events.append({"t": t, "kind": "window", "window": title, "source": "screen"})
        if len(events) >= MAX_EVENTS:
            break
    for ev in events:
        ev.pop("t_end", None)
    return events, notes


async def stop(user_key: str) -> dict:
    """Stop recording; returns {"events", "frames", "notes", "dom_events"}. Never raises."""
    rec = _RECORDERS.pop(user_key, None)
    if rec is None:
        return {"events": [], "frames": [], "notes": [], "dom_events": []}
    if rec.task:
        rec.task.cancel()
    if rec.dom_on:
        try:
            out = await _dom(rec, "stop")
            rec.dom_events.extend(e for e in out.get("events") or [] if isinstance(e, dict))
        except Exception as exc:  # noqa: BLE001
            log.info("teach: stopping the DOM recorder failed user=%s: %s", user_key, type(exc).__name__)
    events: list[dict] = []
    notes = list(rec.notes)
    box = rec.sandbox
    if box is not None:
        try:
            await asyncio.wait_for(_snapshot(rec, box), timeout=12)
        except Exception:  # noqa: BLE001
            pass
    if box is not None and rec.capture_offset is not None:
        text = rec.events_text
        try:
            text = await asyncio.wait_for(asyncio.to_thread(_read_events, box._live.sbx), timeout=20) or text
        except Exception as exc:  # noqa: BLE001 - fall back to the last copy read while recording
            log.warning("teach: reading capture failed user=%s: %s", user_key, type(exc).__name__)
            if not text:
                notes.append("could not read the recorded clicks and keys from the computer")
        events, more = parse_capture(text, rec.capture_offset)
        notes.extend(more)
    elif rec.capture_offset is None and not any("capture" in n for n in notes):
        notes.append("on-screen clicks were not captured (the computer was not running or capture did not start)")
    return {"events": events, "frames": rec.frames, "notes": notes, "dom_events": list(rec.dom_events)}


def peek(user_key: str) -> dict:
    """What has been recorded so far, without stopping: {"events", "frames", "notes"}."""
    rec = _RECORDERS.get(user_key)
    if rec is None:
        return {"events": [], "frames": [], "notes": []}
    events: list[dict] = []
    notes = list(rec.notes)
    if rec.capture_offset is not None and rec.events_text:
        events, more = parse_capture(rec.events_text, rec.capture_offset)
        notes.extend(more)
    return {"events": events, "frames": list(rec.frames), "notes": notes}


def active(user_key: str) -> bool:
    return user_key in _RECORDERS
