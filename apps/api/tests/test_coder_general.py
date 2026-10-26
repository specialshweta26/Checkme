"""General-purpose Coder: explore, IDE tools, checks, review, PR/merge flow, follow-ups, memory, blocked auth."""

import os
import shutil
import subprocess
import uuid
from types import SimpleNamespace

import pytest

from app.agent import coding, coding_memory, dev_tools, git_tools
from app.agent.sandbox import overview as ov
from app.jobs import service


def _agent(name="Coder"):
    return SimpleNamespace(id=uuid.uuid4(), name=name, focus="coding")


def _job(**kw):
    return service.new_job(uuid.uuid4(), agent=_agent(), prompt=kw.pop("prompt", "Fix the bug in a/b"), **kw)


class Rec:
    def __init__(self):
        self.items = []

    async def record(self, kind, text, data=None):
        self.items.append((kind, text, data or {}))


def _ctx(**kw):
    base = dict(
        progress=Rec(), confirmations=[], pending=[], prs=[], merged=[], repo="", overviews={}, check_results={},
        check_attempts={}, user=SimpleNamespace(id=uuid.uuid4()), db=None, agent=None, connectors={},
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- 1/2/5: job questions, merge question, answers, follow-ups ---------------------------------------


def test_root_chat_job_with_a_pr_ends_by_asking_merge_it():
    job = _job(kind="chat")
    pr = {"url": "https://github.com/a/b/pull/5", "repo": "a/b", "number": 5, "branch": "agent/x", "title": "Fix"}
    events = service.apply_outcome(job, "q", {"text": "Opened #5", "prs": [pr], "repo": "a/b"})
    assert job.status == "waiting_input" and job.meta["repo"] == "a/b"
    q = job.meta["question"]
    assert q["question"] == "Merge it? (a/b#5)" and q["options"] == ["Yes, merge it", "No, leave it open"]
    assert [e.kind for e in events] == ["result", "question"]
    service.answer(job, "Yes, merge it")
    assert job.status == "queued" and "check_merge" in job.meta["next_query"]
    # second pass: merged, no second question
    service.apply_outcome(job, "go", {"text": "Merged", "merged": [{"repo": "a/b", "number": 5, "sha": "abc"}]})
    assert job.status == "done" and job.meta["prs"][0]["merged"] and job.meta["prs"][0]["merge_sha"] == "abc"


def test_no_is_final_and_child_jobs_never_ask():
    job = _job(kind="chat")
    service.apply_outcome(job, "q", {"text": "PR", "prs": [{"url": "u1", "repo": "a/b", "number": 1}]})
    service.answer(job, "No, leave it open")
    service.apply_outcome(job, "n", {"text": "Left open"})
    assert job.status == "done"  # asked once only
    root = _job(kind="chat")
    child = service.new_job(root.user_id, agent=_agent(), prompt="build", role="coder", kind="delegate", parent=root)
    service.apply_outcome(child, "q", {"text": "PR", "prs": [{"url": "u2", "repo": "a/b", "number": 2}]})
    assert child.status == "done"


def test_ask_user_question_and_github_app_resume():
    job = _job(kind="chat")
    service.apply_outcome(
        job, "q", {"text": "Blocked", "question": {"question": "Install the App?", "options": ["Done", "Later"], "waiting_for": "github_app"}}
    )
    assert job.status == "waiting_input" and job.meta["question"]["waiting_for"] == "github_app"
    assert service.resume_waiting_for(job, "other", "x") == []
    ev = service.resume_waiting_for(job, "github_app", "The user installed the Checkme GitHub App. Continue.")
    assert ev and job.status == "queued" and "installed" in job.meta["next_query"]
    bad = _job()
    service.apply_outcome(bad, "q", {"text": "x", "question": {"question": "only one?", "options": ["a"]}})
    assert bad.status == "done"  # a question needs 2+ options


def test_followups_queue_reopen_and_answer():
    job = _job(kind="chat")
    job.status = "running"
    service.followup(job, "also fix the README")
    assert job.meta["followups"] == ["also fix the README"]
    service.apply_outcome(job, "q", {"text": "step done"})
    assert job.status == "queued" and "also fix the README" in job.meta["next_query"]
    job.status = "done"
    service.followup(job, "now bump the version")
    assert job.status == "queued" and "same repo, same branch" in job.meta["next_query"]
    job.status = "cancelled"
    with pytest.raises(service.JobError):
        service.followup(job, "x")


def test_continues_job_routing():
    assert coding.continues_job("also fix the typo", "done", "a/b")
    assert coding.continues_job("merge it", "done", "a/b")
    assert coding.continues_job("anything at all", "waiting_input", "a/b")
    assert not coding.continues_job("what does it change?", "done", "a/b")
    assert not coding.continues_job("fix the bug in other/repo", "done", "a/b")
    assert not coding.continues_job("create a calendar event tomorrow", "done", "a/b")
    assert coding.first_step("review PR 4 in a/b").startswith("first I'll read the pull request")
    assert "check that the pull request can merge" in coding.first_step("merge a/b#4")


# --- 3/4: exploring and building ----------------------------------------------------------------------


def test_inspect_script_is_read_only_and_validated(tmp_path):
    with pytest.raises(coding.GitToolError):
        coding.inspect_script("r", "push")
    with pytest.raises(coding.GitToolError):
        coding.inspect_script("r", "read", path="-rf")
    s = coding.inspect_script("r", "grep", query="foo(bar)")
    assert "rg" in s and "-F" in s and "push" not in s


def test_commit_refuses_default_branch_and_uses_identity(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git missing")
    home = tmp_path
    repo = home / "work" / "r"
    repo.mkdir(parents=True)
    env = {**os.environ, "HOME": str(home), "GIT_CONFIG_GLOBAL": str(home / ".gitconfig")}
    subprocess.run("git init -q -b main . && git -c user.name=a -c user.email=b commit -q --allow-empty -m init", shell=True, cwd=repo, env=env, check=True)
    (repo / "f.txt").write_text("x")
    r = subprocess.run(["bash", "-c", coding.commit_script("r", "msg")], env=env, capture_output=True, text=True)
    assert r.returncode == 3 and "refusing to commit on main" in r.stdout
    subprocess.run("git checkout -q -b feat/x", shell=True, cwd=repo, env=env, check=True)
    r = subprocess.run(["bash", "-c", coding.commit_script("r", "msg", author=("octo", "1+octo@users.noreply.github.com"))], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    log = subprocess.run("git log -1 --format=%an/%ae", shell=True, cwd=repo, env=env, capture_output=True, text=True).stdout.strip()
    assert log == "octo/1+octo@users.noreply.github.com"


def test_push_branch_rules():
    assert coding.check_push_branch("agent/x") == "agent/x"
    assert coding.check_push_branch("feature/login", pr_branch="feature/login") == "feature/login"
    with pytest.raises(coding.GitToolError):
        coding.check_push_branch("main")
    with pytest.raises(coding.GitToolError):
        coding.check_push_branch("feature/login", pr_branch="feature/other")


async def test_push_to_named_pr_branch_is_verified(monkeypatch):
    from app.connectors import github_app as gh

    pr = {"state": "open", "head": "feature/login", "head_repo": "a/b", "url": "https://github.com/a/b/pull/9", "title": "Login"}

    async def fake_call(row, name, args, installs=None):
        assert name == "get_pull_request"
        return dict(pr)

    async def no_installs(ctx):
        return []

    async def fake_exec(ctx, script, timeout=0, token=None):
        return {"exit_code": 0, "output": "creds=ok\nabc1 change\n--\n 1 file changed"}

    async def tok(ctx, repo, write):
        return "ghs_x"

    monkeypatch.setattr(gh, "call_github_app", fake_call)
    monkeypatch.setattr(git_tools, "installs_for", no_installs)
    monkeypatch.setattr(git_tools, "sandbox_exec", fake_exec)
    monkeypatch.setattr(git_tools, "app_token", tok)
    ctx = _ctx()
    out = await git_tools.push_branch(ctx, "a/b", "feature/login", pr_number=9)
    assert out["status"] == "awaiting_user_confirmation" and out["preview"]["pr_number"] == 9 and out["preview"]["open_pr"] is False
    assert "updates PR #9" in ctx.pending[0]["title"]
    out = await git_tools.push_branch(_ctx(), "a/b", "feature/other", pr_number=9)
    assert "error" in out
    pr["head_repo"] = "fork/b"
    out = await git_tools.push_branch(_ctx(), "a/b", "feature/login", pr_number=9)
    assert "fork" in out["error"]
    pr.update(head_repo="a/b", state="closed")
    assert "not open" in (await git_tools.push_branch(_ctx(), "a/b", "feature/login", pr_number=9))["error"]


async def test_blocked_push_saves_patch_and_gives_install_link(monkeypatch):
    scripts = []

    async def fake_exec(ctx, script, timeout=0, token=None):
        scripts.append(script)
        if "creds=" in script:
            return {"exit_code": 0, "output": "creds=none"}
        return {"exit_code": 0, "output": "0001-fix.patch"}

    async def no_token(ctx, repo, write):
        return None

    async def status(ctx):
        return {"configured": True, "installed": False, "accounts": [], "install_url": "https://checkme/api/connectors/github/install"}

    monkeypatch.setattr(git_tools, "sandbox_exec", fake_exec)
    monkeypatch.setattr(git_tools, "app_token", no_token)
    monkeypatch.setattr(git_tools, "github_app_status", status)
    ctx = _ctx()
    out = await git_tools.push_branch(ctx, "a/b", "agent/fix")
    assert out["status"] == "blocked" and out["install_url"].endswith("/github/install")
    assert out["patch_dir"] == "~/work/_patches/b-agent-fix" and "waiting_for='github_app'" in out["next"]
    assert any("format-patch" in s for s in scripts) and not ctx.pending
    assert ctx.progress.items[-1][0] == "status"


async def test_pr_is_noted_on_ctx_and_in_memory(monkeypatch):
    notes = []

    async def fake_note(ctx, **kw):
        notes.append(kw)

    monkeypatch.setattr(coding_memory, "note", fake_note)
    ctx = _ctx()
    await git_tools._note_pr(ctx, "a/b", "agent/x", {"pr_url": "https://github.com/a/b/pull/4", "pr_number": 4}, {"title": "T"})
    assert ctx.prs[0]["number"] == 4 and ctx.repo == "a/b" and notes[0]["pr"]["url"].endswith("/4")


def test_coding_memory_block_roundtrip():
    data = coding_memory.update({}, repo="a/b", pr={"url": "https://github.com/a/b/pull/1", "repo": "a/b", "number": 1, "branch": "agent/x"})
    data = coding_memory.update(data, repo="c/d", prefs={"branch_convention": "feat/<slug>", "bogus": "x"})
    text = coding_memory.render(data)
    back = coding_memory.parse(text)
    assert back["default_repo"] == "a/b" and back["repos"].startswith("c/d") and back["branch_convention"] == "feat/<slug>"
    assert "a/b#1" in back["recent_prs"] and "bogus" not in text and len(text) <= coding_memory.MAX_CHARS


async def test_merge_through_app_is_tracked(monkeypatch):
    from app.agent import deep

    notes = []

    async def fake_note(ctx, **kw):
        notes.append(kw)

    monkeypatch.setattr(coding_memory, "note", fake_note)
    ctx = deep.RunContext(db=None, user=SimpleNamespace(id=uuid.uuid4()), connectors={}, progress=Rec())
    conn = SimpleNamespace(provider="github_app")
    await deep._track_github_writes(ctx, conn, "merge_pull_request", {"repo": "a/b", "number": 5}, {"merged": True, "sha": "abc123", "method": "merge"})
    assert ctx.merged == [{"repo": "a/b", "number": 5, "sha": "abc123", "method": "merge", "url": None}]
    assert "Merged a/b#5" in ctx.progress.items[-1][1] and notes[-1]["pr"]["state"] == "merged"
    await deep._track_github_writes(ctx, conn, "merge_pull_request", {"repo": "a/b", "number": 6}, {"merged": False, "blocked": True})
    assert len(ctx.merged) == 1
    await deep._track_github_writes(ctx, conn, "create_pull_request", {"repo": "a/b", "head": "agent/y"}, {"url": "https://github.com/a/b/pull/7", "number": 7})
    assert ctx.prs[-1]["number"] == 7


def test_merge_approval_must_match_the_exact_pr():
    from app.agent import deep

    conn = SimpleNamespace(provider="github_app")
    ctx = SimpleNamespace(confirmations=[{"slug": "github-app", "tool": "merge_pull_request", "args": {"repo": "a/b", "number": 5}}])
    assert deep._approved(ctx, conn, "github-app", "merge_pull_request", {"repo": "a/b", "number": 5, "method": "merge"})
    assert not deep._approved(ctx, conn, "github-app", "merge_pull_request", {"repo": "a/b", "number": 6})
    assert not deep._approved(ctx, conn, "github-app", "merge_pull_request", {"repo": "a/b", "number": 5, "method": "squash"})
    assert not deep._approved(ctx, conn, "github-app", "merge_pull_request", {"repo": "a/b", "number": 5, "delete_branch": True})


def test_coder_tools_are_registered():
    from app.agent import deep

    def names(**kw):
        ctx = deep.RunContext(db=None, user=SimpleNamespace(id=uuid.uuid4()), connectors={}, agent=SimpleNamespace(id=uuid.uuid4(), name="C", focus="coding"), **kw)
        return {t.name for t in deep.harness_tools(ctx)}

    got = names()
    for n in ("git_inspect", "ensure_dev_toolchain", "repo_overview", "find_symbol", "find_references", "read_file_range",
              "run_checks", "review_pull_request", "ask_user", "remember_coding", "push_branch"):
        assert n in got, n
    assert "ask_user" not in names(job=SimpleNamespace(id=uuid.uuid4(), depth=2))


# --- IDE: toolchain, overview, symbols, checks, review ---------------------------------------------------


def test_identity_prefers_the_users_github_account():
    user = SimpleNamespace(account_login="octo", account_type="User", suspended=False)
    org = SimpleNamespace(account_login="acme", account_type="Organization", suspended=False)
    assert dev_tools.identity_for([org, user]) == ("octo", "octo@users.noreply.github.com")
    assert dev_tools.identity_for([org]) == (coding.AUTHOR_NAME, coding.AUTHOR_EMAIL)


def _fake_bin(tmp_path, have=("bash", "sh", "git", "grep", "cat", "date", "sort", "rm", "mkdir", "id", "uname", "ln", "env", "jq", "python3")):
    b = tmp_path / "bin"
    b.mkdir()
    for c in have:
        p = shutil.which(c)
        if p:
            (b / c).symlink_to(p)
    return b


def test_toolchain_bootstrap_reports_and_caches(tmp_path):
    if not shutil.which("bash"):
        pytest.skip("bash missing")
    b = _fake_bin(tmp_path)
    env = {"HOME": str(tmp_path), "PATH": str(b)}
    run = lambda s: subprocess.run([str(b / "bash"), "-c", s], env=env, capture_output=True, text=True)  # noqa: E731
    first = dev_tools.parse_toolchain(run(dev_tools.toolchain_script([], "octo", "o@x")).stdout)
    assert not first["cached"] and "git" in first["present"] and "ripgrep" in first["failed"]
    assert first["identity"] == "octo <o@x>"
    assert "Could not install" in dev_tools.toolchain_summary(first)
    second = dev_tools.parse_toolchain(run(dev_tools.toolchain_script([], "octo", "o@x")).stdout)
    assert second["cached"] and "already set up" in dev_tools.toolchain_summary(second)
    # asking for an extra that is not there re-runs instead of trusting the cache
    third = dev_tools.parse_toolchain(run(dev_tools.toolchain_script(["go"])).stdout)
    assert not third["cached"] and "go" in third["failed"]
    assert "WANT_EXTRAS='';" in dev_tools.toolchain_script(["rust; rm -rf /"])


def test_repo_overview_prefers_ci_commands(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text(
        "jobs:\n  t:\n    defaults:\n      run:\n        working-directory: web\n    steps:\n      - run: npm ci\n"
        "      - name: Lint\n        run: npm run lint\n      - run: |\n          npm test -- --ci\n          npm run e2e\n"
    )
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "package.json").write_text(
        '{"scripts":{"lint":"eslint .","typecheck":"tsc --noEmit","test":"vitest","build":"next build"},"dependencies":{"next":"15","react":"19"}}'
    )
    (tmp_path / "api").mkdir()
    (tmp_path / "api" / "pyproject.toml").write_text('[project]\ndependencies=["fastapi"]\n[tool.pytest.ini_options]\n[tool.ruff]\n')
    (tmp_path / "api" / "main.py").write_text("app = 1\n")
    (tmp_path / "go.mod").write_text("module x\n")
    data = ov.analyze(str(tmp_path))
    cmds = [(c["kind"], c["command"], c["cwd"], c["source"].split(":")[0]) for c in data["commands"]]
    assert ("lint", "npm run lint", "web", "ci") in cmds and ("unit", "npm test -- --ci", "web", "ci") in cmds
    assert ("integration", "npm run e2e", "web", "ci") in cmds
    assert ("typecheck", "npm run typecheck", "web", "package.json") in cmds  # not in CI -> from the manifest
    assert not any(c[1] == "npm run test" for c in cmds)  # CI already covers unit tests in web
    assert ("unit", "python3 -m pytest -q", "api", "pyproject.toml") in cmds
    assert {"Next.js", "React", "FastAPI"} <= set(data["frameworks"]) and data["needs"] == ["go"]
    assert "api/main.py" in data["entry_points"] and data["ci"] == [".github/workflows/ci.yml"]
    plan = dev_tools.plan_checks(data, ["unit"])
    assert plan[0]["kind"] == "setup" and {p["cwd"] for p in plan if p["kind"] == "setup"} <= {p["cwd"] for p in plan if p["kind"] == "unit"}


def test_symbol_and_reference_scripts(tmp_path):
    with pytest.raises(coding.GitToolError):
        dev_tools.symbol_script("r", "x; rm -rf /")
    with pytest.raises(coding.GitToolError):
        dev_tools.references_script("r", "ok", path="-x")
    if not shutil.which("rg"):
        pytest.skip("ripgrep missing")
    repo = tmp_path / "work" / "r"
    repo.mkdir(parents=True)
    (repo / "a.py").write_text("def load_config(path):\n    return path\n\nx = load_config('y')\n")
    env = {**os.environ, "HOME": str(tmp_path)}
    out = subprocess.run(["bash", "-c", dev_tools.symbol_script("r", "load_config")], env=env, capture_output=True, text=True).stdout
    assert "a.py:1:def load_config" in out
    out = subprocess.run(["bash", "-c", dev_tools.references_script("r", "load_config")], env=env, capture_output=True, text=True).stdout
    assert "## 2 matches" in out and "a.py:4" in out


async def test_run_checks_iterates_and_feeds_the_pr_body(monkeypatch):
    calls = []
    fail = {"on": True}

    async def fake_exec(ctx, script, timeout=0, token=None):
        calls.append(script)
        if "pytest" in script and fail["on"]:
            return {"exit_code": 1, "output": "1 failed"}
        return {"exit_code": 0, "output": "ok"}

    monkeypatch.setattr(git_tools, "sandbox_exec", fake_exec)
    ctx = _ctx()
    ctx.overviews["a/b"] = {
        "commands": [
            {"kind": "setup", "command": "uv sync", "cwd": "api", "source": "uv.lock"},
            {"kind": "lint", "command": "uv run ruff check .", "cwd": "api", "source": "ci:ci.yml"},
            {"kind": "unit", "command": "uv run pytest -q", "cwd": "api", "source": "ci:ci.yml"},
        ]
    }
    out = await dev_tools.run_checks(ctx, "a/b")
    assert out["passed"] is False and out["attempt"] == 1 and "attempt 1 of 3" in out["next"]
    assert "| unit | `uv run pytest -q` (in `api`) | failed |" in out["table"]
    assert ctx.progress.items[-1][0] == "test"
    fail["on"] = False
    out = await dev_tools.run_checks(ctx, "a/b")
    assert out["passed"] and out["attempt"] == 2 and "next" not in out
    body = dev_tools.pr_body_with_tests(ctx, "a/b", "## Summary\nFix")
    assert "## Tests" in body and "All checks passed after 2 runs" in body and "| lint |" in body
    assert dev_tools.pr_body_with_tests(ctx, "a/b", "## Tests\nmine") == "## Tests\nmine"
    ctx.check_attempts["a/b"] = 2
    fail["on"] = True
    out = await dev_tools.run_checks(ctx, "a/b")
    assert "Stop iterating" in out["next"]


def test_review_comments_land_inline_only_inside_the_diff():
    files = [{"file": "app.py", "patch": "@@ -10,3 +10,4 @@\n ctx\n-old\n+new\n+more\n ctx2"}]
    assert dev_tools.diff_lines(files[0]["patch"]) == {10, 11, 12, 13}
    inline, loose = dev_tools.split_comments(
        [
            {"path": "app.py", "line": 11, "body": "off by one", "severity": "major"},
            {"path": "app.py", "line": 99, "body": "far away"},
            {"path": "other.py", "line": 1, "body": "not in PR"},
        ],
        files,
    )
    assert inline == [{"path": "app.py", "line": 11, "body": "**Major:** off by one"}]
    assert len(loose) == 2
    body = dev_tools.review_body("Looks fine overall.", loose, "REQUEST_CHANGES")
    assert "## Findings" in body and "`app.py:99`" in body and "**Verdict:** Changes requested" in body


async def test_review_pull_request_posts_through_the_app(monkeypatch):
    from app.connectors import github_app as gh

    posted = {}

    async def fake_call(row, name, args, installs=None):
        if name == "get_pull_request":
            return {"url": "https://github.com/a/b/pull/3", "files": [{"file": "x.py", "patch": "@@ -1 +1,2 @@\n a\n+b"}]}
        posted.update(args)
        return {"ok": True, "url": "https://github.com/a/b/pull/3#r1"}

    async def no_installs(ctx):
        return []

    monkeypatch.setattr(gh, "call_github_app", fake_call)
    monkeypatch.setattr(git_tools, "installs_for", no_installs)
    ctx = _ctx()
    out = await dev_tools.review_pull_request(
        ctx, "a/b", 3, "request changes", "Adds b.", [{"path": "x.py", "line": 2, "body": "b is unused", "severity": "minor"}]
    )
    assert out["ok"] and out["verdict"] == "REQUEST_CHANGES" and out["inline_comments"] == 1
    assert posted["event"] == "REQUEST_CHANGES" and posted["comments"][0]["line"] == 2 and "## Review summary" in posted["body"]
    assert (await dev_tools.review_pull_request(ctx, "a/b", 3, "LGTM"))["error"]


def test_readiness_mentions_toolchain_and_review():
    comp = SimpleNamespace(slug="computer", provider="daytona", name="Computer", alias="", description="")
    r = coding.readiness({"computer": comp}, {"git": "git 2", "dev_toolchain": "none"}, "", {"installed": True, "configured": True, "accounts": ["octo"]})
    assert any("ensure_dev_toolchain" in c for c in r["can"]) and r["dev_toolchain"] == "none"
    assert any("merge them through the Checkme GitHub App" in c for c in r["can"])
    r = coding.readiness({"computer": comp}, {"git": "git 2", "dev_toolchain": "ready"}, "", {})
    assert any("run_checks" in c for c in r["can"])


async def test_installing_the_app_resumes_blocked_jobs(monkeypatch):
    from app.connectors import github_routes
    from app.jobs import worker

    woke = []
    monkeypatch.setattr(worker, "wake", lambda: woke.append(1))
    blocked = _job(kind="chat")
    service.apply_outcome(blocked, "q", {"text": "x", "question": {"question": "Install?", "options": ["Done", "Later"], "waiting_for": "github_app"}})
    merge_wait = _job(kind="chat")
    service.apply_outcome(merge_wait, "q", {"text": "PR", "prs": [{"url": "u", "repo": "a/b", "number": 1}]})

    class Res:
        def scalars(self):
            return self

        def all(self):
            return [blocked, merge_wait]

    class DB:
        added = []

        async def execute(self, q):
            return Res()

        def add(self, o):
            self.added.append(o)

        async def commit(self):
            return None

    n = await github_routes.resume_blocked_jobs(DB(), SimpleNamespace(id=uuid.uuid4()), "octo")
    assert n >= 1 and woke and blocked.status == "queued" and "on octo" in blocked.meta["next_query"]
    assert merge_wait.status == "waiting_input"  # the merge question is untouched
