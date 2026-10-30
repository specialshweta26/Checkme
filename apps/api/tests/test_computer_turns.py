"""Long chat turns survive dropped clients; computer tool calls are bounded and never swap commands."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app.chat import routes
from app.connectors import daytona_box as box


async def _collect(gen):
    out = b""
    async for chunk in gen:
        out += chunk
    return out


async def test_heartbeat_body_streams_spaces_then_json(monkeypatch):
    monkeypatch.setattr(routes, "HEARTBEAT_EVERY_S", 0.02)

    async def slow():
        await asyncio.sleep(0.1)
        return {"text": "done", "session_id": "s1"}

    raw = await _collect(routes._heartbeat_body(asyncio.create_task(slow())))
    assert raw.startswith(b" ") and raw.strip().startswith(b"{")
    assert json.loads(raw) == {"text": "done", "session_id": "s1"}


async def test_heartbeat_body_reports_errors_as_json():
    async def boom():
        raise HTTPException(502, "model down")

    raw = await _collect(routes._heartbeat_body(asyncio.create_task(boom())))
    assert json.loads(raw) == {"detail": "model down", "status_code": 502, "stream_error": True}


async def test_dropped_client_does_not_cancel_the_turn(monkeypatch):
    monkeypatch.setattr(routes, "HEARTBEAT_EVERY_S", 0.01)
    finished = asyncio.Event()

    async def turn():
        await asyncio.sleep(0.1)
        finished.set()
        return {"text": "stored"}

    task = asyncio.create_task(turn())
    gen = routes._heartbeat_body(task)
    assert await gen.__anext__() == b" "
    await gen.aclose()  # the client went away mid-turn
    assert finished.is_set() and task.result() == {"text": "stored"}


def test_tool_time_caps():
    assert box.tool_time_cap("status", {}) is None
    assert box.tool_time_cap("boot", {}) is None
    assert box.tool_time_cap("exec", {"timeout": 300}) == 390
    assert box.tool_time_cap("browser_act", {}) == box._BROWSER_CAP_S


class FakeProc:
    def __init__(self, codes=None, hang=False):
        self.commands: list[str] = []
        self.codes = list(codes or [])
        self.hang = hang

    async def exec(self, command, cwd=None, env=None, timeout=60):
        self.commands.append(command)
        if self.hang and "curl -sS" in command:
            await asyncio.sleep(10)
        code = self.codes.pop(0) if self.codes and "curl -sS" in command else 0
        out = '{"ok": true, "url": "https://x.test/"}' if code == 0 else ""
        return type("R", (), {"exit_code": code, "result": out})()


class FakeFs:
    def __init__(self):
        self.files: dict[str, bytes] = {}

    async def upload_file(self, src, dest):
        from pathlib import Path

        self.files[dest] = Path(src).read_bytes() if isinstance(src, str) else bytes(src)

    async def download_file(self, path):
        return b"\x89PNG" + b"0" * 40


@pytest.fixture
def no_daemon(monkeypatch):
    async def ready(proc, fs=None):
        return None

    monkeypatch.setattr(box, "_ensure_daemon", ready)


async def test_parallel_browser_commands_use_their_own_payload_files(no_daemon):
    proc, fs = FakeProc(), FakeFs()
    await asyncio.gather(
        box._browser_rpc(proc, {"command": "navigate", "url": "https://a.test"}, 30, fs),
        box._browser_rpc(proc, {"command": "snapshot"}, 30, fs),
    )
    paths = [p for p in fs.files if p.startswith("/tmp/checkme_cmd_")]
    assert len(set(paths)) == 2
    payloads = sorted(json.loads(fs.files[p])["command"] for p in paths)
    assert payloads == ["navigate", "snapshot"]
    curls = [c for c in proc.commands if "curl -sS" in c]
    assert all("rm -f /tmp/checkme_cmd_" in c for c in curls)


async def test_undelivered_command_is_retried_once(no_daemon):
    proc, fs = FakeProc(codes=[7, 0]), FakeFs()
    data = await box._browser_rpc(proc, {"command": "act", "actions": [{"kind": "click", "ref": "e1"}]}, 30, fs)
    assert data["ok"] is True
    assert len([c for c in proc.commands if "curl -sS" in c]) == 2


async def test_failed_action_is_not_retried(no_daemon):
    proc, fs = FakeProc(codes=[22, 0]), FakeFs()
    data = await box._browser_rpc(proc, {"command": "act", "actions": [{"kind": "click", "ref": "e1"}]}, 30, fs)
    assert data["ok"] is False
    assert len([c for c in proc.commands if "curl -sS" in c]) == 1


@pytest.mark.asyncio
async def test_ensure_playwright_reinstalls_when_import_fails():
    class Proc:
        def __init__(self):
            self.commands = []
            self.browser_deps = False
            self._imports = 0

        async def exec(self, command, timeout=60):
            self.commands.append(command)
            if "from playwright.sync_api import sync_playwright" in command and "pip install" not in command:
                self._imports += 1
                ok = self._imports > 1
                return type("R", (), {"exit_code": 0, "result": "OK" if ok else "FAIL"})()
            return type("R", (), {"exit_code": 0, "result": "installed"})()

    proc = Proc()
    await box._ensure_playwright(proc)
    assert any("pip install" in c for c in proc.commands)
    assert proc._imports >= 2


def test_daemon_boot_uses_persistent_home_not_login_shell():
    cmd = box._daemon_boot_cmd(type("P", (), {"daemon_env": "DISPLAY=:0 "})())
    assert box.BROWSER_HOME in cmd and box.DAEMON_PY in cmd
    assert "CHECKME_BROWSER_HOME=" in cmd and "PYTHONPATH=" in cmd
    assert "bash -lc" not in cmd
    assert "setsid nohup python3" in cmd


@pytest.mark.asyncio
async def test_write_file_publishes_html_to_durable_storage(monkeypatch):
    from app.agent import computer_harness as ch

    calls = []

    async def fake_run(_ctx, name, args=None):
        calls.append(name)
        if name == "write_file":
            return {"ok": True, "path": args["path"]}
        if name == "publish_file":
            return {"download_url": "https://app.test/api/files?t=abc", "preview_url": "https://app.test/api/files?t=def", "filename": "r.html"}
        return {}

    monkeypatch.setattr(ch, "run_computer", fake_run)
    out = await ch.write_file(None, "/tmp/r.html", "<h1>hi</h1>")
    assert out["download_url"].endswith("t=abc")
    assert calls == ["write_file", "publish_file"]
    py = await ch.write_file(None, "/tmp/app.py", "x=1")
    assert "download_url" not in py


@pytest.mark.asyncio
async def test_browser_start_failure_tells_agent_to_continue(monkeypatch):
    from app.agent import computer_harness as ch

    async def fake_call(_ctx, _slug, _tool, _args):
        return {"ok": False, "error": "ModuleNotFoundError: No module named 'playwright'"}

    monkeypatch.setattr("app.agent.deep.call_user_tool", fake_call)
    monkeypatch.setattr(ch, "computer_slug", lambda _ctx: "computer")
    out = await ch.run_computer(type("C", (), {"pending": []})(), "browser_snapshot", {})
    assert out["continue"] is True
    assert "do not treat this as the task failing" in out["note"].lower()


async def test_hung_browser_command_returns_an_error_instead_of_freezing(no_daemon, monkeypatch):
    proc, fs = FakeProc(hang=True), FakeFs()
    real_wait_for = asyncio.wait_for

    async def quick(aw, timeout):
        return await real_wait_for(aw, timeout=0.05)

    monkeypatch.setattr(box.asyncio, "wait_for", quick)
    data = await box._browser_rpc(proc, {"command": "navigate", "url": "https://slow.test"}, 30, fs)
    assert data["ok"] is False and "did not answer" in data["error"]
