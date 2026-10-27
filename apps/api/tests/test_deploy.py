"""Deploy: detection, routing, CLI sign-in handoff, generated configs, approval cards, secret handling.
All CLIs/sandboxes mocked."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.agent import cli_login, coding, deploy, deploy_config, git_tools

SECRET = "rnd_SuperSecretTokenValue1234567890"


class Rec:
    def __init__(self):
        self.items = []

    async def record(self, kind, text, data=None):
        self.items.append((kind, text, data or {}))


def _ctx(**kw):
    base = dict(progress=Rec(), confirmations=[], pending=[], user=SimpleNamespace(id=uuid.uuid4()), db=None, agent=None,
                connectors={}, job=None)
    base.update(kw)
    return SimpleNamespace(**base)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake(monkeypatch):
    state = SimpleNamespace(saved={}, env={}, calls=[], records=[], exit=0, output="Deployed https://app.onrender.com done",
                            handler=None, logins=[], login={"status": "logged_in", "platform": "x", "via": "cli_session"},
                            checkout={"ok": True})

    async def saved_env_names(ctx):
        return state.saved

    async def load_env(ctx, p):
        return dict(state.env)

    async def _exec(ctx, script, timeout, env=None):
        state.calls.append({"script": script, "timeout": timeout, "env": env})
        if state.handler is not None:
            got = state.handler(script)
            if got is not None:
                return got
        return {"exit_code": state.exit, "output": state.output}

    async def _record(ctx, kind, text, data):
        state.records.append((kind, text, data))

    async def ensure_login(ctx, key, why=""):
        state.logins.append(key)
        if state.login.get("status") == "awaiting_user_login":
            ctx.question = {"question": "Sign in", "options": ["I've signed in", "Cancel"], "waiting_for": f"login_{key}"}
        return dict(state.login, platform=key)

    async def ensure_checkout(ctx, repo):
        return dict(state.checkout)

    monkeypatch.setattr(deploy, "saved_env_names", saved_env_names)
    monkeypatch.setattr(deploy, "load_env", load_env)
    monkeypatch.setattr(deploy, "_exec", _exec)
    monkeypatch.setattr(deploy, "_record", _record)
    monkeypatch.setattr(deploy, "_ensure_checkout", ensure_checkout)
    monkeypatch.setattr(cli_login, "ensure_login", ensure_login)
    return state


def _deploy_runs(state, needle):
    return [c for c in state.calls if needle in c["script"]]


# --- detection -----------------------------------------------------------------------------------------


def test_targets_from_files_covers_every_config():
    files = {
        "render.yaml": "services:\n  - type: web\n    name: api\n",
        "web/vercel.json": "{}",
        "fly.toml": "app = 'my-app'\n",
        "site/netlify.toml": "[build]\npublish = \"dist\"\n",
        "railway.json": "{}",
        "fn/serverless.yml": "service: orders\n",
        "sam/template.yaml": "Transform: AWS::Serverless-2016-10-31\n",
        "sam/samconfig.toml": "stack_name = \"orders\"\n",
        "infra/cdk.json": "{}",
        "gae/app.yaml": "runtime: python312\n",
        "other/app.yaml": "foo: bar\n",
        "azure.yaml": "name: shop\n",
        "svc/Dockerfile": "FROM python",
    }
    t = deploy.targets_from_files(files)
    by = {(x["platform"], x.get("flavor", "")): x for x in t}
    assert by[("render", "")]["facts"]["services"] == ["api"]
    assert by[("vercel", "")]["cwd"] == "web"
    assert by[("fly", "")]["facts"]["app"] == "my-app"
    assert by[("netlify", "")]["cwd"] == "site"
    assert ("railway", "") in by and ("aws", "serverless") in by and ("aws", "cdk") in by
    assert len([x for x in t if x.get("flavor") == "sam"]) == 1
    assert by[("gcloud", "appengine")]["cwd"] == "gae"
    assert by[("azure", "")]["facts"]["name"] == "shop"
    assert by[("container", "")]["config"] == "svc/Dockerfile"
    assert not any(x["cwd"] == "other" for x in t)


def test_parse_detect_round_trip():
    out = "@@FILE render.yaml\nservices: []\n@@END\n@@FILE fly.toml\napp = 'x'\n@@END\n"
    assert deploy.parse_detect(out) == {"render.yaml": "services: []", "fly.toml": "app = 'x'"}


def test_platform_aliases_and_unknown():
    assert deploy.platform("fly.io").key == "fly"
    assert deploy.platform("GCP").key == "gcloud"
    assert deploy.platform("Render").key == "render"
    with pytest.raises(deploy.DeployError):
        deploy.platform("heroku-classic")
    assert set(deploy.PLATFORMS) == {"render", "vercel", "fly", "netlify", "railway", "aws", "gcloud", "azure"}


def test_connect_url_points_at_connectors_page():
    assert deploy.connect_url("vercel").endswith("/connectors?deploy=vercel")
    assert deploy.connect_url("all").endswith("/connectors?deploy=all")


# --- validation and commands ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [{"service": "a; rm -rf /"}, {"service": "$(id)"}, {"app": "../x"}, {"token": "x"}, {"service": "a b"}])
def test_clean_params_rejects_injection(bad):
    with pytest.raises(deploy.DeployError):
        deploy.clean_params(bad)


def test_clean_params_keeps_production_bool():
    assert deploy.clean_params({"service": "srv-1", "production": 1, "region": ""}) == {"service": "srv-1", "production": True}


@pytest.mark.parametrize("bad", ["../etc", "-rf", "a/../../b", "a b", "$(x)"])
def test_check_cwd_rejects(bad):
    with pytest.raises(deploy.DeployError):
        deploy.check_cwd(bad)


def test_check_cwd_ok():
    assert deploy.check_cwd("apps/web/") == "apps/web"
    assert deploy.check_cwd("") == "."


PARAMS = {
    "render": {"service": "srv-abc", "deployment": "dep-1"},
    "vercel": {"deployment": "my-app-xyz.vercel.app"},
    "fly": {"app": "my-app", "version": "v3", "image": "registry.fly.io/my-app:deployment-01"},
    "netlify": {"deployment": "abc123", "service": "site-id"},
    "railway": {"service": "web"},
    "aws": {"flavor": "serverless", "stack": "orders-dev", "stage": "dev"},
    "gcloud": {"service": "default", "version": "v1"},
    "azure": {"app": "shop", "group": "rg1", "version": "shop--rev1"},
}


@pytest.mark.parametrize("key", list(deploy.PLATFORMS))
@pytest.mark.parametrize("action", ["deploy", "status", "logs", "rollback"])
def test_commands_only_reference_env(key, action):
    p = deploy.PLATFORMS[key]
    try:
        cmd = deploy.command(p, action, deploy.clean_params(PARAMS[key]))
    except deploy.DeployError as exc:
        assert key == "railway" and action == "rollback", exc
        return
    assert cmd and SECRET not in cmd
    script = deploy.run_script(p, "repo", ".", cmd)
    assert "command -v" in script or "install" in script.lower()
    # secrets are only ever referenced as $ENV, never inlined
    for env in deploy.env_names(p):
        assert f"{env}=" not in cmd.replace(f"${env}", "")


def test_railway_rollback_explains():
    with pytest.raises(deploy.DeployError) as e:
        deploy.command(deploy.PLATFORMS["railway"], "rollback", {})
    assert "dashboard" in str(e.value).lower() or "railway" in str(e.value).lower()


def test_secrets_of_splits_json_keys():
    env = {"GOOGLE_CREDENTIALS_JSON": '{"private_key": "-----BEGIN KEY-----MIIEvgIBADANBgkqhkiG9w0BAQEFAASC-----END"}', "X": "ab"}
    s = deploy.secrets_of(env)
    assert env["GOOGLE_CREDENTIALS_JSON"] in s and any("MIIEvgIBADANBgkqhkiG9w0BAQEFAASC" in x and len(x) < 60 for x in s)
    assert "ab" not in s


def test_urls_in_skips_api_hosts():
    assert deploy.urls_in("see https://api.render.com/x and https://app.onrender.com.") == ["https://app.onrender.com"]


# --- routing ---------------------------------------------------------------------------------------------


RENDER_MCP = SimpleNamespace(slug="render", provider="mcp", name="Render", alias="", description="", server_url="https://mcp.render.com/mcp", tags=[])
GITHUB = SimpleNamespace(slug="composio-github", provider="composio_github", name="GitHub (render forks)", alias="", description="", server_url="", tags=[])
LEGACY_GITHUB = SimpleNamespace(slug="github-app", provider="github_app", name="GitHub (render forks)", alias="", description="", server_url="", tags=[])


def test_connector_for_prefers_mcp_and_skips_github():
    assert deploy.connector_for({"render": RENDER_MCP}, deploy.PLATFORMS["render"]) is RENDER_MCP
    assert deploy.connector_for({"g": GITHUB}, deploy.PLATFORMS["render"]) is None
    assert deploy.connector_for({"g": LEGACY_GITHUB}, deploy.PLATFORMS["render"]) is None
    assert deploy.connector_for({"render": RENDER_MCP}, deploy.PLATFORMS["vercel"]) is None


def test_route_for_connector_key_or_cli_login():
    p = deploy.PLATFORMS["vercel"]
    assert deploy.route_for(p, {}, {"vercel": deploy.env_names(p)})["auth"] == "saved_key"
    r = deploy.route_for(p, {}, {})
    assert r["route"] == "cli" and r["auth"] == "cli_login" and "sign-in" in r["how"] and "Connectors" not in r["how"]
    assert deploy.route_for(deploy.PLATFORMS["render"], {"render": RENDER_MCP}, {})["route"] == "connector"


def test_add_readiness_never_blocks():
    p = deploy.PLATFORMS["fly"]
    out = deploy.add_readiness({"missing": ["No deploy connector"], "can": []}, {"render": RENDER_MCP}, {"fly": [e for e, _ in p.env]})
    assert set(out["deploy"]["ready"]) == set(deploy.PLATFORMS)
    assert out["deploy"]["keys_optional_at"].endswith("deploy=all")
    assert not out["missing"] and any("signs in through one link" in c for c in out["can"])


def test_is_deploy_write_gates_writes_not_reads():
    w = deploy.is_deploy_write
    assert w(RENDER_MCP, "render", "create_deploy")
    assert w(RENDER_MCP, "render", "update_environment_variables")
    assert not w(RENDER_MCP, "render", "list_deploys")
    assert not w(RENDER_MCP, "render", "get_service")
    comp = SimpleNamespace(slug="composio", provider="composio", name="Composio", alias="", description="", server_url="", tags=[])
    assert w(comp, "composio", "execute", {"tool_slug": "VERCEL_CREATE_DEPLOYMENT"})
    assert not w(comp, "composio", "execute", {"tool_slug": "VERCEL_LIST_DEPLOYMENTS"})
    gmail = SimpleNamespace(slug="gmail", provider="gmail", name="Gmail", alias="", description="", server_url="", tags=[])
    assert not w(gmail, "gmail", "cancel_event")


def test_git_write_gate_covers_deploy_connectors():
    from app.agent import deep

    assert deep.git_write_gate(RENDER_MCP, "render", "create_deploy", {})
    assert not deep.git_write_gate(RENDER_MCP, "render", "list_services", {})


# --- tools -----------------------------------------------------------------------------------------------


def test_request_prefers_connector(fake):
    ctx = _ctx(connectors={"render": RENDER_MCP})
    out = run(deploy.request_action(ctx, "deploy", "o/r", "render", {"service": "srv-1"}))
    assert out["status"] == "use_connector" and not ctx.pending and not fake.calls


def test_request_without_key_starts_cli_login(fake):
    fake.login = {"status": "awaiting_user_login", "url": "https://vercel.com/oauth/device?user_code=ABCD-EFGH", "code": "ABCD-EFGH"}
    ctx = _ctx()
    out = run(deploy.request_action(ctx, "deploy", "o/r", "vercel", {}))
    assert out["status"] == "awaiting_user_login" and out["url"].startswith("https://vercel.com/oauth/device")
    assert fake.logins == ["vercel"] and ctx.question["waiting_for"] == "login_vercel"
    assert not ctx.pending and not _deploy_runs(fake, "vercel deploy")
    assert "connect_url" not in out and "Connectors" not in str(out)


def test_request_with_cli_session_creates_card(fake):
    ctx = _ctx()
    out = run(deploy.request_action(ctx, "deploy", "o/r", "vercel", {}))
    assert out["status"] == "awaiting_user_confirmation" and fake.logins == ["vercel"]
    pv = ctx.pending[0]["preview"]
    assert "CLI session" in pv["credentials"] and pv["cost"].startswith("Vercel Hobby") and pv["repo"] == "o/r"
    assert not _deploy_runs(fake, "vercel deploy")


def test_request_creates_card_and_never_runs(fake):
    fake.env = {"RENDER_API_KEY": SECRET}
    ctx = _ctx(connectors={"render": RENDER_MCP})
    out = run(deploy.request_action(ctx, "deploy", "o/r", "render", {"service": "srv-1"}, use_cli=True))
    assert out["status"] == "awaiting_user_confirmation"
    assert not _deploy_runs(fake, "render deploys create"), "nothing may deploy before the user approves"
    card = ctx.pending[0]
    assert card["needs_confirm"] and card["kind"] == "deploy" and "Render" in card["title"]
    pv = card["preview"]
    assert pv["service"] == "srv-1" and pv["region"] == "singapore" and "free" in pv["cost"].lower()
    assert "saved Render key" in pv["credentials"] and SECRET not in str(card) and not fake.logins


def test_request_runs_only_with_exact_confirmation(fake):
    fake.env = {"RENDER_API_KEY": SECRET}
    args = {"action": "deploy", "repo": "o/r", "platform": "render", "params": {"service": "srv-1"}, "cwd": "."}
    other = {**args, "params": {"service": "srv-2"}}
    ctx = _ctx(confirmations=[{"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": other}])
    run(deploy.request_action(ctx, "deploy", "o/r", "render", {"service": "srv-1"}))
    assert not _deploy_runs(fake, "render deploys create") and ctx.pending
    ctx = _ctx(confirmations=[{"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": args}])
    out = run(deploy.request_action(ctx, "deploy", "o/r", "render", {"service": "srv-1"}))
    assert out["ok"] and _deploy_runs(fake, "render deploys create") and not ctx.pending


def test_run_action_passes_env_and_records(fake):
    fake.env = {"RENDER_API_KEY": SECRET}
    out = run(deploy.run_action(_ctx(), {"action": "deploy", "repo": "o/r", "platform": "render", "params": {"service": "srv-1"}, "cwd": "."}))
    call = fake.calls[0]
    assert call["env"] == {"RENDER_API_KEY": SECRET} and SECRET not in call["script"]
    assert out["ok"] and out["urls"] == ["https://app.onrender.com"]
    kind, text, data = fake.records[-1]
    assert kind == "deploy" and data["url"] == "https://app.onrender.com" and data["ok"]


def test_run_action_failure_suggests_fix(fake):
    fake.env = {"RENDER_API_KEY": SECRET}
    fake.exit, fake.output = 1, "build failed"
    out = run(deploy.run_action(_ctx(), {"action": "deploy", "repo": "o/r", "platform": "render", "params": {"service": "srv-1"}, "cwd": "."}))
    assert not out["ok"] and "next" in out and "failed" in fake.records[-1][1]


def test_run_action_without_session_hands_over_login_and_keeps_approval(fake):
    fake.login = {"status": "awaiting_user_login", "url": "https://dashboard.render.com/device-authorization/X1", "code": "X1"}
    args = {"action": "deploy", "repo": "o/r", "platform": "render", "params": {"service": "srv-1"}, "cwd": "."}
    job = SimpleNamespace(meta={})
    ctx = _ctx(job=job, confirmations=[{"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": args}])
    out = run(deploy.run_action(ctx, args))
    assert out["status"] == "awaiting_user_login" and not _deploy_runs(fake, "render deploys")
    # after sign-in the resumed run deploys the same approved args without a second card
    fake.login = {"status": "logged_in"}
    ctx2 = _ctx(job=job)
    out = run(deploy.request_action(ctx2, "deploy", "o/r", "render", {"service": "srv-1"}))
    assert out["ok"] and not ctx2.pending and _deploy_runs(fake, "render deploys create")


def test_run_action_rejects_tampered_args(fake):
    fake.env = {"RENDER_API_KEY": SECRET}
    out = run(deploy.run_action(_ctx(), {"action": "deploy", "repo": "o/r", "platform": "render", "params": {"service": "x;id"}, "cwd": "."}))
    assert "error" in out and not fake.calls


def test_read_action_has_no_card(fake):
    fake.env = {"RENDER_API_KEY": SECRET}
    ctx = _ctx()
    out = run(deploy.read_action(ctx, "status", "o/r", "render", {"service": "srv-1"}))
    assert out["ok"] and not ctx.pending and fake.calls[0]["env"] == {"RENDER_API_KEY": SECRET}


def test_detect_deploy_targets_routes(fake):
    fake.output = "@@FILE render.yaml\nservices:\n  - name: api\n@@END\n@@FILE Dockerfile\nFROM x\n@@END\n"
    out = run(deploy.detect_deploy_targets(_ctx(), "o/r"))
    assert {t["platform"] for t in out["targets"]} == {"render", "container"}
    assert "fly" in out["platforms"] and out["platforms"]["render"]["auth"] == "cli_login"
    assert "suggested" not in out


def test_run_confirmed_dispatches_deploy(monkeypatch):
    seen = {}

    async def fake_run(ctx, args):
        seen["args"] = args
        return {"ok": True}

    monkeypatch.setattr(deploy, "run_action", fake_run)
    assert run(git_tools.run_confirmed(_ctx(), "deploy_action", {"platform": "fly"})) == {"ok": True}
    assert seen["args"] == {"platform": "fly"}


def test_sandbox_exec_passes_env_and_scrubs():
    calls = []

    async def call_impl(slug, tool, args):
        calls.append(args)
        return {"exit_code": 0, "stdout": f"token is {SECRET} ok"}

    ctx = _ctx(connectors={coding.COMPUTER_SLUG: SimpleNamespace(slug=coding.COMPUTER_SLUG)}, call_impl=call_impl)
    out = run(git_tools.sandbox_exec(ctx, "echo hi", 30, env={"RENDER_API_KEY": SECRET}))
    assert calls[0]["env"]["RENDER_API_KEY"] == SECRET and SECRET not in calls[0]["command"]
    assert SECRET not in out["output"]


def test_deploy_tools_registered():
    from app.agent import deep

    ctx = deep.RunContext(db=None, user=SimpleNamespace(id=uuid.uuid4()), connectors={}, agent=SimpleNamespace(id=uuid.uuid4(), name="C", focus="coding"))
    names = {t.name for t in deep.harness_tools(ctx)}
    for n in ("detect_deploy_targets", "deploy", "deploy_rollback", "deploy_status", "deploy_logs"):
        assert n in names, n


def test_platforms_public_never_returns_values():
    pub = deploy.platforms_public({"render": ["RENDER_API_KEY"]})
    r = next(p for p in pub if p["key"] == "render")
    assert r["connected"] and r["saved"] == ["RENDER_API_KEY"]
    assert all(set(f) == {"env", "label", "required"} for f in r["fields"])


# --- routes (fake DB) ------------------------------------------------------------------------------------


class FakeDB:
    def __init__(self):
        self.rows = []
        self.commits = 0

    async def execute(self, stmt):
        rows = list(self.rows)
        crit = " ".join(str(c) for c in getattr(stmt, "_where_criteria", ()))
        if "platform" in crit:
            key = [c.right.value for c in stmt._where_criteria if getattr(c.left, "name", "") == "platform"][0]
            rows = [r for r in rows if r.platform == key]

        class R:
            def scalar_one_or_none(self_inner):
                return rows[0] if rows else None

            def scalars(self_inner):
                return SimpleNamespace(all=lambda: rows)

        return R()

    def add(self, row):
        self.rows.append(row)

    async def delete(self, row):
        self.rows.remove(row)

    async def commit(self):
        self.commits += 1


def test_deploy_routes_save_list_forget(monkeypatch):
    from fastapi import HTTPException

    from app.connectors import deploy_routes as dr

    resumed = []

    async def fake_resume(db, user, key):
        resumed.append(key)

    monkeypatch.setattr(dr, "_resume", fake_resume)
    db, user = FakeDB(), SimpleNamespace(id=uuid.uuid4())
    with pytest.raises(HTTPException):
        run(dr.save("render", dr.SaveIn(values={"EVIL": "x"}), user=user, db=db))
    with pytest.raises(HTTPException):
        run(dr.save("render", dr.SaveIn(values={"RENDER_API_KEY": ""}), user=user, db=db))
    out = run(dr.save("render", dr.SaveIn(values={"RENDER_API_KEY": SECRET}), user=user, db=db))
    assert out["connected"] and resumed == ["render"] and SECRET not in str(out)
    assert SECRET not in str(db.rows[0].credentials_enc)
    listed = run(dr.list_platforms(user=user, db=db))
    assert SECRET not in str(listed)
    assert next(p for p in listed["platforms"] if p["key"] == "render")["connected"]
    # blank keeps the saved value
    run(dr.save("render", dr.SaveIn(values={"RENDER_API_KEY": ""}), user=user, db=db))
    from app.security import decrypt_json

    assert decrypt_json(db.rows[0].credentials_enc)["RENDER_API_KEY"] == SECRET
    run(dr.forget("render", user=user, db=db))
    assert not db.rows



# --- autonomous deploys: checkout, stack detection, generated config, Render create, retries -------------

NEXT_STACK = (
    '@@STACK {"files": ["app", "package.json", "package-lock.json"], "package": {"name": "testing", "scripts": '
    '{"dev": "next dev", "build": "next build", "start": "next start"}, "deps": ["next", "react", "openai"]}, '
    '"env_keys": ["OPENAI_API_KEY"], "locks": ["package-lock.json"]}\n@@BRANCH main\n'
)


def _repo_handler(script):
    if "@@FILE" in script or "find ." in script and "render.yaml" in script:
        return {"exit_code": 0, "output": ""}
    if "@@STACK" in script:
        return {"exit_code": 0, "output": NEXT_STACK}
    return None


def test_checkout_failure_or_login_is_returned_before_anything_else(fake):
    fake.checkout = {"status": "awaiting_user_login", "platform": "github", "url": "https://github.com/login/device", "code": "AB-CD"}
    ctx = _ctx()
    out = run(deploy.request_action(ctx, "deploy", "o/private", "render", {}))
    assert out["status"] == "awaiting_user_login" and out["platform"] == "github" and not fake.calls and not ctx.pending
    out = run(deploy.detect_deploy_targets(_ctx(), "o/private"))
    assert out["status"] == "awaiting_user_login"


def test_detect_without_config_suggests_platform_and_stack(fake):
    fake.handler = _repo_handler
    out = run(deploy.detect_deploy_targets(_ctx(), "specialshweta26/testing"))
    assert out["targets"] == [] and out["stack"]["kind"] == "next" and out["stack"]["env_keys"] == ["OPENAI_API_KEY"]
    assert out["suggested"]["platform"] in deploy.PLATFORMS and "render" in out["platforms"]
    assert "Ask the user" not in out["note"] and "deploy(platform" in out["note"]


def test_render_first_deploy_creates_service_from_repo(fake):
    fake.handler = _repo_handler
    ctx = _ctx()
    out = run(deploy.request_action(ctx, "deploy", "specialshweta26/testing", "render", {}))
    assert out["status"] == "awaiting_user_confirmation"
    pv = ctx.pending[0]["preview"]
    assert "create node web service 'testing' (free, singapore)" in pv["command"]
    assert pv["branch"] == "main" and pv["service"] == "testing" and pv["region"] == "singapore"
    # the signed args carry the Render spec, so the approved run uses exactly what the card showed
    from app.agent.confirm import read_confirm

    create = read_confirm(ctx.pending[0]["token"])["payload"]["args"]["create"]
    assert create["serviceDetails"]["plan"] == "free" and create["repo"] == "https://github.com/specialshweta26/testing"
    assert create["serviceDetails"]["envSpecificDetails"] == {"buildCommand": "npm ci && npm run build", "startCommand": "npm run start"}


def test_render_create_run_reports_live_url_and_probes_it(fake):
    def handler(script):
        if "api.render.com/v1" in script:
            return {"exit_code": 0, "output": '@@RENDER {"service": "srv-9", "url": "https://testing-x.onrender.com", "dashboard": "https://dashboard.render.com/web/srv-9"}\n'
                                              '@@RENDER {"ok": true, "status": "live", "service": "srv-9", "url": "https://testing-x.onrender.com"}\n'}
        if "curl -s -o /dev/null" in script:
            return {"exit_code": 0, "output": "200"}
        return None

    fake.handler = handler
    create = deploy_config.render_spec({"kind": "next", "runtime": "node", "build": "npm ci && npm run build", "start": "npm start"},
                                       "testing", "https://github.com/o/testing", "main")
    args = {"action": "deploy", "repo": "o/testing", "platform": "render", "params": {}, "cwd": ".", "create": create}
    out = run(deploy.run_action(_ctx(confirmations=[{"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": args}]), args))
    assert out["ok"] and out["live_url"] == "https://testing-x.onrender.com" and out["http_status"] == 200
    assert out["service"] == "srv-9" and fake.records[-1][2]["url"] == "https://testing-x.onrender.com"
    script = _deploy_runs(fake, "api.render.com/v1")[0]["script"]
    assert "cli.yaml" in script and SECRET not in script and "$RK" in script


def test_render_payment_required_is_a_human_step(fake):
    fake.handler = lambda s: ({"exit_code": 7, "output": '@@RENDER {"ok": false, "status": 402, "error": "Render needs a payment method", '
                                                         '"human": "https://dashboard.render.com/billing"}'} if "api.render.com" in s else None)
    create = deploy_config.render_spec({"kind": "node", "runtime": "node", "build": "npm i", "start": "npm start"}, "a", "https://github.com/o/a", "main")
    args = {"action": "deploy", "repo": "o/a", "platform": "render", "params": {}, "cwd": ".", "create": create}
    out = run(deploy.run_action(_ctx(), args))
    assert not out["ok"] and out["human_step"]["url"] == "https://dashboard.render.com/billing"


def test_failed_deploy_retries_reuse_the_approval_a_bounded_number_of_times(fake):
    fake.exit, fake.output = 1, "build failed"
    args = {"action": "deploy", "repo": "o/r", "platform": "vercel", "params": {}, "cwd": "."}
    job = SimpleNamespace(meta={})
    out = run(deploy.run_action(_ctx(job=job, confirmations=[{"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": args}]), args))
    assert not out["ok"] and "retries reuse this approval" in out["next"]
    runs = 0
    for _ in range(deploy.GRANT_RETRIES + 1):
        ctx = _ctx(job=job)
        out = run(deploy.request_action(ctx, "deploy", "o/r", "vercel", {}))
        if ctx.pending:
            break
        runs += 1
    assert runs == deploy.GRANT_RETRIES and ctx.pending, "after the bounded retries a new approval card is required"


def test_rollback_never_reuses_a_grant(fake):
    args = {"action": "rollback", "repo": "o/r", "platform": "vercel", "params": {}, "cwd": "."}
    job = SimpleNamespace(meta={})
    run(deploy.run_action(_ctx(job=job, confirmations=[{"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": args}]), args))
    ctx = _ctx(job=job)
    out = run(deploy.request_action(ctx, "rollback", "o/r", "vercel", {}))
    assert out["status"] == "awaiting_user_confirmation" and ctx.pending


def test_expired_cli_session_relogs_in(fake, monkeypatch):
    fake.exit, fake.output = 1, "Error: not logged in. Please run vercel login"
    seq = [{"status": "logged_in"}, {"status": "awaiting_user_login", "url": "https://vercel.com/oauth/device?user_code=Q", "code": "Q"}]

    async def ensure_login(ctx, key, why=""):
        fake.logins.append(key)
        return dict(seq.pop(0), platform=key)

    monkeypatch.setattr(cli_login, "ensure_login", ensure_login)
    args = {"action": "deploy", "repo": "o/r", "platform": "vercel", "params": {}, "cwd": "."}
    out = run(deploy.run_action(_ctx(job=SimpleNamespace(meta={})), args))
    assert out["status"] == "awaiting_user_login" and fake.logins == ["vercel", "vercel"]


def test_no_platform_named_picks_default_and_says_so(fake):
    fake.handler = _repo_handler
    ctx = _ctx()
    out = run(deploy.request_action(ctx, "deploy", "o/testing", "", {}))
    assert out["status"] == "awaiting_user_confirmation" and "No platform was named" in out["note"]
    assert ctx.pending[0]["preview"]["why_platform"]


def test_aws_without_iac_is_refused_honestly(fake):
    out = run(deploy.request_action(_ctx(), "deploy", "o/r", "aws", {}))
    assert "infrastructure-as-code" in out["error"] and not fake.logins


def test_defaults_fill_names_and_india_regions(fake):
    p = deploy.PLATFORMS
    assert deploy._defaults(p["azure"], "deploy", {}, "My App", []) == {"app": "my-app", "group": "my-app-rg", "region": "centralindia"}
    assert deploy._defaults(p["gcloud"], "deploy", {}, "x", []) == {"flavor": "run", "service": "x", "region": "asia-south1"}
    assert deploy._defaults(p["fly"], "deploy", {}, "x", [])["app"] == "x"
    cmd = deploy.command(p["fly"], "deploy", {"app": "x"}, name="x")
    assert "flyctl launch --no-deploy" in cmd and "--region bom" in cmd
    assert "--site-name app" in deploy.command(p["netlify"], "deploy", {}, name="app")
    assert '${VERCEL_TOKEN:+--token "$VERCEL_TOKEN"}' in deploy.command(p["vercel"], "deploy", {})


def test_generate_config_writes_without_overwriting(fake):
    def handler(script):
        r = _repo_handler(script)
        if r is not None:
            return r
        if "base64 -d" in script:
            return {"exit_code": 0, "output": "wrote render.yaml\n"}
        return None

    fake.handler = handler
    out = run(deploy.generate_config(_ctx(), "o/testing", "render"))
    assert out["platform"] == "render" and out["written"] == ["render.yaml"]
    body = out["files"]["render.yaml"]
    assert "plan: free" in body and "region: singapore" in body and "OPENAI_API_KEY" in body and "sync: false" in body
    assert "if [ -e render.yaml ]" in _deploy_runs(fake, "base64 -d")[0]["script"]


@pytest.mark.parametrize(
    "facts,kind,static",
    [
        ({"package": {"scripts": {"build": "vite build"}, "deps": ["vite", "react"]}}, "static", True),
        ({"package": {"scripts": {"start": "node server.js"}, "deps": ["express"]}, "files": ["server.js"]}, "node", None),
        ({"python": {"requirements.txt": "fastapi\nuvicorn"}, "files": ["main.py"]}, "fastapi", None),
        ({"python": {"requirements.txt": "flask"}}, "flask", None),
        ({"dockerfile": "FROM x\nEXPOSE 9000"}, "docker", None),
        ({"go": True}, "go", None),
        ({"index_html": True, "files": ["index.html"]}, "static", True),
        ({}, "unknown", None),
    ],
)
def test_stack_detection(facts, kind, static):
    st = deploy_config.stack_from_facts(facts)
    assert st["kind"] == kind and st.get("static") == static


@pytest.mark.parametrize("key", ["render", "vercel", "netlify", "fly", "railway", "gcloud", "azure"])
def test_config_files_for_every_platform(key):
    stack = deploy_config.stack_from_facts({"package": {"scripts": {"build": "next build", "start": "next start"}, "deps": ["next"]}})
    gen = deploy_config.config_files(key, stack, "Testing App")
    assert gen["files"] and any("Defaults:" in n for n in gen["notes"])
    if key == "fly":
        assert 'primary_region = "bom"' in gen["files"]["fly.toml"] and "Dockerfile" in gen["files"]


def test_stack_script_prints_only_env_names(tmp_path):
    import subprocess

    repo = tmp_path / "work" / "r"
    repo.mkdir(parents=True)
    (repo / "package.json").write_text('{"scripts": {"build": "next build"}, "dependencies": {"next": "15"}}')
    (repo / ".env.example").write_text("OPENAI_API_KEY=sk-live-should-not-leak\nexport DB_URL=postgres://x\n")
    out = subprocess.run(["bash", "-c", deploy_config.stack_script("r")], capture_output=True, text=True, env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    facts = deploy_config.parse_stack(out.stdout)
    assert facts["env_keys"] == ["DB_URL", "OPENAI_API_KEY"] and "sk-live" not in out.stdout
    assert deploy_config.stack_from_facts(facts)["kind"] == "next"


def test_render_create_script_against_a_fake_api(tmp_path):
    """Runs the real script (bash + python3) against a local fake of the Render REST API."""
    import json as _json
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body):
            raw = _json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            if self.path.startswith("/services?"):
                return self._send(200, [])
            if self.path.startswith("/services/srv-1/deploys"):
                return self._send(200, [{"deploy": {"id": "dep-1", "status": "live"}}])
            return self._send(404, {"message": "nope"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = _json.loads(self.rfile.read(n) or b"{}")
            seen.append(("POST", self.path, body))
            return self._send(201, {"service": {"id": "srv-1", "dashboardUrl": "https://dashboard.render.com/web/srv-1",
                                                "serviceDetails": {"url": "https://testing.onrender.com"}}, "deployId": "dep-1"})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        home = tmp_path
        (home / ".render").mkdir()
        (home / ".render" / "cli.yaml").write_text("version: 1\nworkspace: tea-123\nworkspace_name: Mine\napi:\n    key: rnd_cli_session_token\n    expires_at: 1\n")
        spec = deploy_config.render_spec({"kind": "next", "runtime": "node", "build": "npm ci && npm run build", "start": "npm start"},
                                         "testing", "https://github.com/o/testing", "main")
        cmd = deploy.render_create_command(spec)
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/local/bin", "CHECKME_RENDER_API": f"http://127.0.0.1:{srv.server_port}",
               "CHECKME_RENDER_POLL_S": "0", "CHECKME_RENDER_POLLS": "2"}
        out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, env=env, timeout=60)
    finally:
        srv.shutdown()
    assert out.returncode == 0, out.stdout + out.stderr
    ev = deploy._render_events(out.stdout)
    assert ev["ok"] and ev["status"] == "live" and ev["url"] == "https://testing.onrender.com" and ev["service"] == "srv-1"
    post = next(x for x in seen if x[0] == "POST")
    assert post[1] == "/services" and post[2]["ownerId"] == "tea-123" and post[2]["serviceDetails"]["plan"] == "free"
    assert all(x[2] == "Bearer rnd_cli_session_token" for x in seen if x[0] == "GET")
    assert "rnd_cli_session_token" not in out.stdout
