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
WRITE_TOOLS = frozenset({"create_pull_request", "push_files", "merge_pull_request"})
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


async def _request(method: str, url: str, *, auth: str, json: Any = None, params: dict | None = None) -> Any:
    full = url if url.startswith("http") else API + url
    async with _client() as client:
        res = await client.request(method, full, json=json, params=params, headers={"Authorization": auth})
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
        description="Merge a pull request. Always waits for the user's explicit approval.",
        input_schema=_s(
            {
                "repo": REPO,
                "number": {"type": "integer"},
                "method": {"type": "string", "enum": ["squash", "merge", "rebase"]},
            },
            ["repo", "number"],
        ),
        group="github",
        read_only=False,
        destructive=True,
        tags=("github", "pull requests", "merge"),
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
        out.update(number=args.get("number"), method=args.get("method") or "squash")
    return out


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
    extra = {"issues": "write"} if name == "comment_pull_request" else ({"issues": "read"} if name == "list_issues" else None)
    tok = await repo_token(row.user_id, full, write=write, extra=extra, installs=installs)
    auth = f"token {tok}"
    base = f"/repos/{owner}/{rname}"

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
    if name == "merge_pull_request":
        num = int(args["number"])
        data = await _request(
            "PUT", f"{base}/pulls/{num}/merge", auth=auth, json={"merge_method": args.get("method") or "squash"}
        )
        return {"merged": bool(data.get("merged")), "sha": data.get("sha"), "message": data.get("message")}
    raise ToolError(f"Unknown GitHub App tool {name}")


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
