"""CLI browser/device sign-in handoff: per-platform output parsing, scripts, ensure_login, code stash, worker resume."""

import uuid
from types import SimpleNamespace

import pytest

from app.agent import cli_login
from app.jobs import service

ESC = "\x1b[1m"
OFF = "\x1b[0m"

SAMPLES = {
    "github": (
        f"! First copy your one-time code: {ESC}ABCD-1234{OFF}\r\n"
        "Press Enter to open https://github.com/login/device in your browser... ",
        "https://github.com/login/device", "ABCD-1234",
    ),
    "render": (
        "Complete login in the Render Dashboard, visit the following URL:\n\n"
        "https://dashboard.render.com/device-authorization/RNDR-7Q2X\n\nwith code: RNDR-7Q2X\n",
        "https://dashboard.render.com/device-authorization/RNDR-7Q2X", "RNDR-7Q2X",
    ),
    "vercel": (
        "> Visit https://vercel.com/oauth/device?user_code=WXYZ-QRST to authorize the CLI.\n"
        "  Waiting for authentication...\n",
        "https://vercel.com/oauth/device?user_code=WXYZ-QRST", "WXYZ-QRST",
    ),
    "fly": (
        "Opening https://fly.io/app/auth/cli/7c3e0a1b2d4f5e6a ...\n\nWaiting for session... ",
        "https://fly.io/app/auth/cli/7c3e0a1b2d4f5e6a", "",
    ),
    "netlify": (
        "Logging into your Netlify account...\nOpening https://app.netlify.com/authorize?response_type=ticket&ticket=abc123\n"
        "⠋ Waiting for authorization...\n",
        "https://app.netlify.com/authorize?response_type=ticket&ticket=abc123", "",
    ),
    "railway": (
        "Your pairing code is: friendly-otter-lamp\nTo authenticate with Railway, please go to\n"
        "    https://railway.com/cli-login?d=aGVsbG8\n",
        "https://railway.com/cli-login?d=aGVsbG8", "friendly-otter-lamp",
    ),
    "gcloud": (
        "Go to the following link in your browser, and complete the sign-in prompts:\n\n"
        "    https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=32555940559.apps&scope=openid\n\n"
        "Once finished, enter the verification code provided in your browser: ",
        "https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=32555940559.apps&scope=openid", "",
    ),
    "azure": (
        "To sign in, use a web browser to open the page https://microsoft.com/devicelogin and enter the code "
        "F7KD2LMNQ to authenticate.\n",
        "https://microsoft.com/devicelogin", "F7KD2LMNQ",
    ),
    "aws": (
        "Browser will not be automatically opened.\nPlease visit the following URL:\n\n"
        "https://ap-south-1.signin.aws.amazon.com/v1/authorize?client_id=arn%3Aaws&response_type=code\n\n"
        "Please enter the authorization code displayed in the browser: ",
        "https://ap-south-1.signin.aws.amazon.com/v1/authorize?client_id=arn%3Aaws&response_type=code", "",
    ),
}


@pytest.mark.parametrize("key", sorted(SAMPLES))
def test_parse_login_output_per_platform(key):
    text, url, code = SAMPLES[key]
    got = cli_login.parse_login_output(key, text)
    assert got == {"url": url, "code": code}


def test_every_platform_has_a_spec_and_aliases_resolve():
    assert set(SAMPLES) == set(cli_login.SPECS)
    assert cli_login.spec("gh").key == "github" and cli_login.spec("gcp").key == "gcloud" and cli_login.spec("az").key == "azure"
    with pytest.raises(ValueError):
        cli_login.spec("heroku-nope")
    # only Google Cloud and AWS make the user paste a code back
    assert {k for k, s in cli_login.SPECS.items() if s.needs_code} == {"gcloud", "aws"}


@pytest.mark.parametrize("key", sorted(SAMPLES))
def test_scripts_are_background_and_never_carry_secrets(key):
    start = cli_login.start_script(key)
    s = cli_login.SPECS[key]
    assert "BROWSER=true" in start and "setsid" in start and s.start in start
    assert "$HOME/.checkme/login/" + key in start
    status = cli_login.status_script(key)
    assert "@@AUTHED 1" in status and s.check in status
    sub = cli_login.submit_code_script(key)
    assert "$CHECKME_LOGIN_CODE" in sub  # the code only ever comes from the env, never inline
    assert "@@CANCELLED" in cli_login.cancel_script(key)


def test_parse_status_flags_and_scrubs():
    st = cli_login.parse_status("@@AUTHED 0\n@@RUNNING 1\n@@LOG\nWaiting…")
    assert not st["authed"] and st["running"] and "Waiting" in st["log"]
    assert cli_login.parse_status("@@AUTHED 1")["authed"]
    assert cli_login.parse_status("@@ERROR could not install the gh CLI")["error"] == "could not install the gh CLI"


def test_question_for_link_and_code():
    q = cli_login.question_for("github", "https://github.com/login/device", "ABCD-1234")
    assert q["waiting_for"] == "login_github" and "ABCD-1234" in q["question"] and not q["needs_code"]
    assert "I've signed in" in q["options"]
    q = cli_login.question_for("gcloud", "https://accounts.google.com/o/oauth2/auth?x", "")
    assert q["needs_code"] and "paste" in q["question"]
    assert service.clean_question(q)["login"]["url"].startswith("https://accounts.google.com")


def _ctx(job=None):
    events = []

    async def record(kind, text, data=None):
        events.append((kind, text, data or {}))

    return SimpleNamespace(job=job, question=None, progress=SimpleNamespace(record=record), events=events)


@pytest.fixture
def fake_exec(monkeypatch):
    calls = []
    outputs = []

    async def _exec(ctx, script, timeout, secret_env=None):
        calls.append({"script": script, "secret_env": secret_env})
        return {"output": outputs.pop(0) if outputs else ""}

    async def _record(ctx, kind, text, data):
        ctx.events.append((kind, text, data))

    monkeypatch.setattr(cli_login, "_exec", _exec)
    monkeypatch.setattr(cli_login, "_record", _record)
    return SimpleNamespace(calls=calls, outputs=outputs)


async def test_ensure_login_already_signed_in(fake_exec):
    fake_exec.outputs.append("@@AUTHED 1\n")
    ctx = _ctx()
    res = await cli_login.ensure_login(ctx, "render")
    assert res == {"status": "logged_in", "platform": "render", "via": "cli_session"} and ctx.question is None


async def test_ensure_login_hands_over_the_link(fake_exec):
    fake_exec.outputs.append("@@AUTHED 0\n@@STARTED 1\n" + SAMPLES["github"][0])
    ctx = _ctx(job=service.new_job(uuid.uuid4(), agent=SimpleNamespace(id=uuid.uuid4(), name="Coder", focus="coding"), prompt="x"))
    res = await cli_login.ensure_login(ctx, "github", "clone a/b")
    assert res["status"] == "awaiting_user_login" and res["url"] == "https://github.com/login/device" and res["code"] == "ABCD-1234"
    assert "End your turn" in res["next"] and "resumes by itself" in res["next"]
    assert ctx.question["waiting_for"] == "login_github"
    assert ctx.events[-1][0] == "login" and ctx.events[-1][2]["url"] == res["url"]


async def test_ensure_login_without_a_link_is_an_error(fake_exec):
    fake_exec.outputs.append("@@AUTHED 0\n@@LOG\nsomething odd")
    res = await cli_login.ensure_login(_ctx(), "fly")
    assert "did not print a sign-in link" in res["error"]


async def test_pasted_code_is_stored_encrypted_and_fed_through_env(fake_exec):
    job = service.new_job(uuid.uuid4(), agent=SimpleNamespace(id=uuid.uuid4(), name="Coder", focus="coding"), prompt="deploy")
    service.apply_outcome(job, "q", {"text": "sign in", "question": cli_login.question_for("gcloud", "https://accounts.google.com/o/oauth2/x", "")})
    assert job.status == "waiting_input"
    assert service.is_code_answer(job, "4/0AbCdEf-secret") and not service.is_code_answer(job, "Cancel")
    events = service.answer(job, "4/0AbCdEf-secret")
    assert job.status == "queued" and "4/0AbCdEf" not in str(job.meta) and "4/0AbCdEf" not in str([e.text for e in events])
    assert "authorization code" in job.meta["next_query"]
    fake_exec.outputs.append("@@AUTHED 1")
    ctx = _ctx(job=job)
    res = await cli_login.ensure_login(ctx, "gcloud")
    assert res["status"] == "logged_in"
    first = fake_exec.calls[0]
    assert first["secret_env"] == {"CHECKME_LOGIN_CODE": "4/0AbCdEf-secret"} and "4/0AbCdEf" not in first["script"]
    assert cli_login.take_code(ctx, "gcloud") == ""  # consumed


async def test_poll_resumes_jobs_once_signed_in():
    job = service.new_job(uuid.uuid4(), agent=SimpleNamespace(id=uuid.uuid4(), name="Coder", focus="coding"), prompt="deploy")
    service.apply_outcome(job, "q", {"text": "sign in", "question": cli_login.question_for("render", "https://dashboard.render.com/device-authorization/X", "X")})
    code_job = service.new_job(uuid.uuid4(), agent=SimpleNamespace(id=uuid.uuid4(), name="Coder", focus="coding"), prompt="deploy")
    service.apply_outcome(code_job, "q", {"text": "sign in", "question": cli_login.question_for("aws", "https://signin.aws.amazon.com/x", "")})
    added = []

    class DB:
        async def execute(self, _stmt):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [job, code_job]))

        def add(self, ev):
            added.append(ev)

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    seen = []

    async def exec_impl(j, script):
        seen.append(j)
        return {"stdout": "@@AUTHED 0\n@@RUNNING 1"} if len(seen) == 1 else {"stdout": "@@AUTHED 1"}

    assert await cli_login.poll_waiting_logins(lambda: DB(), exec_impl) == 0
    assert job.status == "waiting_input"
    assert await cli_login.poll_waiting_logins(lambda: DB(), exec_impl) == 1
    assert job.status == "queued" and "signed in to Render" in job.meta["next_query"] and added
    assert seen.count(code_job) == 0  # code logins resume on the pasted code, not by polling
