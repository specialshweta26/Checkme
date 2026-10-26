"""Safe git tools for coding agents: checkout, test, diff, commit and push_branch.

All of them run through Daytona exec on the user's one sandbox. When the Checkme
GitHub App is installed for the repo owner, each command gets a fresh installation
token (scoped to that repo) in the CHECKME_GIT_TOKEN env var only. Otherwise they
fall back to credentials already on the sandbox (gh auth / git credential helper),
or anonymous https for public repos, and point at the install link or the other
GitHub connectors for pushing.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent import coding

log = logging.getLogger(__name__)
PUSH_CONFIRM_TTL = 3 * 24 * 3600


def _err(exc: Exception) -> dict:
    return {"error": coding.scrub(str(exc))[:600]}


async def installs_for(ctx) -> list:
    from app.connectors import github_app as gh

    cached = getattr(ctx, "_github_installs", None)
    if cached is not None:
        return cached
    if not gh.is_configured():
        out: list = []
    else:
        try:
            out = await gh.load_installations(ctx.user.id)
        except Exception as exc:  # noqa: BLE001
            log.warning("github installations lookup failed: %s", exc)
            out = []
    try:
        ctx._github_installs = out
    except AttributeError:
        pass
    return out


async def github_app_status(ctx) -> dict:
    from app.connectors import github_app as gh

    if not gh.is_configured():
        return {"configured": False, "installed": False, "accounts": [], "install_url": ""}
    installs = await installs_for(ctx)
    live = [i for i in installs if not i.suspended]
    return {
        "configured": True,
        "installed": bool(live),
        "accounts": [i.account_login for i in live],
        "install_url": gh.checkme_install_link(),
    }


async def app_token(ctx, repo: str, *, write: bool) -> str | None:
    """Repo-scoped installation token, or None when the App does not cover this repo."""
    from app.connectors import github_app as gh

    if not gh.is_configured():
        return None
    installs = await installs_for(ctx)
    owner, _ = coding.split_repo(repo)
    if not any(i.account_login.lower() == owner.lower() and not i.suspended for i in installs):
        return None
    return await gh.repo_token(ctx.user.id, repo, write=write, installs=installs)


async def sandbox_exec(ctx, script: str, timeout: int = 300, token: str | None = None) -> dict:
    """Run a bash script on the computer without going through call_user_tool's arg logging."""
    from app.connectors.runtime import RuntimeConnector

    conn = ctx.connectors.get(coding.COMPUTER_SLUG)
    if conn is None:
        return {"error": coding.readiness(ctx.connectors)["missing"][0]}
    args: dict[str, Any] = {"command": coding.bash(script), "timeout": coding.clamp_timeout(timeout)}
    if token:
        args["env"] = {coding.TOKEN_ENV: token}
    try:
        if getattr(ctx, "call_impl", None) is not None:
            raw = await ctx.call_impl(coding.COMPUTER_SLUG, "exec", args)
        else:
            raw = await RuntimeConnector(conn).call("exec", args)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    raw = raw if isinstance(raw, dict) else {"stdout": str(raw)}
    if raw.get("error"):
        return {"error": coding.scrub(str(raw["error"]), [token or ""])[:600]}
    out = coding.scrub(str(raw.get("stdout") or raw.get("result") or ""), [token or ""])
    return {"exit_code": raw.get("exit_code"), "output": coding.clip_output(out)}


def _kv(output: str) -> dict:
    return coding.parse_probe(output)


async def git_checkout(ctx, repo: str, branch: str = "", base: str = "") -> dict:
    try:
        owner, name = coding.split_repo(repo)
        branch = coding.check_ref(branch)
        base = coding.check_ref(base, "base branch")
        token = await app_token(ctx, f"{owner}/{name}", write=False)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    res = await sandbox_exec(ctx, coding.checkout_script(owner, name, branch, base, authed=bool(token)), 600, token)
    if res.get("error"):
        return res
    info = _kv(res["output"])
    res.update(
        repo=f"{owner}/{name}",
        repo_dir=info.get("repo_dir", f"~/work/{name}"),
        branch=info.get("branch", branch),
        base=info.get("base", base),
        auth="github_app" if token else "sandbox_or_public",
    )
    if res.get("exit_code") not in (0, None) and not token:
        status = await github_app_status(ctx)
        if status.get("install_url"):
            res["hint"] = f"Private repo? Install the Checkme GitHub App on {owner}: {status['install_url']}"
    return res


async def _record(ctx, kind: str, text: str, data: dict | None = None) -> None:
    """Timeline entry for background jobs (diff, test output, PR links). No-op in plain chat."""
    rec = getattr(getattr(ctx, "progress", None), "record", None)
    if rec is None:
        return
    try:
        await rec(kind, text, data or {})
    except Exception:  # noqa: BLE001
        pass


async def run_tests(ctx, repo: str, command: str = "", timeout: int = 600) -> dict:
    try:
        _, name = coding.split_repo(repo)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    res = await sandbox_exec(ctx, coding.test_script(name, command or ""), timeout)
    if not res.get("error"):
        res["passed"] = res.get("exit_code") == 0
        await _record(
            ctx,
            "test",
            f"Tests {'passed' if res['passed'] else 'failed'} in {repo}",
            {"repo": repo, "passed": res["passed"], "output": str(res.get("output") or "")[-3000:]},
        )
    return res


async def git_diff(ctx, repo: str, base: str = "") -> dict:
    try:
        _, name = coding.split_repo(repo)
        base = coding.check_ref(base, "base branch")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    res = await sandbox_exec(ctx, coding.diff_script(name, base), 120)
    if not res.get("error"):
        await _record(ctx, "diff", f"Diff for {repo}", {"repo": repo, "output": str(res.get("output") or "")[:6000]})
    return res


async def git_commit(ctx, repo: str, message: str, paths: list[str] | None = None) -> dict:
    try:
        _, name = coding.split_repo(repo)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    if not (message or "").strip():
        return {"error": "A commit message is required."}
    paths = [p for p in (paths or []) if isinstance(p, str) and p.strip() and not p.startswith("-")]
    res = await sandbox_exec(ctx, coding.commit_script(name, message.strip(), paths or None), 120)
    if not res.get("error"):
        res["committed"] = res.get("exit_code") == 0
    return res


def _push_args(repo, branch, open_pr, title, body, base, draft) -> dict:
    return {
        "repo": repo,
        "branch": branch,
        "open_pr": bool(open_pr),
        "title": (title or "").strip()[:200],
        "body": (body or "")[:20000],
        "base": base or "",
        "draft": bool(draft),
    }


def _push_confirmed(ctx, repo: str, branch: str) -> bool:
    for c in getattr(ctx, "confirmations", None) or []:
        if c.get("slug") == coding.CODING_SLUG and c.get("tool") == "push_branch":
            a = c.get("args") or {}
            if a.get("repo", "").lower() == repo.lower() and a.get("branch") == branch:
                return True
    return False


async def push_branch(
    ctx,
    repo: str,
    branch: str,
    open_pr: bool = True,
    title: str = "",
    body: str = "",
    base: str = "",
    draft: bool = False,
) -> dict:
    """Always needs the user's approval (also in scheduled runs and background jobs)."""
    from app.agent.confirm import sign_confirm

    try:
        owner, name = coding.split_repo(repo)
        branch = coding.check_agent_branch(branch)
        base = coding.check_ref(base, "base branch")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    full = f"{owner}/{name}"
    args = _push_args(full, branch, open_pr, title, body, base, draft)
    if _push_confirmed(ctx, full, branch):
        return await run_push(ctx, args)
    peek = await sandbox_exec(ctx, coding.preview_script(name, branch, base), 60)
    commits, _, stat = str(peek.get("output") or "").partition("--")
    preview = {
        "repo": full,
        "branch": branch,
        "base": base or "default branch",
        "open_pr": bool(open_pr),
        "title": args["title"] or branch,
        "commits": [line for line in commits.strip().splitlines() if line][:10],
        "diffstat": stat.strip()[:300],
    }
    token = sign_confirm("tool", {"slug": coding.CODING_SLUG, "tool": "push_branch", "args": args}, ttl=PUSH_CONFIRM_TTL)
    pending = {
        "needs_confirm": True,
        "token": token,
        "kind": "git_push",
        "title": f"Push {branch} to {full}" + (" and open a pull request" if open_pr else ""),
        "preview": preview,
    }
    ctx.pending.append(pending)
    return {"status": "awaiting_user_confirmation", "preview": preview}


async def run_push(ctx, args: dict) -> dict:
    """Runs only after the user approved this exact repo + branch."""
    from app.connectors import github_app as gh

    try:
        owner, name = coding.split_repo(args.get("repo") or "")
        branch = coding.check_agent_branch(args.get("branch") or "")
        token = await app_token(ctx, f"{owner}/{name}", write=True)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    full = f"{owner}/{name}"
    script = coding.push_script(name, branch) if token else coding.push_script(name, branch).replace(coding.GIT_AUTH + " ", "")
    res = await sandbox_exec(ctx, script, 300, token)
    if res.get("error"):
        return res
    out: dict[str, Any] = {"repo": full, "branch": branch, "pushed": res.get("exit_code") == 0, "output": res["output"]}
    if out["pushed"]:
        await _record(ctx, "step", f"Pushed {branch} to {full}", {"repo": full, "branch": branch})
    if not out["pushed"]:
        status = await github_app_status(ctx)
        if not token:
            out["hint"] = (
                f"No push access from the computer. Install the Checkme GitHub App on {owner}: {status['install_url']}"
                if status.get("install_url")
                else "No push access from the computer. Use the connected GitHub tools (search_tools 'push files', "
                "'create pull request') or connect GitHub with connect_app github."
            )
        return out
    if not args.get("open_pr"):
        return out
    title = args.get("title") or branch.removeprefix("agent/").replace("-", " ")
    if token:
        row = type("Row", (), {"user_id": ctx.user.id})()
        try:
            pr = await gh.call_github_app(
                row,
                "create_pull_request",
                {"repo": full, "head": branch, "base": args.get("base") or "", "title": title, "body": args.get("body") or "", "draft": args.get("draft")},
                installs=await installs_for(ctx),
            )
            out["pull_request"] = pr
            out["pr_url"] = pr.get("url")
        except gh.GitHubError as exc:
            if exc.status == 422:
                out["pull_request_note"] = "A pull request for this branch already exists; the push updated it."
            else:
                out["pull_request_error"] = coding.scrub(str(exc))
        return out
    q = coding._shlex.quote
    base_flag = f" --base {q(args['base'])}" if args.get("base") else ""
    pr_script = (
        f"cd {coding.repo_dir(name)} && gh pr create --head {q(branch)}{base_flag} --title {q(title)} "
        f"--body {q(args.get('body') or '')}{' --draft' if args.get('draft') else ''} 2>&1 | tail -3"
    )
    pr = await sandbox_exec(ctx, pr_script, 120)
    url = next((w for w in str(pr.get("output") or "").split() if w.startswith("https://github.com/")), "")
    if url:
        out["pr_url"] = url
    else:
        out["pull_request_note"] = "Pushed. Open the PR with the connected GitHub tools (create pull request)."
    return out


async def run_confirmed(ctx, tool: str, args: dict) -> dict:
    if tool == "push_branch":
        out = await run_push(ctx, args)
        if out.get("pr_url"):
            await _record(ctx, "pr", f"Pull request: {out['pr_url']}", {"url": out["pr_url"], "repo": out.get("repo"), "branch": out.get("branch")})
        return out
    return {"error": f"unknown coding action {tool}"}
