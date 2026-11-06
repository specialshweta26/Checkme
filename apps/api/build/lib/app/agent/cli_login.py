"""Interactive CLI sign-in on the computer, handed to the user as one link.

When a deploy (or a private checkout) needs an account and no saved key exists, the coding agent starts the
platform CLI's own browser / device login *in the background on the computer*, reads the sign-in URL and
device code from its output, and gives the user exactly that link (plus the code) in chat. The job then
waits (``waiting_input`` with ``waiting_for='login_<key>'``); the job worker polls the CLI's auth check and
resumes the job automatically once the user has approved in the browser. The CLI keeps the session in the
computer's home directory (``~/.render``, ``~/.config/gh``, ``~/.fly``, ...), so the next job is already
signed in.

Two platforms (Google Cloud, AWS) show an authorization code in the browser that has to be pasted back:
the user replies with it, ``jobs.service.answer`` stores it encrypted in the job meta (never in the
timeline or the model-visible query) and the next run feeds it to the waiting CLI through a FIFO, passed
as a hidden exec env var.

Nothing here ever reads or prints a token. Output that reaches the model or the timeline is scrubbed.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any

from app.agent import coding

LOGIN_ROOT = "$HOME/.checkme/login"
CODE_ENV = "CHECKME_LOGIN_CODE"
URL_WAIT_S = 25
MAX_WAIT_MIN = 30  # the worker stops polling a login after this long

# A browser opener that does nothing: CLIs that try xdg-open (render) then just print the URL.
SHIM = (
    'mkdir -p "$HOME/.checkme/bin" "$HOME/.local/bin"; '
    '[ -x "$HOME/.checkme/bin/xdg-open" ] || { printf \'#!/bin/sh\\nexit 0\\n\' > "$HOME/.checkme/bin/xdg-open"; '
    'chmod +x "$HOME/.checkme/bin/xdg-open"; ln -sf xdg-open "$HOME/.checkme/bin/open"; ln -sf xdg-open "$HOME/.checkme/bin/sensible-browser"; }; '
    'export PATH="$HOME/.checkme/bin:$HOME/.local/bin:$HOME/.fly/bin:$PATH"; export BROWSER=true; unset CI; '
)

_GH_INSTALL = (
    'A=$(uname -m); case "$A" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; esac; '
    'V=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | sed -n \'s/.*"tag_name": *"v\\([^"]*\\)".*/\\1/p\' | head -1); '
    '[ -n "$V" ] || V=2.63.2; curl -fsSL "https://github.com/cli/cli/releases/download/v$V/gh_${V}_linux_$A.tar.gz" | tar -xz -C /tmp && '
    'cp "/tmp/gh_${V}_linux_$A/bin/gh" "$HOME/.local/bin/gh"; rm -rf "/tmp/gh_${V}_linux_$A"; true'
)


@dataclass(frozen=True)
class LoginSpec:
    key: str
    name: str
    cli: str
    check: str  # exits 0 when signed in
    start: str  # the CLI's own interactive login (runs in the background)
    url: str  # regex for the sign-in URL
    code: str = ""  # regex (group 1) for the device / pairing code shown next to the URL
    needs_code: bool = False  # the browser shows a code the user pastes back
    post: str = ""  # after sign-in (pick a workspace, wire git credentials, ...)
    install: str = ""  # defaults to the deploy platform's install script
    pre: str = ""  # before starting (non-interactive defaults)
    press_enter: bool = False  # the CLI waits for Enter before polling


SPECS: dict[str, LoginSpec] = {
    "github": LoginSpec(
        "github", "GitHub", "gh",
        "gh auth status -h github.com >/dev/null 2>&1",
        "gh auth login -h github.com -p https -w",
        r"https://github\.com/login/device\S*",
        r"one-time code:\s*([A-Z0-9]{4}-[A-Z0-9]{4})",
        post="gh auth setup-git -h github.com >/dev/null 2>&1; true",
        install=_GH_INSTALL,
        press_enter=True,
    ),
    "render": LoginSpec(
        "render", "Render", "render",
        "render whoami -o json >/dev/null 2>&1",
        "render login",
        r"https://dashboard\.render\.com/device-authorization/\S+",
        r"with code:\s*(\S+)",
        # A fresh login has no active workspace; pick the first one (the user's own) non-interactively.
        post=(
            "render workspace current -o json >/dev/null 2>&1 || { "
            "W=$(render workspaces -o json 2>/dev/null | python3 -c 'import json,sys; d=json.load(sys.stdin); "
            "d=d if isinstance(d,list) else d.get(\"workspaces\") or d.get(\"items\") or []; "
            "print((d[0].get(\"id\") or (d[0].get(\"owner\") or {}).get(\"id\") or \"\") if d else \"\")' 2>/dev/null); "
            '[ -n "$W" ] && render workspace set "$W" -o json >/dev/null 2>&1; }; true'
        ),
    ),
    "vercel": LoginSpec(
        "vercel", "Vercel", "vercel",
        "vercel whoami >/dev/null 2>&1",
        "vercel login",
        r"https://vercel\.com/(?:oauth/device|login|api/registration)\S*",
        r"user_code=([A-Z0-9-]+)",
    ),
    "fly": LoginSpec(
        "fly", "Fly.io", "flyctl",
        "flyctl auth whoami >/dev/null 2>&1",
        "flyctl auth login",
        r"https://fly\.io/app/auth/cli/\S+",
    ),
    "netlify": LoginSpec(
        "netlify", "Netlify", "netlify",
        "netlify api getCurrentUser >/dev/null 2>&1",
        "netlify login",
        r"https://app\.netlify\.com/authorize\S+",
    ),
    "railway": LoginSpec(
        "railway", "Railway", "railway",
        "railway whoami >/dev/null 2>&1",
        "railway login --browserless",
        r"https://railway\.(?:com|app)/cli-login\S*",
        r"pairing code is:?\s*([A-Za-z0-9-]+)",
    ),
    "gcloud": LoginSpec(
        "gcloud", "Google Cloud", "gcloud",
        "gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | grep -q .",
        "gcloud auth login --no-launch-browser --update-adc --quiet",
        r"https://accounts\.google\.com/o/oauth2/\S+",
        needs_code=True,
        post=(
            "gcloud config get-value project 2>/dev/null | grep -q . || { "
            "P=$(gcloud projects list --format='value(projectId)' --limit 1 2>/dev/null); "
            '[ -n "$P" ] && gcloud config set project "$P" >/dev/null 2>&1; }; true'
        ),
    ),
    "azure": LoginSpec(
        "azure", "Azure", "az",
        "az account show >/dev/null 2>&1",
        "az login --use-device-code --output none",
        r"https://(?:microsoft\.com/devicelogin|login\.microsoft\.com/device|aka\.ms/devicelogin)\S*",
        r"enter the code\s+([A-Z0-9]{6,})",
        pre="az config set core.login_experience_v2=off >/dev/null 2>&1; az config set core.collect_telemetry=false >/dev/null 2>&1; ",
    ),
    "aws": LoginSpec(
        "aws", "AWS", "aws",
        "aws sts get-caller-identity >/dev/null 2>&1",
        # AWS CLI >= 2.32: browser sign-in with console credentials; --remote prints a URL and asks for the code.
        "aws login --remote",
        r"https://\S*(?:signin\.aws|amazonaws\.com|awsapps\.com)\S*",
        needs_code=True,
        pre='aws configure get region >/dev/null 2>&1 || aws configure set region "${AWS_REGION:-ap-south-1}"; ',
    ),
}
ALIASES = {"gh": "github", "git": "github", "flyio": "fly", "fly.io": "fly", "gcp": "gcloud", "google": "gcloud", "az": "azure"}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\r")
_ANY_URL = re.compile(r"https://[^\s\"'<>\x1b]+")
_GENERIC_CODE = re.compile(r"\b(?:code|code is)\b[:\s]+([A-Z0-9]{4,}(?:-[A-Z0-9]{3,})*)\b")


def spec(key: str) -> LoginSpec:
    k = re.sub(r"[\s_]+", "", str(key or "").strip().lower())
    k = ALIASES.get(k, k)
    if k not in SPECS:
        raise ValueError(f"No CLI sign-in for {key!r}. Use one of: {', '.join(SPECS)}.")
    return SPECS[k]


def clean_log(text: str) -> str:
    return _ANSI.sub("", text or "")


def parse_login_output(key: str, text: str) -> dict:
    """{url, code} from a CLI's login output (ANSI and carriage returns stripped)."""
    s = spec(key)
    t = clean_log(text)
    url = ""
    m = re.search(s.url, t)
    if m:
        url = m.group(0)
    url = url.rstrip(".,);'\"")
    code = ""
    if s.code:
        m = re.search(s.code, t, re.I)
        if m:
            code = m.group(1).strip().rstrip(".")
    if not code and not s.needs_code:
        m = _GENERIC_CODE.search(t)
        if m and m.group(1) not in url:
            code = m.group(1)
    if s.key == "render" and code and len(code) > 40:
        code = ""  # the long render code is already part of the URL
    return {"url": url, "code": code}


def _q(s: str) -> str:
    return shlex.quote(s)


def _install(s: LoginSpec) -> str:
    inst = s.install
    if not inst:
        from app.agent import deploy

        p = deploy.PLATFORMS.get(s.key)
        inst = p.install if p is not None else "true"
    return (
        f"command -v {s.cli} >/dev/null 2>&1 || {{ echo 'Installing the {s.cli} CLI…'; {inst}; }}; "
        f"command -v {s.cli} >/dev/null 2>&1 || {{ echo '@@ERROR could not install the {s.cli} CLI'; exit 5; }}; "
    )


def start_script(key: str) -> str:
    """Install the CLI if needed; if already signed in say so; else (re)start its login in the background
    with stdin on a FIFO, wait for the sign-in URL and print the log."""
    s = spec(key)
    d = f'{LOGIN_ROOT}/{s.key}'
    run = s.pre + s.start
    enter = 'sleep 2; printf "\\n" > "$D/in"; ' if s.press_enter else ""
    return (
        SHIM
        + f'D="{d}"; mkdir -p "$D"; '
        + _install(s)
        + f"if {s.check}; then {s.post or 'true'}; echo '@@AUTHED 1'; exit 0; fi; "
        + 'if [ -f "$D/pid" ] && kill -0 "$(cat "$D/pid")" 2>/dev/null && grep -q "https://" "$D/log" 2>/dev/null; then echo "@@RUNNING 1"; else '
        + '[ -f "$D/pid" ] && kill "$(cat "$D/pid")" 2>/dev/null; [ -f "$D/holder" ] && kill "$(cat "$D/holder")" 2>/dev/null; '
        + 'rm -f "$D/in" "$D/log" "$D/pid" "$D/holder"; mkfifo "$D/in" || exit 6; '
        # The holder keeps the FIFO open so the CLI never sees EOF while it waits for a code.
        + f'(exec sleep {MAX_WAIT_MIN * 60} > "$D/in" 2>/dev/null < /dev/null) & echo $! > "$D/holder"; '
        + "if command -v script >/dev/null 2>&1; then "
        + f'setsid nohup script -qfec {_q(run)} /dev/null < "$D/in" > "$D/log" 2>&1 & '
        + f'else setsid nohup bash -c {_q(run)} < "$D/in" > "$D/log" 2>&1 & fi; '
        + 'echo $! > "$D/pid"; echo "@@STARTED 1"; '
        + enter
        + "fi; "
        + f'for i in $(seq 1 {URL_WAIT_S}); do grep -q "https://" "$D/log" 2>/dev/null && break; '
        + f"if {s.check}; then {s.post or 'true'}; echo '@@AUTHED 1'; exit 0; fi; sleep 1; done; "
        + 'sleep 1; echo "@@LOG"; tail -c 4000 "$D/log" 2>/dev/null'
    )


def status_script(key: str) -> str:
    s = spec(key)
    return (
        SHIM
        + f'D="{LOGIN_ROOT}/{s.key}"; '
        + f"if command -v {s.cli} >/dev/null 2>&1 && {s.check}; then {s.post or 'true'}; echo '@@AUTHED 1'; "
        + '[ -f "$D/holder" ] && kill "$(cat "$D/holder")" 2>/dev/null; rm -f "$D/holder" "$D/in"; exit 0; fi; '
        + "echo '@@AUTHED 0'; "
        + 'if [ -f "$D/pid" ] && kill -0 "$(cat "$D/pid")" 2>/dev/null; then echo "@@RUNNING 1"; else echo "@@RUNNING 0"; fi; '
        + 'echo "@@LOG"; tail -c 3000 "$D/log" 2>/dev/null'
    )


def submit_code_script(key: str) -> str:
    """Feed the pasted authorization code (only ever in $CHECKME_LOGIN_CODE) to the waiting CLI."""
    s = spec(key)
    return (
        SHIM
        + f'D="{LOGIN_ROOT}/{s.key}"; '
        + '[ -p "$D/in" ] && [ -f "$D/pid" ] && kill -0 "$(cat "$D/pid")" 2>/dev/null || { echo "@@NOLOGIN 1"; exit 6; }; '
        + f'printf "%s\\n" "${CODE_ENV}" > "$D/in"; '
        + f"for i in $(seq 1 45); do if {s.check}; then {s.post or 'true'}; echo '@@AUTHED 1'; "
        + '[ -f "$D/holder" ] && kill "$(cat "$D/holder")" 2>/dev/null; exit 0; fi; '
        + 'kill -0 "$(cat "$D/pid")" 2>/dev/null || break; sleep 1; done; '
        + "echo '@@AUTHED 0'; echo '@@LOG'; tail -c 2000 \"$D/log\" 2>/dev/null"
    )


def cancel_script(key: str) -> str:
    s = spec(key)
    return (
        f'D="{LOGIN_ROOT}/{s.key}"; for f in pid holder; do [ -f "$D/$f" ] && kill "$(cat "$D/$f")" 2>/dev/null; done; '
        'rm -rf "$D"; echo "@@CANCELLED 1"'
    )


def parse_status(output: str) -> dict:
    out = output or ""
    log = out.split("@@LOG", 1)[1] if "@@LOG" in out else ""
    err = re.search(r"@@ERROR (.+)", out)
    return {
        "authed": "@@AUTHED 1" in out,
        "running": "@@RUNNING 1" in out or "@@STARTED 1" in out,
        "no_login": "@@NOLOGIN 1" in out,
        "error": err.group(1).strip() if err else "",
        "log": coding.scrub(clean_log(log))[-1500:],
    }


# ---------------------------------------------------------------------------
# Agent-side flow


def waiting_for(key: str) -> str:
    return f"login_{spec(key).key}"


def question_for(key: str, url: str, code: str) -> dict:
    s = spec(key)
    if s.needs_code:
        text = (
            f"Sign in to {s.name} here: {url} — then paste the authorization code it shows as your reply. "
            "I'll finish the setup and continue."
        )
        options = ["Cancel", "Use a different platform"]
    else:
        text = f"Sign in to {s.name} here: {url}" + (f" (code {code})" if code else "") + ". I'll continue automatically once you approve."
        options = ["I've signed in", "Cancel"]
    return {"question": text, "options": options, "waiting_for": waiting_for(key), "needs_code": s.needs_code,
            "login": {"platform": s.key, "url": url, "code": code}}


async def _exec(ctx, script: str, timeout: int, secret_env: dict | None = None) -> dict:
    from app.agent import git_tools

    return await git_tools.sandbox_exec(ctx, script, timeout, secret_env=secret_env)


async def _record(ctx, kind: str, text: str, data: dict) -> None:
    from app.agent import git_tools

    await git_tools._record(ctx, kind, text, data)


def take_code(ctx, key: str) -> str:
    """A pasted authorization code stashed (encrypted) on the job by jobs.service.answer. Removed once read."""
    job = getattr(ctx, "job", None)
    if job is None:
        return ""
    meta = dict(getattr(job, "meta", None) or {})
    codes = dict(meta.get("login_codes") or {})
    enc = codes.pop(spec(key).key, None)
    if not enc:
        return ""
    meta["login_codes"] = codes
    job.meta = meta
    try:
        from app.security import decrypt_json

        return str(decrypt_json(enc).get("code") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


async def ensure_login(ctx, key: str, why: str = "") -> dict:
    """Signed in already -> {status: logged_in}. Else start the CLI's login and hand the user its link:
    {status: awaiting_user_login, url, code, next}. Sets ctx.question so a background job waits and resumes."""
    try:
        s = spec(key)
    except ValueError as exc:
        return {"error": str(exc)}
    code = take_code(ctx, s.key)
    if code:
        res = await _exec(ctx, submit_code_script(s.key), 120, secret_env={CODE_ENV: code})
        st = parse_status(res.get("output", ""))
        if st["authed"]:
            await _record(ctx, "login", f"Signed in to {s.name}", {"platform": s.key, "ok": True})
            return {"status": "logged_in", "platform": s.key, "via": "cli_session"}
        # wrong / expired code, or the login process is gone: start over with a fresh link
    res = await _exec(ctx, start_script(s.key), 900)
    if res.get("error"):
        return {"error": res["error"], "platform": s.key}
    st = parse_status(res.get("output", ""))
    if st["authed"]:
        return {"status": "logged_in", "platform": s.key, "via": "cli_session"}
    if st["error"]:
        return {"error": st["error"], "platform": s.key}
    found = parse_login_output(s.key, res.get("output", ""))
    if not found["url"]:
        return {
            "error": f"The {s.cli} login did not print a sign-in link.", "platform": s.key, "log": st["log"],
            "next": "Read the log and retry once; if it still fails, deploy to another platform and say why in one line.",
        }
    q = question_for(s.key, found["url"], found["code"])
    ctx.question = q
    await _record(
        ctx, "login", f"Waiting for you to sign in to {s.name}",
        {"platform": s.key, "url": found["url"], "code": found["code"], "needs_code": s.needs_code},
    )
    in_job = getattr(ctx, "job", None) is not None
    return {
        "status": "awaiting_user_login",
        "platform": s.key,
        "url": found["url"],
        "code": found["code"],
        "needs_code": s.needs_code,
        "why": why,
        "next": (
            "End your turn now. Reply with exactly one short line: the sign-in link"
            + (" and the code" if found["code"] else "")
            + (" and ask them to paste back the authorization code" if s.needs_code else "")
            + ". The question with buttons is already shown"
            + (" and this job resumes by itself after they sign in." if in_job and not s.needs_code else ".")
            + " Do not tell them to configure anything else."
            + ("" if in_job else " When they say they have signed in, call the same tool again.")
        ),
    }


async def cancel(ctx, key: str) -> dict:
    res = await _exec(ctx, cancel_script(key), 30)
    return {"cancelled": "@@CANCELLED 1" in (res.get("output") or "")}


# ---------------------------------------------------------------------------
# Worker: resume jobs whose user finished signing in


def _login_key(job) -> str:
    q = (getattr(job, "meta", None) or {}).get("question") or {}
    wf = str(q.get("waiting_for") or "")
    if not wf.startswith("login_") or q.get("needs_code"):
        return ""
    return wf[len("login_"):]


async def poll_waiting_logins(session_factory, exec_impl: Any = None) -> int:
    """For jobs waiting on a device/browser login: run the CLI's auth check on the user's computer and requeue
    the job once it passes. Returns how many jobs were resumed."""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.jobs import service
    from app.models import AgentJob, Connector

    resumed = 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=MAX_WAIT_MIN)
    async with session_factory() as db:
        rows = (await db.execute(select(AgentJob).where(AgentJob.status == "waiting_input").limit(50))).scalars().all()
        for job in rows:
            key = _login_key(job)
            if not key or key not in SPECS:
                continue
            hb = getattr(job, "heartbeat_at", None)
            if hb is not None and hb < cutoff:
                continue
            try:
                if exec_impl is not None:
                    raw = await exec_impl(job, status_script(key))
                else:
                    conn = (
                        await db.execute(select(Connector).where(Connector.user_id == job.user_id, Connector.slug == coding.COMPUTER_SLUG))
                    ).scalar_one_or_none()
                    if conn is None:
                        continue
                    from app.connectors.runtime import RuntimeConnector

                    raw = await RuntimeConnector(conn).call("exec", {"command": coding.bash(status_script(key)), "timeout": 60})
            except Exception:  # noqa: BLE001 - busy computer, sandbox stopped, ...: try again next round
                continue
            out = str((raw or {}).get("stdout") or (raw or {}).get("result") or "") if isinstance(raw, dict) else str(raw or "")
            if "@@AUTHED 1" not in out:
                continue
            name = SPECS[key].name
            note = (
                f"The user signed in to {name} on the computer (CLI session saved). Continue exactly where you stopped: "
                "call the same tool again (deploy / git_checkout); do not ask them anything else."
            )
            for ev in service.resume_waiting_for(job, waiting_for(key), note):
                db.add(ev)
                resumed += 1
        if resumed:
            await db.commit()
    return resumed
