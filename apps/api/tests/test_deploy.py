"""Deploy (PR #17): detection, routing, commands, approval cards, secret handling. All CLIs/sandboxes mocked."""

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.agent import coding, deploy, git_tools

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
    state = SimpleNamespace(saved={}, env={}, calls=[], records=[], exit=0, output="Deployed https://app.onrender.com done")

    async def saved_env_names(ctx):
        return state.saved

    async def load_env(ctx, p):
        return dict(state.env)

    async def _exec(ctx, script, timeout, env=None):
        state.calls.append({"script": script, "timeout": timeout, "env": env})
        return {"exit_code": state.exit, "output": state.output}

    async def _record(ctx, kind, text, data):
        state.records.append((kind, text, data))

    monkeypatch.setattr(deploy, "saved_env_names", saved_env_names)
    monkeypatch.setattr(deploy, "load_env", load_env)
    monkeypatch.setattr(deploy, "_exec", _exec)
    monkeypatch.setattr(deploy, "_record", _record)
    return state


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


def test_route_for_connector_cli_connect():
    p = deploy.PLATFORMS["vercel"]
    assert deploy.route_for(p, {}, {"vercel": deploy.env_names(p)})["route"] == "cli"
    r = deploy.route_for(p, {}, {})
    assert r["route"] == "connect" and r["connect_url"].endswith("deploy=vercel")
    assert deploy.route_for(deploy.PLATFORMS["render"], {"render": RENDER_MCP}, {})["route"] == "connector"


def test_add_readiness_lists_ready_platforms():
    p = deploy.PLATFORMS["fly"]
    out = deploy.add_readiness({"missing": ["No deploy connector"], "can": []}, {"render": RENDER_MCP}, {"fly": [e for e, _ in p.env]})
    assert set(out["deploy"]["ready"]) == {"render", "fly"}
    assert out["deploy"]["connect_url"].endswith("deploy=all")
    assert not any("No deploy connector" in m for m in out.get("missing", []))


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


def test_request_blocked_gives_connect_link(fake):
    ctx = _ctx()
    out = run(deploy.request_action(ctx, "deploy", "o/r", "vercel", {}))
    assert out["status"] == "blocked" and out["connect_url"].endswith("deploy=vercel")
    assert "VERCEL_TOKEN" in out["missing"] and "deploy_vercel" in out["next"]
    assert not ctx.pending and not fake.calls


def test_request_creates_card_and_never_runs(fake):
    fake.saved = {"render": ["RENDER_API_KEY"]}
    ctx = _ctx(connectors={"render": RENDER_MCP})
    out = run(deploy.request_action(ctx, "deploy", "o/r", "render", {"service": "srv-1"}, use_cli=True))
    assert out["status"] == "awaiting_user_confirmation"
    assert not fake.calls, "nothing may run before the user approves"
    card = ctx.pending[0]
    assert card["needs_confirm"] and card["kind"] == "deploy" and "Render" in card["title"]
    assert "RENDER_API_KEY" in card["preview"]["credentials"] and SECRET not in str(card)


def test_request_runs_only_with_exact_confirmation(fake):
    fake.saved = {"render": ["RENDER_API_KEY"]}
    fake.env = {"RENDER_API_KEY": SECRET}
    args = {"action": "deploy", "repo": "o/r", "platform": "render", "params": {"service": "srv-1"}, "cwd": "."}
    other = {**args, "params": {"service": "srv-2"}}
    ctx = _ctx(confirmations=[{"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": other}])
    run(deploy.request_action(ctx, "deploy", "o/r", "render", {"service": "srv-1"}))
    assert not fake.calls and ctx.pending
    ctx = _ctx(confirmations=[{"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": args}])
    out = run(deploy.request_action(ctx, "deploy", "o/r", "render", {"service": "srv-1"}))
    assert out["ok"] and fake.calls and not ctx.pending


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


def test_run_action_blocked_when_credential_removed(fake):
    out = run(deploy.run_action(_ctx(), {"action": "deploy", "repo": "o/r", "platform": "render", "params": {"service": "srv-1"}, "cwd": "."}))
    assert out["status"] == "blocked" and not fake.calls


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
    assert "fly" in out["platforms"] and out["platforms"]["render"]["route"] == "connect"


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
