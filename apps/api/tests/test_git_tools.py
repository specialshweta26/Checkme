import json
from types import SimpleNamespace

import pytest

from app.agent import coding, git_tools
from app.agent.confirm import read_confirm
from app.agent.deep import RunContext, harness_tools
from app.connectors import github_app as gh

TOKEN = "ghs_INSTALLTOKEN0123456789"


def _agent(focus="coding"):
    return SimpleNamespace(id="a1", name="Coder", title="software engineer", instructions="", focus=focus, kind="custom", color="")


def _ctx(calls, *, app=True, auto_confirm=False, confirmations=None, stdout="ok"):
    async def fake_call(slug, tool, args):
        calls.append((slug, tool, args))
        out = stdout(args) if callable(stdout) else stdout
        return {"exit_code": 0, "stdout": out}

    computer = SimpleNamespace(slug="computer", name="Computer", provider="daytona", tags=["computer"], alias="", description="")
    ctx = RunContext(
        db=None,
        user=SimpleNamespace(id="u1", timezone="Asia/Kolkata"),
        connectors={"computer": computer},
        agent=_agent(),
        auto_confirm=auto_confirm,
        confirmations=list(confirmations or []),
        call_impl=fake_call,
    )
    ctx._github_installs = [gh.Installation(42, "octo", "User", "selected")] if app else []
    return ctx


@pytest.fixture
def app_on(monkeypatch):
    monkeypatch.setattr(gh, "is_configured", lambda: True)
    minted = []

    async def fake_token(user_id, repo, *, write, extra=None, installs=None):
        minted.append((repo, write))
        return TOKEN

    monkeypatch.setattr(gh, "repo_token", fake_token)
    return minted


async def test_checkout_injects_token_only_through_env(app_on):
    calls = []
    ctx = _ctx(calls, stdout=f"repo_dir=/home/daytona/work/app\nbranch=agent/fix\nbase=main\nleaked {TOKEN}")
    out = await git_tools.git_checkout(ctx, "octo/app", "agent/fix")
    slug, tool, args = calls[0]
    assert (slug, tool) == ("computer", "exec")
    assert TOKEN not in args["command"]
    assert args["env"] == {coding.TOKEN_ENV: TOKEN}
    assert "https://github.com/octo/app.git" in args["command"]
    assert "x-access-token:" not in args["command"]  # no token-in-URL clones
    assert TOKEN not in json.dumps(out)  # output scrubbed
    assert out["auth"] == "github_app" and out["branch"] == "agent/fix"
    assert app_on == [("octo/app", False)]  # read-scoped token for checkout


async def test_checkout_without_app_runs_anonymous(monkeypatch):
    monkeypatch.setattr(gh, "is_configured", lambda: False)
    calls = []
    out = await git_tools.git_checkout(_ctx(calls, app=False), "octocat/Hello-World", "agent/x")
    assert "env" not in calls[0][2]
    assert "credential.helper=!f()" not in calls[0][2]["command"]
    assert out["auth"] == "sandbox_or_public"


async def test_bad_inputs_are_refused_before_running(app_on):
    calls = []
    ctx = _ctx(calls)
    assert "error" in await git_tools.git_checkout(ctx, "not a repo")
    assert "error" in await git_tools.git_checkout(ctx, "octo/app", "--upload-pack=evil")
    assert "error" in await git_tools.git_checkout(ctx, "octo/app", "a/../b")
    assert calls == []


async def test_push_only_to_agent_branches(app_on):
    calls = []
    ctx = _ctx(calls)
    for bad in ("main", "master", "feature/x", "agent/", "agentx/y"):
        out = await git_tools.push_branch(ctx, "octo/app", bad)
        assert "agent/" in out["error"]
    assert ctx.pending == [] and calls == []


async def test_push_always_waits_for_approval_even_with_auto_confirm(app_on):
    calls = []
    ctx = _ctx(calls, auto_confirm=True, stdout="abc123 Fix login\n--\n 2 files changed")
    out = await git_tools.push_branch(ctx, "octo/app", "agent/fix-login", title="Fix login")
    assert out["status"] == "awaiting_user_confirmation"
    assert len(ctx.pending) == 1
    card = ctx.pending[0]
    assert card["preview"]["commits"] == ["abc123 Fix login"]
    payload = read_confirm(card["token"])["payload"]
    assert payload["slug"] == coding.CODING_SLUG and payload["args"]["branch"] == "agent/fix-login"
    assert TOKEN not in json.dumps(card)
    # only the read-only preview ran; nothing was pushed and no write token was minted
    assert all("push" not in c[2]["command"] for c in calls)
    assert all(not write for _, write in app_on)


async def test_confirmed_push_pushes_and_opens_pr(app_on, monkeypatch):
    seen = {}

    async def fake_app(row, name, args, installs=None):
        seen[name] = args
        return {"number": 9, "url": "https://github.com/octo/app/pull/9"}

    monkeypatch.setattr(gh, "call_github_app", fake_app)
    calls = []
    args = {"repo": "octo/app", "branch": "agent/fix-login", "open_pr": True, "title": "Fix login", "body": "b", "base": "", "draft": False}
    ctx = _ctx(calls, stdout="To https://github.com/octo/app.git\n*\trefs/heads/agent/fix-login:refs/heads/agent/fix-login\t[new branch]\nDone")
    out = await git_tools.run_confirmed(ctx, "push_branch", args)
    assert out["pushed"] is True and out["pr_url"].endswith("/pull/9")
    cmd = calls[0][2]["command"]
    assert "push --porcelain origin refs/heads/agent/fix-login:refs/heads/agent/fix-login" in cmd
    assert "--force" not in cmd and TOKEN not in cmd
    assert calls[0][2]["env"][coding.TOKEN_ENV] == TOKEN
    assert app_on[-1] == ("octo/app", True)
    assert seen["create_pull_request"]["head"] == "agent/fix-login"


async def test_confirmation_for_one_branch_does_not_cover_another(app_on):
    calls = []
    conf = [{"slug": coding.CODING_SLUG, "tool": "push_branch", "args": {"repo": "octo/app", "branch": "agent/a"}}]
    ctx = _ctx(calls, confirmations=conf)
    out = await git_tools.push_branch(ctx, "octo/app", "agent/b")
    assert out["status"] == "awaiting_user_confirmation"


async def test_push_without_any_credentials_says_how_to_fix(monkeypatch):
    monkeypatch.setattr(gh, "is_configured", lambda: True)

    async def fail_call(slug, tool, args):
        return {"exit_code": 128, "stdout": "fatal: could not read Username"}

    ctx = _ctx([], app=False)
    ctx.call_impl = fail_call
    out = await git_tools.run_push(ctx, {"repo": "octo/app", "branch": "agent/x", "open_pr": True})
    assert out["pushed"] is False
    assert "/api/connectors/github/install" in out["hint"]


async def test_commit_script_refuses_default_branch():
    script = coding.commit_script("app", "msg")
    assert "agent/*" in script and "exit 3" in script
    assert "Checkme Coder" in script


def test_scrub_removes_tokens():
    assert TOKEN not in coding.scrub(f"x {TOKEN} y")
    assert "[redacted]" in coding.scrub("https://x-access-token:abc@github.com/o/r", [])


def test_coding_agent_gets_git_tools_and_shell_steers_to_them():
    ctx = _ctx([])
    tools = {t.name: t for t in harness_tools(ctx)}
    for name in coding.GIT_TOOLS:
        assert name in tools
    assert "git_checkout" in tools["sandbox_shell"].description
    plain = RunContext(db=None, user=SimpleNamespace(id="u1"), connectors={}, agent=_agent(focus="research"))
    assert "push_branch" not in {t.name for t in harness_tools(plain)}


def test_readiness_offers_install_link_when_app_not_installed():
    conns = {"computer": SimpleNamespace(slug="computer", name="Computer", provider="daytona", alias="", description="", tags=[])}
    out = coding.readiness(conns, {"git": "git 2"}, "", {"configured": True, "installed": False, "install_url": "https://w/api/connectors/github/install"})
    assert any("https://w/api/connectors/github/install" in m for m in out["missing"])
    ok = coding.readiness(conns, {"git": "git 2"}, "", {"configured": True, "installed": True, "accounts": ["octo"]})
    assert any("Checkme GitHub App (octo)" in c for c in ok["can"])
    assert not any("GitHub" in m for m in ok["missing"])


class _SeedDB:
    def __init__(self, rows):
        self.rows = rows
        self.added = []

    async def execute(self, _stmt):
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


async def test_coder_is_seeded_once():
    from app.agents import service

    user = SimpleNamespace(id="u1", coder_seeded=False)
    db = _SeedDB([SimpleNamespace(name="Chief", focus="", kind="default")])
    row = await service.ensure_coder_agent(db, user)
    assert row.name == "Coder" and row.focus == "coding" and user.coder_seeded
    assert await service.ensure_coder_agent(db, user) is None
    assert len(db.added) == 1


async def test_coder_seed_reuses_existing_coding_agent():
    from app.agents import service

    mine = SimpleNamespace(name="Abhy", focus="coding", kind="custom")
    user = SimpleNamespace(id="u1", coder_seeded=False)
    db = _SeedDB([mine])
    assert await service.ensure_coder_agent(db, user) is mine
    assert db.added == [] and user.coder_seeded
