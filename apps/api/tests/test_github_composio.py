"""GitHub through Composio: proxy routing, account lookup, token masking, tools and the git-write gates."""

import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from app.agent.deep import RunContext, call_user_tool, git_write_gate
from app.config import get_settings
from app.connectors import github as gh

HEAD = "a" * 40
MAIN = "b" * 40
FULL_TOKEN = "gho_" + "Z" * 36


class FakeGitHub:
    """Tiny GitHub REST fake. Reached only through FakeComposio's proxy."""

    def __init__(self):
        self.calls = []
        self.refs = {"main": MAIN}
        self.pr8 = {
            "number": 8, "title": "T", "state": "open", "draft": False, "merged": False, "mergeable": True,
            "mergeable_state": "clean", "html_url": "https://github.com/octo/app/pull/8", "user": {"login": "bot"},
            "head": {"ref": "agent/x", "sha": HEAD, "repo": {"full_name": "octo/app"}},
            "base": {"ref": "main", "repo": {"full_name": "octo/app"}},
        }
        self.check_runs = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        self.deleted = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}") if request.content else {}
        self.calls.append((request.method, path, body, dict(request.url.params), dict(request.headers)))
        m = request.method
        if path == "/user":
            return httpx.Response(200, json={"login": "octo", "id": 77, "name": "Octo Cat"})
        if path == "/user/repos":
            return httpx.Response(200, json=[{"full_name": "octo/app", "private": True, "default_branch": "main", "html_url": "u", "permissions": {"push": True}}])
        if path == "/repos/octo/app/contents/README.md":
            return httpx.Response(200, json={"path": "README.md", "sha": "s", "encoding": "base64", "content": base64.b64encode(b"hello").decode()})
        if path == "/repos/octo/app/pulls" and m == "GET":
            return httpx.Response(200, json=[{"number": 7, "title": "Fix", "state": "open", "head": {"ref": "agent/fix"}, "base": {"ref": "main"}, "user": {"login": "bot"}, "html_url": "pr7"}])
        if path == "/repos/octo/app/pulls" and m == "POST":
            return httpx.Response(201, json={"number": 8, "html_url": "https://github.com/octo/app/pull/8"})
        if path == "/repos/octo/app":
            return httpx.Response(200, json={"default_branch": "main"})
        if path.startswith("/repos/octo/app/git/ref/heads/"):
            ref = path.split("/heads/", 1)[1]
            if ref in self.refs:
                return httpx.Response(200, json={"object": {"sha": self.refs[ref]}})
            return httpx.Response(404, json={"message": "Not Found"})
        if path.startswith("/repos/octo/app/git/commits/") and m == "GET":
            return httpx.Response(200, json={"tree": {"sha": "tree-base"}})
        if path == "/repos/octo/app/git/blobs":
            return httpx.Response(201, json={"sha": "blob-" + str(len(self.calls))})
        if path == "/repos/octo/app/git/trees":
            return httpx.Response(201, json={"sha": "tree-new"})
        if path == "/repos/octo/app/git/commits":
            return httpx.Response(201, json={"sha": "c" * 40})
        if path == "/repos/octo/app/git/refs":
            return httpx.Response(201, json={})
        if path.startswith("/repos/octo/app/git/refs/heads/") and m == "PATCH":
            return httpx.Response(200, json={})
        if path.startswith("/repos/octo/app/git/refs/heads/") and m == "DELETE":
            self.deleted.append(path.split("/heads/", 1)[1])
            return httpx.Response(204)
        if path == "/repos/octo/app/pulls/8/merge":
            return httpx.Response(200, json={"merged": True, "sha": "m"})
        if path == "/repos/octo/app/pulls/8" and m == "GET":
            return httpx.Response(200, json=self.pr8)
        if path == "/repos/octo/app/pulls/8/files":
            return httpx.Response(200, json=[{"filename": "app.py", "status": "modified", "additions": 2, "deletions": 1, "patch": "@@ -1 +1 @@\n-b\n+c"}])
        if path == f"/repos/octo/app/commits/{HEAD}/check-runs":
            return httpx.Response(200, json={"check_runs": self.check_runs})
        if path == f"/repos/octo/app/commits/{HEAD}/status":
            return httpx.Response(200, json={"statuses": []})
        if path.startswith("/repos/octo/app/tarball/"):
            return httpx.Response(302, headers={"location": "https://codeload.github.com/octo/app/legacy.tar.gz/x?token=TMP"})
        return httpx.Response(404, json={"message": f"unhandled {path}"})


class FakeComposio:
    """Composio v3.1: connected accounts + Proxy Execute forwarding to FakeGitHub."""

    def __init__(self, github: FakeGitHub):
        self.github = github
        self.accounts = [
            {"id": "ca_old", "status": "ACTIVE", "toolkit": {"slug": "github"}, "created_at": "2026-01-01", "auth_config": {"id": "ac_1", "is_composio_managed": True}},
            {"id": "ca_new", "status": "ACTIVE", "toolkit": {"slug": "github"}, "created_at": "2026-09-01", "auth_config": {"id": "ac_1", "is_composio_managed": True}},
            {"id": "ca_slack", "status": "ACTIVE", "toolkit": {"slug": "slack"}, "created_at": "2026-10-01"},
        ]
        self.access_token = "REDACTED"
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("x-api-key") == "ck_test"
        path = request.url.path.removeprefix("/api/v3.1")
        self.requests.append((request.method, path, dict(request.url.params)))
        if path == "/connected_accounts":
            return httpx.Response(200, json={"items": self.accounts})
        if path.startswith("/connected_accounts/"):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[1], "state": {"authScheme": "OAUTH2", "val": {"access_token": self.access_token}}})
        if path == "/tools/execute/proxy":
            body = json.loads(request.content)
            assert body["connected_account_id"] and body["user_id"]
            params = {p["name"]: p["value"] for p in body.get("parameters") or [] if p["type"] == "query"}
            headers = {p["name"]: p["value"] for p in body.get("parameters") or [] if p["type"] == "header"}
            assert "authorization" not in {k.lower() for k in headers}  # Composio injects the credential
            inner = httpx.Request(
                body["method"], body["endpoint"], params=params, headers=headers,
                content=json.dumps(body["body"]).encode() if "body" in body else None,
            )
            res = self.github.handler(inner)
            data = res.json() if res.content and res.headers.get("content-type", "").startswith("application/json") else None
            return httpx.Response(200, json={"data": data, "status": res.status_code, "headers": dict(res.headers)})
        return httpx.Response(404, json={"error": {"message": "nope"}})


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(get_settings(), "composio_api_key", "ck_test")
    monkeypatch.setattr(get_settings(), "composio_github_auth_config_id", "")
    g = FakeGitHub()
    c = FakeComposio(g)
    gh.set_transport(httpx.MockTransport(c.handler))
    yield SimpleNamespace(github=g, composio=c)
    gh.set_transport(None)


ROW = SimpleNamespace(user_id="u1")


async def test_not_configured_without_composio_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "composio_api_key", "")
    assert not gh.is_configured()
    with pytest.raises(gh.ToolError) as exc:
        await gh.call_github(ROW, "list_repos", {})
    assert "COMPOSIO_API_KEY" in str(exc.value)


async def test_not_connected_points_to_connectors_page(fake):
    fake.composio.accounts = [a for a in fake.composio.accounts if a["toolkit"]["slug"] != "github"]
    with pytest.raises(gh.NotConnected) as exc:
        await gh.call_github(ROW, "list_repos", {})
    assert "Connectors" in str(exc.value) and "Composio" in str(exc.value)


async def test_account_lookup_filters_and_prefers_newest_or_pinned(fake, monkeypatch):
    acct = await gh.account_for("u1")
    assert acct.id == "ca_new" and acct.user_id == "u1"
    method, path, params = fake.composio.requests[0]
    assert path == "/connected_accounts" and params["toolkit_slugs"] == "github" and params["statuses"] == "ACTIVE"
    assert params["user_ids"] == "u1"
    fake.composio.accounts[0]["auth_config"] = {"id": "ac_custom", "is_composio_managed": False}
    monkeypatch.setattr(get_settings(), "composio_github_auth_config_id", "ac_custom")
    acct = await gh.account_for("u1", fresh=True)
    assert acct.id == "ca_old" and acct.managed is False


async def test_reads_go_through_proxy_with_github_headers(fake):
    repos = await gh.call_github(ROW, "list_repos", {})
    assert repos["repos"][0]["full_name"] == "octo/app" and repos["repos"][0]["push"] is True
    f = await gh.call_github(ROW, "get_file", {"repo": "octo/app", "path": "README.md"})
    assert f["content"] == "hello"
    prs = await gh.call_github(ROW, "list_prs", {"repo": "octo/app"})
    assert prs["pull_requests"][0]["head"] == "agent/fix"
    method, path, _body, params, headers = fake.github.calls[0]
    assert path == "/user/repos" and params["per_page"] == "100"
    assert headers["accept"] == "application/vnd.github+json" and headers["x-github-api-version"] == "2022-11-28"
    assert all(p == "/tools/execute/proxy" for _, p, _ in fake.composio.requests[1:])


async def test_github_errors_keep_status(fake):
    with pytest.raises(gh.GitHubError) as exc:
        await gh.call_github(ROW, "get_file", {"repo": "octo/app", "path": "missing.txt"})
    assert exc.value.status == 404


def test_usable_token_rejects_masked_values():
    for masked in ("REDACTED", "", None, "gho_abc...", "gho_****", "not-a-token"):
        assert gh.usable_token(masked) == ""
    assert gh.usable_token(FULL_TOKEN) == FULL_TOKEN


async def test_git_token_only_when_unmasked(fake):
    assert await gh.git_token("u1") == ""  # Composio-managed auth: always REDACTED
    fake.composio.access_token = "gho_ZZZZ..."
    assert await gh.git_token("u1") == ""
    fake.composio.access_token = FULL_TOKEN
    assert await gh.git_token("u1") == FULL_TOKEN


async def test_viewer_gives_noreply_identity(fake):
    me = await gh.viewer("u1")
    assert me == {"login": "octo", "id": 77, "name": "Octo Cat", "email": "77+octo@users.noreply.github.com"}


async def test_push_files_only_to_agent_branches_with_binary_and_modes(fake):
    with pytest.raises(gh.ToolError):
        await gh.call_github(ROW, "push_files", {"repo": "octo/app", "branch": "main", "message": "x", "files": [{"path": "a", "content": "b"}]})
    out = await gh.call_github(
        ROW,
        "push_files",
        {
            "repo": "octo/app",
            "branch": "agent/fix-typo",
            "message": "Fix typo",
            "files": [
                {"path": "a.txt", "content": "b"},
                {"path": "bin/run", "content_base64": base64.b64encode(b"\x00x").decode(), "mode": "100755"},
                {"path": "old.txt", "delete": True},
            ],
        },
    )
    assert out["created_branch"] is True and out["parent"] == MAIN
    tree = [c for c in fake.github.calls if c[1] == "/repos/octo/app/git/trees"][0][2]
    items = {i["path"]: i for i in tree["tree"]}
    assert items["a.txt"]["content"] == "b"
    assert items["bin/run"]["mode"] == "100755" and items["bin/run"]["sha"].startswith("blob-")
    assert items["old.txt"]["sha"] is None
    ref_call = [c for c in fake.github.calls if c[1] == "/repos/octo/app/git/refs"][0]
    assert ref_call[2]["ref"] == "refs/heads/agent/fix-typo"


async def test_commit_files_never_forces_and_detects_a_moved_branch(fake):
    fake.github.refs["agent/x"] = HEAD
    out = await gh.commit_files("/repos/octo/app", await gh.account_for("u1"), branch="agent/x", files=[{"path": "a", "content": "1"}], message="m", parent_sha=HEAD)
    patch = [c for c in fake.github.calls if c[0] == "PATCH"][-1]
    assert patch[2]["force"] is False and out["created_branch"] is False
    with pytest.raises(gh.ToolError) as exc:
        await gh.commit_files("/repos/octo/app", await gh.account_for("u1"), branch="agent/x", files=[{"path": "a", "content": "1"}], message="m", parent_sha=MAIN)
    assert "moved" in str(exc.value)
    for bad in ("../x", ".git/config"):
        with pytest.raises(gh.ToolError):
            await gh.commit_files("/repos/octo/app", await gh.account_for("u1"), branch="agent/y", files=[{"path": bad, "content": "1"}], message="m")


async def test_open_pr_and_merge_pinned_to_reviewed_sha(fake):
    pr = await gh.call_github(ROW, "create_pull_request", {"repo": "octo/app", "head": "agent/x", "title": "T"})
    assert pr["url"].endswith("/pull/8") and pr["base"] == "main"
    with pytest.raises(gh.ToolError):
        await gh.call_github(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8})  # head_sha required
    merged = await gh.call_github(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": HEAD})
    assert merged["merged"] is True and merged["method"] == "merge"
    put = [c for c in fake.github.calls if c[0] == "PUT"][-1]
    assert put[2] == {"merge_method": "merge", "sha": HEAD}


async def test_merge_refuses_when_branch_moved_since_review(fake):
    out = await gh.call_github(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": "d" * 40})
    assert out["merged"] is False and "moved" in out["blockers"][0]
    assert not [c for c in fake.github.calls if c[0] == "PUT"]


async def test_merge_squash_and_delete_branch(fake):
    out = await gh.call_github(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": HEAD, "method": "squash", "delete_branch": True})
    assert out["merged"] and out["method"] == "squash" and out["branch_deleted"] == "agent/x"
    assert fake.github.deleted == ["agent/x"]


async def test_merge_refuses_conflicts_and_failing_checks(fake):
    fake.github.pr8.update(mergeable=False, mergeable_state="dirty")
    st = await gh.call_github(ROW, "check_merge", {"repo": "octo/app", "number": 8})
    assert st["conflicts"] and not st["ready"] and st["head_sha"] == HEAD
    out = await gh.call_github(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": HEAD})
    assert out["merged"] is False and out["blocked"]
    fake.github.pr8.update(mergeable=True, mergeable_state="clean")
    fake.github.check_runs = [{"name": "tests", "status": "completed", "conclusion": "failure"}]
    out = await gh.call_github(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": HEAD})
    assert out["merged"] is False and "Checks are failing: tests (failure)" in out["blockers"]
    out = await gh.call_github(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": HEAD, "allow_failing_checks": True})
    assert out["merged"] is True


async def test_merge_card_preview_shows_live_status_and_moved_head(fake):
    card = await gh.preview_live(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "method": "rebase", "head_sha": HEAD})
    assert card["method"] == "rebase" and card.get("ready") is True and card["checks"] == "passing"
    card = await gh.preview_live(ROW, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": "d" * 40})
    assert card["ready"] is False and any("moved" in b for b in card["blockers"])


async def test_snapshot_source_returns_redirect_url(fake):
    src = await gh.snapshot_source("u1", "octo/app", "agent/new", "")
    assert src["ref"] == "main" and src["sha"] == MAIN and src["base"] == "main"
    assert src["url"].startswith("https://codeload.github.com/")


def test_error_messages_scrub_tokens():
    assert "ghs_" not in gh._scrub("bad token ghs_abcdefghijklmnop1234")
    assert FULL_TOKEN not in gh._scrub(f"x {FULL_TOKEN}")


def test_write_tools_are_destructive_reads_are_not():
    by = {t.name: t for t in gh.GITHUB_TOOLS}
    for name in gh.WRITE_TOOLS:
        assert by[name].destructive
    for name in ("list_repos", "get_file", "list_prs", "list_issues", "get_pull_request"):
        assert by[name].read_only and not by[name].destructive
    assert "head_sha" in by["merge_pull_request"].input_schema["required"]


def test_git_write_gate_covers_composio_github_and_other_connectors():
    for provider in ("composio_github", "github_app"):
        conn = SimpleNamespace(provider=provider, name="GitHub", alias="", server_url="")
        assert git_write_gate(conn, gh.SLUG, "merge_pull_request", {})
        assert git_write_gate(conn, gh.SLUG, "push_files", {})
        assert not git_write_gate(conn, gh.SLUG, "list_prs", {})
    mcp = SimpleNamespace(provider="mcp", name="GitHub", alias="github", server_url="https://api.githubcopilot.com/mcp/")
    assert git_write_gate(mcp, "github", "merge_pull_request", {})
    assert not git_write_gate(mcp, "github", "list_pull_requests", {})
    composio = SimpleNamespace(provider="composio", name="Composio", alias="", server_url="")
    assert git_write_gate(composio, "composio", "execute", {"tool_slug": "GITHUB_MERGE_A_PULL_REQUEST"})
    assert git_write_gate(composio, "composio", "execute", {"tool_slug": "GITHUB_COMMIT_MULTIPLE_FILES"})
    assert not git_write_gate(composio, "composio", "execute", {"tool_slug": "GMAIL_SEND_EMAIL"})


def _gh_ctx(called, confirmations=None):
    async def fake_call(slug, tool, args):
        called.append((tool, args))
        return {"merged": True}

    conn = SimpleNamespace(
        id="1", name="GitHub", alias="", slug=gh.SLUG, kind="builtin", provider=gh.PROVIDER,
        description="", server_key="builtin:composio_github", health="ok", server_url="",
    )
    ctx = RunContext(
        db=None, user=SimpleNamespace(id="u1", timezone="Asia/Kolkata"), connectors={gh.SLUG: conn},
        auto_confirm=True, call_impl=fake_call, confirmations=list(confirmations or []),
    )
    ctx.gateway_surface[gh.SLUG] = {"execute": None}
    return ctx


async def test_composio_github_writes_wait_for_approval_even_with_auto_confirm(monkeypatch):
    from app.agent import invoke

    async def no_preview(user, conn, tool, args):
        return {}

    monkeypatch.setattr(invoke, "preview", no_preview)
    called = []
    ctx = _gh_ctx(called)
    out = await call_user_tool(ctx, gh.SLUG, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": HEAD})
    assert out["status"] == "awaiting_user_confirmation" and called == [] and ctx.pending
    await call_user_tool(ctx, gh.SLUG, "list_prs", {"repo": "octo/app"})
    assert [t for t, _ in called] == ["list_prs"]


async def test_approval_for_one_head_sha_does_not_cover_another(monkeypatch):
    from app.agent import invoke

    async def no_preview(user, conn, tool, args):
        return {}

    monkeypatch.setattr(invoke, "preview", no_preview)
    approved = {"slug": gh.SLUG, "tool": "merge_pull_request", "args": {"repo": "octo/app", "number": 8, "head_sha": HEAD}}
    called = []
    ctx = _gh_ctx(called, [approved])
    out = await call_user_tool(ctx, gh.SLUG, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": "d" * 40})
    assert out["status"] == "awaiting_user_confirmation" and called == []
    out = await call_user_tool(ctx, gh.SLUG, "merge_pull_request", {"repo": "octo/app", "number": 8, "head_sha": HEAD})
    assert called and called[-1][0] == "merge_pull_request"


async def test_runtime_routes_new_and_legacy_rows(fake):
    from app.connectors.runtime import RuntimeConnector

    for provider in ("composio_github", "github_app"):
        row = SimpleNamespace(id="1", kind="builtin", provider=provider, alias="", name="GitHub", slug=gh.SLUG, user_id="u1")
        out = await RuntimeConnector(row).call("list_repos", {})
        assert out["repos"][0]["full_name"] == "octo/app"


async def test_status_route_reports_transport_without_secrets(fake):
    from app.connectors import github_status

    user = SimpleNamespace(id="u1")
    st = await github_status.github_status(user)
    assert st["connected"] and st["login"] == "octo" and st["git_transport"] == "api" and st["managed_auth"] is True
    fake.composio.access_token = FULL_TOKEN
    st = await github_status.github_status(user)
    assert st["git_transport"] == "git" and FULL_TOKEN not in json.dumps(st)
    fake.composio.accounts = []
    st = await github_status.github_status(user)
    assert st["connected"] is False and st["configured"] is True


async def test_legacy_github_app_row_is_disabled_not_deleted():
    from app.connectors import platform

    old = SimpleNamespace(slug=gh.LEGACY_SLUG, kind="builtin", enabled=True, health_detail="")

    class DB:
        deleted = []

        async def execute(self, _q):
            return SimpleNamespace(scalar_one_or_none=lambda: old)

        async def flush(self):
            return None

        async def delete(self, row):
            self.deleted.append(row)

    db = DB()
    await platform._retire_github_app(db, SimpleNamespace(id="u1"))
    assert old.enabled is False and "Composio" in old.health_detail and db.deleted == []
    assert platform.PLATFORM["github_app"] is platform.PLATFORM[gh.PROVIDER]
    assert platform.PLATFORM[gh.PROVIDER]["slug"] == "composio-github"
