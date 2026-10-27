"""Plan-and-build helpers for coding agents (focus "coding").

The Coder is a general-purpose software engineer: explore a repo, change code, test,
commit, open/update/review PRs and merge (merge behind an approval card), and help with
deploys. Everything runs through existing infrastructure: the `computer` connector
(Daytona sandbox `exec`), the Checkme GitHub App or another GitHub connector, and any
deploy connector (Render MCP, Vercel, ...).
"""

from __future__ import annotations

from typing import Any

COMPUTER_SLUG = "computer"
SHELL_TIMEOUT_DEFAULT = 300
SHELL_TIMEOUT_MAX = 900
DEPLOY_HINTS = ("render", "vercel", "netlify", "fly", "railway", "heroku", "cloudflare")

# One shell round-trip that reports the toolchain and whether git can push.
PROBE_COMMAND = (
    "echo git=$(git --version 2>/dev/null | head -1); "
    "echo gh=$(gh --version 2>/dev/null | head -1); "
    "echo gh_auth=$(gh auth status >/dev/null 2>&1 && echo ok || echo none); "
    "echo git_cred=$(git config --global credential.helper 2>/dev/null || echo none); "
    "echo node=$(node --version 2>/dev/null); echo python=$(python3 --version 2>/dev/null); "
    "echo rg=$(command -v rg >/dev/null 2>&1 && echo ok); echo ctags=$(command -v ctags >/dev/null 2>&1 && echo ok); "
    "echo dev_toolchain=$([ -f \"$HOME/.checkme/toolchain-v1\" ] && echo ready || echo none); "
    "echo home=$HOME; mkdir -p ~/work && echo work=$(cd ~/work && pwd)"
)


def _text(conn) -> str:
    bits = [getattr(conn, a, "") or "" for a in ("slug", "provider", "name", "alias", "description")]
    tags = getattr(conn, "tags", None) or []
    return " ".join([*map(str, bits), *map(str, tags)]).lower()


def classify_connectors(connectors: dict[str, Any]) -> dict:
    computer = connectors.get(COMPUTER_SLUG)
    github = [c for c in connectors.values() if "github" in _text(c)]
    deploy = [c for c in connectors.values() if any(h in _text(c) for h in DEPLOY_HINTS) and c is not computer]
    composio = connectors.get("composio")
    return {
        "computer": computer,
        "github": github,
        "deploy": deploy,
        "composio": composio,
    }


def parse_probe(stdout: str) -> dict:
    out: dict[str, str] = {}
    for line in (stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def readiness(
    connectors: dict[str, Any], probe: dict | None = None, probe_error: str = "", github_app: dict | None = None
) -> dict:
    """What this coding agent can do right now, and exactly what is missing."""
    app = github_app or {}
    found = classify_connectors(connectors)
    found["github"] = [c for c in found["github"] if getattr(c, "provider", "") != "github_app"]
    computer = found["computer"]
    missing: list[str] = []
    can: list[str] = ["plan the work (write_todos)"]
    probe = probe or {}
    if computer is None:
        missing.append(
            "The computer (Daytona sandbox) is not available for this agent, so I cannot clone, edit or run code. "
            "It needs DAYTONA_API_KEY on the server, or the computer connector assigned to this agent."
        )
    elif probe_error:
        missing.append(f"The computer did not respond: {probe_error[:200]}")
    else:
        can.append(
            "clone repos, edit files, install dependencies and run tests on the computer "
            "(git_checkout, sandbox_shell, run_tests, git_diff, git_commit)"
        )
        if probe and not probe.get("git"):
            can.append("install git on the computer (git_checkout does it automatically)")
        if probe and probe.get("dev_toolchain") != "ready":
            can.append(
                "set up the headless IDE once with ensure_dev_toolchain (gh, ripgrep, fd, jq, tree, ctags, node package "
                "managers, uv, pytest); it is not set up on this computer yet"
            )
        elif probe:
            can.append(
                "understand code (repo_overview, find_symbol, find_references, read_file_range, git_inspect) and test all "
                "cases (run_checks: lint, type-check, unit, integration, build)"
            )
    git_push = probe.get("gh_auth") == "ok" or probe.get("git_cred", "none") not in {"", "none"}
    if app.get("installed"):
        can.append(
            "explore repos (tree, files, code search, history, blame, PRs, issues), clone private repos, push agent/* "
            "branches, open/update/review pull requests and merge them through the Checkme GitHub App ("
            + ", ".join(app.get("accounts") or [])
            + "); every push, PR edit and merge waits for the user's approval"
        )
    elif app.get("configured") and not found["github"] and not git_push:
        missing.append(
            "The Checkme GitHub App is not installed yet, so I cannot reach private repos, push or open pull requests. "
            f"Install it on the repos I should work on: {app.get('install_url')}"
        )
    elif found["github"]:
        can.append("push branches and open pull requests through " + ", ".join(sorted({c.name for c in found["github"]})))
    elif git_push:
        can.append("push and open pull requests with git/gh credentials already on the computer")
    elif found["composio"] is not None:
        missing.append(
            "GitHub may not be connected. Check call_tool(composio, list_connections); if github is not listed, "
            "connect it with connect_app github so I can push branches and open pull requests. "
            "Public repos can still be cloned and worked on."
        )
    else:
        missing.append(
            "GitHub is not connected, so I cannot push or open pull requests. Connect a GitHub connector "
            "(or connect_app github). Public repos can still be cloned and worked on."
        )
    if found["deploy"]:
        can.append("trigger and check deploys through " + ", ".join(sorted({c.name for c in found["deploy"]})))
    else:
        missing.append("No deploy connector (Render, Vercel, Netlify, ...) is connected, so I cannot trigger or check deploys.")
    return {
        "computer": "ready" if computer is not None and not probe_error else "missing",
        "toolchain": {k: v for k, v in probe.items() if k in {"git", "gh", "node", "python", "rg", "ctags", "work"} and v},
        "dev_toolchain": probe.get("dev_toolchain") or "unknown",
        "git_push_credentials": git_push,
        "github_connectors": [{"slug": c.slug, "name": c.name} for c in found["github"]],
        "deploy_connectors": [{"slug": c.slug, "name": c.name} for c in found["deploy"]],
        "github_app": {
            "configured": bool(app.get("configured")),
            "installed": bool(app.get("installed")),
            "accounts": list(app.get("accounts") or []),
            "install_url": app.get("install_url") or "",
        },
        "can": can,
        "missing": missing,
        "workdir": probe.get("work") or "~/work",
    }


def clamp_timeout(value: Any) -> int:
    try:
        n = int(value or SHELL_TIMEOUT_DEFAULT)
    except (TypeError, ValueError):
        n = SHELL_TIMEOUT_DEFAULT
    return max(10, min(n, SHELL_TIMEOUT_MAX))


def is_coding_agent(agent) -> bool:
    return (getattr(agent, "focus", "") or "") == "coding"


# ---------------------------------------------------------------------------
# Safe git tools (git_checkout, run_tests, git_diff, git_commit, push_branch).
#
# They run through Daytona exec like sandbox_shell, with three differences:
# * The GitHub token is minted per command (GitHub App installation token scoped to
#   one repo) and passed as the CHECKME_GIT_TOKEN env var. A one-shot `-c
#   credential.helper` reads it, so the token never appears in the command string,
#   the clone URL, .git/config, the credential store, logs or tool output.
# * Pushes only go to agent/* branches (or the verified head branch of a PR the user
#   named), and always behind an approval card.
# * Output is scrubbed of the token before anything sees it.

import re as _re
import shlex as _shlex

CODING_SLUG = "coding"  # confirm-card namespace for push_branch
TOKEN_ENV = "CHECKME_GIT_TOKEN"
AGENT_BRANCH = _re.compile(r"^agent/[A-Za-z0-9._/-]{1,120}$")
REF_RE = _re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
OUTPUT_MAX = 16_000
GIT_TOOLS = ("git_checkout", "git_inspect", "run_tests", "git_diff", "git_commit", "push_branch")
AUTHOR_NAME = "Checkme Coder"
AUTHOR_EMAIL = "checkme-coder@users.noreply.github.com"

# `credential.helper=` clears any configured helpers for this one command, then ours
# prints the token from the environment. Single quotes keep $CHECKME_GIT_TOKEN
# unexpanded until git runs the helper.
GIT_AUTH = (
    "-c credential.helper= "
    "-c 'credential.helper=!f() { echo username=x-access-token; echo \"password=${CHECKME_GIT_TOKEN}\"; }; f'"
)

PRELUDE = (
    'export PATH="$HOME/.local/bin:$PATH"; '
    "for n in /usr/local/share/nvm/nvm.sh /usr/local/nvm/nvm.sh \"$HOME/.nvm/nvm.sh\"; do "
    '[ -s "$n" ] && . "$n" >/dev/null 2>&1 && break; done; '
    "export GIT_TERMINAL_PROMPT=0; "
)

# The Daytona slim image (self-hosted default) ships without git; the cloud default does.
ENSURE_GIT = (
    "command -v git >/dev/null 2>&1 || { echo 'installing git...'; "
    "(sudo -n apt-get update -qq && sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git) >/dev/null 2>&1 || "
    "(apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git) >/dev/null 2>&1; "
    "command -v git >/dev/null 2>&1 || { echo 'git is not installed and could not be installed'; exit 127; }; }; "
)


class GitToolError(ValueError):
    pass


def split_repo(repo: str) -> tuple[str, str]:
    from app.connectors.github_app import split_repo as _split

    try:
        return _split(repo)
    except Exception as exc:  # ToolError
        raise GitToolError(str(exc)) from exc


def check_ref(ref: str | None, what: str = "branch") -> str:
    ref = (ref or "").strip()
    if not ref:
        return ""
    if not REF_RE.match(ref) or ".." in ref or ref.startswith(("-", "/")) or ref.endswith((".lock", "/")):
        raise GitToolError(f"Bad {what} name {ref!r}")
    return ref


def check_agent_branch(branch: str) -> str:
    branch = check_ref(branch)
    if not AGENT_BRANCH.match(branch or ""):
        raise GitToolError(
            "Agents can only push branches named agent/<short-name>, e.g. agent/fix-login. "
            "Check out such a branch with git_checkout first."
        )
    return branch


def check_push_branch(branch: str, pr_branch: str = "") -> str:
    """agent/* branches always; another branch only when it is the verified head of the PR the user named."""
    branch = check_ref(branch)
    if AGENT_BRANCH.match(branch or ""):
        return branch
    if branch and pr_branch and branch == pr_branch:
        return branch
    return check_agent_branch(branch)


INSPECT_ACTIONS = ("status", "branches", "log", "show", "diff", "blame", "grep", "tree", "read")


def inspect_script(name: str, action: str, ref: str = "", ref2: str = "", path: str = "", query: str = "",
                   start: int = 0, end: int = 0, limit: int = 50, regex: bool = False) -> str:
    """Read-only git and code exploration in the sandbox clone (fallback for the GitHub App reads)."""
    q = _shlex.quote
    action = (action or "").strip().lower()
    if action not in INSPECT_ACTIONS:
        raise GitToolError(f"action must be one of {', '.join(INSPECT_ACTIONS)}")
    ref, ref2 = check_ref(ref, "ref"), check_ref(ref2, "ref")
    path = (path or "").strip()
    if path.startswith("-") or "\n" in path:
        raise GitToolError("Bad path")
    limit = max(1, min(int(limit or 50), 500))
    pathspec = f" -- {q(path)}" if path else ""
    cd = f"cd {repo_dir(name)} || {{ echo 'run git_checkout first'; exit 1; }}; "
    if action == "status":
        body = "git status -sb; git log --oneline -5"
    elif action == "branches":
        body = "git fetch --quiet --prune origin 2>/dev/null; git branch -a --sort=-committerdate | head -200"
    elif action == "log":
        body = f"git log --format='%h %ad %an  %s' --date=short -n {limit} {q(ref) if ref else ''}{pathspec}"
    elif action == "show":
        body = f"git show --stat --patch {q(ref or 'HEAD')}{pathspec} | head -c 14000"
    elif action == "diff":
        left = q(ref or "origin/HEAD")
        right = q(ref2) if ref2 else ""
        rng = f"{left}...{right}" if right else left
        body = f"git diff --stat {rng}{pathspec} | tail -40; echo '--'; git diff {rng}{pathspec} | head -c 14000"
    elif action == "blame":
        if not path:
            raise GitToolError("blame needs path")
        lines = f" -L {int(start)},{int(end or start + 80)}" if start else ""
        body = f"git blame --date=short{lines} {q(ref) if ref else ''} -- {q(path)} | head -400"
    elif action == "grep":
        if not (query or "").strip():
            raise GitToolError("grep needs query")
        where = q(path) if path else "."
        rg_flag, gg_flag = ("", "-E") if regex else ("-F", "-F")
        body = (
            f"if command -v rg >/dev/null; then rg -n --hidden -g '!.git' -m 20 {rg_flag} -e {q(query)} -- {where}; "
            f"else git grep -n {gg_flag} -e {q(query)} -- {where}; fi | head -{limit * 4}"
        )
    elif action == "tree":
        body = f"git ls-tree -r --name-only {q(ref or 'HEAD')}{pathspec} | head -{limit * 20}"
    else:  # read
        if not path:
            raise GitToolError("read needs path")
        a, b = (int(start) or 1), (int(end) or (int(start) or 1) + 400)
        src = f"git show {q(ref)}:{q(path)}" if ref else f"cat -- {q(path)}"
        body = f"{src} | awk 'NR>={a} && NR<={b} {{ printf \"%6d  %s\\n\", NR, $0 }}' | head -c 30000"
    return cd + "{ " + body + "; } 2>&1"


def repo_dir(name: str) -> str:
    return f'"$HOME/work/{name}"'


def bash(script: str) -> str:
    """Daytona runs commands with zsh in its default snapshots; run ours with bash."""
    return "bash -c " + _shlex.quote("set -o pipefail; " + PRELUDE + script)


def checkout_script(owner: str, name: str, branch: str = "", base: str = "", authed: bool = True) -> str:
    q = _shlex.quote
    auth = GIT_AUTH + " " if authed else ""
    url = f"https://github.com/{owner}/{name}.git"
    base_expr = (
        q(base)
        if base
        else "$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##')"
    )
    lines = [
        ENSURE_GIT,
        'mkdir -p "$HOME/work" && cd "$HOME/work" || exit 1; ',
        f"if [ -d {q(name)}/.git ]; then cd {q(name)} && git remote set-url origin {q(url)} && git {auth}fetch --prune origin 2>&1 | tail -5; "
        f"else git {auth}clone --quiet {q(url)} {q(name)} 2>&1 | tail -5; cd {q(name)} || exit 1; fi; ",
        # Make sure nothing credential-shaped is stored for this repo.
        "git config --local --unset-all credential.helper >/dev/null 2>&1; ",
        f"git remote set-url origin {q(url)}; ",
        "git remote set-head origin --auto >/dev/null 2>&1; ",
        f"BASE={base_expr}; [ -n \"$BASE\" ] || BASE=main; ",
    ]
    if branch:
        b = q(branch)
        lines.append(
            f"if git show-ref --verify --quiet refs/heads/{b}; then git checkout --quiet {b}; "
            f"elif git rev-parse --verify --quiet origin/{b} >/dev/null; then git checkout --quiet -b {b} --track origin/{b}; "
            f'else git checkout --quiet --no-track -b {b} "origin/$BASE"; fi || exit 1; '
        )
    else:
        lines.append('git checkout --quiet "$BASE" 2>/dev/null || true; ')
    lines.append(
        'echo "repo_dir=$(pwd)"; echo "base=$BASE"; echo "branch=$(git rev-parse --abbrev-ref HEAD)"; '
        "git status -sb | head -20; git log --oneline -5"
    )
    return "".join(lines)


def test_script(name: str, command: str = "") -> str:
    cd = f"cd {repo_dir(name)} || {{ echo 'run git_checkout first'; exit 1; }}; "
    if command.strip():
        return cd + "{ " + command + " ; } 2>&1 | tail -c 15000"
    detect = (
        "if [ -f package.json ] && grep -q '\"test\"' package.json; then "
        "[ -d node_modules ] || { (npm ci --no-audit --no-fund || npm install --no-audit --no-fund) 2>&1 | tail -5; }; "
        "echo '$ npm test'; npm test --silent 2>&1 | tail -c 15000; "
        "elif [ -f pyproject.toml ] || [ -f pytest.ini ] || [ -f setup.cfg ] || [ -d tests ]; then "
        "if [ -f uv.lock ] && command -v uv >/dev/null; then echo '$ uv run pytest -q'; uv run pytest -q 2>&1 | tail -c 15000; "
        "else echo '$ python3 -m pytest -q'; python3 -m pytest -q 2>&1 | tail -c 15000; fi; "
        "elif [ -f go.mod ]; then echo '$ go test ./...'; go test ./... 2>&1 | tail -c 15000; "
        "elif [ -f Cargo.toml ]; then echo '$ cargo test'; cargo test 2>&1 | tail -c 15000; "
        "else echo 'No test runner detected. Pass command=...'; exit 2; fi"
    )
    return cd + detect


def diff_script(name: str, base: str = "") -> str:
    q = _shlex.quote
    base_ref = f"origin/{q(base)}" if base else "origin/HEAD"
    return (
        f"cd {repo_dir(name)} || {{ echo 'run git_checkout first'; exit 1; }}; "
        "git add -N . >/dev/null 2>&1; "
        "echo '## status'; git status -sb; "
        f"echo '## commits not on base'; git log --oneline {base_ref}..HEAD 2>/dev/null | head -30; "
        f"echo '## diffstat vs base'; git diff --stat {base_ref} 2>/dev/null | tail -40; "
        f"echo '## diff vs base'; git diff {base_ref} 2>/dev/null | head -c 14000"
    )


def commit_script(name: str, message: str, paths: list[str] | None = None, author: tuple[str, str] | None = None) -> str:
    q = _shlex.quote
    add = "git add -- " + " ".join(q(p) for p in paths) if paths else "git add -A"
    return (
        f"cd {repo_dir(name)} || {{ echo 'run git_checkout first'; exit 1; }}; "
        'BR=$(git rev-parse --abbrev-ref HEAD); '
        "DEF=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##'); "
        'if [ "$BR" = HEAD ] || [ "$BR" = "$DEF" ] || [ "$BR" = main ] || [ "$BR" = master ]; then '
        "echo \"refusing to commit on $BR: check out an agent/* branch (or the PR branch you were asked to update) "
        "with git_checkout first\"; exit 3; fi; "
        f"{add} && "
        f"git -c user.name={q((author or (AUTHOR_NAME, AUTHOR_EMAIL))[0])} -c user.email={q((author or (AUTHOR_NAME, AUTHOR_EMAIL))[1])} commit --quiet -m {q(message)} && "
        "git log --oneline -1 && git show --stat --format= HEAD | tail -20"
    )


def push_script(name: str, branch: str) -> str:
    q = _shlex.quote
    b = q(branch)
    return (
        f"cd {repo_dir(name)} || {{ echo 'run git_checkout first'; exit 1; }}; "
        f"git rev-parse --verify --quiet refs/heads/{b} >/dev/null || {{ echo 'no local branch {branch}'; exit 1; }}; "
        f"git {GIT_AUTH} push --porcelain origin refs/heads/{b}:refs/heads/{b} 2>&1 | tail -15"
    )


def preview_script(name: str, branch: str, base: str = "") -> str:
    q = _shlex.quote
    base_ref = f"origin/{q(base)}" if base else "origin/HEAD"
    return (
        f"cd {repo_dir(name)} 2>/dev/null || exit 0; "
        f"git log --oneline {base_ref}..{q(branch)} 2>/dev/null | head -10; echo '--'; "
        f"git diff --shortstat {base_ref}...{q(branch)} 2>/dev/null"
    )


def creds_script() -> str:
    """Does the computer have its own git credentials (gh auth or a credential helper)?"""
    return (
        "if gh auth status >/dev/null 2>&1 || [ -n \"$(git config --global credential.helper 2>/dev/null)\" ]; "
        "then echo creds=ok; else echo creds=none; fi"
    )


def save_patch_script(name: str, branch: str, base: str, slug: str) -> str:
    """Keep the work when pushing is blocked: format-patch of the branch (plus a diff of anything uncommitted)."""
    q = _shlex.quote
    base_ref = f"origin/{q(base)}" if base else "origin/HEAD"
    out = f'"$HOME/work/_patches/{name}-{slug}"'
    return (
        f"cd {repo_dir(name)} || exit 1; mkdir -p {out} && "
        f"git format-patch --quiet -o {out} {base_ref}..{q(branch)} && "
        f"git diff HEAD > {out}/uncommitted.diff; ls {out} | head -20"
    )


_TOKENISH = _re.compile(r"(gh[usopr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{20,}|x-access-token:[^@\s]+@)")


def scrub(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    text = text or ""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return _TOKENISH.sub("[redacted]", text)


def clip_output(text: str, limit: int = OUTPUT_MAX) -> str:
    if len(text) <= limit:
        return text
    return f"… {len(text) - limit} earlier chars clipped\n" + text[-limit:]


# ---------------------------------------------------------------------------
# Chat routing: which messages to a coding agent become background jobs.
# Conservative on purpose: questions and chit-chat stay inline; the model can also
# start a job itself with start_coding_job.

_REPO_REF = _re.compile(r"(github\.com/[\w.-]+/[\w.-]+|\b[\w-]{2,39}/[\w.-]{2,100}\b|\b(repo|repository|codebase|pull request|pr|branch)\b)", _re.I)
_DO_VERB = _re.compile(
    r"\b(fix|implement|add|build|create|refactor|rename|bump|upgrade|migrate|write (?:a )?tests?|"
    r"open (?:a )?(?:pr|pull request)|review (?:the )?(?:pr|pull request)|merge|ship|deploy|port|remove|delete|update|change)\b",
    _re.I,
)
_QUESTION = _re.compile(r"^\s*(what|why|how|which|who|when|where|can you explain|explain|is|are|does|do)\b.*\?\s*$", _re.I | _re.S)


def looks_like_coding_task(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 12 or _QUESTION.match(t):
        return False
    if _re.search(r"\b(and/or|either/or|\d+/\d+)\b", t) and not _re.search(r"github\.com|\b(repo|repository|pr|branch)\b", t, _re.I):
        return False
    return bool(_REPO_REF.search(t) and _DO_VERB.search(t))


_FOLLOW = _re.compile(
    r"^\s*(also|and|now|then|next|please|pls|ok(ay)?|yes|yep|yeah|no|nope|go ahead|do it|ship it|merge( it)?|deploy( it)?|"
    r"squash|rebase|retry|try again|continue)\b",
    _re.I,
)
_OWNER_REPO = _re.compile(r"(?:github\.com/)?\b([\w-]{2,39}/[\w.-]{2,100})\b")


def named_repos(text: str) -> set[str]:
    return {m.group(1).lower().removesuffix(".git") for m in _OWNER_REPO.finditer(text or "") if not _re.match(r"^\d+/\d+$", m.group(1))}


def continues_job(text: str, status: str, job_repo: str = "") -> bool:
    """Does this chat message continue the latest coding job (same repo, branch and PR)?

    A job waiting for an answer takes any reply. Otherwise instructions ("also fix X", "merge it")
    continue it; questions stay inline, and a message naming a different repo starts fresh."""
    t = (text or "").strip()
    if not t:
        return False
    repos = named_repos(t)
    if job_repo and repos and job_repo.lower() not in repos:
        return False
    if status == "waiting_input":
        return True
    if _QUESTION.match(t):
        return False
    return bool(_FOLLOW.search(t) or (_DO_VERB.search(t) and (_REPO_REF.search(t) or _re.search(r"\b(it|that|this|the (?:tests?|build|bug|typo|lint))\b", t, _re.I))))


def first_step(text: str) -> str:
    """The first thing the job will do, for the one-line acknowledgement."""
    t = (text or "").lower()
    m = next((m for m in _OWNER_REPO.finditer(text or "") if not _re.match(r"^\d+/\d+$", m.group(1))), None)
    repo = m.group(1).removesuffix(".git") if m else ""
    where = f" {repo}" if repo else " the repo"
    if _re.search(r"\breview\b", t):
        return "first I'll read the pull request and its diff"
    if _re.search(r"\bmerge\b", t):
        return "first I'll check that the pull request can merge (conflicts and checks)"
    if _re.search(r"\bdeploy|release|rollback\b", t):
        return "first I'll check the deploy setup"
    return f"first I'll explore{where} and write a todo list"
