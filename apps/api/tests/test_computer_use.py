"""Computer-use contract: policy, method choice, and one verified launch. No live desktop."""

import pytest

from app.computer import runtime


@pytest.fixture(autouse=True)
def _clear_tasks():
    runtime._TASKS.clear()
    yield
    runtime._TASKS.clear()


def test_router_stops_when_the_better_method_is_denied():
    caps = runtime.capabilities_for("native")
    assert runtime.choose_method(caps) == "script"
    assert runtime.choose_method(caps, denied={"script"}) is None
    assert runtime.choose_method(runtime.capabilities_for("browser")) == "browser"


def test_categories_prefer_browser_tools_and_scripting():
    assert runtime.categorize("Google Chrome") == "browser"
    assert runtime.categorize("Microsoft Excel") == "native"
    assert runtime.categorize("Visual Studio Code") == "hybrid"
    assert runtime.categorize("Terminal") == "terminal"


async def test_prohibited_and_high_risk_do_not_run():
    called = False

    async def call(*_a, **_k):
        nonlocal called
        called = True
        return {"ok": True}

    blocked = await runtime.run("u", "click", text="bypass the captcha", call=call)
    assert blocked["status"] == "failed" and blocked["error"]["code"] == "prohibited"
    waiting = await runtime.run("u", "send", target="WhatsApp", text="send the message", call=call)
    assert waiting["status"] == "needs_approval"
    assert called is False


async def test_chrome_launch_is_unsupported():
    async def call(*_a, **_k):
        raise AssertionError("browser launch must not reach the computer")

    out = await runtime.run("u", "launch", target="Chrome", call=call)
    assert out["status"] == "unsupported" and out["error"]["code"] == "use_browser_tools"


async def test_launch_succeeds_only_when_the_app_is_in_front():
    async def good(_user, tool, args):
        if tool == "launch_app":
            assert args["name"] == "Microsoft Excel"
            return {"ok": True}
        return {"ok": True, "platform": "macos", "front": "Microsoft Excel", "installed": ["Microsoft Excel"], "limitations": []}

    opened = await runtime.run("u", "launch", target="Excel", call=good)
    assert opened["status"] == "succeeded"
    assert opened["evidence"]["front"] == "Microsoft Excel"
    assert opened["result"]["method"] == "script"

    async def wrong_front(_user, tool, _args):
        if tool == "launch_app":
            return {"ok": True}
        return {"ok": True, "platform": "macos", "front": "Finder", "installed": ["Microsoft Excel"], "limitations": []}

    missed = await runtime.run("u", "launch", target="Excel", call=wrong_front)
    assert missed["status"] == "failed" and missed["error"]["code"] == "not_in_front"


async def test_missing_app_and_incomplete_discovery():
    async def missing(_user, _tool, _args):
        return {"ok": True, "platform": "macos", "front": "Finder", "installed": ["Finder"], "limitations": []}

    out = await runtime.run("u", "inspect", target="Excel", call=missing)
    assert out["status"] == "failed" and out["error"]["code"] == "not_installed"

    async def windows(_user, _tool, _args):
        return {"ok": True, "platform": "windows", "front": "", "installed": [], "limitations": ["Windows app discovery is not in this build."]}

    pending = await runtime.run("u", "inspect", target="Excel", call=windows)
    assert pending["status"] == "unsupported" and pending["error"]["code"] == "discovery_incomplete"


def _apps(**extra):
    base = {"ok": True, "platform": "macos", "front": "", "installed": [], "running": [], "electron": [], "tabs": [], "limitations": []}
    base.update(extra)
    return base


async def test_examples_route_without_becoming_an_allowlist():
    async def empty(_user, _tool, _args):
        return _apps(installed=["WhatsApp", "Microsoft Excel", "Visual Studio Code"], electron=["Visual Studio Code"])

    web = await runtime.run("user-web", "inspect", target="WhatsApp Web", call=empty)
    assert web["result"]["method"] == "browser" and web["result"]["url"].startswith("https://web.whatsapp.com/")
    desk = await runtime.run("user-desk", "inspect", target="WhatsApp", call=empty)
    assert desk["result"]["category"] == "hybrid"
    mail = await runtime.run("user-mail", "inspect", target="Gmail", connectors={"gmail"}, call=empty)
    assert mail["result"]["method"] == "api"
    sheet = await runtime.run("user-sheet", "inspect", target="Excel", call=empty)
    assert sheet["result"]["method"] == "script"
    code = await runtime.run("user-code", "inspect", target="Visual Studio Code", call=empty)
    assert code["result"]["category"] == "hybrid"
    unknown = await runtime.run("user-unknown", "inspect", target="Acme Ledger", call=empty)
    assert unknown["status"] == "failed" and unknown["error"]["code"] == "not_installed"


async def test_site_launch_is_verified_and_a_repeat_stops():
    async def open_site(_user, tool, args):
        assert tool == "browser_navigate" and args["url"] == "https://example.com/report"
        return {"ok": True, "url": "https://example.com/report"}

    opened = await runtime.run("user-site", "launch", target="https://example.com/report", call=open_site)
    assert opened["status"] == "succeeded"

    async def missing(_user, _tool, _args):
        return _apps(installed=["Finder"], front="Finder")

    first = await runtime.run("user-repeat", "launch", target="Not A Real App", call=missing)
    second = await runtime.run("user-repeat", "launch", target="Not A Real App", call=missing)
    third = await runtime.run("user-repeat", "launch", target="Not A Real App", call=missing)
    assert first["status"] == "failed" and second["status"] == "failed"
    assert third["error"]["code"] == "recover"


async def test_pause_blocks_the_next_step():
    runtime.task_control("user-pause", "pause")

    async def call(*_a, **_k):
        raise AssertionError("paused")

    out = await runtime.run("user-pause", "discover", call=call)
    assert out["error"]["code"] == "paused"
    assert runtime.task_view("user-pause")["status"] == "paused"


async def test_offline_desktop_is_a_failure():
    from app.desktop.agent import LocalOffline

    async def down(*_a, **_k):
        raise LocalOffline("desktop app is not connected")

    out = await runtime.run("u", "discover", call=down)
    assert out["status"] == "failed" and out["error"]["code"] == "desktop_offline"
