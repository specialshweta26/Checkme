"""Deploy from the coding agent: detect targets, prefer a connector, else a CLI on the computer.

Flow (every platform):

1. ``detect_deploy_targets(repo)`` lists the deploy configs in ``~/work/<repo>`` (render.yaml,
   vercel.json, fly.toml, netlify.toml, railway.json/toml, serverless.yml, SAM, CDK, app.yaml,
   azure.yaml, Dockerfile) and, for each platform, the route: a connected MCP/Composio connector
   (preferred), a saved CLI credential, or the link to connect one.
2. ``deploy`` / ``deploy_rollback`` never run directly: they always create an approval card
   (kind ``deploy``), also in scheduled runs and background jobs where ``auto_confirm`` is ignored.
   The card shows the platform, the exact command (with ``$ENV`` names, never values) and the target.
3. On approval the CLI is installed on demand (``~/.local/bin``), the user's credential is decrypted
   server-side and passed to the computer only as exec ``env``. It is never put in the command, a
   file in the repo, the card, the timeline or model-visible output (output is scrubbed of every
   secret value). ``deploy_status`` / ``deploy_logs`` are read-only and run without a card.

Credentials are stored per user in ``deploy_credentials`` (Fernet, like connector credentials) and
are only ever entered on the Connectors page, never in chat.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from app.agent import coding

DEPLOY_CONFIRM_TTL = 3 * 24 * 3600
ACTIONS = ("deploy", "rollback")
_PARAM_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@=+-]{0,199}$")
PARAM_KEYS = ("service", "app", "stack", "stage", "region", "group", "ref", "deployment", "version", "image", "flavor")


@dataclass(frozen=True)
class Platform:
    key: str
    name: str
    files: tuple[str, ...]
    cli: str
    install: str
    env: tuple[tuple[str, str], ...]  # required (ENV, label)
    optional_env: tuple[tuple[str, str], ...] = ()
    hints: tuple[str, ...] = ()
    token_url: str = ""
    login: str = ""  # runs before every command (uses only $ENV references)
    notes: str = ""
    flavors: tuple[str, ...] = field(default_factory=tuple)


_NPM = 'npm i -g {pkg} --prefix "$HOME/.local" >/dev/null 2>&1'

PLATFORMS: dict[str, Platform] = {
    "render": Platform(
        "render", "Render", ("render.yaml",), "render",
        'curl -fsSL https://raw.githubusercontent.com/render-oss/cli/refs/heads/main/bin/install.sh | sh >/dev/null 2>&1; '
        'for d in /usr/local/bin "$HOME/.render/bin" "$HOME/bin"; do [ -x "$d/render" ] && ln -sf "$d/render" "$HOME/.local/bin/render"; done; true',
        (("RENDER_API_KEY", "API key (Account Settings → API Keys)"),),
        hints=("render",), token_url="https://dashboard.render.com/u/settings#api-keys",
        notes="Pass service=srv-… (deploy_status with no service lists them).",
    ),
    "vercel": Platform(
        "vercel", "Vercel", ("vercel.json", ".vercel/project.json"), "vercel", _NPM.format(pkg="vercel"),
        (("VERCEL_TOKEN", "Access token"),),
        (("VERCEL_ORG_ID", "Team / org id (optional)"), ("VERCEL_PROJECT_ID", "Project id (optional)")),
        hints=("vercel",), token_url="https://vercel.com/account/tokens",
    ),
    "fly": Platform(
        "fly", "Fly.io", ("fly.toml",), "flyctl",
        'curl -fsSL https://fly.io/install.sh | sh >/dev/null 2>&1; ln -sf "$HOME/.fly/bin/flyctl" "$HOME/.local/bin/flyctl"; true',
        (("FLY_API_TOKEN", "Deploy token (fly tokens create deploy)"),),
        hints=("fly.io", "flyio", "flyctl"), token_url="https://fly.io/user/personal_access_tokens",
        notes="Rollback redeploys an earlier image: pass image=registry.fly.io/<app>:<tag> (see deploy_status).",
    ),
    "netlify": Platform(
        "netlify", "Netlify", ("netlify.toml",), "netlify", _NPM.format(pkg="netlify-cli"),
        (("NETLIFY_AUTH_TOKEN", "Personal access token"), ("NETLIFY_SITE_ID", "Site id")),
        hints=("netlify",), token_url="https://app.netlify.com/user/applications#personal-access-tokens",
    ),
    "railway": Platform(
        "railway", "Railway", ("railway.json", "railway.toml"), "railway", _NPM.format(pkg="@railway/cli"),
        (("RAILWAY_TOKEN", "Project token"),),
        hints=("railway",), token_url="https://docs.railway.com/guides/public-api#project-token",
        notes="Rollback is not in the Railway CLI; use Rollback on the deployment in the Railway dashboard.",
    ),
    "aws": Platform(
        "aws", "AWS", ("serverless.yml", "serverless.yaml", "serverless.ts", "template.yaml", "template.yml", "samconfig.toml", "cdk.json"), "aws",
        'curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscli.zip && '
        '(command -v unzip >/dev/null || sudo -n apt-get install -y -qq unzip >/dev/null 2>&1) && unzip -q -o /tmp/awscli.zip -d /tmp && '
        '/tmp/aws/install -i "$HOME/.local/aws-cli" -b "$HOME/.local/bin" --update >/dev/null 2>&1; rm -rf /tmp/aws /tmp/awscli.zip; true',
        (("AWS_ACCESS_KEY_ID", "Access key id"), ("AWS_SECRET_ACCESS_KEY", "Secret access key"), ("AWS_REGION", "Region, e.g. us-east-1")),
        (("AWS_SESSION_TOKEN", "Session token (optional)"),),
        hints=("aws", "amazon web services", "lambda"), token_url="https://console.aws.amazon.com/iam/home#/security_credentials",
        notes="Rollback only applies to a stack stuck in UPDATE_FAILED (CloudFormation rollback-stack).",
        flavors=("serverless", "sam", "cdk"),
    ),
    "gcloud": Platform(
        "gcloud", "Google Cloud", ("app.yaml",), "gcloud",
        'curl -fsSL https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz | tar -xz -C "$HOME/.local" && '
        '"$HOME/.local/google-cloud-sdk/install.sh" -q >/dev/null 2>&1; ln -sf "$HOME/.local/google-cloud-sdk/bin/gcloud" "$HOME/.local/bin/gcloud"; true',
        (("GCP_SERVICE_ACCOUNT_JSON", "Service account key (JSON)"), ("CLOUDSDK_CORE_PROJECT", "Project id")),
        (("CLOUDSDK_RUN_REGION", "Cloud Run region (optional)"),),
        hints=("google cloud", "gcloud", "gcp", "cloud run", "app engine"), token_url="https://console.cloud.google.com/iam-admin/serviceaccounts",
        login='K=$(mktemp); trap \'rm -f "$K"\' EXIT; printf %s "$GCP_SERVICE_ACCOUNT_JSON" > "$K"; '
        'gcloud auth activate-service-account --key-file="$K" --quiet >/dev/null 2>&1 || { echo "gcloud sign-in failed"; exit 4; }',
        notes="App Engine (app.yaml) by default; pass flavor=run and service=<name> for Cloud Run (needs a Dockerfile or buildpacks).",
        flavors=("appengine", "run"),
    ),
    "azure": Platform(
        "azure", "Azure", ("azure.yaml",), "az",
        '(command -v uv >/dev/null && uv tool install -q azure-cli >/dev/null 2>&1) || python3 -m pip install -q --user azure-cli >/dev/null 2>&1; true',
        (("AZURE_CLIENT_ID", "Service principal app id"), ("AZURE_CLIENT_SECRET", "Service principal secret"),
         ("AZURE_TENANT_ID", "Tenant id"), ("AZURE_SUBSCRIPTION_ID", "Subscription id")),
        hints=("azure",), token_url="https://learn.microsoft.com/cli/azure/azure-cli-sp-tutorial-1",
        login='az login --service-principal -u "$AZURE_CLIENT_ID" -p "$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID" >/dev/null 2>&1 '
        '&& az account set -s "$AZURE_SUBSCRIPTION_ID" || { echo "az sign-in failed"; exit 4; }',
        notes="Deploys a Container App from source: pass app=<name> and group=<resource group>.",
    ),
}
ALIASES = {"flyio": "fly", "fly.io": "fly", "gcp": "gcloud", "google": "gcloud", "googlecloud": "gcloud", "cloudrun": "gcloud",
           "appengine": "gcloud", "az": "azure", "amazon": "aws", "lambda": "aws", "sam": "aws", "cdk": "aws", "serverless": "aws"}
CONTAINER_PLATFORMS = ("fly", "gcloud", "azure", "railway", "render")


class DeployError(ValueError):
    pass


def platform(key: str) -> Platform:
    k = re.sub(r"[\s_-]+", "", str(key or "").strip().lower())
    k = ALIASES.get(k, k)
    if k not in PLATFORMS:
        raise DeployError(f"Unknown platform {key!r}. Use one of: {', '.join(PLATFORMS)}.")
    return PLATFORMS[k]


def env_names(p: Platform) -> list[str]:
    return [e for e, _ in p.env] + [e for e, _ in p.optional_env]


def connect_url(p: Platform | str) -> str:
    from app.config import get_settings

    key = p.key if isinstance(p, Platform) else (p or "all")
    return f"{get_settings().public_web_url.rstrip('/')}/connectors?deploy={key}"


# ---------------------------------------------------------------------------
# Detection


def detect_script(name: str) -> str:
    """Lists deploy configs (depth ≤ 3) and prints a little of each, between markers."""
    names = sorted({f.split("/")[-1] for p in PLATFORMS.values() for f in p.files} | {"Dockerfile", "Procfile"})
    expr = " -o ".join(f"-name {shlex.quote(n)}" for n in names)
    return (
        f'cd "$HOME/work/{name}" || {{ echo "Run git_checkout first: ~/work/{name} is missing."; exit 3; }}\n'
        f"find . -maxdepth 3 \\( -path ./node_modules -o -path ./.git -o -path '*/node_modules' -o -path ./.venv \\) -prune -o "
        f"\\( {expr} -o -path './.vercel/project.json' \\) -type f -print 2>/dev/null | sort | head -40 | while read -r f; do\n"
        '  echo "@@FILE ${f#./}"; head -c 3000 "$f"; echo; echo "@@END"\n'
        "done\n"
    )


def parse_detect(output: str) -> dict[str, str]:
    files: dict[str, str] = {}
    cur, buf = None, []
    for line in (output or "").splitlines():
        if line.startswith("@@FILE "):
            cur, buf = line[7:].strip(), []
        elif line == "@@END" and cur:
            files[cur] = "\n".join(buf)
            cur = None
        elif cur is not None:
            buf.append(line)
    return files


def _first(pattern: str, text: str) -> str:
    m = re.search(pattern, text or "", re.M)
    return m.group(1).strip().strip("\"'") if m else ""


def targets_from_files(files: dict[str, str]) -> list[dict]:
    """[{platform, name, config, cwd, flavor?, facts}] from the detected files."""
    out: list[dict] = []
    dockerfiles: list[str] = []
    for path, text in files.items():
        base = path.split("/")[-1]
        cwd = "/".join(path.split("/")[:-1]) or "."
        if path.endswith(".vercel/project.json"):
            cwd = "/".join(path.split("/")[:-2]) or "."
        facts: dict[str, Any] = {}
        key, flavor = "", ""
        if base == "render.yaml":
            key = "render"
            facts["services"] = re.findall(r"^\s*-?\s*name:\s*([\w.-]+)", text, re.M)[:10]
        elif base == "vercel.json" or path.endswith(".vercel/project.json"):
            key = "vercel"
        elif base == "fly.toml":
            key, facts["app"] = "fly", _first(r"^\s*app\s*=\s*(.+)$", text)
        elif base == "netlify.toml":
            key, facts["publish"] = "netlify", _first(r"^\s*publish\s*=\s*(.+)$", text)
        elif base in ("railway.json", "railway.toml"):
            key = "railway"
        elif base.startswith("serverless."):
            key, flavor = "aws", "serverless"
            facts["service"] = _first(r"^service:\s*(.+)$", text)
        elif base in ("template.yaml", "template.yml") and "AWS::Serverless" in text:
            key, flavor = "aws", "sam"
        elif base == "samconfig.toml":
            key, flavor = "aws", "sam"
            facts["stack"] = _first(r"^\s*stack_name\s*=\s*(.+)$", text)
        elif base == "cdk.json":
            key, flavor = "aws", "cdk"
        elif base == "app.yaml" and re.search(r"^\s*runtime\s*:", text, re.M):
            key, flavor = "gcloud", "appengine"
            facts["service"] = _first(r"^\s*service\s*:\s*(.+)$", text) or "default"
        elif base == "azure.yaml":
            key, facts["name"] = "azure", _first(r"^name:\s*(.+)$", text)
        elif base == "Dockerfile":
            dockerfiles.append(cwd)
            continue
        if not key:
            continue
        item = {"platform": key, "name": PLATFORMS[key].name, "config": path, "cwd": cwd, "facts": {k: v for k, v in facts.items() if v}}
        if flavor:
            item["flavor"] = flavor
        # SAM is often template.yaml + samconfig.toml in the same dir: keep one
        if not any(o["platform"] == key and o["cwd"] == cwd and o.get("flavor") == item.get("flavor") for o in out):
            out.append(item)
    for cwd in dockerfiles:
        out.append(
            {"platform": "container", "name": "Container (Dockerfile)", "config": f"{cwd}/Dockerfile".lstrip("./") if cwd != "." else "Dockerfile",
             "cwd": cwd, "facts": {"can_deploy_with": list(CONTAINER_PLATFORMS)}}
        )
    return out


def _conn_text(c: Any) -> str:
    return " ".join(
        str(getattr(c, a, "") or "") for a in ("slug", "provider", "name", "alias", "description", "server_url")
    ).lower() + " " + " ".join(getattr(c, "tags", None) or []).lower()


def connector_for(connectors: dict[str, Any], p: Platform) -> Any | None:
    """A connected MCP / Composio connector for this platform (preferred over the CLI)."""
    words = (p.key, *p.hints)
    for c in (connectors or {}).values():
        if getattr(c, "slug", "") in (coding.COMPUTER_SLUG, coding.CODING_SLUG) or getattr(c, "provider", "") in ("daytona", "github_app"):
            continue
        text = _conn_text(c)
        if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in words):
            return c
    return None


def route_for(p: Platform, connectors: dict[str, Any], saved: dict[str, list[str]]) -> dict:
    conn = connector_for(connectors, p)
    if conn is not None:
        return {"route": "connector", "connector": getattr(conn, "slug", ""), "connector_name": getattr(conn, "name", ""),
                "how": f"Use the {getattr(conn, 'name', p.name)} connector's tools first (their writes show an approval card)."}
    have = set(saved.get(p.key) or [])
    missing = [e for e, _ in p.env if e not in have]
    if not missing:
        return {"route": "cli", "cli": p.cli, "how": f"deploy(platform='{p.key}', …) installs the {p.cli} CLI on the computer if needed and asks for approval."}
    return {"route": "connect", "missing": missing, "connect_url": connect_url(p),
            "how": f"Connect {p.name} (or its MCP connector) at {connect_url(p)}; the user enters {', '.join(missing)} there, never in chat."}


# ---------------------------------------------------------------------------
# Commands (only $ENV references, validated params)


def clean_params(raw: dict | None) -> dict:
    out: dict[str, Any] = {}
    for k, v in (raw or {}).items():
        if k == "production":
            out[k] = bool(v)
            continue
        if k not in PARAM_KEYS:
            raise DeployError(f"Unknown deploy parameter {k!r}. Allowed: {', '.join(PARAM_KEYS)}, production.")
        s = str(v or "").strip()
        if not s:
            continue
        if not _PARAM_OK.match(s) or ".." in s:
            raise DeployError(f"Bad value for {k}: {s!r}")
        out[k] = s
    return out


def check_cwd(cwd: str) -> str:
    cwd = (cwd or ".").strip().strip("/") or "."
    if cwd != "." and (not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", cwd) or ".." in cwd.split("/") or cwd.startswith("-")):
        raise DeployError(f"Bad directory {cwd!r}")
    return cwd


def _need(params: dict, key: str, why: str) -> str:
    if not params.get(key):
        raise DeployError(f"Missing {key}: {why}")
    return shlex.quote(params[key])


def command(p: Platform, action: str, params: dict) -> str:
    """The CLI command for an action. Secrets appear only as $ENV references."""
    q = shlex.quote
    k = p.key
    if k == "render":
        if action == "deploy":
            svc = _need(params, "service", "the Render service id (srv-…); deploy_status(platform='render') lists them")
            return f"render deploys create {svc} --wait --confirm -o json" + (f" --commit {q(params['ref'])}" if params.get("ref") else "")
        if action == "status":
            return f"render deploys list {q(params['service'])} --confirm -o json" if params.get("service") else "render services --confirm -o json"
        if action == "logs":
            return f"render logs -r {_need(params, 'service', 'the Render service id')} --limit 200 --confirm -o text"
        if action == "rollback":
            svc = _need(params, "service", "the Render service id")
            _need(params, "deployment", "the deploy id (dep-…) to roll back to; see deploy_status")
            body = q(json.dumps({"deployId": params["deployment"]}))
            return (f'curl -fsS -X POST -H "Authorization: Bearer $RENDER_API_KEY" -H "Content-Type: application/json" '
                    f"https://api.render.com/v1/services/{svc}/rollback -d {body}")
    if k == "vercel":
        tok = '--token "$VERCEL_TOKEN"'
        if action == "deploy":
            return f"vercel deploy --yes {tok}" + (" --prod" if params.get("production", True) else "")
        if action == "status":
            return f"vercel ls {tok}" + (f" {q(params['app'])}" if params.get("app") else "")
        if action == "logs":
            return f"vercel inspect {_need(params, 'deployment', 'the deployment URL or id')} --logs {tok}"
        if action == "rollback":
            return f"vercel rollback {q(params['deployment'])} --yes {tok}" if params.get("deployment") else f"vercel rollback --yes {tok}"
    if k == "fly":
        app = f" -a {q(params['app'])}" if params.get("app") else ""
        if action == "deploy":
            return f"flyctl deploy --remote-only --yes{app}"
        if action == "status":
            return f"flyctl status{app} && flyctl releases --image{app}"
        if action == "logs":
            return f"flyctl logs --no-tail{app}"
        if action == "rollback":
            return f"flyctl deploy --yes --image {_need(params, 'image', 'the earlier image (flyctl releases --image)')}{app}"
    if k == "netlify":
        site = '"$NETLIFY_SITE_ID"'
        if action == "deploy":
            return f"netlify deploy --build --site {site} --json" + (" --prod" if params.get("production", True) else "")
        if action == "status":
            return f"netlify api listSiteDeploys --data \"{{\\\"site_id\\\":\\\"$NETLIFY_SITE_ID\\\",\\\"per_page\\\":10}}\""
        if action == "logs":
            _need(params, "deployment", "the deploy id; see deploy_status")
            return f"netlify api getDeploy --data {q(json.dumps({'deploy_id': params['deployment']}))}"
        if action == "rollback":
            _need(params, "deployment", "the deploy id to restore; see deploy_status")
            data = '"{\\"site_id\\":\\"$NETLIFY_SITE_ID\\",\\"deploy_id\\":\\"' + params["deployment"] + '\\"}"'
            return f"netlify api restoreSiteDeploy --data {data}"
    if k == "railway":
        svc = f" --service {q(params['service'])}" if params.get("service") else ""
        if action == "deploy":
            return f"railway up --ci{svc}"
        if action == "status":
            return "railway status --json"
        if action == "logs":
            return f"timeout 30 railway logs{svc} || true"
        if action == "rollback":
            raise DeployError(p.notes)
    if k == "aws":
        flavor = params.get("flavor") or "serverless"
        stage = f" --stage {q(params['stage'])}" if params.get("stage") else ""
        stack = params.get("stack", "")
        if action == "deploy":
            if flavor == "serverless":
                return f"npx --yes serverless deploy{stage}"
            if flavor == "sam":
                name = f" --stack-name {q(stack)}" if stack else ""
                return f"uvx --from aws-sam-cli sam build && uvx --from aws-sam-cli sam deploy --no-confirm-changeset --no-fail-on-empty-changeset --resolve-s3{name}"
            if flavor == "cdk":
                return "npx --yes aws-cdk deploy --all --require-approval never"
            raise DeployError("flavor must be serverless, sam or cdk")
        if action == "status":
            return f"aws cloudformation describe-stacks --stack-name {q(stack)}" if stack else "aws cloudformation list-stacks --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE UPDATE_FAILED UPDATE_ROLLBACK_COMPLETE --max-items 30"
        if action == "logs":
            return f"aws cloudformation describe-stack-events --stack-name {_need(params, 'stack', 'the CloudFormation stack name')} --max-items 40"
        if action == "rollback":
            return f"aws cloudformation rollback-stack --stack-name {_need(params, 'stack', 'the CloudFormation stack name')}"
    if k == "gcloud":
        flavor = params.get("flavor") or "appengine"
        region = f" --region {q(params['region'])}" if params.get("region") else ' --region "${CLOUDSDK_RUN_REGION:-us-central1}"'
        if flavor == "run":
            svc = _need(params, "service", "the Cloud Run service name")
            if action == "deploy":
                return f"gcloud run deploy {svc} --source .{region} --quiet"
            if action == "status":
                return f"gcloud run services describe {svc}{region} --format='yaml(status)' && gcloud run revisions list --service {svc}{region} --limit 10"
            if action == "logs":
                return f"gcloud run services logs read {svc}{region} --limit 100"
            if action == "rollback":
                return f"gcloud run services update-traffic {svc}{region} --to-revisions {_need(params, 'version', 'the revision to send traffic to')}=100"
        svc = q(params.get("service") or "default")
        if action == "deploy":
            return "gcloud app deploy --quiet" + (f" --version {q(params['version'])}" if params.get("version") else "")
        if action == "status":
            return f"gcloud app versions list --service {svc}"
        if action == "logs":
            return f"gcloud app logs read --service {svc} --limit 100"
        if action == "rollback":
            return f"gcloud app services set-traffic {svc} --splits {_need(params, 'version', 'the version to send traffic to')}=1 --quiet"
    if k == "azure":
        app = _need(params, "app", "the Container App name")
        grp = _need(params, "group", "the resource group")
        if action == "deploy":
            return f"az containerapp up --name {app} --resource-group {grp} --source ."
        if action == "status":
            return f"az containerapp revision list --name {app} --resource-group {grp} -o table"
        if action == "logs":
            return f"az containerapp logs show --name {app} --resource-group {grp} --tail 100"
        if action == "rollback":
            return f"az containerapp ingress traffic set --name {app} --resource-group {grp} --revision-weight {_need(params, 'version', 'the revision name')}=100"
    raise DeployError(f"{action} is not supported for {p.name}")


def run_script(p: Platform, name: str, cwd: str, cmd: str) -> str:
    """Install the CLI if missing, sign in from env, run the command in the repo directory."""
    where = (f'"$HOME/work/{name}"' + ("" if cwd == "." else f"/{shlex.quote(cwd)}")) if name else '"$HOME"'
    install = f"command -v {p.cli} >/dev/null 2>&1 || {{ echo 'Installing the {p.cli} CLI…'; {p.install}; }}"
    return "\n".join(
        x for x in (
            'mkdir -p "$HOME/.local/bin"; export PATH="$HOME/.local/bin:$HOME/.fly/bin:$PATH"; export CI=1',
            f"cd {where} || {{ echo 'Run git_checkout first: the repo directory is missing.'; exit 3; }}",
            install,
            f"command -v {p.cli} >/dev/null 2>&1 || {{ echo 'Could not install the {p.cli} CLI on the computer.'; exit 5; }}",
            p.login,
            cmd,
        ) if x
    )


def secrets_of(env: dict[str, str]) -> list[str]:
    """Every value to scrub: whole values plus long pieces (e.g. a JSON key's private_key lines)."""
    out: list[str] = []
    for v in (env or {}).values():
        v = str(v or "")
        if len(v) < 4:
            continue
        out.append(v)
        out.extend(part for part in re.split(r"[\s\"',:{}]+", v) if len(part) >= 16)
    return sorted(set(out), key=len, reverse=True)


_URL = re.compile(r"https://[^\s\"'<>]+")


def urls_in(text: str) -> list[str]:
    seen: list[str] = []
    for u in _URL.findall(text or ""):
        u = u.rstrip(".,)")
        if u not in seen and not u.startswith("https://api."):
            seen.append(u)
    return seen[:5]


# ---------------------------------------------------------------------------
# Credentials (never model-visible)


async def saved_env_names(ctx) -> dict[str, list[str]]:
    """{platform: [ENV names saved]} for this user. Values are never returned."""
    db = getattr(ctx, "db", None)
    if db is None:
        return {}
    try:
        from sqlalchemy import select

        from app.models import DeployCredential
        from app.security import decrypt_json

        rows = (await db.execute(select(DeployCredential).where(DeployCredential.user_id == ctx.user.id))).scalars().all()
        return {r.platform: sorted(k for k, v in decrypt_json(r.credentials_enc).items() if v) for r in rows}
    except Exception:  # noqa: BLE001
        return {}


async def load_env(ctx, p: Platform) -> dict[str, str]:
    db = getattr(ctx, "db", None)
    if db is None:
        return {}
    from sqlalchemy import select

    from app.models import DeployCredential
    from app.security import decrypt_json

    row = (
        await db.execute(select(DeployCredential).where(DeployCredential.user_id == ctx.user.id, DeployCredential.platform == p.key))
    ).scalar_one_or_none()
    if row is None:
        return {}
    allowed = set(env_names(p))
    return {k: str(v) for k, v in decrypt_json(row.credentials_enc).items() if k in allowed and v}


# ---------------------------------------------------------------------------
# Agent tools


async def _exec(ctx, script: str, timeout: int, env: dict | None = None) -> dict:
    from app.agent import git_tools

    return await git_tools.sandbox_exec(ctx, script, timeout, env=env)


async def _record(ctx, kind: str, text: str, data: dict) -> None:
    from app.agent import git_tools

    await git_tools._record(ctx, kind, text, data)


async def detect_deploy_targets(ctx, repo: str) -> dict:
    try:
        owner, name = coding.split_repo(repo)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    res = await _exec(ctx, detect_script(name), 60)
    if res.get("error"):
        return res
    if res.get("exit_code") == 3:
        return {"error": res.get("output", "").strip()}
    targets = targets_from_files(parse_detect(res.get("output", "")))
    saved = await saved_env_names(ctx)
    keys = [t["platform"] for t in targets if t["platform"] != "container"]
    if any(t["platform"] == "container" for t in targets):
        keys += [k for k in CONTAINER_PLATFORMS if k not in keys]
    routes = {k: {"name": PLATFORMS[k].name, **route_for(PLATFORMS[k], ctx.connectors, saved)} for k in dict.fromkeys(keys)}
    out = {"repo": f"{owner}/{name}", "targets": targets, "platforms": routes}
    if not targets:
        out["note"] = (
            "No deploy config found. Ask the user where it should run (ask_user with platform options), "
            "or add a config (render.yaml, vercel.json, fly.toml, …) in the PR first."
        )
    return out


def _confirmed(ctx, args: dict) -> bool:
    for c in getattr(ctx, "confirmations", None) or []:
        if c.get("slug") == coding.CODING_SLUG and c.get("tool") == "deploy_action" and (c.get("args") or {}) == args:
            return True
    return False


async def request_action(ctx, action: str, repo: str, platform_key: str, params: dict | None = None, cwd: str = ".", use_cli: bool = False) -> dict:
    """deploy / rollback: validate, check the route and credentials, then create the approval card."""
    from app.agent.confirm import sign_confirm

    try:
        if action not in ACTIONS:
            raise DeployError(f"action must be one of {ACTIONS}")
        p = platform(platform_key)
        owner, name = coding.split_repo(repo)
        params = clean_params(params)
        cwd = check_cwd(cwd)
        cmd = command(p, action, params)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    conn = connector_for(ctx.connectors, p)
    if conn is not None and not use_cli:
        return {
            "status": "use_connector", "connector": getattr(conn, "slug", ""),
            "next": f"Prefer the {getattr(conn, 'name', p.name)} connector: search its tools for '{action}' and call that "
                    "(it shows an approval card). Call this again with use_cli=true only if the connector cannot do it.",
        }
    saved = (await saved_env_names(ctx)).get(p.key, [])
    missing = [e for e, _ in p.env if e not in saved]
    if missing:
        await _record(ctx, "status", f"Deploy blocked: {p.name} is not connected", {"platform": p.key, "connect_url": connect_url(p)})
        return {
            "status": "blocked", "platform": p.key, "missing": missing, "connect_url": connect_url(p), "token_help": p.token_url,
            "next": f"Tell the user exactly what is missing ({', '.join(missing)}) and give the connect link; they enter it on the "
                    "Connectors page, never in chat. Then ask_user(waiting_for='deploy_" + p.key + "', options=['Connected', 'Not now']).",
        }
    args = {"action": action, "repo": f"{owner}/{name}", "platform": p.key, "params": params, "cwd": cwd}
    if _confirmed(ctx, args):
        return await run_action(ctx, args)
    preview = {
        "kind": "deploy", "action": action, "platform": p.name, "repo": f"{owner}/{name}", "directory": cwd,
        "target": params, "command": cmd, "credentials": f"saved {p.name} credential ({', '.join(saved)}), passed as env only",
    }
    token = sign_confirm("tool", {"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": args}, ttl=DEPLOY_CONFIRM_TTL)
    label = "Deploy" if action == "deploy" else "Roll back"
    target = params.get("service") or params.get("app") or params.get("stack") or name
    ctx.pending.append({"needs_confirm": True, "token": token, "kind": "deploy", "title": f"{label} {target} on {p.name}", "preview": preview})
    return {"status": "awaiting_user_confirmation", "preview": preview}


async def run_action(ctx, args: dict) -> dict:
    """Runs only after the user approved this exact action, platform, repo, directory and target."""
    try:
        p = platform(args.get("platform") or "")
        owner, name = coding.split_repo(args.get("repo") or "")
        params = clean_params(args.get("params"))
        cwd = check_cwd(args.get("cwd") or ".")
        action = args.get("action")
        if action not in ACTIONS:
            raise DeployError("bad action")
        cmd = command(p, action, params)
        env = await load_env(ctx, p)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    if any(e not in env for e, _ in p.env):
        return {"status": "blocked", "connect_url": connect_url(p), "error": f"{p.name} is no longer connected."}
    res = await _exec(ctx, run_script(p, name, cwd, cmd), 900, env)
    if res.get("error"):
        return res
    ok = res.get("exit_code") == 0
    out = {"platform": p.key, "action": action, "repo": f"{owner}/{name}", "ok": ok, "exit_code": res.get("exit_code"),
           "output": res.get("output", "")[-6000:], "urls": urls_in(res.get("output", ""))}
    verb = "Deployed" if action == "deploy" else "Rolled back"
    await _record(
        ctx, "deploy",
        (f"{verb} {owner}/{name} on {p.name}" if ok else f"{action.title()} on {p.name} failed (exit {res.get('exit_code')})")
        + (f": {out['urls'][0]}" if ok and out["urls"] else ""),
        {"platform": p.key, "action": action, "ok": ok, "url": (out["urls"] or [None])[0], "output": out["output"][-3000:]},
    )
    if not ok:
        out["next"] = "Read the output, fix the cause (config, build, missing env on the platform) and propose the deploy again, or report it."
    return out


async def read_action(ctx, action: str, repo: str, platform_key: str, params: dict | None = None, cwd: str = ".") -> dict:
    """deploy_status / deploy_logs: read-only, no card, credentials still only in env."""
    try:
        p = platform(platform_key)
        owner, name = coding.split_repo(repo) if repo else ("", "")
        params = clean_params(params)
        cwd = check_cwd(cwd)
        cmd = command(p, action, params)
        env = await load_env(ctx, p)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    conn = connector_for(ctx.connectors, p)
    if any(e not in env for e, _ in p.env):
        if conn is not None:
            return {"status": "use_connector", "connector": getattr(conn, "slug", ""), "next": f"Use the {conn.name} connector's {action} tools."}
        return {"status": "blocked", "missing": [e for e, _ in p.env if e not in env], "connect_url": connect_url(p)}
    res = await _exec(ctx, run_script(p, name, cwd, cmd), 300, env)
    if res.get("error"):
        return res
    return {"platform": p.key, "action": action, "ok": res.get("exit_code") == 0, "exit_code": res.get("exit_code"), "output": res.get("output", "")[-8000:]}


def platforms_public(saved: dict[str, list[str]] | None = None) -> list[dict]:
    """For the Connectors page: fields to enter per platform and which are saved (never values)."""
    saved = saved or {}
    return [
        {
            "key": p.key, "name": p.name, "cli": p.cli, "token_url": p.token_url, "notes": p.notes,
            "fields": [{"env": e, "label": lbl, "required": True} for e, lbl in p.env]
            + [{"env": e, "label": lbl, "required": False} for e, lbl in p.optional_env],
            "saved": saved.get(p.key, []),
            "connected": all(e in saved.get(p.key, []) for e, _ in p.env),
        }
        for p in PLATFORMS.values()
    ]


def add_readiness(out: dict, connectors: dict[str, Any], saved: dict[str, list[str]]) -> dict:
    """coding_workspace_status: which platforms can deploy now (connector or saved CLI credential) and how to connect."""
    routes = {k: route_for(p, connectors, saved) for k, p in PLATFORMS.items()}
    ready = {k: r for k, r in routes.items() if r["route"] != "connect"}
    out["deploy"] = {
        "ready": {k: (r.get("connector_name") or f"{PLATFORMS[k].cli} CLI") for k, r in ready.items()},
        "connect_url": connect_url("all"),
    }
    cli = sorted(PLATFORMS[k].name for k, r in ready.items() if r["route"] == "cli")
    if cli:
        out.setdefault("can", []).append(
            "deploy, check status and logs, and roll back on " + ", ".join(cli) + " with the CLI on the computer (each deploy or rollback asks for approval)"
        )
    if ready:
        out["missing"] = [m for m in out.get("missing", []) if not m.startswith("No deploy connector")]
    else:
        out["missing"] = [m for m in out.get("missing", []) if not m.startswith("No deploy connector")] + [
            "No deploy platform is connected, so I cannot deploy yet. Connect one at "
            f"{out['deploy']['connect_url']} (Render, Vercel, Fly.io, Netlify, Railway, AWS, Google Cloud, Azure), "
            "or add its MCP connector."
        ]
    return out


_DEPLOY_WRITE = re.compile(
    r"(deploy|rollback|roll_back|redeploy|restart|promote|scale|suspend|resume|update_service|update_env|set_env|"
    r"create_service|delete_service|update_environment|purge|cancel)",
    re.I,
)
_READ_PREFIX = re.compile(r"^(?:[A-Z]+_)?(list|get|describe|read|search|fetch|query|show|view|status|logs?|retrieve)", re.I)


def is_deploy_write(conn: Any, slug: str, tool: str, args: dict | None = None) -> bool:
    """A connector action that changes a deployment (Render/Vercel/… MCP or Composio). Always needs approval."""
    names = [tool] + [str(v) for k, v in (args or {}).items() if k in ("tool_slug", "tool", "action", "name") and isinstance(v, str)]
    deployish = any(connector_for({"c": conn}, p) is not None for p in PLATFORMS.values()) or any(
        re.search(rf"(?<![a-z]){re.escape(p.key)}", " ".join(names).lower()) for p in PLATFORMS.values()
    )
    if not deployish:
        return False
    for n in names:
        last = re.split(r"[./]", n)[-1]
        if _DEPLOY_WRITE.search(last) and not _READ_PREFIX.match(last):
            return True
    return False
