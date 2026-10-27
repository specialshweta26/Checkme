import base64
import json
from types import SimpleNamespace

import pytest

from app.agent import coding, git_tools
from app.agent.confirm import read_confirm
from app.agent.deep import RunContext, harness_tools
from app.connectors import github as gh

TOKEN = "gho_" + "T" * 36
ACCT = gh.Account(id="ca_1", user_id="u1", auth_config_id="ac_1", managed=False)


def _agent(focus="coding"):
    return SimpleNamespace(id="a1", name="Coder", title="software engineer", instructions="", focus=focus, kind="custom", color="")


def _ctx(calls, *, auto_confirm=False, confirmations=None, stdout="ok", exit_code=0, files=None):
    async def fake_call(slug, tool, args):
        calls.append((slug, tool, args))
        if tool == "read_bytes":
            return {"path": args["path"], "base64": base64.b64encode((files or {})[args["path"]]).decode()}
        out = stdout(args) if callable(stdout) else stdout
        code = exit_code(args) if callable(exit_code) else exit_code
        return {"exit_code": code, "stdout": out}

    computer = SimpleNamespace(slug="computer", name="Computer", provider="daytona", tags=["computer"], alias="", description="")
    return RunContext(
        db=None,
        user=SimpleNamespace(id="u1", timezone="Asia/Kolkata"),
        connectors={"computer": computer},
        agent=_agent(),
        auto_confirm=auto_confirm,
        confirmations=list(confirmations or []),
        call_impl=fake_call,
    )


def _connect(monkeypatch, *, connected=True, token=TOKEN):
    monkeypatch.setattr(gh, "is_configured", lambda: True)

    async def account_for(user_id, fresh=False):
        if not connected:
            raise gh.NotConnected(gh.CONNECT_HINT)
        return ACCT

    async def viewer(user_id):
        return {"login": "octo", "id": 77, "name": "Octo Cat", "email": "77+octo@users.noreply.github.com"}

    async def git_token(user_id):
        return token

    monkeypatch.setattr(gh, "account_for", account_for)
    monkeypatch.setattr(gh, "viewer", viewer)
    monkeypatch.setattr(gh, "git_token", git_token)


@pytest.fixture
def app_on(monkeypatch):
    _connect(monkeypatch)


@pytest.fixture
def api_only(monkeypatch):
    _connect(monkeypatch, token="")


async def test_checkout_injects_token_only_through_env(app_on):
    calls = []
    ctx = _ctx(calls, stdout=f"repo_dir=/home/daytona/work/app\nbranch=agent/fix\nbase=main\nleaked {TOKEN}")
    out = await git_tools.git_checkout(ctx, "octo/app", "agent/fix")
    slug, tool, args = calls[0]
    assert (slug, tool) == ("computer", "exec")
    assert TOKEN not in args["command"]
    assert args["env"] == {coding.TOKEN_ENV: TOKEN}
    assert "https://github.com/octo/app.git" in args["command"]
    assert "x-access-token:" not in args["command"]  # no token-in-URL clones, nothing lands in .git/config
    assert TOKEN not in json.dumps(out)  # output scrubbed
    assert out["auth"] == "composio_token" and out["branch"] == "agent/fix"


async def test_checkout_without_connection_runs_anonymous(monkeypatch):
    _connect(monkeypatch, connected=False)
    calls = []
    out = await git_tools.git_checkout(_ctx(calls), "octocat/Hello-World", "agent/x")
    assert "env" not in calls[0][2]
    assert out["auth"] == "public_or_sandbox_credentials"


async def test_private_checkout_without_token_falls_back_to_snapshot(api_only, monkeypatch):
    url = "https://codeload.github.com/octo/app/legacy.tar.gz/main?token=SECRETURL"

    async def snap(user_id, repo, branch, base):
        return {"ref": "main", "sha": "b" * 40, "base": "main", "url": url}

    monkeypatch.setattr(gh, "snapshot_source", snap)
    calls = []

    def stdout(args):
        return "fatal: could not read Username" if "clone" in args["command"] and coding.ARCHIVE_ENV not in args.get("env", {}) else f"repo_dir=/home/daytona/work/app\nbranch=agent/x\nbase=main\n{url}"

    def code(args):
        return 0 if coding.ARCHIVE_ENV in args.get("env", {}) else 128

    out = await git_tools.git_checkout(_ctx(calls, stdout=stdout, exit_code=code), "octo/app", "agent/x")
    assert out["auth"] == "composio_snapshot" and out["exit_code"] == 0 and "snapshot" in out["note"]
    snap_call = calls[-1][2]
    assert snap_call["env"] == {coding.ARCHIVE_ENV: url} and url not in snap_call["command"]
    assert "SECRETURL" not in json.dumps(out)


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
    # only the read-only preview ran; nothing was pushed
    assert all("push" not in c[2]["command"] for c in calls)


async def test_confirmed_push_pushes_and_opens_pr(app_on, monkeypatch):
    seen = {}

    async def fake_call(row, name, args):
        seen[name] = args
        return {"number": 9, "url": "https://github.com/octo/app/pull/9"}

    monkeypatch.setattr(gh, "call_github", fake_call)
    calls = []
    args = {"repo": "octo/app", "branch": "agent/fix-login", "open_pr": True, "title": "Fix login", "body": "b", "base": "", "draft": False}
    ctx = _ctx(calls, stdout="To https://github.com/octo/app.git\n*\trefs/heads/agent/fix-login:refs/heads/agent/fix-login\t[new branch]\nDone")
    out = await git_tools.run_confirmed(ctx, "push_branch", args)
    assert out["pushed"] is True and out["via"] == "git" and out["pr_url"].endswith("/pull/9")
    cmd = calls[0][2]["command"]
    assert "push --porcelain origin refs/heads/agent/fix-login:refs/heads/agent/fix-login" in cmd
    assert "--force" not in cmd and TOKEN not in cmd
    assert calls[0][2]["env"][coding.TOKEN_ENV] == TOKEN
    assert seen["create_pull_request"]["head"] == "agent/fix-login"


async def test_confirmed_push_without_token_goes_through_github_api(api_only, monkeypatch):
    bundle_path = "/home/daytona/work/_push/app-agent-fix.json"
    bundle = {
        "parent": "b" * 40,
        "head": "e" * 40,
        "message": "Fix it",
        "files": [{"path": "a.py", "content_base64": base64.b64encode(b"x=1\n").decode(), "mode": "100644"}, {"path": "gone.txt", "delete": True}],
    }
    committed = {}

    async def commit_files(base, auth, *, branch, files, message, start=None, parent_sha=None):
        committed.update(base=base, auth=auth, branch=branch, files=files, message=message, parent_sha=parent_sha)
        return {"branch": branch, "commit": "c" * 40, "parent": parent_sha, "files": len(files), "created_branch": True}

    async def fake_call(row, name, args):
        if name == "create_pull_request":
            raise gh.GitHubError(422, "A pull request already exists")
        return {}

    monkeypatch.setattr(gh, "commit_files", commit_files)
    monkeypatch.setattr(gh, "call_github", fake_call)
    calls = []

    def stdout(args):
        if "_push" in args["command"] and "mark" not in args["command"]:
            return f"bundle={bundle_path}\nfiles=2\nbytes=10\nhead={'e' * 40}\nparent={'b' * 40}"
        return "ok"

    ctx = _ctx(calls, stdout=stdout, files={bundle_path: json.dumps(bundle).encode()})
    out = await git_tools.run_confirmed(ctx, "push_branch", {"repo": "octo/app", "branch": "agent/fix", "open_pr": True, "title": "Fix"})
    assert out["pushed"] is True and out["via"] == "github_api" and out["commit"] == "c" * 40
    assert committed["parent_sha"] == "b" * 40 and committed["branch"] == "agent/fix" and committed["auth"] is ACCT
    assert committed["base"] == "/repos/octo/app" and len(committed["files"]) == 2
    assert "already exists" in out["pull_request_note"]
    assert ("computer", "read_bytes", {"path": bundle_path}) in calls
    assert all(" push " not in c[2].get("command", "") for c in calls)  # no git push without a token


async def test_api_push_refuses_unexpected_bundle_path(api_only):
    calls = []
    ctx = _ctx(calls, stdout="bundle=/etc/passwd\nfiles=1")
    out = await git_tools.api_push(ctx, "octo/app", "app", "agent/fix", "")
    assert out["pushed"] is False and "bundle path" in out["error"]
    assert not [c for c in calls if c[1] == "read_bytes"]


async def test_confirmation_for_one_branch_does_not_cover_another(app_on):
    calls = []
    conf = [{"slug": coding.CODING_SLUG, "tool": "push_branch", "args": {"repo": "octo/app", "branch": "agent/a"}}]
    ctx = _ctx(calls, confirmations=conf)
    out = await git_tools.push_branch(ctx, "octo/app", "agent/b")
    assert out["status"] == "awaiting_user_confirmation"


async def test_push_without_connection_is_blocked_with_connect_steps(monkeypatch):
    _connect(monkeypatch, connected=False)
    calls = []
    ctx = _ctx(calls, stdout="creds=none")
    out = await git_tools.push_branch(ctx, "octo/app", "agent/x")
    assert out["status"] == "blocked" and "Composio" in out["connect"]
    assert "waiting_for='github'" in out["next"] and ctx.pending == []


async def test_push_without_any_credentials_says_how_to_fix(monkeypatch):
    _connect(monkeypatch, connected=False)

    async def fail_call(slug, tool, args):
        return {"exit_code": 128, "stdout": "fatal: could not read Username"}

    ctx = _ctx([])
    ctx.call_impl = fail_call
    out = await git_tools.run_push(ctx, {"repo": "octo/app", "branch": "agent/x", "open_pr": True})
    assert out["pushed"] is False and out["via"] == "sandbox_credentials"
    assert "Connectors" in out["hint"] and "Composio" in out["hint"]


async def test_github_status_never_carries_the_token(app_on):
    st = await git_tools.github_status(_ctx([]))
    assert st["connected"] and st["login"] == "octo" and st["git_transport"] == "git"
    assert TOKEN not in json.dumps(st)
    assert git_tools.identity(st) == ("Octo Cat", "77+octo@users.noreply.github.com")
    assert git_tools.identity({}) == (coding.AUTHOR_NAME, coding.AUTHOR_EMAIL)


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


def test_readiness_points_to_composio_when_github_not_connected():
    conns = {"computer": SimpleNamespace(slug="computer", name="Computer", provider="daytona", alias="", description="", tags=[])}
    out = coding.readiness(conns, {"git": "git 2"}, "", {"configured": True, "connected": False})
    assert any("Connectors" in m and "Composio" in m for m in out["missing"])
    ok = coding.readiness(conns, {"git": "git 2"}, "", {"configured": True, "connected": True, "login": "octo", "git_transport": "api"})
    assert any("@octo" in c for c in ok["can"])
    assert not any("GitHub" in m for m in ok["missing"])
    assert ok["github"]["via"] and ok["github"]["connected"] is True


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
