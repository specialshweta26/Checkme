"""Smoke test for the E2B computer, using the same code paths as production.

    python scripts/e2b_smoke.py            # live: needs E2B_API_KEY in the environment (e.g. a Render shell)
    python scripts/e2b_smoke.py --mock     # no network: a fake E2B SDK
    python scripts/e2b_smoke.py --mock --fail auth|quota|stream

Steps (each printed with timing): backend selection -> status before boot -> ensure_sandbox (create or
connect, desktop, noVNC stream) -> status (must be running with stream_url) -> call_daytona("boot")
-> the stream page answers over HTTPS (live only) -> kill the sandbox (live, unless --keep).
The API key and the stream password are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SECRET_KEY", "smoke-test-secret-key-not-for-production-0000")


def _redact(text: str) -> str:
    return re.sub(r'password=[^&\s"]+', "password=***", str(text))


def _step(name: str, started: float, detail: object = "") -> None:
    print(f"[{time.monotonic() - started:6.1f}s] {name}: {_redact(json.dumps(detail, default=str) if not isinstance(detail, str) else detail)}")


def _fake_sdk(fail: str):
    class SandboxException(Exception):
        pass

    class NotFoundException(SandboxException):
        pass

    class TimeoutException(SandboxException):
        pass

    class CommandExitException(SandboxException):
        def __init__(self, code=1):
            super().__init__("exit")
            self.exit_code, self.stdout, self.stderr = code, "", ""

    class InvalidArgumentException(SandboxException):
        pass

    class AuthenticationException(SandboxException):
        pass

    class RateLimitException(SandboxException):
        pass

    class Sbx:
        def __init__(self, sid):
            self.sandbox_id, self.up, self.commands = sid, set(), self

        def run(self, cmd, background=None, timeout=60, **_):
            if "x11vnc -bg" in cmd:
                self.up.add("vnc")
            if "novnc_proxy" in cmd and background:
                if fail == "stream":
                    return SimpleNamespace(exit_code=0, stdout="", stderr="")
                self.up.add("web")
            if "pgrep -x x11vnc" in cmd and "vnc" not in self.up:
                raise CommandExitException()
            if "grep -q ':6080 '" in cmd and "web" not in self.up:
                raise CommandExitException()
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

        def get_host(self, port):
            return f"{port}-{self.sandbox_id}.e2b.app"

        def is_running(self):
            return True

        def set_timeout(self, _):
            pass

        def _start_xfce4(self):
            pass

    boxes: dict = {}

    class Sandbox:
        @staticmethod
        def create(**_):
            time.sleep(0.2)
            if fail == "auth":
                raise AuthenticationException("401: Invalid API key")
            if fail == "quota":
                raise RateLimitException("429: concurrent sandboxes limit reached")
            boxes["mock-1"] = Sbx("mock-1")
            return boxes["mock-1"]

        @staticmethod
        def connect(sid, **_):
            if sid not in boxes:
                raise NotFoundException("not found")
            return boxes[sid]

        @staticmethod
        def list(**_):
            return SimpleNamespace(next_items=lambda: [])

        @staticmethod
        def get_info(sid, **_):
            if sid not in boxes:
                raise NotFoundException("gone")
            return SimpleNamespace(state="running")

        @staticmethod
        def kill(sid, **_):
            boxes.pop(sid, None)

    return SimpleNamespace(
        Sandbox=Sandbox,
        SandboxQuery=lambda **kw: SimpleNamespace(**kw),
        SandboxException=SandboxException,
        NotFoundException=NotFoundException,
        TimeoutException=TimeoutException,
        CommandExitException=CommandExitException,
        InvalidArgumentException=InvalidArgumentException,
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--fail", choices=["auth", "quota", "stream"], default="")
    ap.add_argument("--keep", action="store_true", help="live: leave the sandbox running")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="    log %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "e2b"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from app.config import get_settings
    from app.connectors import daytona_box, e2b_box

    if args.mock:
        os.environ["E2B_API_KEY"] = "mock-key"
        get_settings.cache_clear()
        fake = _fake_sdk(args.fail)
        e2b_box._sdk = lambda: fake  # type: ignore[assignment]
    started = time.monotonic()
    s = get_settings()
    _step("backend", started, {"computer_backend": s.computer_backend, "COMPUTER_PROVIDER": s.computer_provider or "(unset)", "E2B_API_KEY set": bool(s.e2b_api_key)})
    if s.computer_backend != "e2b":
        print("FAIL: the computer backend is not e2b; set E2B_API_KEY (and COMPUTER_PROVIDER=e2b).")
        return 1

    from app.security import encrypt

    row = SimpleNamespace(id="smoke", user_id=f"smoke-{int(time.time())}", credentials_enc=encrypt({}))

    async def no_db(_row):
        return None

    e2b_box._persist = no_db  # type: ignore[assignment]
    _step("status before boot", started, await e2b_box.status(row))
    try:
        box = await asyncio.wait_for(e2b_box.ensure_sandbox(row), timeout=120)
    except Exception as exc:  # noqa: BLE001
        _step("ensure_sandbox FAILED", started, str(exc))
        _step("status after failure", started, await e2b_box.status(row))
        return 2
    _step("ensure_sandbox", started, {"sandbox_id": box.id})
    e2b_box._STATUS.clear()
    st = await e2b_box.status(row)
    _step("status after boot", started, st)
    ok = st.get("state") == "running" and str(st.get("stream_url", "")).startswith("https://")
    boot = await daytona_box.call_daytona(row, "boot", {})
    _step("boot route payload", started, {k: boot.get(k) for k in ("state", "provider", "stream_url")})
    if not args.mock and ok:
        import httpx

        async with httpx.AsyncClient(timeout=20) as client:
            res = await client.get(st["stream_url"])
        _step("stream page", started, {"status": res.status_code, "novnc": "noVNC" in res.text})
        ok = ok and res.status_code == 200
        if not args.keep:
            sdk = e2b_box._sdk()
            await asyncio.to_thread(sdk.Sandbox.kill, box.id, api_key=s.e2b_api_key)
            _step("killed sandbox", started, box.id)
    for task in list(daytona_box._WARMING):
        task.cancel()
    print("PASS" if ok else "FAIL: status did not return a running stream_url")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
