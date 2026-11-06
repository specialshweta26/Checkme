"""Safe git tools for coding agents: checkout, test, diff, commit and push_branch.

All of them run through Daytona exec on the user's one sandbox. GitHub access comes
from the user's Composio GitHub connection:

* If Composio exposes the connection's OAuth token unmasked (customer-owned auth
  config, secret masking off), each git command gets it in the CHECKME_GIT_TOKEN env
  var only and runs a real clone / fetch / push.
* Otherwise public repos clone anonymously, private repos are unpacked from a tarball
  fetched through the Composio proxy (a snapshot without history), and push_branch
  sends the branch's committed changes through the GitHub git data API via the proxy
  (blobs, tree, one commit, non-force ref update) to the same agent/* branch.

Without a Composio connection they fall back to credentials already on the sandbox
(gh auth / git credential helper) or anonymous https for public repos.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

from app.agent import coding

log = logging.getLogger(__name__)
PUSH_CONFIRM_TTL = 3 * 24 * 3600


def _err(exc: Exception) -> dict:
    return {"error": coding.scrub(str(exc))[:600]}


def _row(ctx):
    return SimpleNamespace(user_id=ctx.user.id)


async def github_status(ctx, *, fresh: bool = False) -> dict:
    """Composio GitHub connection for this user: connected, login, identity, git transport. Never a token."""
    from app.connectors import github as gh

    cached = getattr(ctx, "_github_status", None)
    if cached is not None and not fresh:
        return cached
    out: dict[str, Any] = {"configured": gh.is_configured(), "connected": False, "login": "", "git_transport": ""}
    if out["configured"]:
        try:
            await gh.account_for(ctx.user.id)
            out["connected"] = True
            try:
                me = await gh.viewer(ctx.user.id)
                out.update(login=me.get("login") or "", name=me.get("name") or "", email=me.get("email") or "")
            except Exception as exc:  # noqa: BLE001
                log.warning("github viewer lookup failed: %s", coding.scrub(str(exc)))
            out["git_transport"] = "git" if await gh.git_token(ctx.user.id) else "api"
        except gh.NotConnected:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("composio github lookup failed: %s", coding.scrub(str(exc)))
            out["error"] = coding.scrub(str(exc))[:200]
    if not out["connected"]:
        out["connect_hint"] = gh.CONNECT_HINT
    try:
        ctx._github_status = out
    except AttributeError:
        pass
    return out


async def git_token(ctx) -> str | None:
    """The connection's raw token for one git command, or None (masked by Composio / not connected)."""
    from app.connectors import github as gh

    if not (await github_status(ctx)).get("connected"):
        return None
    return await gh.git_token(ctx.user.id) or None


async def gh_call(ctx, tool: str, args: dict) -> Any:
    from app.connectors import github as gh

    return await gh.call_github(_row(ctx), tool, args)


def identity(status: dict) -> tuple[str, str]:
    """Commit author: the connected GitHub user's name and noreply email, else the Checkme Coder identity."""
    if status.get("login") and status.get("email"):
        return status.get("name") or status["login"], status["email"]
    return coding.AUTHOR_NAME, coding.AUTHOR_EMAIL


async def sandbox_exec(
    ctx, script: str, timeout: int = 300, token: str | None = None, env: dict | None = None, secret_env: dict | None = None
) -> dict:
    """Run a bash script on the computer without going through call_user_tool's arg logging.

    ``token`` / ``env`` values only travel as exec env and are scrubbed from the output."""
    from app.connectors.runtime import RuntimeConnector

    conn = ctx.connectors.get(coding.COMPUTER_SLUG)
    if conn is None:
        return {"error": coding.readiness(ctx.connectors)["missing"][0]}
    args: dict[str, Any] = {"command": coding.bash(script), "timeout": coding.clamp_timeout(timeout)}
    hidden = [token or ""]
    if env:
        from app.agent.deploy import secrets_of

        args["env"] = {str(k): str(v) for k, v in env.items()}
        hidden += secrets_of(env)
    if secret_env:
        args["env"] = {**(args.get("env") or {}), **{str(k): str(v) for k, v in secret_env.items()}}
        hidden += [str(v) for v in secret_env.values()]
    if token:
        args["env"] = {**(args.get("env") or {}), coding.TOKEN_ENV: token}
    try:
        if getattr(ctx, "call_impl", None) is not None:
            raw = await ctx.call_impl(coding.COMPUTER_SLUG, "exec", args)
        else:
            raw = await RuntimeConnector(conn).call("exec", args)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    raw = raw if isinstance(raw, dict) else {"stdout": str(raw)}
    if raw.get("error"):
        return {"error": coding.scrub(str(raw["error"]), hidden)[:600]}
    out = coding.scrub(str(raw.get("stdout") or raw.get("result") or ""), hidden)
    return {"exit_code": raw.get("exit_code"), "output": coding.clip_output(out)}


def _kv(output: str) -> dict:
    return coding.parse_probe(output)


async def sandbox_read_bytes(ctx, path: str) -> bytes:
    import base64

    from app.connectors.runtime import RuntimeConnector

    conn = ctx.connectors.get(coding.COMPUTER_SLUG)
    if conn is None:
        raise coding.GitToolError(coding.readiness(ctx.connectors)["missing"][0])
    args = {"path": path}
    if getattr(ctx, "call_impl", None) is not None:
        raw = await ctx.call_impl(coding.COMPUTER_SLUG, "read_bytes", args)
    else:
        raw = await RuntimeConnector(conn).call("read_bytes", args)
    if not isinstance(raw, dict) or raw.get("error") or "base64" not in raw:
        raise coding.GitToolError(f"Could not read {path} from the computer.")
    return base64.b64decode(raw["base64"])


async def git_checkout(ctx, repo: str, branch: str = "", base: str = "") -> dict:
    """Clone or update ~/work/<repo>. Tries, in order: the Composio token; anonymous https and the computer's own git
    credentials (gh auth); a Composio tarball snapshot; a snapshot rebuilt from the git trees API. When all fail it
    starts `gh auth login --web` on the computer and returns its link (status=awaiting_user_login)."""
    try:
        owner, name = coding.split_repo(repo)
        branch = coding.check_ref(branch)
        base = coding.check_ref(base, "base branch")
        token = await git_token(ctx)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    full = f"{owner}/{name}"
    res = await sandbox_exec(ctx, coding.checkout_script(owner, name, branch, base, authed=bool(token)), 600, token)
    if res.get("error"):
        return res
    auth = "composio_token" if token else "public_or_sandbox_credentials"
    snapshot_dir = "snapshot_dir=1" in str(res.get("output") or "")
    tried = [auth]
    if res.get("exit_code") not in (0, None) and token and not snapshot_dir:
        # A stale or under-scoped token makes even public repos fail; retry without it (anonymous / gh auth).
        retry = await sandbox_exec(ctx, coding.checkout_script(owner, name, branch, base, authed=False), 600)
        tried.append("public_or_sandbox_credentials")
        if not retry.get("error"):
            res, auth = retry, "public_or_sandbox_credentials"
    if res.get("exit_code") not in (0, None):
        status = await github_status(ctx)
        if status.get("connected"):
            snap = await _snapshot_checkout(ctx, full, name, branch, base)
            tried.append("composio_snapshot")
            if snap is not None and snap.get("exit_code") in (0, None) and not snap.get("error"):
                res, auth = snap, "composio_snapshot"
            else:
                tree = await _tree_checkout(ctx, full, name, branch, base)
                tried.append("github_tree_snapshot")
                if tree is not None and tree.get("exit_code") in (0, None) and not tree.get("error"):
                    res, auth = tree, "composio_snapshot"
                elif snap is not None and snap.get("error"):
                    res.setdefault("detail", snap["error"])
    if res.get("exit_code") not in (0, None):
        from app.agent import cli_login

        login = await cli_login.ensure_login(ctx, "github", f"to check out {full}")
        if login.get("status") == "logged_in":
            again = await sandbox_exec(ctx, coding.checkout_script(owner, name, branch, base, authed=False), 600)
            tried.append("gh_cli_session")
            if not again.get("error"):
                res, auth = again, "gh_cli_session"
        elif login.get("status") == "awaiting_user_login":
            return {**login, "repo": full, "exit_code": res.get("exit_code"), "tried": tried,
                    "output": str(res.get("output") or "")[-1500:]}
    info = _kv(res.get("output") or "")
    res.update(
        repo=full,
        repo_dir=info.get("repo_dir", f"~/work/{name}"),
        branch=info.get("branch", branch),
        base=info.get("base", base),
        auth=auth,
    )
    if res.get("exit_code") not in (0, None):
        res["tried"] = tried
    if auth == "composio_snapshot" and res.get("exit_code") in (0, None):
        res["note"] = (
            "Private repo unpacked from a GitHub snapshot (no git history; git log shows snapshot commits). Edit, test "
            "and git_commit as usual; push_branch sends the committed changes through the GitHub API."
        )
    return res


async def ensure_checkout(ctx, repo: str, branch: str = "") -> dict:
    """Make sure ~/work/<repo> exists before deploy / inspect tools run; clone it (git_checkout) when missing.
    Returns {"ok": True, ...} or the git_checkout result (an error or a sign-in handoff)."""
    try:
        owner, name = coding.split_repo(repo)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    probe = await sandbox_exec(ctx, f'[ -d "$HOME/work/{name}/.git" ] && echo present=1 || echo present=0', 30)
    if probe.get("error"):
        return probe
    if "present=1" in str(probe.get("output") or ""):
        return {"ok": True, "repo": f"{owner}/{name}", "checked_out": False}
    res = await git_checkout(ctx, f"{owner}/{name}", branch)
    if res.get("status") == "awaiting_user_login" or res.get("error") or res.get("exit_code") not in (0, None):
        return res
    await _record(ctx, "step", f"Checked out {owner}/{name}", {"repo": f"{owner}/{name}", "auth": res.get("auth")})
    return {"ok": True, "repo": f"{owner}/{name}", "checked_out": True, "auth": res.get("auth"), "branch": res.get("branch")}


async def _snapshot_checkout(ctx, full: str, name: str, branch: str, base: str) -> dict | None:
    """Unpack the repo from a tarball fetched through the Composio proxy. The URL travels as a hidden env var."""
    from app.connectors import github as gh

    try:
        src = await gh.snapshot_source(ctx.user.id, full, branch, base)
    except Exception as exc:  # noqa: BLE001
        return {"exit_code": 1, "output": "", "error": f"Could not fetch {full} through Composio GitHub: {coding.scrub(str(exc))[:300]}"}
    return await sandbox_exec(
        ctx,
        coding.snapshot_script(name, src["ref"], src["sha"], src["base"], branch),
        600,
        secret_env={coding.ARCHIVE_ENV: src["url"]},
    )


async def sandbox_write(ctx, path: str, content: str) -> dict:
    from app.connectors.runtime import RuntimeConnector

    conn = ctx.connectors.get(coding.COMPUTER_SLUG)
    if conn is None:
        return {"error": coding.readiness(ctx.connectors)["missing"][0]}
    args = {"path": path, "content": content}
    try:
        if getattr(ctx, "call_impl", None) is not None:
            raw = await ctx.call_impl(coding.COMPUTER_SLUG, "write_file", args)
        else:
            raw = await RuntimeConnector(conn).call("write_file", args)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    return raw if isinstance(raw, dict) else {"ok": True}


async def _tree_checkout(ctx, full: str, name: str, branch: str, base: str) -> dict | None:
    """When the proxy gives no tarball URL: rebuild the archive from the trees + blobs API, write it to the computer
    and unpack it the same way."""
    from app.connectors import github as gh

    try:
        src = await gh.tree_archive(ctx.user.id, full, branch, base)
    except Exception as exc:  # noqa: BLE001
        return {"exit_code": 1, "output": "", "error": f"Could not rebuild {full} from the GitHub API: {coding.scrub(str(exc))[:300]}"}
    path = f"/tmp/checkme-{name}-{src['sha'][:12]}.tgz.b64"
    wrote = await sandbox_write(ctx, path, src["archive_b64"])
    if wrote.get("error") or wrote.get("ok") is False:
        return {"exit_code": 1, "output": "", "error": wrote.get("error") or "could not write the archive"}
    return await sandbox_exec(
        ctx, coding.snapshot_script(name, src["ref"], src["sha"], src["base"], branch), 600, env={coding.ARCHIVE_FILE_ENV: path}
    )


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
    author = identity(await github_status(ctx))
    res = await sandbox_exec(ctx, coding.commit_script(name, message.strip(), paths or None, author), 120)
    if not res.get("error"):
        res["committed"] = res.get("exit_code") == 0
    return res


def _push_args(repo, branch, open_pr, title, body, base, draft, pr_number: int = 0) -> dict:
    out = {
        "repo": repo,
        "branch": branch,
        "open_pr": bool(open_pr),
        "title": (title or "").strip()[:200],
        "body": (body or "")[:20000],
        "base": base or "",
        "draft": bool(draft),
    }
    if pr_number:
        out["pr_number"] = int(pr_number)
    return out


async def pr_head(ctx, repo: str, number: int) -> dict:
    """The open PR's head branch, verified through Composio GitHub (same repo, still open)."""
    pr = await gh_call(ctx, "get_pull_request", {"repo": repo, "number": int(number)})
    if pr.get("state") != "open":
        raise coding.GitToolError(f"{repo}#{number} is not open, so I will not push to it.")
    if str(pr.get("head_repo") or "").lower() != repo.lower():
        raise coding.GitToolError(f"{repo}#{number} comes from a fork ({pr.get('head_repo')}); I can only push to branches in {repo}.")
    return {"branch": pr.get("head") or "", "url": pr.get("url"), "title": pr.get("title"), "number": int(number)}


def _patch_slug(branch: str) -> str:
    return coding._re.sub(r"[^A-Za-z0-9._-]+", "-", branch).strip("-")[:80] or "work"


async def blocked_push(ctx, owner: str, name: str, branch: str, base: str) -> dict | None:
    """No Composio GitHub connection and no git credentials on the computer: sign the GitHub CLI in on the computer
    (one link for the user), keeping the commits as a patch meanwhile. None when pushing can go ahead."""
    status = await github_status(ctx)
    if status.get("connected"):
        return None
    probe = await sandbox_exec(ctx, coding.creds_script(), 30)
    if "creds=ok" in str(probe.get("output") or ""):
        return None
    from app.agent import cli_login

    login = await cli_login.ensure_login(ctx, "github", f"to push {branch} to {owner}/{name}")
    if login.get("status") == "logged_in":
        return None
    slug = _patch_slug(branch)
    saved = await sandbox_exec(ctx, coding.save_patch_script(name, branch, base, slug), 60)
    patch_dir = f"~/work/_patches/{name}-{slug}"
    if login.get("status") == "awaiting_user_login":
        return {**login, "branch": branch, "patch_dir": patch_dir,
                "saved": "commits kept on the branch and as patches in " + patch_dir if saved.get("exit_code") == 0 else ""}
    missing = f"GitHub access to {owner}/{name}: " + (login.get("error") or "could not sign in to GitHub on the computer")
    await _record(ctx, "status", f"Blocked: {missing}", {"patch_dir": patch_dir})
    return {
        "status": "blocked", "missing": missing, "branch": branch, "patch_dir": patch_dir,
        "saved": "commits saved as patches in " + patch_dir if saved.get("exit_code") == 0 else "could not save a patch",
        "next": "Report the error in one sentence; the commits are still on the branch.",
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
    pr_number: int = 0,
) -> dict:
    """Always needs the user's approval (also in scheduled runs and background jobs).

    Pushes go to agent/* branches. With pr_number, the branch may instead be the head branch of
    that open PR in the same repo (the user named it), verified through Composio GitHub."""
    from app.agent import dev_tools
    from app.agent.confirm import sign_confirm

    try:
        owner, name = coding.split_repo(repo)
        full = f"{owner}/{name}"
        pr_branch = ""
        if pr_number:
            head = await pr_head(ctx, full, int(pr_number))
            pr_branch = head["branch"]
            open_pr = False
        branch = coding.check_push_branch(branch, pr_branch)
        base = coding.check_ref(base, "base branch")
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    if open_pr:
        body = dev_tools.pr_body_with_tests(ctx, full, body)
    args = _push_args(full, branch, open_pr, title, body, base, draft, int(pr_number or 0))
    if _push_confirmed(ctx, full, branch):
        return await run_push(ctx, args)
    blocked = await blocked_push(ctx, owner, name, branch, base)
    if blocked is not None:
        return blocked
    peek = await sandbox_exec(ctx, coding.preview_script(name, branch, base), 60)
    commits, _, stat = str(peek.get("output") or "").partition("--")
    preview = {
        "repo": full,
        "branch": branch,
        "base": base or "default branch",
        "open_pr": bool(open_pr),
        "pr_number": int(pr_number or 0) or None,
        "title": args["title"] or branch,
        "commits": [line for line in commits.strip().splitlines() if line][:10],
        "diffstat": stat.strip()[:300],
    }
    token = sign_confirm("tool", {"slug": coding.CODING_SLUG, "tool": "push_branch", "args": args}, ttl=PUSH_CONFIRM_TTL)
    pending = {
        "needs_confirm": True,
        "token": token,
        "kind": "git_push",
        "title": f"Push {branch} to {full}"
        + (" and open a pull request" if open_pr else (f" (updates PR #{int(pr_number)})" if pr_number else "")),
        "preview": preview,
    }
    ctx.pending.append(pending)
    return {"status": "awaiting_user_confirmation", "preview": preview}


async def run_push(ctx, args: dict) -> dict:
    """Runs only after the user approved this exact repo + branch."""
    from app.connectors import github as gh

    try:
        owner, name = coding.split_repo(args.get("repo") or "")
        pr_branch = ""
        if args.get("pr_number"):
            pr_branch = (await pr_head(ctx, f"{owner}/{name}", int(args["pr_number"])))["branch"]
        branch = coding.check_push_branch(args.get("branch") or "", pr_branch)
        status = await github_status(ctx)
        token = await git_token(ctx)
    except Exception as exc:  # noqa: BLE001
        return _err(exc)
    full = f"{owner}/{name}"
    if token:
        res = await sandbox_exec(ctx, coding.push_script(name, branch), 300, token)
        via = "git"
    elif status.get("connected"):
        res = await api_push(ctx, full, name, branch, args.get("base") or "")
        via = "github_api"
    else:
        res = await sandbox_exec(ctx, coding.push_script(name, branch).replace(coding.GIT_AUTH + " ", ""), 300)
        via = "sandbox_credentials"
    if res.get("error") and "pushed" not in res:
        return {**res, "repo": full, "branch": branch, "pushed": False, "via": via}
    pushed = bool(res.get("pushed")) if "pushed" in res else res.get("exit_code") == 0
    out: dict[str, Any] = {"repo": full, "branch": branch, "pushed": pushed, "via": via, "output": res.get("output") or ""}
    for k in ("commit", "files", "error"):
        if res.get(k):
            out[k] = res[k]
    if out["pushed"]:
        await _record(ctx, "step", f"Pushed {branch} to {full}", {"repo": full, "branch": branch, "via": via})
    if not out["pushed"]:
        if not status.get("connected"):
            out["hint"] = "No push access from the computer; call push_branch again and it signs the GitHub CLI in (one link for the user)."
        return out
    if args.get("pr_number"):
        out["pr_number"] = int(args["pr_number"])
        out["pull_request_note"] = f"Updated the existing pull request #{int(args['pr_number'])}."
    if not args.get("open_pr"):
        await _note_pr(ctx, full, branch, out, args)
        return out
    title = args.get("title") or branch.removeprefix("agent/").replace("-", " ")
    if status.get("connected"):
        try:
            pr = await gh_call(
                ctx,
                "create_pull_request",
                {"repo": full, "head": branch, "base": args.get("base") or "", "title": title, "body": args.get("body") or "", "draft": args.get("draft")},
            )
            out["pull_request"] = pr
            out["pr_url"] = pr.get("url")
            out["pr_number"] = pr.get("number")
        except gh.GitHubError as exc:
            if exc.status == 422:
                out["pull_request_note"] = "A pull request for this branch already exists; the push updated it."
            else:
                out["pull_request_error"] = coding.scrub(str(exc))
        except Exception as exc:  # noqa: BLE001
            out["pull_request_error"] = coding.scrub(str(exc))[:400]
        await _note_pr(ctx, full, branch, out, args)
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
        tail = url.rstrip("/").rsplit("/", 1)[-1]
        if tail.isdigit():
            out["pr_number"] = int(tail)
    else:
        out["pull_request_note"] = "Pushed. Open the PR with the connected GitHub tools (create pull request)."
    await _note_pr(ctx, full, branch, out, args)
    return out


async def api_push(ctx, full: str, name: str, branch: str, base: str) -> dict:
    """Push without a git token: the sandbox bundles the branch's committed changes since its GitHub base, and the
    server commits them through the Composio proxy (git data API, never forced). Only after approval."""
    from app.connectors import github as gh

    res = await sandbox_exec(ctx, coding.push_bundle_script(name, branch, base), 300)
    if res.get("error"):
        return {"pushed": False, "error": res["error"]}
    info = _kv(res.get("output") or "")
    if res.get("exit_code") not in (0, None) or not info.get("bundle"):
        return {"pushed": False, "error": "Could not prepare the push: " + str(res.get("output") or "")[-400:]}
    if info.get("files") == "0":
        return {"pushed": False, "error": f"Nothing to push: {branch} has no committed changes since its base."}
    path = info["bundle"]
    if not coding.BUNDLE_RE.match(path):
        return {"pushed": False, "error": "Unexpected bundle path from the computer."}
    try:
        bundle = json.loads(await sandbox_read_bytes(ctx, path))
        owner, rname = coding.split_repo(full)
        parent = str(bundle.get("parent") or "")
        if not gh.SHA_RE.match(parent):
            raise coding.GitToolError("The branch base is unknown; run git_checkout again.")
        result = await gh.commit_files(
            f"/repos/{owner}/{rname}",
            await gh.account_for(ctx.user.id),
            branch=branch,
            files=bundle.get("files") or [],
            message=str(bundle.get("message") or f"Update {branch}")[:20000],
            parent_sha=parent,
        )
    except Exception as exc:  # noqa: BLE001
        return {"pushed": False, "error": coding.scrub(str(exc))[:600]}
    await sandbox_exec(ctx, coding.mark_pushed_script(name, branch, str(bundle.get("head") or ""), result["commit"]), 60)
    return {
        "pushed": True,
        "commit": result["commit"],
        "files": result.get("files"),
        "output": f"Pushed {result.get('files')} file change(s) to {branch} as {result['commit'][:12]} through the GitHub API.",
    }


async def _note_pr(ctx, repo: str, branch: str, out: dict, args: dict) -> None:
    """Remember the PR on the job (for the "Merge it?" question) and in the agent's coding memory."""
    from app.agent import coding_memory

    url = out.get("pr_url")
    if not url and args.get("pr_number"):
        url = f"https://github.com/{repo}/pull/{int(args['pr_number'])}"
    if not url:
        return
    pr = {"url": url, "repo": repo, "number": out.get("pr_number") or args.get("pr_number"), "branch": branch, "title": args.get("title") or branch}
    prs = getattr(ctx, "prs", None)
    if isinstance(prs, list) and not any(p.get("url") == url for p in prs):
        prs.append(pr)
    try:
        ctx.repo = getattr(ctx, "repo", "") or repo
    except AttributeError:
        pass
    await coding_memory.note(ctx, repo=repo, pr=pr)


async def run_confirmed(ctx, tool: str, args: dict) -> dict:
    if tool == "deploy_action":
        from app.agent import deploy

        return await deploy.run_action(ctx, args)
    if tool == "push_branch":
        out = await run_push(ctx, args)
        if out.get("pr_url"):
            await _record(ctx, "pr", f"Pull request: {out['pr_url']}", {"url": out["pr_url"], "repo": out.get("repo"), "branch": out.get("branch")})
        return out
    return {"error": f"unknown coding action {tool}"}
