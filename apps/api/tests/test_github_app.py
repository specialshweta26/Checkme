import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.agent.deep import RunContext, call_user_tool, git_write_gate
from app.config import get_settings
from app.connectors import github_app as gh

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
).decode()
INSTALLS = [gh.Installation(42, "octo", "User", "selected")]


@pytest.fixture
def app_env(monkeypatch):
    s = get_settings()
    for key, val in {
        "github_app_id": "1234",
        "github_app_private_key": PEM.replace("\n", "\\n"),
        "github_app_slug": "checkme-test",
        "github_app_client_id": "Iv1.abc",
        "github_app_client_secret": "shh",
        "github_app_webhook_secret": "hook",
    }.items():
        monkeypatch.setattr(s, key, val)
    yield s
    gh.set_transport(None)


class FakeGitHub:
    """Tiny GitHub REST fake on top of httpx.MockTransport."""

    def __init__(self):
        self.calls = []
        self.token_requests = []
        self.refs = {"main": "sha-main"}
        self.pr8 = {
            "number": 8, "title": "T", "state": "open", "draft": False, "merged": False, "mergeable": True,
            "mergeable_state": "clean", "html_url": "https://github.com/octo/app/pull/8", "user": {"login": "bot"},
            "head": {"ref": "agent/x", "sha": "headsha", "repo": {"full_name": "octo/app"}},
            "base": {"ref": "main", "repo": {"full_name": "octo/app"}},
        }
        self.check_runs = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        self.deleted = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.content else {}
        self.calls.append((request.method, path, body, request.headers.get("authorization", "")))
        if path.startswith("/app/installations/") and path.endswith("/access_tokens"):
            self.token_requests.append(body)
            n = len(self.token_requests)
            return httpx.Response(201, json={"token": f"ghs_FAKETOKEN{n:012d}", "expires_at": "2099-01-01T00:00:00Z"})
        if path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": [{"full_name": "octo/app", "private": True, "default_branch": "main", "html_url": "u"}]})
        if path == "/repos/octo/app/contents/README.md":
            return httpx.Response(200, json={"path": "README.md", "sha": "s", "encoding": "base64", "content": base64.b64encode(b"hello").decode()})
        if path == "/repos/octo/app/pulls" and request.method == "GET":
            return httpx.Response(200, json=[{"number": 7, "title": "Fix", "state": "open", "head": {"ref": "agent/fix"}, "base": {"ref": "main"}, "user": {"login": "bot"}, "html_url": "pr7"}])
        if path == "/repos/octo/app/pulls" and request.method == "POST":
            return httpx.Response(201, json={"number": 8, "html_url": "https://github.com/octo/app/pull/8"})
        if path == "/repos/octo/app":
            return httpx.Response(200, json={"default_branch": "main"})
        if path.startswith("/repos/octo/app/git/ref/heads/"):
            ref = path.rsplit("/heads/", 1)[1]
            if ref in self.refs:
                return httpx.Response(200, json={"object": {"sha": self.refs[ref]}})
            return httpx.Response(404, json={"message": "Not Found"})
        if path.startswith("/repos/octo/app/git/commits/") and request.method == "GET":
            return httpx.Response(200, json={"tree": {"sha": "tree-base"}})
        if path == "/repos/octo/app/git/trees":
            return httpx.Response(201, json={"sha": "tree-new"})
        if path == "/repos/octo/app/git/commits":
            return httpx.Response(201, json={"sha": "commit-new"})
        if path == "/repos/octo/app/git/refs":
            return httpx.Response(201, json={})
        if path == "/repos/octo/app/pulls/8/merge":
            return httpx.Response(200, json={"merged": True, "sha": "m"})
        if path == "/repos/octo/app/pulls/8" and request.method == "GET":
            return httpx.Response(200, json=self.pr8)
        if path == "/repos/octo/app/pulls/8/files":
            return httpx.Response(200, json=[{"filename": "app.py", "status": "modified", "additions": 2, "deletions": 1,
                                               "patch": "@@ -1,2 +1,3 @@\n a\n-b\n+c\n+d"}])
        if path == "/repos/octo/app/pulls/8/reviews" and request.method == "POST":
            return httpx.Response(200, json={"id": 1, "state": body.get("event"), "html_url": "https://github.com/octo/app/pull/8#r1"})
        if path == "/repos/octo/app/commits/headsha/check-runs":
            return httpx.Response(200, json={"check_runs": self.check_runs})
        if path == "/repos/octo/app/commits/headsha/status":
            return httpx.Response(200, json={"statuses": []})
        if path.startswith("/repos/octo/app/git/refs/heads/") and request.method == "DELETE":
            self.deleted.append(path.rsplit("/heads/", 1)[1])
            return httpx.Response(204)
        if path == "/user/installations":
            return httpx.Response(200, json={"installations": [{"id": 42}]})
        return httpx.Response(404, json={"message": f"unhandled {path}"})


@pytest.fixture
def fake(app_env):
    f = FakeGitHub()
    gh.set_transport(httpx.MockTransport(f.handler))
    return f


ROW = SimpleNamespace(user_id="u1")


def test_unconfigured_degrades(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "github_app_id", "")
    assert not gh.is_configured()
    assert "GITHUB_APP_ID" in gh.missing_env()


async def test_unconfigured_call_explains_fallback(monkeypatch):
    monkeypatch.setattr(get_settings(), "github_app_id", "")
    with pytest.raises(gh.ToolError) as exc:
        await gh.call_github_app(ROW, "list_repos", {}, installs=[])
    assert "Composio" in str(exc.value)


def test_app_jwt_is_rs256_and_short(app_env):
    now = time.time()
    token = gh.app_jwt(now)
    claims = jwt.decode(token, KEY.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "1234"
    assert claims["exp"] - claims["iat"] <= 600


def test_private_key_accepts_base64(app_env, monkeypatch):
    monkeypatch.setattr(app_env, "github_app_private_key", base64.b64encode(PEM.encode()).decode())
    assert gh.private_key().startswith("-----BEGIN")


async def test_token_is_scoped_to_one_repo_and_cached(fake):
    t1 = await gh.repo_token("u1", "octo/app", write=False, installs=INSTALLS)
    t2 = await gh.repo_token("u1", "octo/app", write=False, installs=INSTALLS)
    assert t1 == t2
    assert len(fake.token_requests) == 1
    req = fake.token_requests[0]
    assert req["repositories"] == ["app"]
    assert req["permissions"]["contents"] == "read"
    await gh.repo_token("u1", "octo/app", write=True, installs=INSTALLS)
    assert fake.token_requests[1]["permissions"]["contents"] == "write"


async def test_repo_on_uninstalled_owner_offers_install_link(fake):
    with pytest.raises(gh.ToolError) as exc:
        await gh.call_github_app(ROW, "list_prs", {"repo": "someoneelse/x"}, installs=INSTALLS)
    assert "/api/connectors/github/install" in str(exc.value)


async def test_read_tools(fake):
    repos = await gh.call_github_app(ROW, "list_repos", {}, installs=INSTALLS)
    assert repos["repos"][0]["full_name"] == "octo/app"
    f = await gh.call_github_app(ROW, "get_file", {"repo": "octo/app", "path": "README.md"}, installs=INSTALLS)
    assert f["content"] == "hello"
    prs = await gh.call_github_app(ROW, "list_prs", {"repo": "octo/app"}, installs=INSTALLS)
    assert prs["pull_requests"][0]["head"] == "agent/fix"
    # results never carry the installation token
    assert "ghs_" not in json.dumps([repos, f, prs])


async def test_push_files_only_to_agent_branches(fake):
    with pytest.raises(gh.ToolError):
        await gh.call_github_app(
            ROW, "push_files", {"repo": "octo/app", "branch": "main", "message": "x", "files": [{"path": "a", "content": "b"}]}, installs=INSTALLS
        )
    out = await gh.call_github_app(
        ROW,
        "push_files",
        {"repo": "octo/app", "branch": "agent/fix-typo", "message": "Fix typo", "files": [{"path": "a.txt", "content": "b"}]},
        installs=INSTALLS,
    )
    assert out["created_branch"] is True and out["commit"] == "commit-new"
    ref_call = [c for c in fake.calls if c[1] == "/repos/octo/app/git/refs"][0]
    assert ref_call[2]["ref"] == "refs/heads/agent/fix-typo"


async def test_open_pr_and_merge(fake):
    pr = await gh.call_github_app(ROW, "create_pull_request", {"repo": "octo/app", "head": "agent/x", "title": "T"}, installs=INSTALLS)
    assert pr["url"].endswith("/pull/8") and pr["base"] == "main"
    merged = await gh.call_github_app(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8}, installs=INSTALLS)
    assert merged["merged"] is True and merged["method"] == "merge" and merged["sha"] == "m"
    put = [c for c in fake.calls if c[0] == "PUT"][-1]
    assert put[2] == {"merge_method": "merge", "sha": "headsha"}


async def test_merge_squash_and_delete_branch(fake):
    out = await gh.call_github_app(
        ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "method": "squash", "delete_branch": True}, installs=INSTALLS
    )
    assert out["merged"] and out["method"] == "squash" and out["branch_deleted"] == "agent/x"
    assert fake.deleted == ["agent/x"]


async def test_merge_refuses_conflicts_with_fix_steps(fake):
    fake.pr8.update(mergeable=False, mergeable_state="dirty")
    st = await gh.call_github_app(ROW, "check_merge", {"repo": "octo/app", "number": 8}, installs=INSTALLS)
    assert st["conflicts"] and not st["ready"] and "merge conflicts with main" in st["blockers"][0]
    out = await gh.call_github_app(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8}, installs=INSTALLS)
    assert out["merged"] is False and out["blocked"] and "offer to fix the conflicts" in out["note"]
    assert not [c for c in fake.calls if c[0] == "PUT"]


async def test_merge_waits_for_failing_checks_unless_allowed(fake):
    fake.check_runs = [{"name": "tests", "status": "completed", "conclusion": "failure"}]
    out = await gh.call_github_app(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8}, installs=INSTALLS)
    assert out["merged"] is False and "Checks are failing: tests (failure)" in out["blockers"]
    out = await gh.call_github_app(
        ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "allow_failing_checks": True}, installs=INSTALLS
    )
    assert out["merged"] is True


async def test_merge_card_preview_shows_live_status(fake):
    from app.connectors.github_app import preview_live

    card = await preview_live(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "method": "rebase"}, installs=INSTALLS)
    assert card["method"] == "rebase" and card.get("ready") is True and card["checks"] == "passing"
    fake.pr8.update(mergeable=False, mergeable_state="dirty")
    card = await preview_live(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8}, installs=INSTALLS)
    assert card["ready"] is False and "merge conflicts" in card["blockers"][0]


def test_error_messages_scrub_tokens():
    assert "ghs_" not in gh._scrub("bad token ghs_abcdefghijklmnop1234")


def test_webhook_signature(app_env):
    body = b'{"action":"deleted"}'
    sig = "sha256=" + hmac.new(b"hook", body, hashlib.sha256).hexdigest()
    assert gh.verify_webhook(body, sig)
    assert not gh.verify_webhook(body, "sha256=bad")


def test_write_tools_are_destructive_reads_are_not():
    by = {t.name: t for t in gh.GITHUB_TOOLS}
    for name in gh.WRITE_TOOLS:
        assert by[name].destructive
    for name in ("list_repos", "get_file", "list_prs", "list_issues", "get_pull_request"):
        assert by[name].read_only and not by[name].destructive


def test_git_write_gate_covers_other_github_connectors():
    app = SimpleNamespace(provider="github_app", name="GitHub", alias="", server_url="")
    assert git_write_gate(app, "github-app", "merge_pull_request", {})
    assert not git_write_gate(app, "github-app", "list_prs", {})
    mcp = SimpleNamespace(provider="mcp", name="GitHub", alias="github", server_url="https://api.githubcopilot.com/mcp/")
    assert git_write_gate(mcp, "github", "merge_pull_request", {})
    assert git_write_gate(mcp, "github", "push_files", {})
    assert git_write_gate(mcp, "github", "create_pull_request", {})
    assert not git_write_gate(mcp, "github", "list_pull_requests", {})
    composio = SimpleNamespace(provider="composio", name="Composio", alias="", server_url="")
    assert git_write_gate(composio, "composio", "execute", {"tool_slug": "GITHUB_MERGE_A_PULL_REQUEST"})
    assert not git_write_gate(composio, "composio", "execute", {"tool_slug": "GMAIL_SEND_EMAIL"})
    slack = SimpleNamespace(provider="mcp", name="Slack", alias="slack", server_url="https://slack")
    assert not git_write_gate(slack, "slack", "push_notification", {})


async def test_git_writes_wait_for_approval_even_with_auto_confirm():
    called = []

    async def fake_call(slug, tool, args):
        called.append(tool)
        return {"merged": True}

    mcp = SimpleNamespace(
        id="1", name="GitHub", alias="github", slug="github", kind="mcp", provider="mcp",
        description="", server_key="mcp:github", health="ok", server_url="https://api.githubcopilot.com/mcp/",
    )
    ctx = RunContext(
        db=None, user=SimpleNamespace(id="u1", timezone="Asia/Kolkata"), connectors={"github": mcp},
        auto_confirm=True, call_impl=fake_call,
    )
    ctx.gateway_surface["github"] = {"execute": None}
    out = await call_user_tool(ctx, "github", "merge_pull_request", {"repo": "o/r", "number": 1})
    assert out["status"] == "awaiting_user_confirmation"
    assert called == [] and ctx.pending
    # reads still run straight through in auto_confirm runs
    await call_user_tool(ctx, "github", "list_pull_requests", {"repo": "o/r"})
    assert called == ["list_pull_requests"]


class _DB:
    def __init__(self, user):
        self.user = user

    async def get(self, model, key):
        return self.user


async def _run_callback(monkeypatch, fake, *, visible=True, code="c0de"):
    from app.connectors import github_routes as routes

    user = SimpleNamespace(id="11111111-1111-1111-1111-111111111111")
    saved = []

    async def pop_state(state):
        return {"user_id": user.id} if state == "good" else None

    async def save(db, u, data):
        saved.append(data)

    async def ensure(db, u):
        return None

    async def exchange(code):
        return "ghu_usertoken1234567890"

    async def ids(token):
        return {42} if visible else {99}

    async def app_get(path):
        return {"id": 42, "account": {"login": "octo", "type": "User"}, "repository_selection": "selected"}

    monkeypatch.setattr(routes, "_pop_state", pop_state)
    monkeypatch.setattr(routes, "save_installation", save)
    monkeypatch.setattr(routes, "ensure_connector", ensure)
    monkeypatch.setattr(gh, "exchange_code", exchange)
    monkeypatch.setattr(gh, "user_installation_ids", ids)
    monkeypatch.setattr(gh, "app_get", app_get)
    res = await routes.callback(
        SimpleNamespace(cookies={}), code=code, installation_id=42, setup_action="install", state="good", db=_DB(user)
    )
    return res.headers["location"], saved


async def test_callback_saves_verified_installation(app_env, monkeypatch):
    loc, saved = await _run_callback(monkeypatch, None)
    assert loc.endswith("/connectors?github=connected")
    assert saved and saved[0]["id"] == 42


async def test_callback_rejects_installation_user_cannot_see(app_env, monkeypatch):
    loc, saved = await _run_callback(monkeypatch, None, visible=False)
    assert "github_installation_not_yours" in loc and not saved


async def test_callback_requires_oauth_code(app_env, monkeypatch):
    loc, saved = await _run_callback(monkeypatch, None, code=None)
    assert "github_needs_user_authorization" in loc and not saved


def test_installation_fields():
    f = gh.installation_fields({"id": 5, "account": {"login": "acme", "type": "Organization"}, "repository_selection": "all", "suspended_at": None})
    assert f == {"installation_id": 5, "account_login": "acme", "account_type": "Organization", "repository_selection": "all", "permissions": {}, "suspended": False}
