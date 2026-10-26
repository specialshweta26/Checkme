"""Headless "IDE" for coding agents: toolchain bootstrap, code understanding, checks and reviews.

There is no GUI IDE on the computer. Instead the Coder gets the pieces an IDE gives a person:
  * ensure_dev_toolchain: installs git/gh/ripgrep/fd/jq/tree/ctags, node package managers
    (corepack), python + uv + pytest, and go/java/rust only on demand. Idempotent: a marker
    file (~/.checkme/toolchain-v1) caches the result; coding_workspace_status re-checks it.
  * repo_overview: languages, frameworks, entry points and the lint / type-check / unit /
    integration / build commands, from CI workflows first and manifests second.
  * find_symbol / find_references / read_file_range: ctags + ripgrep navigation.
  * run_checks: runs the detected checks in order and keeps the results for the PR body.
  * review_pull_request: reads the PR diff and posts a structured review with inline
    comments through the Checkme GitHub App.
Everything runs through git_tools.sandbox_exec, so tokens never reach the model.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any

from app.agent import coding

TOOLCHAIN_MARKER = "$HOME/.checkme/toolchain-v1"
EXTRAS = ("go", "java", "rust")
CHECK_KINDS = ("lint", "typecheck", "unit", "integration", "build")
MAX_CHECK_ATTEMPTS = 3
DEV_TOOLS = (
    "ensure_dev_toolchain",
    "repo_overview",
    "find_symbol",
    "find_references",
    "read_file_range",
    "run_checks",
    "review_pull_request",
)
_OVERVIEW_SRC = Path(__file__).with_name("sandbox") / "overview.py"
_SYMBOL = re.compile(r"^[A-Za-z_$][\w$.:]{0,120}$")


# ---------------------------------------------------------------------------
# Toolchain bootstrap


def identity_for(installs: list | None) -> tuple[str, str]:
    """The user's GitHub name + noreply email when the App is installed on their personal account."""
    for i in installs or []:
        if getattr(i, "suspended", False):
            continue
        if (getattr(i, "account_type", "") or "").lower() == "user" and getattr(i, "account_login", ""):
            login = i.account_login
            aid = getattr(i, "account_id", None)
            email = f"{aid}+{login}@users.noreply.github.com" if aid else f"{login}@users.noreply.github.com"
            return login, email
    return coding.AUTHOR_NAME, coding.AUTHOR_EMAIL


def toolchain_script(extras: list[str] | None = None, name: str = "", email: str = "", force: bool = False) -> str:
    """Idempotent installer. Prints `present=`, `installed=`, `failed=` and `cached=` lines."""
    q = shlex.quote
    extras = [e for e in (extras or []) if e in EXTRAS]
    name = name or coding.AUTHOR_NAME
    email = email or coding.AUTHOR_EMAIL
    lines = [
        f'MARK="{TOOLCHAIN_MARKER}"; mkdir -p "$HOME/.checkme" "$HOME/.local/bin"; ',
        f"WANT_EXTRAS={q(' '.join(extras))}; ",
        f'if [ -f "$MARK" ] && [ {"1" if force else "0"} = 0 ]; then '
        'HAVE_ALL=1; for e in $WANT_EXTRAS; do grep -q "^extra=$e$" "$MARK" || HAVE_ALL=0; done; '
        'if [ "$HAVE_ALL" = 1 ]; then echo cached=1; cat "$MARK"; exit 0; fi; fi; ',
        'INST=""; FAIL=""; PRES=""; ',
        'SUDO=""; if [ "$(id -u)" != 0 ]; then sudo -n true 2>/dev/null && SUDO="sudo -n"; fi; ',
        'APT_DONE=0; apt_i() { command -v apt-get >/dev/null || return 1; '
        '[ "$APT_DONE" = 1 ] || { $SUDO apt-get update -qq >/dev/null 2>&1; APT_DONE=1; }; '
        "$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends \"$@\" >/dev/null 2>&1; }; ",
        # want <command> <label> <install command...>
        'want() { c="$1"; l="$2"; shift 2; if command -v "$c" >/dev/null 2>&1; then PRES="$PRES $l"; '
        'elif "$@" >/dev/null 2>&1 && command -v "$c" >/dev/null 2>&1; then INST="$INST $l"; else FAIL="$FAIL $l"; fi; }; ',
        "want git git apt_i git ca-certificates; ",
        "want curl curl apt_i curl; ",
        "want rg ripgrep apt_i ripgrep; ",
        "install_fd() { command -v fdfind >/dev/null 2>&1 || apt_i fd-find; "
        'command -v fdfind >/dev/null 2>&1 && ln -sf "$(command -v fdfind)" "$HOME/.local/bin/fd"; }; ',
        "want fd fd install_fd; ",
        "want jq jq apt_i jq; ",
        "want tree tree apt_i tree; ",
        "want ctags universal-ctags apt_i universal-ctags; ",
        "install_gh() { apt_i gh && return 0; "
        'V=2.62.0; A=$(uname -m); case "$A" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; esac; '
        'curl -fsSL "https://github.com/cli/cli/releases/download/v$V/gh_${V}_linux_$A.tar.gz" -o /tmp/gh.tgz && '
        'tar -xzf /tmp/gh.tgz -C /tmp && cp "/tmp/gh_${V}_linux_$A/bin/gh" "$HOME/.local/bin/gh"; }; ',
        "want gh gh install_gh; ",
        "install_node() { apt_i nodejs npm; }; ",
        "want node node install_node; ",
        'if command -v node >/dev/null 2>&1; then '
        'if command -v corepack >/dev/null 2>&1; then corepack enable --install-directory "$HOME/.local/bin" >/dev/null 2>&1; fi; '
        'for pm in pnpm yarn; do if command -v $pm >/dev/null 2>&1; then PRES="$PRES $pm"; '
        'elif npm install -g --prefix "$HOME/.local" $pm >/dev/null 2>&1 && command -v $pm >/dev/null 2>&1; then INST="$INST $pm"; '
        'else FAIL="$FAIL $pm"; fi; done; fi; ',
        "want python3 python3 apt_i python3 python3-pip python3-venv; ",
        "install_uv() { curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=\"$HOME/.local/bin\" sh >/dev/null 2>&1 "
        "|| python3 -m pip install -q --user uv >/dev/null 2>&1; }; ",
        "want uv uv install_uv; ",
        "install_pytest() { uv tool install -q pytest >/dev/null 2>&1 || python3 -m pip install -q --user pytest >/dev/null 2>&1 "
        "|| python3 -m pip install -q --user --break-system-packages pytest >/dev/null 2>&1; }; ",
        "want pytest pytest install_pytest; ",
        "for e in $WANT_EXTRAS; do case $e in "
        "go) want go go apt_i golang-go;; "
        "java) want java java apt_i default-jdk-headless maven;; "
        "rust) want cargo rust sh -c 'curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal >/dev/null 2>&1 && "
        "ln -sf \"$HOME/.cargo/bin/\"* \"$HOME/.local/bin/\"';; esac; done; ",
        # git identity: only when none is set yet, so a user's own config wins
        f'git config --global user.name >/dev/null 2>&1 || git config --global user.name {q(name)}; ',
        f'git config --global user.email >/dev/null 2>&1 || git config --global user.email {q(email)}; ',
        "git config --global init.defaultBranch main; git config --global pull.rebase false; ",
        'echo "present=${PRES# }"; echo "installed=${INST# }"; echo "failed=${FAIL# }"; '
        'echo "identity=$(git config --global user.name) <$(git config --global user.email)>"; ',
        'PREV=$(grep "^extra=" "$MARK" 2>/dev/null); '
        '{ echo "present=${PRES# }${INST}"; echo "failed=${FAIL# }"; [ -n "$PREV" ] && echo "$PREV"; for e in $WANT_EXTRAS; do '
        'case $e in go) c=go;; java) c=java;; rust) c=cargo;; esac; command -v $c >/dev/null 2>&1 && echo "extra=$e"; '
        'done; date -u +at=%FT%TZ; } > "$MARK.tmp" && sort -u -o "$MARK" "$MARK.tmp" && rm -f "$MARK.tmp"',
    ]
    return "".join(lines)


def parse_toolchain(output: str) -> dict:
    info = coding.parse_probe(output)
    split = lambda k: [w for w in (info.get(k) or "").split() if w]  # noqa: E731
    return {
        "cached": info.get("cached") == "1",
        "present": split("present"),
        "installed": split("installed"),
        "failed": split("failed"),
        "identity": info.get("identity", ""),
    }


def toolchain_summary(t: dict) -> str:
    if t.get("cached"):
        msg = "The dev toolchain was already set up on the computer"
    else:
        msg = "Set up the dev toolchain"
        if t.get("installed"):
            msg += ": installed " + ", ".join(t["installed"])
    if t.get("failed"):
        msg += ". Could not install " + ", ".join(t["failed"]) + " (no package manager access or no network); I will work without them"
    return msg + "."


# ---------------------------------------------------------------------------
# Code understanding


def overview_script(name: str) -> str:
    src = _OVERVIEW_SRC.read_text()
    return (
        f"cd {coding.repo_dir(name)} || {{ echo 'run git_checkout first'; exit 1; }}; "
        "python3 - . <<'CHECKME_PY'\n" + src + "\nCHECKME_PY\n"
    )


def _excludes() -> str:
    return " ".join(f"--glob '!{d}/**'" for d in ("node_modules", ".git", "dist", "build", ".next", ".venv", "venv", "target", "vendor"))


def symbol_script(name: str, symbol: str, limit: int = 40) -> str:
    if not _SYMBOL.match(symbol or ""):
        raise coding.GitToolError("symbol must be an identifier like parse_config, UserService or api.client")
    q = shlex.quote
    short = symbol.split(".")[-1].split(":")[-1]
    lim = max(1, min(int(limit or 40), 200))
    defn = (
        rf"(def|class|function|func|fn|interface|type|struct|enum|trait|impl|module|const|let|var|export (default )?(function|class|const))\s+\*?{re.escape(short)}\b"
        rf"|\b{re.escape(short)}\s*[:=]\s*(async\s+)?(\(|function|lambda)"
    )
    return (
        f"cd {coding.repo_dir(name)} || {{ echo 'run git_checkout first'; exit 1; }}; "
        "{ if command -v ctags >/dev/null 2>&1; then "
        "echo '## ctags'; ctags -R -x --exclude=node_modules --exclude=.git --exclude=dist --exclude=build --exclude=.next "
        f"--exclude=.venv --exclude=target . 2>/dev/null | awk -v n={q(short)} '$1==n' | head -{lim}; fi; "
        f"echo '## definitions (ripgrep)'; if command -v rg >/dev/null 2>&1; then rg -n --no-heading -e {q(defn)} {_excludes()} . | head -{lim}; "
        f"else grep -rnE --exclude-dir=node_modules --exclude-dir=.git {q(defn)} . | head -{lim}; fi; }} 2>&1"
    )


def references_script(name: str, symbol: str, path: str = "", limit: int = 80) -> str:
    if not _SYMBOL.match(symbol or ""):
        raise coding.GitToolError("symbol must be an identifier")
    if path and (path.startswith("-") or "\n" in path):
        raise coding.GitToolError("Bad path")
    q = shlex.quote
    lim = max(1, min(int(limit or 80), 400))
    where = q(path) if path else "."
    return (
        f"cd {coding.repo_dir(name)} || {{ echo 'run git_checkout first'; exit 1; }}; "
        "{ if command -v rg >/dev/null 2>&1; then "
        f"echo \"## $(rg -c -w -F {q(symbol)} {_excludes()} {where} | awk -F: '{{s+=$NF}} END {{print s+0}}') matches\"; "
        f"rg -n -w -F --no-heading {q(symbol)} {_excludes()} {where} | head -{lim}; "
        f"else grep -rnwF --exclude-dir=node_modules --exclude-dir=.git {q(symbol)} {where} | head -{lim}; fi; }} 2>&1"
    )


# ---------------------------------------------------------------------------
# Checks


def plan_checks(overview: dict, kinds: list[str] | None = None) -> list[dict]:
    """Setup steps for the dirs involved, then the checks in lint → typecheck → unit → integration → build order."""
    kinds = [k for k in (kinds or CHECK_KINDS) if k in CHECK_KINDS] or list(CHECK_KINDS)
    cmds = overview.get("commands") or []
    checks = [c for c in cmds if c.get("kind") in kinds]
    dirs = {c.get("cwd") or "." for c in checks}
    setup = []
    seen_dirs = set()
    for c in cmds:
        if c.get("kind") == "setup" and (c.get("cwd") or ".") in dirs and (c.get("cwd") or ".") not in seen_dirs:
            seen_dirs.add(c.get("cwd") or ".")
            setup.append(c)
    return setup + checks


def check_script(name: str, step: dict) -> str:
    cwd = (step.get("cwd") or ".").strip()
    if cwd.startswith("/") or ".." in cwd.split("/") or "\n" in cwd:
        raise coding.GitToolError("Bad working directory")
    q = shlex.quote
    cmd = step.get("command") or ""
    if not cmd.strip() or "\n" in cmd:
        raise coding.GitToolError("Bad command")
    # setup steps are skipped when deps look installed already
    guard = ""
    if step.get("kind") == "setup":
        if cmd.startswith(("npm", "pnpm", "yarn")):
            guard = "[ -d node_modules ] && { echo 'deps already installed'; exit 0; }; "
        elif cmd.startswith("uv sync"):
            guard = "[ -d .venv ] && { echo 'deps already installed'; exit 0; }; "
    return (
        f"cd {coding.repo_dir(name)} || {{ echo 'run git_checkout first'; exit 1; }}; cd {q(cwd)} || exit 1; "
        f"export CI=1; {guard}{{ {cmd} ; }} 2>&1 | tail -c 6000"
    )


def checks_table(results: list[dict]) -> str:
    """Markdown block used in the PR body and the job timeline."""
    if not results:
        return ""
    rows = ["| Check | Command | Result |", "| --- | --- | --- |"]
    for r in results:
        if r.get("kind") == "setup":
            continue
        where = f" (in `{r['cwd']}`)" if r.get("cwd") not in (None, "", ".") else ""
        mark = "passed" if r.get("passed") else ("skipped" if r.get("skipped") else "failed")
        rows.append(f"| {r.get('kind')} | `{r.get('command')}`{where} | {mark} |")
    return "\n".join(rows)


def tests_section(results: list[dict], attempts: int = 1) -> str:
    table = checks_table(results)
    if not table:
        return ""
    ok = all(r.get("passed") or r.get("skipped") for r in results if r.get("kind") != "setup")
    head = "All checks passed" if ok else "Some checks still fail"
    tries = f" after {attempts} run{'s' if attempts != 1 else ''}" if attempts else ""
    return f"## Tests\n\n{head}{tries} on the Checkme computer.\n\n{table}\n"


# ---------------------------------------------------------------------------
# Review


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def diff_lines(patch: str) -> set[int]:
    """Right-side (new file) line numbers that a review comment can attach to."""
    out: set[int] = set()
    line = 0
    for raw in (patch or "").splitlines():
        m = _HUNK.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if not line or raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        out.add(line)
        line += 1
    return out


SEVERITIES = ("blocker", "major", "minor", "nit")


def review_body(summary: str, findings: list[dict], verdict: str, checks: list[dict] | None = None, loose: list[dict] | None = None) -> str:
    parts = ["## Review summary", "", (summary or "").strip() or "Reviewed the changes in this pull request."]
    if findings:
        parts += ["", "## Findings", ""]
        for f in sorted(findings, key=lambda f: SEVERITIES.index(f.get("severity", "minor")) if f.get("severity") in SEVERITIES else 2):
            where = f"`{f['path']}:{f['line']}`" if f.get("path") and f.get("line") else (f"`{f['path']}`" if f.get("path") else "general")
            parts.append(f"- **{(f.get('severity') or 'minor').title()}** {where}: {f.get('body', '').strip()}")
    if loose:
        parts += ["", "_Comments on lines outside the diff are listed above instead of inline._"]
    if checks:
        parts += ["", checks_table(checks)]
    word = {"APPROVE": "Approve", "REQUEST_CHANGES": "Changes requested", "COMMENT": "Comments only"}[verdict]
    parts += ["", f"**Verdict:** {word}", "", "_Review by the Checkme Coder._"]
    return "\n".join(parts)


def split_comments(findings: list[dict], files: list[dict]) -> tuple[list[dict], list[dict]]:
    """Inline comments GitHub will accept (line inside the diff) vs. the rest (go into the body)."""
    valid = {f.get("file"): diff_lines(f.get("patch") or "") for f in files or []}
    inline, loose = [], []
    for f in findings or []:
        path, line, body = f.get("path"), f.get("line"), (f.get("body") or "").strip()
        if not body:
            continue
        sev = f.get("severity") if f.get("severity") in SEVERITIES else "minor"
        text = f"**{sev.title()}:** {body}"
        if path and line and int(line) in valid.get(path, set()):
            inline.append({"path": path, "line": int(line), "body": text})
        else:
            loose.append(f)
    return inline, loose


# ---------------------------------------------------------------------------
# Async tools (ctx is deep.RunContext)


async def _exec(ctx, script: str, timeout: int = 300) -> dict:
    from app.agent import git_tools

    return await git_tools.sandbox_exec(ctx, script, timeout)


def _repo_name(repo: str) -> tuple[str, str]:
    return coding.split_repo(repo)


async def ensure_dev_toolchain(ctx, extras: list[str] | None = None, force: bool = False) -> dict:
    from app.agent import git_tools

    installs = await git_tools.installs_for(ctx)
    name, email = identity_for(installs)
    res = await _exec(ctx, toolchain_script(extras, name, email, force), 900)
    if res.get("error"):
        return res
    t = parse_toolchain(res.get("output") or "")
    t["summary"] = toolchain_summary(t)
    t["exit_code"] = res.get("exit_code")
    await git_tools._record(ctx, "step", t["summary"], {k: t[k] for k in ("installed", "failed", "present")})
    return t


async def repo_overview(ctx, repo: str) -> dict:
    from app.agent import git_tools

    try:
        _, name = _repo_name(repo)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
    res = await _exec(ctx, overview_script(name), 120)
    if res.get("error"):
        return res
    text = str(res.get("output") or "").strip()
    try:
        data = json.loads(text[text.index("{"):])
    except ValueError:
        return {"error": "Could not read the repo overview", "output": text[-1500:]}
    data.pop("root", None)
    cache = getattr(ctx, "overviews", None)
    if isinstance(cache, dict):
        cache[repo.lower()] = data
    langs = ", ".join(lang["language"] for lang in data.get("languages", [])[:3]) or "unknown languages"
    await git_tools._record(
        ctx, "step", f"Read {repo}: {langs}; {len(data.get('commands') or [])} check commands found", {"repo": repo, "summary": data.get("summary")}
    )
    return data


async def find_symbol(ctx, repo: str, symbol: str, limit: int = 40) -> dict:
    try:
        _, name = _repo_name(repo)
        script = symbol_script(name, symbol, limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
    return await _exec(ctx, script, 120)


async def find_references(ctx, repo: str, symbol: str, path: str = "", limit: int = 80) -> dict:
    try:
        _, name = _repo_name(repo)
        script = references_script(name, symbol, path, limit)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
    return await _exec(ctx, script, 120)


async def read_file_range(ctx, repo: str, path: str, start: int = 1, end: int = 200, ref: str = "") -> dict:
    try:
        _, name = _repo_name(repo)
        script = coding.inspect_script(name, "read", ref=ref, path=path, start=start, end=end)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
    return await _exec(ctx, script, 60)


async def run_checks(ctx, repo: str, kinds: list[str] | None = None, commands: list[dict] | None = None, timeout_each: int = 600) -> dict:
    """Run lint / typecheck / unit / integration / build. Results are kept for the PR body."""
    from app.agent import git_tools

    try:
        _, name = _repo_name(repo)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}
    if commands:
        plan = [
            {"kind": (c.get("kind") or "unit"), "command": str(c.get("command") or ""), "cwd": str(c.get("cwd") or "."), "source": "agent"}
            for c in commands
            if str(c.get("command") or "").strip()
        ]
    else:
        overview = (getattr(ctx, "overviews", None) or {}).get(repo.lower())
        if overview is None:
            overview = await repo_overview(ctx, repo)
            if overview.get("error"):
                return overview
        plan = plan_checks(overview, kinds)
    if not plan:
        return {"error": "No lint, test or build commands were found. Pass commands=[{kind, command, cwd}]."}
    results: list[dict] = []
    timeout = coding.clamp_timeout(timeout_each)
    setup_failed: set[str] = set()
    for step in plan:
        cwd = step.get("cwd") or "."
        entry = {k: step.get(k) for k in ("kind", "command", "cwd", "source")}
        if step["kind"] != "setup" and cwd in setup_failed:
            entry.update(passed=False, skipped=True, tail="skipped: installing dependencies failed")
            results.append(entry)
            continue
        try:
            res = await _exec(ctx, check_script(name, step), timeout)
        except coding.GitToolError as exc:
            entry.update(passed=False, tail=str(exc))
            results.append(entry)
            continue
        if res.get("error"):
            entry.update(passed=False, tail=str(res["error"])[:600])
        else:
            entry.update(passed=res.get("exit_code") == 0, exit_code=res.get("exit_code"), tail=str(res.get("output") or "")[-2500:])
        if step["kind"] == "setup" and not entry["passed"]:
            setup_failed.add(cwd)
        results.append(entry)
    attempts_map = getattr(ctx, "check_attempts", None)
    attempt = 1
    if isinstance(attempts_map, dict):
        attempt = attempts_map.get(repo.lower(), 0) + 1
        attempts_map[repo.lower()] = attempt
    store = getattr(ctx, "check_results", None)
    if isinstance(store, dict):
        store[repo.lower()] = {"results": results, "attempts": attempt}
    checks = [r for r in results if r["kind"] != "setup"]
    failed = [r for r in checks if not r.get("passed")]
    passed = not failed
    out: dict[str, Any] = {
        "passed": passed,
        "attempt": attempt,
        "max_attempts": MAX_CHECK_ATTEMPTS,
        "results": [{**r, "tail": r.get("tail", "")[-1500:] if not r.get("passed") else r.get("tail", "")[-300:]} for r in results],
        "table": checks_table(results),
    }
    if not passed:
        out["next"] = (
            f"Fix the failures and call run_checks again (attempt {attempt} of {MAX_CHECK_ATTEMPTS})."
            if attempt < MAX_CHECK_ATTEMPTS
            else "Stop iterating: report which checks still fail and why in the PR body and your reply."
        )
    await git_tools._record(
        ctx,
        "test",
        f"Checks {'passed' if passed else 'failed'} in {repo}: " + ", ".join(f"{r['kind']} {'ok' if r.get('passed') else 'failed'}" for r in checks),
        {"repo": repo, "passed": passed, "attempt": attempt, "table": out["table"], "output": "\n\n".join(f"$ {r['command']}\n{r.get('tail', '')[-800:]}" for r in failed)[-3000:]},
    )
    return out


def pr_body_with_tests(ctx, repo: str, body: str) -> str:
    """Append the latest run_checks table to a PR body that has no Tests section yet."""
    store = (getattr(ctx, "check_results", None) or {}).get((repo or "").lower())
    if not store or re.search(r"^#+\s*Tests?\b", body or "", re.M | re.I):
        return body
    section = tests_section(store["results"], store.get("attempts", 1))
    return (body.rstrip() + "\n\n" + section) if body.strip() else section


async def review_pull_request(
    ctx,
    repo: str,
    number: int,
    verdict: str = "COMMENT",
    summary: str = "",
    findings: list[dict] | None = None,
    include_checks: bool = True,
) -> dict:
    """Post a structured review: summary, findings by severity (inline where the line is in the diff), verdict."""
    from app.agent import git_tools
    from app.connectors import github_app as gh

    verdict = (verdict or "COMMENT").upper().replace("-", "_").replace(" ", "_")
    verdict = {"REQUEST_CHANGE": "REQUEST_CHANGES", "CHANGES": "REQUEST_CHANGES", "APPROVED": "APPROVE"}.get(verdict, verdict)
    if verdict not in ("COMMENT", "REQUEST_CHANGES", "APPROVE"):
        return {"error": "verdict must be COMMENT, REQUEST_CHANGES or APPROVE"}
    installs = await git_tools.installs_for(ctx)
    row = type("Row", (), {"user_id": ctx.user.id})()
    try:
        pr = await gh.call_github_app(row, "get_pull_request", {"repo": repo, "number": int(number)}, installs=installs)
    except Exception as exc:  # noqa: BLE001
        status = await git_tools.github_app_status(ctx)
        hint = f" Install the Checkme GitHub App: {status['install_url']}" if status.get("install_url") else ""
        return {"error": coding.scrub(str(exc))[:400] + hint}
    findings = [f for f in (findings or []) if isinstance(f, dict)]
    inline, loose = split_comments(findings, pr.get("files") or [])
    checks = None
    if include_checks:
        store = (getattr(ctx, "check_results", None) or {}).get(repo.lower())
        checks = store["results"] if store else None
    body = review_body(summary, loose, verdict, checks, loose)
    if inline and not loose:
        body = review_body(summary, [], verdict, checks)
        body = body.replace("## Review summary", "## Review summary", 1) + f"\n\n_{len(inline)} inline comment(s)._"
    try:
        res = await gh.call_github_app(
            row, "create_review", {"repo": repo, "number": int(number), "event": verdict, "body": body, "comments": inline}, installs=installs
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": coding.scrub(str(exc))[:400]}
    await git_tools._record(
        ctx, "pr", f"Posted a review on {repo}#{number}: {verdict.replace('_', ' ').lower()}", {"repo": repo, "number": int(number), "url": res.get("url")}
    )
    return {"ok": True, "verdict": verdict, "inline_comments": len(inline), "in_body": len(loose), "url": res.get("url") or pr.get("url")}
