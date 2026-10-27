"""git_checkout fallback chain (token -> anonymous/gh creds -> Composio snapshot -> trees API -> gh CLI sign-in),
ensure_checkout before deploy / inspect, and the trees-API archive."""

import base64
import io
import os
import shutil
import subprocess
import tarfile
import uuid
from types import SimpleNamespace

import pytest

from app.agent import cli_login, coding, git_tools
from app.connectors import github as gh


def _ctx():
    events = []

    async def record(kind, text, data=None):
        events.append((kind, text, data or {}))

    return SimpleNamespace(user=SimpleNamespace(id=uuid.uuid4()), connectors={}, job=None, question=None,
                           progress=SimpleNamespace(record=record), events=events)


@pytest.fixture
def chain(monkeypatch):
    st = SimpleNamespace(token="tok", connected=False, clones=[], results=[], login={"status": "logged_in"}, logins=0,
                         snap=None, tree=None)

    async def git_token(ctx):
        return st.token

    async def sandbox_exec(ctx, script, timeout, token=None, env=None, secret_env=None):
        st.clones.append({"authed": "x-access-token" in script or bool(token), "token": token, "script": script})
        return st.results.pop(0) if st.results else {"exit_code": 0, "output": "repo_dir=/home/u/work/testing\nbranch=main\nbase=main"}

    async def github_status(ctx):
        return {"connected": st.connected}

    async def snap(ctx, *a):
        return st.snap

    async def tree(ctx, *a):
        return st.tree

    async def ensure_login(ctx, key, why=""):
        st.logins += 1
        return st.login

    monkeypatch.setattr(git_tools, "git_token", git_token)
    monkeypatch.setattr(git_tools, "sandbox_exec", sandbox_exec)
    monkeypatch.setattr(git_tools, "github_status", github_status)
    monkeypatch.setattr(git_tools, "_snapshot_checkout", snap)
    monkeypatch.setattr(git_tools, "_tree_checkout", tree)
    monkeypatch.setattr(cli_login, "ensure_login", ensure_login)
    return st


FAIL = {"exit_code": 128, "output": "remote: Invalid username or token.\nfatal: Authentication failed"}
OK = {"exit_code": 0, "output": "repo_dir=/home/u/work/testing\nbranch=main\nbase=main"}


async def test_bad_composio_token_falls_back_to_anonymous_clone(chain):
    chain.results = [dict(FAIL), dict(OK)]
    res = await git_tools.git_checkout(_ctx(), "specialshweta26/testing")
    assert res["exit_code"] == 0 and res["auth"] == "public_or_sandbox_credentials"
    assert chain.clones[0]["token"] == "tok" and chain.clones[1]["token"] is None
    assert chain.logins == 0


async def test_all_routes_fail_hands_over_github_cli_login(chain):
    chain.token = ""
    chain.results = [dict(FAIL)]
    chain.login = {"status": "awaiting_user_login", "platform": "github", "url": "https://github.com/login/device", "code": "ABCD-1234"}
    res = await git_tools.git_checkout(_ctx(), "specialshweta26/testing")
    assert res["status"] == "awaiting_user_login" and res["url"] == "https://github.com/login/device"
    assert res["repo"] == "specialshweta26/testing" and res["tried"] == ["public_or_sandbox_credentials"]


async def test_after_gh_cli_login_the_clone_is_retried(chain):
    chain.token = ""
    chain.results = [dict(FAIL), dict(OK)]
    res = await git_tools.git_checkout(_ctx(), "specialshweta26/testing")
    assert res["exit_code"] == 0 and res["auth"] == "gh_cli_session" and chain.logins == 1


async def test_composio_snapshot_then_trees_api(chain):
    chain.connected = True
    chain.results = [dict(FAIL), dict(FAIL)]
    chain.snap = {"exit_code": 1, "output": "", "error": "no tarball url"}
    chain.tree = {"exit_code": 0, "output": "repo_dir=/home/u/work/testing\nbranch=main\nbase=main\nsnapshot=1"}
    res = await git_tools.git_checkout(_ctx(), "specialshweta26/testing")
    assert res["exit_code"] == 0 and res["auth"] == "composio_snapshot" and "snapshot" in res["note"]
    assert chain.logins == 0


async def test_ensure_checkout_clones_only_when_missing(chain):
    ctx = _ctx()
    chain.results = [{"exit_code": 0, "output": "present=1"}]
    assert (await git_tools.ensure_checkout(ctx, "specialshweta26/testing")) == {
        "ok": True, "repo": "specialshweta26/testing", "checked_out": False}
    chain.results = [{"exit_code": 0, "output": "present=0"}, dict(OK)]
    res = await git_tools.ensure_checkout(ctx, "specialshweta26/testing")
    assert res["ok"] and res["checked_out"] and ctx.events[-1][1] == "Checked out specialshweta26/testing"
    chain.token = ""
    chain.results = [{"exit_code": 0, "output": "present=0"}, dict(FAIL)]
    chain.login = {"status": "awaiting_user_login", "url": "https://github.com/login/device", "code": "X"}
    res = await git_tools.ensure_checkout(ctx, "specialshweta26/testing")
    assert res["status"] == "awaiting_user_login"


async def test_tree_archive_rebuilds_a_tarball(monkeypatch):
    sha = "a" * 40
    files = {"README.md": b"# hi\n", "app/main.py": b"print('x')\n"}

    async def account_for(user_id):
        return "acct"

    async def default_branch(path, acct):
        return "main"

    async def request(method, path, auth=None, params=None, **kw):
        if "/git/ref/heads/" in path:
            return {"object": {"sha": sha}}
        if "/git/trees/" in path:
            return {"tree": [{"type": "blob", "path": p, "sha": p, "size": len(c), "mode": "100644"} for p, c in files.items()]
                    + [{"type": "tree", "path": "app"}]}
        p = path.rsplit("/git/blobs/", 1)[1]
        return {"encoding": "base64", "content": base64.b64encode(files[p]).decode()}

    monkeypatch.setattr(gh, "account_for", account_for)
    monkeypatch.setattr(gh, "_default_branch", default_branch)
    monkeypatch.setattr(gh, "_request", request)
    src = await gh.tree_archive(uuid.uuid4(), "specialshweta26/testing")
    assert src["sha"] == sha and src["ref"] == "main" and src["files"] == 2
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(src["archive_b64"]))) as tar:
        names = sorted(tar.getnames())
    assert names == ["testing-aaaaaaa/README.md", "testing-aaaaaaa/app/main.py"]
    with pytest.raises(Exception):
        await gh.tree_archive(uuid.uuid4(), "specialshweta26/testing", max_files=1)


@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_snapshot_script_unpacks_an_archive_file(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"hello\n"
        info = tarfile.TarInfo("testing-abc/README.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    arc = tmp_path / "a.tgz.b64"
    arc.write_text(base64.b64encode(buf.getvalue()).decode())
    env = {**os.environ, "HOME": str(tmp_path), coding.ARCHIVE_FILE_ENV: str(arc)}
    out = subprocess.run(["bash", "-c", coding.snapshot_script("testing", "main", "b" * 40, "main", "agent/x")],
                         capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (tmp_path / "work/testing/README.md").read_text() == "hello\n"
    assert "branch=agent/x" in out.stdout and not arc.exists()
