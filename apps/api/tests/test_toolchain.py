"""Cloud shell routing and the self-healing toolchain (docs/COMPUTER.md)."""

import json
import shutil
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from app.agent import computer_harness, toolchain
from app.agent.deep import RunContext, harness_tools, shell_routing_hint, system_prompt
from app.agent.fast_path import COMPUTER, classify_turn
from app.config import get_settings


def _user():
    return SimpleNamespace(id=uuid.uuid4(), timezone="Asia/Kolkata", email="a@b.c", name="A")


def _agent(focus=""):
    return SimpleNamespace(id=uuid.uuid4(), name="tony", title="web Researcher", instructions="", color="#000", kind="custom", focus=focus)


def _computer():
    return SimpleNamespace(slug="computer", name="Computer", provider="daytona", tags=["computer"], alias="", description="", kind="builtin")


def _local():
    return SimpleNamespace(slug="laptop", name="Laptop", provider="trusted_local", tags=[], alias="", description="", kind="builtin")


def _ctx(connectors, agent=None, call_impl=None):
    return RunContext(db=None, user=_user(), connectors={c.slug: c for c in connectors}, agent=agent, call_impl=call_impl)


# --- routing -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Install vercel cli", "Install all requirements", "npm install", "run the tests", "pip install pandas"])
def test_install_requests_are_shell_turns(text):
    plan = classify_turn(text)
    assert plan.kind == COMPUTER and plan.reason == "needs the cloud shell"


def test_news_still_quick():
    assert classify_turn("latest news on Virat Kohli").kind == "quick_web"


def test_non_coding_agent_has_cloud_shell_and_toolchain_but_not_local_shell():
    names = {t.name for t in harness_tools(_ctx([_computer()], _agent("research")))}
    assert {"shell", "ensure_toolchain", "read_file", "write_file", "list_files"} <= names
    assert "user_computer_shell" not in names
    assert "git_checkout" not in names  # repo/PR tools stay with coding agents


def test_local_shell_offered_only_when_connected():
    names = [t.name for t in harness_tools(_ctx([_computer(), _local()], _agent()))]
    assert "user_computer_shell" in names


def test_prompt_routes_installs_to_cloud_and_delegates_code():
    hint = shell_routing_hint([_computer()], _agent("research"))
    assert "Never tell the user to install" in hint
    assert "No local computer is connected" in hint
    assert "role='coder'" in hint
    coder_hint = shell_routing_hint([_computer(), _local()], _agent("coding"))
    assert "user_computer_shell" in coder_hint and "role='coder'" not in coder_hint
    prompt = system_prompt(_user(), [_computer()], "", agent=_agent("research"))
    assert "ensure_toolchain" in prompt and "is unavailable until a local computer" not in prompt


def test_stale_daytona_wording_is_gone_from_shell_docs():
    ctx = _ctx([_computer()], _agent("coding"))
    docs = {t.name: t.description for t in harness_tools(ctx)}
    assert "Daytona sandbox)" not in docs["sandbox_shell"]
    assert "E2B" in docs["sandbox_shell"] and "E2B" in docs["shell"]


# --- not-connected fallback ----------------------------------------------------------------------------------


async def test_local_shell_not_connected_retries_on_cloud():
    calls = []

    async def fake(slug, tool, args):
        calls.append((slug, tool))
        return {"exit_code": 0, "stdout": "Vercel CLI 1.0"}

    ctx = _ctx([_computer()], _agent(), fake)
    out = await computer_harness.user_computer_shell(ctx, "vercel --version")
    assert calls == [("computer", "exec")]
    assert out["ran_on"] == "cloud_computer" and out["exit_code"] == 0


# --- toolchain script ----------------------------------------------------------------------------------------


def test_expand_adds_dependencies_in_order():
    order, unknown = toolchain.expand(["vercel", "npm", "pip3", "bogus", "deploy"])
    assert order.index("curl") < order.index("node") < order.index("vercel")
    assert order.index("python3") < order.index("pip")
    assert "render" in order and "gh" in order
    assert unknown == ["bogus"]
    assert order.count("node") == 1


def test_install_script_is_non_interactive_and_user_space():
    script = toolchain.install_script(["vercel", "python3"])
    assert "sudo -n" in script and "sudo apt" not in script.replace("sudo -n", "")
    assert "DEBIAN_FRONTEND=noninteractive" in script and "-y" in script
    assert '--prefix "$HOME/.npm-global"' in script
    assert "NODE_MAJOR=22" in script and "nodejs.org/dist/latest-v${NODE_MAJOR}.x" in script
    assert 'command -v "$c"' in script  # present tools are skipped (idempotent)
    assert "inst_vercel" in script and "inst_node" in script


@pytest.mark.parametrize(
    "out,cmd,tool",
    [
        ("bash: line 1: vercel: command not found", "vercel deploy", "vercel"),
        ("sh: 1: npm: not found", "npm i", "node"),
        ("/bin/bash: pip3: command not found\n", "pip3 install x", "pip"),
        ("", "gh auth status", "gh"),
        ("bash: foo: command not found", "foo", None),
    ],
)
def test_missing_tool_parsing(out, cmd, tool):
    assert toolchain.missing_tool(out, cmd) == tool


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_install_script_idempotent_in_real_bash(tmp_path, monkeypatch):
    # An offline fake tool: its installer drops a script into ~/.local/bin.
    fake = {"cmds": ("checkme-fake",), "deps": (), "install": 'printf "#!/bin/sh\\necho fake\\n" > "$HOME/.local/bin/checkme-fake" && chmod +x "$HOME/.local/bin/checkme-fake"'}
    monkeypatch.setitem(toolchain.TOOLS, "checkme-fake", fake)
    script = toolchain.install_script(["checkme-fake"])
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    first = toolchain.parse(subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60).stdout)
    second = toolchain.parse(subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60).stdout)
    assert first["installed"] == ["checkme-fake"] and first["failed"] == []
    assert second["installed"] == [] and second["present"] == ["checkme-fake"]
    assert (tmp_path / ".checkme" / "tools" / "checkme-fake").exists()
    assert "npm-global" in (tmp_path / ".bashrc").read_text()


# --- exit 127 auto-install and retry -----------------------------------------------------------------------------


async def test_exit_127_installs_known_tool_and_retries_once():
    calls = []

    async def fake(slug, tool, args):
        cmd = args["command"]
        calls.append(cmd)
        if "inst_vercel" in cmd:
            return {"exit_code": 0, "stdout": "present=curl node\ninstalled=vercel\nfailed=\nnode=v22.1.0\nsudo=yes\n"}
        if len([c for c in calls if "inst_vercel" not in c]) == 1:
            return {"exit_code": 127, "stdout": "bash: line 1: vercel: command not found"}
        return {"exit_code": 0, "stdout": "Vercel CLI 39.0.0"}

    ctx = _ctx([_computer()], _agent(), fake)
    tool = next(t for t in harness_tools(ctx) if t.name == "shell")
    out = json.loads(await tool.coroutine(command="vercel --version"))
    assert out["exit_code"] == 0 and out["auto_installed"] == ["vercel"]
    assert len(calls) == 3  # run, install, retry


async def test_exit_127_install_failure_is_reported_without_loop():
    calls = []

    async def fake(slug, tool, args):
        calls.append(args["command"])
        if "inst_render" in args["command"]:
            return {"exit_code": 1, "stdout": "present=curl unzip\ninstalled=\nfailed=render\nlog_render=no network\n"}
        return {"exit_code": 127, "stdout": "bash: render: command not found"}

    ctx = _ctx([_computer()], _agent(), fake)
    out = await computer_harness.shell(ctx, "render services")
    assert out["exit_code"] == 127 and out["auto_install"]["ok"] is False
    assert "own machine" in out["note"]
    assert len(calls) == 2


async def test_auto_install_can_be_disabled(monkeypatch):
    calls = []

    async def fake(slug, tool, args):
        calls.append(args["command"])
        return {"exit_code": 127, "stdout": "bash: vercel: command not found"}

    monkeypatch.setattr(get_settings(), "computer_auto_install", False)
    out = await computer_harness.shell(_ctx([_computer()], _agent(), fake), "vercel")
    assert out["exit_code"] == 127 and len(calls) == 1


async def test_ensure_toolchain_tool_reports_unknown():
    ctx = _ctx([_computer()], _agent(), None)
    out = await toolchain.ensure_tools(ctx, ["not-a-tool"])
    assert out["ok"] is False and "Unknown" in out["error"]
