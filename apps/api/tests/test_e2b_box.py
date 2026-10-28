"""E2B Desktop adapter: reconnect, recreate on expiry, create when none, Daytona-shaped exec/fs, stream URLs."""

import json
from types import SimpleNamespace

import pytest

from app.connectors import daytona_box, e2b_box


class CommandExitException(Exception):
    def __init__(self, exit_code=1, stdout="", stderr=""):
        super().__init__(stderr)
        self.exit_code, self.stdout, self.stderr = exit_code, stdout, stderr


class SandboxException(Exception):
    pass


class NotFoundException(SandboxException):
    pass


class TimeoutException(SandboxException):
    pass


class InvalidArgumentException(SandboxException):
    pass


class FakeCommands:
    def __init__(self, sbx):
        self.sbx = sbx
        self.calls = []

    def run(self, cmd, background=None, envs=None, cwd=None, timeout=60, request_timeout=None, **_):
        self.calls.append({"cmd": cmd, "background": background, "envs": envs, "cwd": cwd, "timeout": timeout})
        if self.sbx.fail_next:
            code, out, err = self.sbx.fail_next.pop(0)
            raise CommandExitException(code, out, err)
        if "pgrep -x x11vnc" in cmd:
            if not self.sbx.vnc:
                raise CommandExitException(1)
        if f":{e2b_box.STREAM_PORT} " in cmd and "grep -q" in cmd and not self.sbx.novnc and "novnc_proxy" not in cmd:
            raise CommandExitException(1)
        if "x11vnc -bg" in cmd:
            self.sbx.vnc = True
        if "novnc_proxy" in cmd and background:
            self.sbx.novnc = True
        if cmd.startswith("sleep-forever"):
            raise TimeoutException("deadline")
        return SimpleNamespace(exit_code=0, stdout=self.sbx.stdout, stderr="")


class FakeFiles:
    def __init__(self):
        self.data = {}

    def read(self, path, format="text"):
        return bytearray(self.data[path])

    def write(self, path, data):
        self.data[path] = bytes(data)

    def list(self, path):
        return [SimpleNamespace(name=k.rsplit("/", 1)[-1], type="file") for k in self.data if k.startswith(path)]


class FakeSandbox:
    def __init__(self, sandbox_id, running_stream=False):
        self.sandbox_id = sandbox_id
        self.vnc = running_stream
        self.novnc = running_stream
        self.fail_next = []
        self.stdout = ""
        self.commands = FakeCommands(self)
        self.files = FakeFiles()
        self.timeouts = []
        self.running = True
        self.paused = False

    def get_host(self, port):
        return f"{port}-{self.sandbox_id}.e2b.app"

    def is_running(self):
        return self.running

    def set_timeout(self, secs):
        self.timeouts.append(secs)

    def pause(self):
        self.paused = True

    def screenshot(self):
        return bytearray(b"\x89PNG" + b"0" * 40)

    def _start_xfce4(self):
        pass

    def write(self, text, chunk_size=25, delay_in_ms=75):
        self.typed = getattr(self, "typed", []) + [text]

    def press(self, key):
        self.pressed = getattr(self, "pressed", []) + [key]


class FakeSDK:
    CommandExitException = CommandExitException
    SandboxException = SandboxException
    NotFoundException = NotFoundException
    TimeoutException = TimeoutException
    InvalidArgumentException = InvalidArgumentException

    def __init__(self):
        self.existing = {}
        self.created = []
        self.connected = []
        self.listed = []
        self.info = {}
        sdk = self

        class Sandbox:
            @staticmethod
            def create(**kwargs):
                sbx = FakeSandbox(f"new-{len(sdk.created) + 1}")
                sdk.created.append(kwargs)
                sdk.existing[sbx.sandbox_id] = sbx
                return sbx

            @staticmethod
            def connect(sandbox_id, timeout=None, api_key=None):
                sdk.connected.append(sandbox_id)
                if sandbox_id not in sdk.existing:
                    raise NotFoundException(f"sandbox {sandbox_id} not found")
                return sdk.existing[sandbox_id]

            @staticmethod
            def list(query=None, api_key=None):
                return SimpleNamespace(next_items=lambda: list(sdk.listed), has_next=False)

            @staticmethod
            def get_info(sandbox_id, api_key=None):
                if sandbox_id not in sdk.info:
                    raise NotFoundException("gone")
                return SimpleNamespace(state=SimpleNamespace(value=sdk.info[sandbox_id]))

        self.Sandbox = Sandbox
        self.SandboxQuery = lambda metadata=None, state=None: SimpleNamespace(metadata=metadata, state=state)


@pytest.fixture
def sdk(monkeypatch):
    fake = FakeSDK()
    settings = SimpleNamespace(
        e2b_api_key="test-key",
        e2b_template="",
        e2b_sandbox_timeout=3600,
        computer_backend="e2b",
    )
    monkeypatch.setattr(e2b_box, "_sdk", lambda: fake)
    monkeypatch.setattr(e2b_box, "get_settings", lambda: settings)
    monkeypatch.setattr(daytona_box, "get_settings", lambda: settings)
    monkeypatch.setattr(e2b_box, "decrypt_json", lambda raw: json.loads(raw) if raw else {})
    monkeypatch.setattr(e2b_box, "encrypt", lambda data: json.dumps(data))
    persisted = []

    async def fake_persist(row):
        persisted.append(row.credentials_enc)

    monkeypatch.setattr(e2b_box, "_persist", fake_persist)
    monkeypatch.setattr(e2b_box, "KEEPALIVE_EVERY", 0.0)
    fake.persisted = persisted
    e2b_box.forget()
    yield fake
    e2b_box.forget()


def _row(creds=None, user_id="u1"):
    return SimpleNamespace(id="c1", user_id=user_id, credentials_enc=json.dumps(creds) if creds is not None else None)


async def test_creates_sandbox_when_none_and_persists_id(sdk):
    row = _row({})
    box = await e2b_box.ensure_sandbox(row)
    assert box.id == "new-1"
    assert len(sdk.created) == 1
    created = sdk.created[0]
    assert created["metadata"] == {"app": "checkme", "checkme_user": "u1"}
    assert created["timeout"] == 3600
    assert created["lifecycle"] == {"on_timeout": "pause", "auto_resume": False}
    assert created["api_key"] == "test-key"
    saved = json.loads(row.credentials_enc)
    assert saved["e2b_sandbox_id"] == "new-1"
    assert len(saved["e2b_stream_key"]) >= 16
    assert sdk.persisted, "new sandbox id is written to the DB right away"
    urls = box.stream_urls()
    assert urls["stream_url"].startswith("https://6080-new-1.e2b.app/vnc.html?")
    assert "view_only=true" in urls["stream_url"] and "view_only" not in urls["control_url"]
    assert f"password={saved['e2b_stream_key']}" in urls["control_url"]


async def test_reconnects_to_stored_sandbox_and_reuses_stream(sdk):
    existing = FakeSandbox("sbx-old", running_stream=True)
    sdk.existing["sbx-old"] = existing
    row = _row({"e2b_sandbox_id": "sbx-old", "e2b_stream_key": "k" * 20})
    box = await e2b_box.ensure_sandbox(row)
    assert box.id == "sbx-old"
    assert sdk.connected == ["sbx-old"] and not sdk.created
    assert not any("x11vnc -bg" in c["cmd"] for c in existing.commands.calls), "running stream is reused"
    assert sdk.persisted == []  # nothing changed
    assert existing._display == ":0"


async def test_recreates_when_stored_sandbox_expired(sdk):
    row = _row({"e2b_sandbox_id": "sbx-dead", "e2b_stream_key": "old"})
    box = await e2b_box.ensure_sandbox(row)
    assert sdk.connected == ["sbx-dead"]
    assert box.id == "new-1"
    saved = json.loads(row.credentials_enc)
    assert saved["e2b_sandbox_id"] == "new-1"
    assert saved["e2b_stream_key"] != "old"


async def test_finds_sandbox_by_metadata_when_id_lost(sdk):
    sdk.existing["sbx-meta"] = FakeSandbox("sbx-meta", running_stream=True)
    sdk.listed = [SimpleNamespace(sandbox_id="sbx-meta")]
    row = _row({})
    box = await e2b_box.ensure_sandbox(row)
    assert box.id == "sbx-meta" and not sdk.created


async def test_cached_handle_keeps_alive_and_reconnects_when_paused(sdk):
    row = _row({})
    first = await e2b_box.ensure_sandbox(row)
    sbx = sdk.existing[first.id]
    await e2b_box.ensure_sandbox(row)
    assert sbx.timeouts == [3600], "a second call extends the sandbox timeout"
    sbx.running = False  # paused on idle timeout
    again = await e2b_box.ensure_sandbox(row)
    assert again.id == first.id
    assert sdk.connected[-1] == first.id, "resume goes through connect()"


async def test_exec_is_daytona_shaped_and_detaches_background_jobs(sdk):
    box = await e2b_box.ensure_sandbox(_row({}))
    sbx = sdk.existing[box.id]
    sbx.stdout = "hello\n"
    res = await box.process.exec("bash -lc 'nohup python3 d.py & disown'", env={"A": "1"}, timeout=12)
    assert res.exit_code == 0 and res.result == "hello\n"
    call = sbx.commands.calls[-1]
    assert "setsid nohup python3 d.py" in call["cmd"]
    assert call["envs"] == {"DISPLAY": ":0", "A": "1"}
    assert call["cwd"] == "/home/daytona" and call["timeout"] == 12
    sbx.fail_next.append((2, "out", "boom"))
    res = await box.process.exec("false")
    assert res.exit_code == 2 and "out" in res.result and "boom" in res.result
    res = await box.process.exec("sleep-forever", timeout=1)
    assert res.exit_code == 124 and "timed out" in res.result


async def test_fs_is_daytona_shaped(sdk, tmp_path):
    box = await e2b_box.ensure_sandbox(_row({}))
    await box.fs.upload_file(b"abc", "/tmp/a.txt")
    local = tmp_path / "b.bin"
    local.write_bytes(b"xyz")
    await box.fs.upload_file(str(local), "/tmp/b.bin")
    assert await box.fs.download_file("/tmp/a.txt") == b"abc"
    assert await box.fs.download_file("/tmp/b.bin") == b"xyz"
    names = {e.name for e in await box.fs.list_files("/tmp")}
    assert names == {"a.txt", "b.bin"}


async def test_status_never_resumes_a_paused_sandbox(sdk):
    sdk.info["sbx-p"] = "paused"
    row = _row({"e2b_sandbox_id": "sbx-p", "e2b_stream_key": "k"})
    data = await e2b_box.status(row)
    assert data["state"] == "suspended" and data["provider"] == "e2b"
    assert sdk.connected == []
    e2b_box.forget()
    assert (await e2b_box.status(_row({})))["state"] == "stopped"


async def test_status_running_returns_stream_urls(sdk):
    row = _row({})
    box = await e2b_box.ensure_sandbox(row)
    sdk.info[box.id] = "running"
    e2b_box._STATUS.clear()
    data = await e2b_box.status(row)
    assert data["state"] == "running" and data["screenAvailable"] is True
    assert data["stream_url"].startswith("https://6080-")


async def test_call_daytona_routes_to_e2b(sdk, monkeypatch):
    row = _row({})
    info = await daytona_box.call_daytona(row, "sandbox_info", {})
    assert info["provider"] == "e2b" and info["sandbox_id"] == "new-1" and info["cwd"] == "/home/daytona"
    sbx = sdk.existing["new-1"]
    sbx.stdout = "ok"
    out = await daytona_box.call_daytona(row, "exec", {"command": "echo ok"})
    assert out == {"exit_code": 0, "stdout": "ok"}
    png = await daytona_box.call_daytona(row, "screen_png", {})
    assert png.startswith(b"\x89PNG")


async def test_screen_png_without_running_sandbox_is_a_clear_error(sdk):
    from connector_core import ToolError

    with pytest.raises(ToolError):
        await daytona_box.call_daytona(_row({}, user_id="nobody"), "screen_png", {})


def test_detach_background_is_idempotent():
    assert e2b_box.detach_background("setsid nohup x &") == "setsid nohup x &"
    assert e2b_box.detach_background("nohup x & nohup y &") == "setsid nohup x & setsid nohup y &"


def test_backend_selection():
    from app.config import Settings

    assert Settings(e2b_api_key="e", daytona_api_key="d").computer_backend == "e2b"
    assert Settings(e2b_api_key="e", daytona_api_key="d", computer_provider="daytona").computer_backend == "daytona"
    assert Settings(daytona_api_key="d", computer_provider="e2b").computer_backend == "daytona"
    assert Settings(e2b_api_key="", daytona_api_key="").computer_backend == ""


def test_daemon_version_check_matches_current_daemon():
    from app.connectors.computer_daemon import VERSION

    assert daytona_box.daemon_current(f'{{"ok": true, "version": "{VERSION}"}}\nOK')
    assert not daytona_box.daemon_current('{"ok": true, "version": "9"}\nOK')
    assert not daytona_box.daemon_current("DOWN")


class AuthenticationException(SandboxException):
    pass


class RateLimitException(SandboxException):
    pass


async def test_boot_failure_is_reported_not_swallowed(sdk, caplog):
    from connector_core import ToolError

    def refuse(**_):
        raise AuthenticationException("401: Invalid API key test-key")

    sdk.Sandbox.create = staticmethod(refuse)
    row = _row({})
    with caplog.at_level("INFO", logger="app.connectors.e2b_box"):
        with pytest.raises(ToolError) as err:
            await e2b_box.ensure_sandbox(row)
    assert str(err.value) == "E2B: invalid API key (E2B_API_KEY was rejected)."
    logs = caplog.text
    assert "e2b: boot requested" in logs and "e2b: creating desktop sandbox" in logs and "e2b: boot failed" in logs
    assert "test-key" not in logs, "the API key is never logged"
    data = await e2b_box.status(row)
    assert data["state"] == "stopped" and data["error"].startswith("E2B: invalid API key")
    assert e2b_box.booting(row) == 0


def test_describe_error_maps_quota_and_scrubs_the_key(sdk):
    assert e2b_box.describe_error(RateLimitException("429 too many")).startswith("E2B: quota exceeded")
    msg = e2b_box.describe_error(SandboxException("team test-key has exhausted its credits"))
    assert msg.startswith("E2B: quota exceeded") and "test-key" not in msg
    assert "test-key" not in e2b_box.describe_error(RuntimeError("boom test-key"))


async def test_status_reports_booting_while_a_boot_is_in_flight(sdk):
    import time

    row = _row({})
    e2b_box._BOOTING["u1"] = time.monotonic() - 12
    data = await e2b_box.status(row)
    assert data["state"] == "booting" and data["booting_for"] >= 12 and data["provider"] == "e2b"


async def test_success_clears_previous_error_and_status_streams(sdk):
    row = _row({})
    e2b_box._LAST_ERROR["u1"] = "E2B: old failure"
    box = await e2b_box.ensure_sandbox(row)
    sdk.info[box.id] = "running"
    data = await e2b_box.status(row)
    assert data["state"] == "running" and "error" not in data and data["stream_url"].startswith("https://")


async def test_e2b_boot_returns_stream_without_waiting_for_the_browser(sdk, monkeypatch):
    import asyncio

    started = asyncio.Event()

    async def slow_boot(proc, fs=None):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(daytona_box, "_boot_computer", slow_boot)
    data = await asyncio.wait_for(daytona_box.call_daytona(_row({}), "boot", {}), timeout=5)
    assert data["provider"] == "e2b" and data["state"] == "running"
    assert data["stream_url"].startswith("https://6080-new-1.e2b.app/")
    await asyncio.wait_for(started.wait(), timeout=2)
    for task in list(daytona_box._WARMING):
        task.cancel()


async def test_status_route_explains_missing_backend(monkeypatch):
    from app.connectors import routes

    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(computer_backend=""))
    data = await routes.computer_status(user=SimpleNamespace(id="u1"), db=None)
    assert data["state"] == "stopped" and data["provider"] == "none" and "E2B_API_KEY" in data["error"]


async def test_boot_route_times_out_with_a_clear_message(monkeypatch):
    import asyncio

    from fastapi import HTTPException

    from app.connectors import routes

    async def row_for(db, user, provision=False):
        return _row({})

    async def hang(row, name, args):
        await asyncio.sleep(3600)

    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(computer_backend="e2b"))
    monkeypatch.setattr(routes, "_computer_row", row_for)
    monkeypatch.setattr(routes, "BOOT_TIMEOUT", 0.05)
    monkeypatch.setattr(daytona_box, "call_daytona", hang)
    with pytest.raises(HTTPException) as err:
        await routes.computer_boot(user=SimpleNamespace(id="u1"), db=None)
    assert err.value.status_code == 504 and "did not start within" in err.value.detail


def test_parse_key_allowlist():
    from connector_core import ToolError

    assert e2b_box.parse_key("Enter") == ["enter"]
    assert e2b_box.parse_key("ArrowUp") == ["up"]
    assert e2b_box.parse_key("Cmd+V") == ["ctrl", "v"]
    assert e2b_box.parse_key("ctrl+shift+t") == ["ctrl", "shift", "t"]
    for bad in ("a; rm -rf /", "ctrl+$(id)", "x+a", "", "foo"):
        with pytest.raises(ToolError):
            e2b_box.parse_key(bad)


async def test_send_input_types_and_presses_on_the_desktop(sdk):
    row = _row({})
    box = await e2b_box.ensure_sandbox(row)
    sbx = sdk.existing[box.id]
    out = await e2b_box.send_input(row, "hello world", "Enter")
    assert out == {"ok": True, "typed": 11, "key": "enter"}
    assert sbx.typed == ["hello world"] and sbx.pressed == ["enter"]
    await e2b_box.send_input(row, None, "cmd+a")
    assert sbx.pressed[-1] == ["ctrl", "a"]


class _DB:
    async def commit(self):
        pass


async def test_input_route_uses_e2b_sdk(sdk, monkeypatch):
    from fastapi import HTTPException

    from app.connectors import routes

    row = _row({})
    box = await e2b_box.ensure_sandbox(row)

    async def row_for(db, user, provision=True):
        return row

    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(computer_backend="e2b"))
    monkeypatch.setattr(routes, "_computer_row", row_for)
    out = await routes.computer_input(routes.ComputerInputBody(text="abc", key="Tab"), user=SimpleNamespace(id="u1"), db=_DB())
    assert out["ok"] is True and sdk.existing[box.id].typed == ["abc"] and sdk.existing[box.id].pressed == ["tab"]
    with pytest.raises(HTTPException) as err:
        await routes.computer_input(routes.ComputerInputBody(key="rm -rf"), user=SimpleNamespace(id="u1"), db=_DB())
    assert err.value.status_code == 400 and "Unsupported key" in err.value.detail
    with pytest.raises(HTTPException):
        await routes.computer_input(routes.ComputerInputBody(), user=SimpleNamespace(id="u1"), db=_DB())


async def test_input_route_daytona_uses_computer_act(monkeypatch):
    from app.connectors import routes

    seen = {}

    async def row_for(db, user, provision=True):
        return _row({})

    async def fake_call(row, name, args):
        seen["name"], seen["args"] = name, args
        return {"ok": True}

    monkeypatch.setattr(routes, "get_settings", lambda: SimpleNamespace(computer_backend="daytona"))
    monkeypatch.setattr(routes, "_computer_row", row_for)
    monkeypatch.setattr(daytona_box, "call_daytona", fake_call)
    out = await routes.computer_input(routes.ComputerInputBody(text="hi", key="Enter"), user=SimpleNamespace(id="u1"), db=_DB())
    assert out == {"ok": True, "error": ""}
    assert seen["name"] == "computer_act"
    assert seen["args"]["actions"] == [{"kind": "type", "text": "hi"}, {"kind": "key", "key": "Enter"}]
    assert routes._playwright_key("ctrl+a") == "Control+A" and routes._playwright_key("Left") == "ArrowLeft"


def test_every_agent_keeps_the_computer_even_when_assigned_elsewhere():
    from app.agents.service import filter_connectors

    computer = SimpleNamespace(id="c-comp", slug="computer", provider="daytona", kind="builtin")
    mail = SimpleNamespace(id="c-mail", slug="gmail", provider="gmail", kind="builtin")
    assignments = {"c-comp": ["coder"], "c-mail": ["coder"]}
    kept = filter_connectors([computer, mail], "chief", [], assignments)
    assert computer in kept and mail not in kept


async def test_run_adds_computer_row_dropped_by_health(monkeypatch):
    from app.agent import deep

    row = SimpleNamespace(slug="computer", enabled=True, health="down")

    class Result:
        def scalar_one_or_none(self):
            return row

    class DB:
        async def execute(self, _stmt):
            return Result()

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(computer_backend="e2b"))
    out = await deep.with_computer(DB(), SimpleNamespace(id="u1"), [SimpleNamespace(slug="gmail")])
    assert row in out
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(computer_backend=""))
    assert row not in await deep.with_computer(DB(), SimpleNamespace(id="u1"), [])


def test_computer_missing_gives_the_real_reason(monkeypatch):
    from app.agent import computer_harness

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(computer_backend=""))
    assert "E2B_API_KEY" in computer_harness.computer_missing()["error"]
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(computer_backend="e2b"))
    msg = computer_harness.computer_missing()["error"]
    assert "not available for this agent" not in msg and "configured" in msg
