"""Checkme GitHub App connector.

The App authenticates as itself with a short-lived RS256 JWT, then mints an
installation access token for every call. Tokens are scoped to the one repo the
call touches and to the permissions that call needs (read or write), cached in
memory until five minutes before expiry, and never stored in the database,
returned to the model, or logged. See docs/GITHUB_APP.md for App setup.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from connector_core import ToolError, ToolSpec

from app.config import get_settings

log = logging.getLogger(__name__)

API = "https://api.github.com"
OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"
PROVIDER = "github_app"
SLUG = "github-app"
AGENT_BRANCH = re.compile(r"^agent/[A-Za-z0-9._/-]{1,120}$")
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
REQUIRED_ENV = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_SLUG")
VERIFY_ENV = ("GITHUB_APP_CLIENT_ID", "GITHUB_APP_CLIENT_SECRET")
# Tools that change a repository. They are destructive (confirm card) and are
# also caught by the git-write gate that ignores auto_confirm (scheduled runs).
WRITE_TOOLS = frozenset({"create_pull_request", "update_pull_request", "push_files", "merge_pull_request"})
# Writes that only add discussion (comments, reviews, reviewer requests). They change no code.
DISCUSSION_TOOLS = frozenset({"comment_pull_request", "create_review", "request_reviewers"})
MERGE_METHODS = ("merge", "squash", "rebase")
# Optional permissions for reading CI. Asked for when the App has them; skipped otherwise.
CHECK_PERMS = {"checks": "read", "statuses": "read"}
READ_PERMS = {"contents": "read", "metadata": "read", "pull_requests": "read"}
_clip_max = 60_000

# ---------------------------------------------------------------------------
# configuration


def _env_value(name: str) -> str:
    s = get_settings()
    return str(getattr(s, name.lower(), "") or "").strip()


def missing_env() -> list[str]:
    return [n for n in (*REQUIRED_ENV, *VERIFY_ENV) if not _env_value(n)]


def is_configured() -> bool:
    """True when the App can mint tokens. Client id/secret are only needed to verify installers."""
    return all(_env_value(n) for n in REQUIRED_ENV)


def private_key() -> str:
    raw = _env_value("GITHUB_APP_PRIVATE_KEY")
    if not raw:
        return ""
    if "-----BEGIN" not in raw:
        try:
            raw = base64.b64decode(raw).decode()
        except Exception:  # noqa: BLE001
            return raw
    return raw.replace("\\n", "\n")


def app_install_url(state: str = "") -> str:
    slug = _env_value("GITHUB_APP_SLUG")
    if not slug:
        return ""
    base = f"https://github.com/apps/{quote(slug)}/installations/new"
    return f"{base}?{urlencode({'state': state})}" if state else base


def checkme_install_link() -> str:
    """The link users click in Checkme; it stashes state then redirects to GitHub."""
    return f"{get_settings().public_web_url.rstrip('/')}/api/connectors/github/install"


def app_jwt(now: float | None = None) -> str:
    import jwt

    if not is_configured():
        raise ToolError("The Checkme GitHub App is not configured on this server.")
    t = int(now if now is not None else time.time())
    payload = {"iat": t - 60, "exp": t + 540, "iss": _env_value("GITHUB_APP_ID")}
    return jwt.encode(payload, private_key(), algorithm="RS256")


def verify_webhook(body: bytes, signature: str) -> bool:
    secret = _env_value("GITHUB_APP_WEBHOOK_SECRET")
    if not secret or not signature.startswith("sha256="):
        return False
    want = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature)


# ---------------------------------------------------------------------------
# HTTP + token cache

_transport: httpx.AsyncBaseTransport | None = None  # tests swap in httpx.MockTransport
_TOKENS: dict[tuple, tuple[str, float]] = {}


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport
    _TOKENS.clear()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_transport,
        timeout=30,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "checkme"},
    )


class GitHubError(ToolError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _scrub(text: str) -> str:
    return re.sub(r"(gh[usopr]_[A-Za-z0-9]{10,}|v1\.[0-9a-f]{20,}|eyJ[A-Za-z0-9._-]{20,})", "[redacted]", text or "")


async def _request(
    method: str, url: str, *, auth: str, json: Any = None, params: dict | None = None, headers: dict | None = None
) -> Any:
    full = url if url.startswith("http") else API + url
    async with _client() as client:
        res = await client.request(
            method, full, json=json, params=params, headers={"Authorization": auth, **(headers or {})}
        )
    if res.status_code >= 400:
        try:
            msg = res.json().get("message") or res.text
        except Exception:  # noqa: BLE001
            msg = res.text
        raise GitHubError(res.status_code, f"GitHub {res.status_code}: {_scrub(str(msg))[:300]}")
    if res.status_code == 204 or not res.content:
        return {}
    return res.json()


async def app_get(path: str) -> Any:
    return await _request("GET", path, auth=f"Bearer {app_jwt()}")


async def installation_token(
    installation_id: int, repos: list[str] | None = None, permissions: dict[str, str] | None = None
) -> str:
    """Short-lived installation token, scoped to `repos` (names, no owner) and `permissions`."""
    key = (int(installation_id), tuple(sorted(repos or [])), tuple(sorted((permissions or {}).items())))
    hit = _TOKENS.get(key)
    if hit and hit[1] - 300 > time.time():
        return hit[0]
    body: dict[str, Any] = {}
    if repos:
        body["repositories"] = list(repos)
    if permissions:
        body["permissions"] = dict(permissions)
    data = await _request(
        "POST", f"/app/installations/{int(installation_id)}/access_tokens", auth=f"Bearer {app_jwt()}", json=body
    )
    token = data.get("token") or ""
    if not token:
        raise ToolError("GitHub did not return an installation token")
    expires = _parse_ts(data.get("expires_at")) or time.time() + 3600
    _TOKENS[key] = (token, expires)
    return token


def _parse_ts(value: Any) -> float | None:
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def exchange_code(code: str) -> str:
    """OAuth code from the install callback -> user-to-server token (used once to verify the installer)."""
    async with _client() as client:
        res = await client.post(
            OAUTH_TOKEN_URL,
            data={
                "client_id": _env_value("GITHUB_APP_CLIENT_ID"),
                "client_secret": _env_value("GITHUB_APP_CLIENT_SECRET"),
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
    data = res.json() if res.content else {}
    token = data.get("access_token") or ""
    if not token:
        raise ToolError(f"GitHub sign-in failed: {data.get('error_description') or data.get('error') or res.status_code}")
    return token


async def user_installation_ids(user_token: str) -> set[int]:
    data = await _request("GET", "/user/installations", auth=f"Bearer {user_token}", params={"per_page": 100})
    return {int(i["id"]) for i in data.get("installations") or []}


# ---------------------------------------------------------------------------
# installations


@dataclass
class Installation:
    installation_id: int
    account_login: str
    account_type: str = ""
    repository_selection: str = ""
    suspended: bool = False


def installation_fields(data: dict) -> dict:
    account = data.get("account") or {}
    return {
        "installation_id": int(data["id"]),
        "account_login": str(account.get("login") or "")[:100],
        "account_type": str(account.get("type") or "")[:20],
        "repository_selection": str(data.get("repository_selection") or "")[:20],
        "permissions": dict(data.get("permissions") or {}),
        "suspended": bool(data.get("suspended_at")),
    }


async def load_installations(user_id) -> list[Installation]:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import GitHubInstallation

    async with SessionLocal() as db:
        rows = (
            await db.execute(select(GitHubInstallation).where(GitHubInstallation.user_id == user_id))
        ).scalars().all()
    return [
        Installation(r.installation_id, r.account_login, r.account_type, r.repository_selection, r.suspended)
        for r in rows
    ]


def split_repo(repo: str) -> tuple[str, str]:
    repo = (repo or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not REPO_RE.match(repo):
        raise ToolError("Give the repository as owner/name, for example octocat/hello-world.")
    owner, name = repo.split("/", 1)
    return owner, name


def pick_installation(installs: list[Installation], owner: str) -> Installation:
    live = [i for i in installs if not i.suspended]
    for inst in live:
        if inst.account_login.lower() == owner.lower():
            return inst
    raise ToolError(
        f"The Checkme GitHub App is not installed on {owner}. Install it here and pick that account: "
        f"{checkme_install_link()}"
    )


async def repo_token(user_id, repo: str, *, write: bool, extra: dict[str, str] | None = None, installs=None) -> str:
    """Installation token for exactly one repo. Used by the sandbox git tools too."""
    owner, name = split_repo(repo)
    installs = installs if installs is not None else await load_installations(user_id)
    inst = pick_installation(installs, owner)
    perms = {"contents": "write", "pull_requests": "write"} if write else dict(READ_PERMS)
    perms.update(extra or {})
    return await installation_token(inst.installation_id, [name], perms)


# ---------------------------------------------------------------------------
# tools


def _s(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


REPO = {"type": "string", "description": "owner/name"}
GITHUB_TOOLS = [
    ToolSpec(
        name="list_repos",
        description="List repositories the Checkme GitHub App can reach for this user.",
        input_schema=_s({}, []),
        group="github",
        tags=("github", "code", "repos"),
    ),
    ToolSpec(
        name="get_file",
        description="Read a file (or list a directory) from a GitHub repository at an optional ref.",
        input_schema=_s(
            {"repo": REPO, "path": {"type": "string"}, "ref": {"type": "string", "description": "Branch, tag or sha"}},
            ["repo", "path"],
        ),
        group="github",
        tags=("github", "code"),
    ),
    ToolSpec(
        name="list_prs",
        description="List pull requests on a repository.",
        input_schema=_s(
            {
                "repo": REPO,
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "limit": {"type": "integer"},
            },
            ["repo"],
        ),
        group="github",
        tags=("github", "pull requests"),
    ),
    ToolSpec(
        name="get_pull_request",
        description="Read one pull request with its changed files and patches (for review).",
        input_schema=_s({"repo": REPO, "number": {"type": "integer"}}, ["repo", "number"]),
        group="github",
        tags=("github", "pull requests", "review"),
    ),
    ToolSpec(
        name="list_issues",
        description="List issues on a repository (pull requests excluded).",
        input_schema=_s(
            {
                "repo": REPO,
                "state": {"type": "string", "enum": ["open", "closed", "all"]},
                "labels": {"type": "string", "description": "Comma-separated label names"},
                "limit": {"type": "integer"},
            },
            ["repo"],
        ),
        group="github",
        tags=("github", "issues", "triage"),
    ),
    ToolSpec(
        name="comment_pull_request",
        description="Post a review comment on a pull request (or issue). Use for code review notes.",
        input_schema=_s({"repo": REPO, "number": {"type": "integer"}, "body": {"type": "string"}}, ["repo", "number", "body"]),
        group="github",
        read_only=False,
        tags=("github", "review"),
    ),
    ToolSpec(
        name="create_pull_request",
        description="Open a pull request from a pushed branch. Needs the user's approval.",
        input_schema=_s(
            {
                "repo": REPO,
                "head": {"type": "string", "description": "Branch with the changes, e.g. agent/fix-login"},
                "base": {"type": "string", "description": "Target branch. Defaults to the repo default."},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "draft": {"type": "boolean"},
            },
            ["repo", "head", "title"],
        ),
        group="github",
        read_only=False,
        destructive=True,
        tags=("github", "pull requests"),
    ),
    ToolSpec(
        name="push_files",
        description=(
            "Commit file changes straight to an agent/* branch through the GitHub API (creates the branch from base). "
            "Prefer push_branch from the sandbox for real code work. Needs the user's approval."
        ),
        input_schema=_s(
            {
                "repo": REPO,
                "branch": {"type": "string", "description": "Must start with agent/"},
                "base": {"type": "string"},
                "message": {"type": "string"},
                "files": {
                    "type": "array",
                    "items": _s(
                        {"path": {"type": "string"}, "content": {"type": "string"}, "delete": {"type": "boolean"}},
                        ["path"],
                    ),
                },
            },
            ["repo", "branch", "message", "files"],
        ),
        group="github",
        read_only=False,
        destructive=True,
        tags=("github", "code", "push"),
    ),
    ToolSpec(
        name="merge_pull_request",
        description=(
            "Merge a pull request (merge commit by default; squash or rebase on request). Always shows the user an "
            "approval card with mergeability and CI status first, and only merges after they approve. Refuses on "
            "conflicts, drafts and failing or pending checks (unless allow_failing_checks). Optionally deletes the "
            "head branch afterwards. Call check_merge first to report blockers."
        ),
        input_schema=_s(
            {
                "repo": REPO,
                "number": {"type": "integer"},
                "method": {"type": "string", "enum": list(MERGE_METHODS), "description": "Default merge (a merge commit)."},
                "delete_branch": {"type": "boolean", "description": "Delete the head branch after a successful merge."},
                "commit_title": {"type": "string"},
                "commit_message": {"type": "string"},
                "allow_failing_checks": {
                    "type": "boolean",
                    "description": "Merge even if checks are failing or pending. Only when the user said so.",
                },
            },
            ["repo", "number"],
        ),
        group="github",
        read_only=False,
        destructive=True,
        tags=("github", "pull requests", "merge"),
    ),
    # --- exploring a repository ---
    ToolSpec(
        name="list_branches",
        description="List branches of a repository (name, head sha, protected) and the default branch.",
        input_schema=_s({"repo": REPO, "limit": {"type": "integer"}}, ["repo"]),
        group="github",
        tags=("github", "code", "branches"),
    ),
    ToolSpec(
        name="get_tree",
        description="List files in a repository at a ref (recursive), optionally under one directory path.",
        input_schema=_s(
            {"repo": REPO, "ref": {"type": "string", "description": "Branch, tag or sha. Default branch if empty."}, "path": {"type": "string"}},
            ["repo"],
        ),
        group="github",
        tags=("github", "code", "files"),
    ),
    ToolSpec(
        name="search_code",
        description="Search code in one repository with GitHub code search (default branch only). For regex or other branches use git_inspect grep in the sandbox clone.",
        input_schema=_s(
            {"repo": REPO, "query": {"type": "string", "description": "Search terms, may include path:, language:, extension:"}, "limit": {"type": "integer"}},
            ["repo", "query"],
        ),
        group="github",
        tags=("github", "code", "search"),
    ),
    ToolSpec(
        name="list_commits",
        description="Commit history of a branch or ref, optionally for one path or author.",
        input_schema=_s(
            {"repo": REPO, "ref": {"type": "string"}, "path": {"type": "string"}, "author": {"type": "string"}, "limit": {"type": "integer"}},
            ["repo"],
        ),
        group="github",
        tags=("github", "code", "history"),
    ),
    ToolSpec(
        name="get_commit",
        description="One commit with its message, author and per-file patches.",
        input_schema=_s({"repo": REPO, "sha": {"type": "string"}}, ["repo", "sha"]),
        group="github",
        tags=("github", "code", "history"),
    ),
    ToolSpec(
        name="compare_refs",
        description="Diff between two refs (base...head): ahead/behind counts, commits and per-file patches.",
        input_schema=_s({"repo": REPO, "base": {"type": "string"}, "head": {"type": "string"}}, ["repo", "base", "head"]),
        group="github",
        tags=("github", "code", "diff"),
    ),
    ToolSpec(
        name="blame_file",
        description="Blame for a file at a ref: which commit and author last touched each line range. Optional start_line/end_line.",
        input_schema=_s(
            {"repo": REPO, "path": {"type": "string"}, "ref": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
            ["repo", "path"],
        ),
        group="github",
        tags=("github", "code", "blame", "history"),
    ),
    ToolSpec(
        name="get_pr_comments",
        description="Discussion on a pull request: conversation comments, reviews (verdicts) and inline review comments.",
        input_schema=_s({"repo": REPO, "number": {"type": "integer"}}, ["repo", "number"]),
        group="github",
        tags=("github", "pull requests", "review"),
    ),
    ToolSpec(
        name="get_issue",
        description="Read one issue with its comments.",
        input_schema=_s({"repo": REPO, "number": {"type": "integer"}}, ["repo", "number"]),
        group="github",
        tags=("github", "issues"),
    ),
    ToolSpec(
        name="check_merge",
        description=(
            "Before merging: is the pull request open, mergeable without conflicts, and are its status checks passing? "
            "Returns blockers in plain words."
        ),
        input_schema=_s({"repo": REPO, "number": {"type": "integer"}}, ["repo", "number"]),
        group="github",
        tags=("github", "pull requests", "merge", "checks", "ci"),
    ),
    # --- PR discussion and edits ---
    ToolSpec(
        name="update_pull_request",
        description="Edit a pull request: title, body, base branch, or close/reopen it. Needs the user's approval.",
        input_schema=_s(
            {
                "repo": REPO,
                "number": {"type": "integer"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "base": {"type": "string"},
                "state": {"type": "string", "enum": ["open", "closed"]},
            },
            ["repo", "number"],
        ),
        group="github",
        read_only=False,
        destructive=True,
        tags=("github", "pull requests"),
    ),
    ToolSpec(
        name="create_review",
        description=(
            "Post a pull request review: event COMMENT (default) or REQUEST_CHANGES, with a summary body and optional "
            "inline comments [{path, line, body}]. APPROVE is allowed only when the user asked you to approve."
        ),
        input_schema=_s(
            {
                "repo": REPO,
                "number": {"type": "integer"},
                "event": {"type": "string", "enum": ["COMMENT", "REQUEST_CHANGES", "APPROVE"]},
                "body": {"type": "string"},
                "comments": {
                    "type": "array",
                    "items": _s({"path": {"type": "string"}, "line": {"type": "integer"}, "body": {"type": "string"}}, ["path", "line", "body"]),
                },
            },
            ["repo", "number"],
        ),
        group="github",
        read_only=False,
        tags=("github", "pull requests", "review"),
    ),
    ToolSpec(
        name="request_reviewers",
        description="Request reviews on a pull request from GitHub users (logins) or teams (slugs).",
        input_schema=_s(
            {
                "repo": REPO,
                "number": {"type": "integer"},
                "reviewers": {"type": "array", "items": {"type": "string"}},
                "team_reviewers": {"type": "array", "items": {"type": "string"}},
            },
            ["repo", "number"],
        ),
        group="github",
        read_only=False,
        tags=("github", "pull requests", "review"),
    ),
]


def preview(tool: str, args: dict) -> dict:
    """What the approval card shows for a GitHub write."""
    out = {"repo": args.get("repo", "")}
    if tool == "create_pull_request":
        out.update(head=args.get("head", ""), base=args.get("base") or "default branch", title=args.get("title", ""))
    elif tool == "push_files":
        files = args.get("files") or []
        out.update(branch=args.get("branch", ""), message=args.get("message", ""), files=len(files))
    elif tool == "merge_pull_request":
        out.update(
            number=args.get("number"),
            method=merge_method(args.get("method")),
            delete_branch=bool(args.get("delete_branch")),
        )
        if args.get("allow_failing_checks"):
            out["allow_failing_checks"] = True
    elif tool == "update_pull_request":
        out["number"] = args.get("number")
        out.update({k: args[k] for k in ("title", "base", "state") if args.get(k)})
        if args.get("body"):
            out["body"] = str(args["body"])[:300]
    return out


async def preview_live(row, tool: str, args: dict, *, installs: list | None = None) -> dict:
    """Card preview; for a merge it adds the live mergeability and CI state so the user approves with facts."""
    out = preview(tool, args)
    if tool != "merge_pull_request" or not is_configured():
        return out
    try:
        status = await call_github_app(row, "check_merge", {"repo": args.get("repo"), "number": args.get("number")}, installs=installs)
    except (GitHubError, ToolError) as exc:
        out["status_error"] = _scrub(str(exc))[:200]
        return out
    except Exception:  # noqa: BLE001
        log.warning("merge preview status failed", exc_info=True)
        out["status_error"] = "Could not check mergeability right now; the merge re-checks it before merging."
        return out
    out.update(
        title=status.get("title"),
        head=status.get("head"),
        base=status.get("base"),
        mergeable=status.get("mergeable"),
        checks=status.get("checks", {}).get("state"),
        ready=status.get("ready"),
        blockers=status.get("blockers") or [],
    )
    return {k: v for k, v in out.items() if v not in (None, "")}


def merge_method(value: Any) -> str:
    v = str(value or "").strip().lower()
    return v if v in MERGE_METHODS else "merge"


def _clip(text: str, limit: int = _clip_max) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n… clipped {len(text) - limit} chars"


async def call_github_app(row, name: str, args: dict[str, Any], *, installs: list[Installation] | None = None) -> Any:
    if not is_configured():
        raise ToolError(
            "The Checkme GitHub App is not configured on this server yet. Use the GitHub connector or Composio GitHub "
            "instead, or ask the admin to follow docs/GITHUB_APP.md."
        )
    if installs is None:
        installs = await load_installations(row.user_id)
    if name == "list_repos":
        out = []
        for inst in installs:
            if inst.suspended:
                continue
            tok = await installation_token(inst.installation_id, None, {"metadata": "read"})
            data = await _request("GET", "/installation/repositories", auth=f"token {tok}", params={"per_page": 100})
            for r in data.get("repositories") or []:
                out.append(
                    {
                        "full_name": r.get("full_name"),
                        "private": r.get("private"),
                        "default_branch": r.get("default_branch"),
                        "url": r.get("html_url"),
                    }
                )
        if not installs:
            return {"repos": [], "install_url": checkme_install_link(), "note": "The GitHub App is not installed yet."}
        return {"repos": out}

    repo = args.get("repo") or ""
    owner, rname = split_repo(repo)
    full = f"{owner}/{rname}"
    write = name in WRITE_TOOLS
    extra = TOOL_PERMS.get(name)
    base = f"/repos/{owner}/{rname}"
    if name in ("check_merge", "merge_pull_request"):
        tok, ci = await _token_with_optional(row.user_id, full, write=write, installs=installs)
        if name == "check_merge":
            return await merge_status(base, f"token {tok}", int(args["number"]), ci_readable=ci)
        return await _merge(base, f"token {tok}", args, ci_readable=ci)
    tok = await repo_token(row.user_id, full, write=write, extra=extra, installs=installs)
    auth = f"token {tok}"
    if name in HANDLERS:
        return await HANDLERS[name](base, auth, args, owner=owner, name=rname)

    if name == "get_file":
        path = quote(str(args.get("path") or "").lstrip("/"))
        params = {"ref": args["ref"]} if args.get("ref") else None
        data = await _request("GET", f"{base}/contents/{path}", auth=auth, params=params)
        if isinstance(data, list):
            return {"type": "dir", "entries": [{"name": e.get("name"), "type": e.get("type"), "path": e.get("path")} for e in data]}
        content = data.get("content") or ""
        text = base64.b64decode(content).decode("utf-8", errors="replace") if data.get("encoding") == "base64" else content
        return {"type": "file", "path": data.get("path"), "sha": data.get("sha"), "content": _clip(text)}
    if name == "list_prs":
        limit = max(1, min(int(args.get("limit") or 20), 100))
        data = await _request("GET", f"{base}/pulls", auth=auth, params={"state": args.get("state") or "open", "per_page": limit})
        return {
            "pull_requests": [
                {
                    "number": p.get("number"),
                    "title": p.get("title"),
                    "state": p.get("state"),
                    "head": (p.get("head") or {}).get("ref"),
                    "base": (p.get("base") or {}).get("ref"),
                    "author": (p.get("user") or {}).get("login"),
                    "url": p.get("html_url"),
                    "draft": p.get("draft"),
                }
                for p in data
            ]
        }
    if name == "get_pull_request":
        num = int(args["number"])
        pr = await _request("GET", f"{base}/pulls/{num}", auth=auth)
        files = await _request("GET", f"{base}/pulls/{num}/files", auth=auth, params={"per_page": 100})
        budget = _clip_max
        shown = []
        for f in files:
            patch = f.get("patch") or ""
            if budget <= 0:
                patch = ""
            elif len(patch) > budget:
                patch = patch[:budget] + "\n… clipped"
            budget -= len(patch)
            shown.append({"file": f.get("filename"), "status": f.get("status"), "additions": f.get("additions"), "deletions": f.get("deletions"), "patch": patch})
        return {
            "number": num,
            "title": pr.get("title"),
            "body": _clip(pr.get("body") or "", 8000),
            "state": pr.get("state"),
            "head": (pr.get("head") or {}).get("ref"),
            "base": (pr.get("base") or {}).get("ref"),
            "mergeable": pr.get("mergeable"),
            "mergeable_state": pr.get("mergeable_state"),
            "draft": pr.get("draft"),
            "merged": pr.get("merged"),
            "head_sha": (pr.get("head") or {}).get("sha"),
            "head_repo": ((pr.get("head") or {}).get("repo") or {}).get("full_name"),
            "author": (pr.get("user") or {}).get("login"),
            "comments": (pr.get("comments") or 0) + (pr.get("review_comments") or 0),
            "url": pr.get("html_url"),
            "files": shown,
        }
    if name == "list_issues":
        limit = max(1, min(int(args.get("limit") or 30), 100))
        params = {"state": args.get("state") or "open", "per_page": limit}
        if args.get("labels"):
            params["labels"] = args["labels"]
        data = await _request("GET", f"{base}/issues", auth=auth, params=params)
        return {
            "issues": [
                {
                    "number": i.get("number"),
                    "title": i.get("title"),
                    "labels": [lb.get("name") for lb in i.get("labels") or []],
                    "author": (i.get("user") or {}).get("login"),
                    "comments": i.get("comments"),
                    "url": i.get("html_url"),
                    "body": _clip(i.get("body") or "", 1500),
                }
                for i in data
                if "pull_request" not in i
            ]
        }
    if name == "comment_pull_request":
        data = await _request("POST", f"{base}/issues/{int(args['number'])}/comments", auth=auth, json={"body": args.get("body") or ""})
        return {"ok": True, "url": data.get("html_url")}
    if name == "create_pull_request":
        target = args.get("base") or (await _request("GET", base, auth=auth)).get("default_branch") or "main"
        data = await _request(
            "POST",
            f"{base}/pulls",
            auth=auth,
            json={
                "title": args.get("title") or args["head"],
                "head": args["head"],
                "base": target,
                "body": args.get("body") or "",
                "draft": bool(args.get("draft")),
            },
        )
        return {"number": data.get("number"), "url": data.get("html_url"), "head": args["head"], "base": target}
    if name == "push_files":
        return await _push_files(base, auth, args)
    raise ToolError(f"Unknown GitHub App tool {name}")


# ---------------------------------------------------------------------------
# repository exploration, PR discussion and merging

TOOL_PERMS: dict[str, dict[str, str]] = {
    "comment_pull_request": {"issues": "write"},
    "list_issues": {"issues": "read"},
    "get_issue": {"issues": "read"},
    "get_pr_comments": {"issues": "read"},
    "create_review": {"pull_requests": "write"},
    "request_reviewers": {"pull_requests": "write"},
    "update_pull_request": {"pull_requests": "write"},
}
_REF_OK = re.compile(r"^[A-Za-z0-9._/@{}~^-]{1,200}$")
FAILING = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "error"}


def _ref(value: Any, what: str = "ref") -> str:
    ref = str(value or "").strip()
    if not ref:
        return ""
    if not _REF_OK.match(ref) or ".." in ref or ref.startswith(("-", "/")):
        raise ToolError(f"Bad {what} {ref!r}")
    return ref


def _limit(args: dict, default: int, top: int = 100) -> int:
    try:
        return max(1, min(int(args.get("limit") or default), top))
    except (TypeError, ValueError):
        return default


def _files(files: list, budget: int = _clip_max) -> list[dict]:
    shown = []
    for f in files or []:
        patch = f.get("patch") or ""
        if budget <= 0:
            patch = ""
        elif len(patch) > budget:
            patch = patch[:budget] + "\n… clipped"
        budget -= len(patch)
        shown.append(
            {
                "file": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "patch": patch,
            }
        )
    return shown


def _commit_line(c: dict) -> dict:
    info = c.get("commit") or {}
    author = info.get("author") or {}
    return {
        "sha": str(c.get("sha") or "")[:12],
        "message": str(info.get("message") or "").split("\n", 1)[0][:200],
        "author": author.get("name") or (c.get("author") or {}).get("login"),
        "date": author.get("date"),
    }


def _comment(c: dict, limit: int = 3000) -> dict:
    out = {
        "author": (c.get("user") or {}).get("login"),
        "created_at": c.get("created_at") or c.get("submitted_at"),
        "body": _clip(c.get("body") or "", limit),
    }
    for key in ("path", "line", "state"):
        if c.get(key) not in (None, ""):
            out[key] = c[key]
    return out


async def _default_branch(base: str, auth: str) -> str:
    return (await _request("GET", base, auth=auth)).get("default_branch") or "main"


async def _list_branches(base, auth, args, **_):
    data = await _request("GET", f"{base}/branches", auth=auth, params={"per_page": _limit(args, 100)})
    return {
        "default_branch": await _default_branch(base, auth),
        "branches": [
            {"name": b.get("name"), "sha": str((b.get("commit") or {}).get("sha") or "")[:12], "protected": b.get("protected")}
            for b in data
        ],
    }


async def _get_tree(base, auth, args, **_):
    ref = _ref(args.get("ref")) or await _default_branch(base, auth)
    prefix = str(args.get("path") or "").strip("/")
    data = await _request("GET", f"{base}/git/trees/{quote(ref, safe='')}", auth=auth, params={"recursive": "1"})
    entries = [
        {"path": e.get("path"), "type": "dir" if e.get("type") == "tree" else "file", "size": e.get("size")}
        for e in data.get("tree") or []
        if not prefix or str(e.get("path") or "") == prefix or str(e.get("path") or "").startswith(prefix + "/")
    ]
    cap = 2000
    return {
        "ref": ref,
        "path": prefix,
        "count": len(entries),
        "entries": entries[:cap],
        "truncated": bool(data.get("truncated")) or len(entries) > cap,
    }


async def _search_code(base, auth, args, owner="", name=""):
    query = str(args.get("query") or "").strip()
    if not query:
        raise ToolError("query is required")
    data = await _request(
        "GET",
        "/search/code",
        auth=auth,
        params={"q": f"{query} repo:{owner}/{name}", "per_page": _limit(args, 30)},
        headers={"Accept": "application/vnd.github.text-match+json"},
    )
    return {
        "total": data.get("total_count"),
        "note": "GitHub code search covers the default branch only.",
        "results": [
            {
                "path": i.get("path"),
                "url": i.get("html_url"),
                "matches": [str(m.get("fragment") or "")[:400] for m in i.get("text_matches") or []][:3],
            }
            for i in data.get("items") or []
        ],
    }


async def _list_commits(base, auth, args, **_):
    params: dict[str, Any] = {"per_page": _limit(args, 30)}
    if args.get("ref"):
        params["sha"] = _ref(args["ref"])
    for key in ("path", "author"):
        if args.get(key):
            params[key] = str(args[key])
    data = await _request("GET", f"{base}/commits", auth=auth, params=params)
    return {"commits": [_commit_line(c) for c in data]}


async def _get_commit(base, auth, args, **_):
    sha = _ref(args.get("sha"), "sha")
    if not sha:
        raise ToolError("sha is required")
    c = await _request("GET", f"{base}/commits/{quote(sha, safe='')}", auth=auth)
    info = c.get("commit") or {}
    return {
        **_commit_line(c),
        "sha": c.get("sha"),
        "message": _clip(info.get("message") or "", 4000),
        "parents": [str(p.get("sha") or "")[:12] for p in c.get("parents") or []],
        "stats": c.get("stats"),
        "files": _files(c.get("files") or []),
        "url": c.get("html_url"),
    }


async def _compare_refs(base, auth, args, **_):
    left, right = _ref(args.get("base"), "base"), _ref(args.get("head"), "head")
    if not left or not right:
        raise ToolError("base and head are required")
    data = await _request("GET", f"{base}/compare/{quote(left, safe='/')}...{quote(right, safe='/')}", auth=auth)
    return {
        "base": left,
        "head": right,
        "status": data.get("status"),
        "ahead_by": data.get("ahead_by"),
        "behind_by": data.get("behind_by"),
        "commits": [_commit_line(c) for c in (data.get("commits") or [])[-50:]],
        "files": _files(data.get("files") or []),
        "url": data.get("html_url"),
    }


BLAME_QUERY = """
query($owner: String!, $name: String!, $ref: String!, $path: String!) {
  repository(owner: $owner, name: $name) {
    object(expression: $ref) {
      ... on Commit {
        blame(path: $path) {
          ranges {
            startingLine
            endingLine
            commit { oid message author { name date } }
          }
        }
      }
    }
  }
}
"""


async def _blame_file(base, auth, args, owner="", name=""):
    path = str(args.get("path") or "").strip().lstrip("/")
    if not path:
        raise ToolError("path is required")
    ref = _ref(args.get("ref")) or await _default_branch(base, auth)
    data = await _request(
        "POST",
        "/graphql",
        auth=auth,
        json={"query": BLAME_QUERY, "variables": {"owner": owner, "name": name, "ref": ref, "path": path}},
    )
    if data.get("errors"):
        raise ToolError("GitHub blame failed: " + _scrub(str(data["errors"][0].get("message") or ""))[:200])
    obj = ((data.get("data") or {}).get("repository") or {}).get("object") or {}
    ranges = (obj.get("blame") or {}).get("ranges") or []
    lo = int(args.get("start_line") or 0)
    hi = int(args.get("end_line") or 0)
    out = []
    for r in ranges:
        a, b = int(r.get("startingLine") or 0), int(r.get("endingLine") or 0)
        if (lo and b < lo) or (hi and a > hi):
            continue
        c = r.get("commit") or {}
        out.append(
            {
                "lines": f"{a}-{b}",
                "sha": str(c.get("oid") or "")[:12],
                "author": (c.get("author") or {}).get("name"),
                "date": (c.get("author") or {}).get("date"),
                "message": str(c.get("message") or "").split("\n", 1)[0][:160],
            }
        )
    return {"path": path, "ref": ref, "ranges": out[:400], "truncated": len(out) > 400}


async def _get_pr_comments(base, auth, args, **_):
    num = int(args["number"])
    talk = await _request("GET", f"{base}/issues/{num}/comments", auth=auth, params={"per_page": 100})
    reviews = await _request("GET", f"{base}/pulls/{num}/reviews", auth=auth, params={"per_page": 100})
    inline = await _request("GET", f"{base}/pulls/{num}/comments", auth=auth, params={"per_page": 100})
    return {
        "number": num,
        "comments": [_comment(c) for c in talk],
        "reviews": [_comment(r, 2000) for r in reviews],
        "inline_comments": [_comment(c, 1500) for c in inline],
    }


async def _get_issue(base, auth, args, **_):
    num = int(args["number"])
    i = await _request("GET", f"{base}/issues/{num}", auth=auth)
    talk = await _request("GET", f"{base}/issues/{num}/comments", auth=auth, params={"per_page": 100}) if i.get("comments") else []
    return {
        "number": num,
        "title": i.get("title"),
        "state": i.get("state"),
        "author": (i.get("user") or {}).get("login"),
        "labels": [lb.get("name") for lb in i.get("labels") or []],
        "assignees": [a.get("login") for a in i.get("assignees") or []],
        "is_pull_request": "pull_request" in i,
        "body": _clip(i.get("body") or "", 8000),
        "comments": [_comment(c) for c in talk],
        "url": i.get("html_url"),
    }


async def _update_pull_request(base, auth, args, **_):
    num = int(args["number"])
    body = {k: args[k] for k in ("title", "body", "state") if args.get(k) not in (None, "")}
    if args.get("base"):
        body["base"] = _ref(args["base"], "base branch")
    if not body:
        raise ToolError("Nothing to update: give title, body, base or state.")
    pr = await _request("PATCH", f"{base}/pulls/{num}", auth=auth, json=body)
    return {"number": num, "updated": sorted(body), "state": pr.get("state"), "url": pr.get("html_url")}


async def _create_review(base, auth, args, **_):
    num = int(args["number"])
    event = str(args.get("event") or "COMMENT").upper()
    if event not in {"COMMENT", "REQUEST_CHANGES", "APPROVE"}:
        raise ToolError("event must be COMMENT, REQUEST_CHANGES or APPROVE")
    comments = [
        {"path": str(c.get("path") or ""), "line": int(c.get("line") or 0), "side": "RIGHT", "body": str(c.get("body") or "")}
        for c in args.get("comments") or []
        if c.get("path") and c.get("line") and c.get("body")
    ]
    text = str(args.get("body") or "")
    if event != "APPROVE" and not text.strip() and not comments:
        raise ToolError("A review needs a body or inline comments.")
    payload: dict[str, Any] = {"event": event, "body": text}
    if comments:
        payload["comments"] = comments
    data = await _request("POST", f"{base}/pulls/{num}/reviews", auth=auth, json=payload)
    return {"ok": True, "id": data.get("id"), "state": data.get("state"), "url": data.get("html_url")}


async def _request_reviewers(base, auth, args, **_):
    num = int(args["number"])
    users = [str(u).lstrip("@") for u in args.get("reviewers") or [] if str(u).strip()]
    teams = [str(t) for t in args.get("team_reviewers") or [] if str(t).strip()]
    if not users and not teams:
        raise ToolError("Give reviewers (logins) or team_reviewers.")
    data = await _request(
        "POST", f"{base}/pulls/{num}/requested_reviewers", auth=auth, json={"reviewers": users, "team_reviewers": teams}
    )
    return {
        "ok": True,
        "requested": [u.get("login") for u in data.get("requested_reviewers") or []],
        "teams": [t.get("slug") for t in data.get("requested_teams") or []],
    }


HANDLERS = {
    "list_branches": _list_branches,
    "get_tree": _get_tree,
    "search_code": _search_code,
    "list_commits": _list_commits,
    "get_commit": _get_commit,
    "compare_refs": _compare_refs,
    "blame_file": _blame_file,
    "get_pr_comments": _get_pr_comments,
    "get_issue": _get_issue,
    "update_pull_request": _update_pull_request,
    "create_review": _create_review,
    "request_reviewers": _request_reviewers,
}


async def _token_with_optional(user_id, repo: str, *, write: bool, installs) -> tuple[str, bool]:
    """Token that can also read CI when the App has the Checks / Commit statuses permissions."""
    try:
        return await repo_token(user_id, repo, write=write, extra=dict(CHECK_PERMS), installs=installs), True
    except GitHubError as exc:
        if exc.status not in (403, 422):
            raise
    return await repo_token(user_id, repo, write=write, installs=installs), False


async def ci_status(base: str, auth: str, sha: str) -> dict:
    """Combined state of check runs and commit statuses for one commit."""
    failing, pending, passing = [], [], []
    seen = False
    try:
        runs = await _request("GET", f"{base}/commits/{sha}/check-runs", auth=auth, params={"per_page": 100})
        for r in runs.get("check_runs") or []:
            seen = True
            label = str(r.get("name") or "check")
            if r.get("status") != "completed":
                pending.append(label)
            elif r.get("conclusion") in FAILING:
                failing.append(f"{label} ({r.get('conclusion')})")
            else:
                passing.append(label)
    except GitHubError as exc:
        if exc.status not in (403, 404):
            raise
    try:
        combined = await _request("GET", f"{base}/commits/{sha}/status", auth=auth)
        for st in combined.get("statuses") or []:
            seen = True
            label = str(st.get("context") or "status")
            state = st.get("state")
            if state == "pending":
                pending.append(label)
            elif state in FAILING or state == "failure":
                failing.append(f"{label} ({state})")
            else:
                passing.append(label)
    except GitHubError as exc:
        if exc.status not in (403, 404):
            raise
    state = "failing" if failing else "pending" if pending else "passing" if seen else "none"
    return {"state": state, "failing": failing, "pending": pending, "passing": len(passing)}


async def merge_status(base: str, auth: str, num: int, *, ci_readable: bool = True) -> dict:
    """Is this PR ready to merge? Plain-language blockers for conflicts, drafts, protection and CI."""
    import asyncio

    pr = await _request("GET", f"{base}/pulls/{num}", auth=auth)
    for _ in range(3):  # GitHub computes mergeability lazily after a push
        if pr.get("state") != "open" or pr.get("mergeable") is not None:
            break
        await asyncio.sleep(1.5)
        pr = await _request("GET", f"{base}/pulls/{num}", auth=auth)
    head = pr.get("head") or {}
    target = pr.get("base") or {}
    head_ref, base_ref = head.get("ref") or "", target.get("ref") or ""
    mstate = pr.get("mergeable_state") or "unknown"
    if ci_readable and head.get("sha"):
        checks = await ci_status(base, auth, head["sha"])
    else:
        checks = {
            "state": "unknown",
            "note": "CI status is unknown: the Checkme GitHub App lacks the Checks / Commit statuses read permissions.",
        }
    conflicts = pr.get("mergeable") is False or mstate == "dirty"
    blockers: list[str] = []
    notes: list[str] = []
    if pr.get("merged"):
        blockers.append("It is already merged.")
    elif pr.get("state") != "open":
        blockers.append("The pull request is closed.")
    if pr.get("draft"):
        blockers.append("It is a draft. Mark it ready for review first.")
    if conflicts:
        blockers.append(
            f"It has merge conflicts with {base_ref}. To fix: git_checkout the branch {head_ref}, merge origin/{base_ref} "
            "in the sandbox, resolve the conflicted files, run the tests, commit, and push the branch (with approval)."
        )
    elif pr.get("state") == "open" and pr.get("mergeable") is None:
        blockers.append("GitHub is still computing whether it can merge. Try again in a minute.")
    if checks["state"] == "failing":
        blockers.append("Checks are failing: " + ", ".join(checks["failing"][:8]))
    elif checks["state"] == "pending":
        blockers.append("Checks are still running: " + ", ".join(checks["pending"][:8]))
    if mstate == "blocked" and not conflicts:
        blockers.append("Branch protection blocks the merge (required reviews or required checks).")
    if mstate == "behind":
        notes.append(f"The branch is behind {base_ref}; merging is still possible unless protection requires it to be up to date.")
    if checks.get("note"):
        notes.append(checks["note"])
    return {
        "number": num,
        "title": pr.get("title"),
        "url": pr.get("html_url"),
        "state": "merged" if pr.get("merged") else pr.get("state"),
        "draft": bool(pr.get("draft")),
        "head": head_ref,
        "head_sha": head.get("sha"),
        "head_repo": (head.get("repo") or {}).get("full_name"),
        "base": base_ref,
        "base_repo": (target.get("repo") or {}).get("full_name"),
        "mergeable": pr.get("mergeable"),
        "mergeable_state": mstate,
        "conflicts": conflicts,
        "checks": checks,
        "ready": not blockers,
        "blockers": blockers,
        "notes": notes,
    }


async def _merge(base: str, auth: str, args: dict, *, ci_readable: bool) -> dict:
    """Runs only after the user approved the merge card."""
    num = int(args["number"])
    method = merge_method(args.get("method"))
    st = await merge_status(base, auth, num, ci_readable=ci_readable)
    if st["state"] == "merged":
        return {"merged": False, "already_merged": True, "url": st["url"]}
    hard = [b for b in st["blockers"] if not b.startswith(("Checks are", "Branch protection"))]
    soft = [b for b in st["blockers"] if b.startswith("Checks are")]
    if hard or (soft and not args.get("allow_failing_checks")):
        return {
            "merged": False,
            "blocked": True,
            "conflicts": st["conflicts"],
            "blockers": hard + soft,
            "checks": st["checks"],
            "url": st["url"],
            "note": "Nothing was merged. Report the blockers to the user"
            + (" and offer to fix the conflicts on the branch." if st["conflicts"] else "."),
        }
    body: dict[str, Any] = {"merge_method": method}
    if st.get("head_sha"):
        body["sha"] = st["head_sha"]
    for key in ("commit_title", "commit_message"):
        if args.get(key):
            body[key] = str(args[key])[:5000]
    try:
        data = await _request("PUT", f"{base}/pulls/{num}/merge", auth=auth, json=body)
    except GitHubError as exc:
        why = {
            405: "GitHub refused the merge (branch protection, a merge method the repo does not allow, or it is not mergeable).",
            409: "The branch changed after it was checked. Nothing was merged; check it again before asking to merge.",
            422: "GitHub could not merge this pull request.",
        }.get(exc.status, "The merge failed.")
        return {"merged": False, "error": f"{why} {exc}", "blockers": st["blockers"], "url": st["url"]}
    out: dict[str, Any] = {
        "merged": bool(data.get("merged")),
        "sha": data.get("sha"),
        "method": method,
        "message": data.get("message"),
        "url": st["url"],
    }
    if out["merged"] and args.get("delete_branch"):
        same_repo = (st.get("head_repo") or "").lower() == (st.get("base_repo") or "").lower()
        if not same_repo:
            out["branch_note"] = "The head branch is on a fork; it was not deleted."
        else:
            try:
                await _request("DELETE", f"{base}/git/refs/heads/{quote(st['head'], safe='/')}", auth=auth)
                out["branch_deleted"] = st["head"]
            except GitHubError as exc:
                out["branch_note"] = f"Merged, but the branch was not deleted: {exc}"
    return out


async def _push_files(base: str, auth: str, args: dict) -> dict:
    branch = str(args.get("branch") or "")
    if not AGENT_BRANCH.match(branch):
        raise ToolError("Agents may only push to branches named agent/<something>.")
    files = args.get("files") or []
    if not files:
        raise ToolError("No files to push.")
    head_sha = None
    try:
        ref = await _request("GET", f"{base}/git/ref/heads/{quote(branch)}", auth=auth)
        head_sha = ref["object"]["sha"]
        created = False
    except GitHubError as exc:
        if exc.status != 404:
            raise
        start = args.get("base") or (await _request("GET", base, auth=auth)).get("default_branch") or "main"
        ref = await _request("GET", f"{base}/git/ref/heads/{quote(start)}", auth=auth)
        head_sha = ref["object"]["sha"]
        created = True
    commit = await _request("GET", f"{base}/git/commits/{head_sha}", auth=auth)
    tree_items = []
    for f in files:
        path = str(f.get("path") or "").lstrip("/")
        if not path or ".." in path.split("/"):
            raise ToolError(f"Bad file path {path!r}")
        if f.get("delete"):
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
        else:
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "content": str(f.get("content") or "")})
    tree = await _request("POST", f"{base}/git/trees", auth=auth, json={"base_tree": commit["tree"]["sha"], "tree": tree_items})
    new = await _request(
        "POST",
        f"{base}/git/commits",
        auth=auth,
        json={"message": args.get("message") or "Update from Checkme", "tree": tree["sha"], "parents": [head_sha]},
    )
    if created:
        await _request("POST", f"{base}/git/refs", auth=auth, json={"ref": f"refs/heads/{branch}", "sha": new["sha"]})
    else:
        await _request("PATCH", f"{base}/git/refs/heads/{quote(branch)}", auth=auth, json={"sha": new["sha"], "force": False})
    return {"branch": branch, "commit": new["sha"], "files": len(files), "created_branch": created}


# ---------------------------------------------------------------------------
# git-write gate (covers this App and other GitHub connectors)

_GIT_WRITE = re.compile(
    r"(merge|push|create[_ ]?(a[_ ])?pull|pull[_ ]?request[_ ]?create|open[_ ]?pull|commit|create[_ ]or[_ ]update[_ ]file"
    r"|update[_ ]?ref|create[_ ]?ref|delete[_ ]?(a[_ ])?(branch|ref|file)|update[_ ]?file)",
    re.I,
)


def is_git_write(provider: str, slug: str, tool: str, args: dict | None = None, connector_text: str = "") -> bool:
    """True for any action that writes to a git host. These always need a human approval."""
    if provider == PROVIDER:
        return tool in WRITE_TOOLS
    names = [tool]
    for key in ("tool_slug", "tool", "action", "name", "slug"):
        val = (args or {}).get(key)
        if isinstance(val, str):
            names.append(val)
    joined = " ".join(names)
    githubish = any(k in f"{slug} {connector_text} {joined}".lower() for k in ("github", "gitlab", "bitbucket"))
    return githubish and bool(_GIT_WRITE.search(joined))
