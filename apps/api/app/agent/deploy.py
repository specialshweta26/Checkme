"""Deploy from the coding agent on any platform, without the user configuring anything first.

Flow (every platform):

1. ``detect_deploy_targets(repo)`` checks the repo out on the computer if it is not there yet, lists the
   deploy configs in ``~/work/<repo>`` (render.yaml, vercel.json, fly.toml, netlify.toml, railway.json/toml,
   serverless.yml, SAM, CDK, app.yaml, azure.yaml, Dockerfile) and, when there is none, detects the stack and
   suggests a platform plus a generated config (``deploy_config``).
2. Auth, per platform, in this order: a connected MCP/Composio connector; a key saved on the Connectors page
   (optional shortcut); the CLI's own session on the computer; else the CLI's browser / device login is
   started on the computer and the user gets its link (``cli_login``). The job waits and resumes by itself.
3. ``deploy`` / ``deploy_rollback`` always create ONE approval card (kind ``deploy``: platform, service,
   repo/branch, region and cost tier, the exact command), also in scheduled runs and background jobs where
   ``auto_confirm`` is ignored. After approval, bounded retries of the same deploy (after a fix) in the same
   job reuse that approval. Installs, sign-in, config generation, status and logs need no approval.
4. Saved keys are decrypted server-side and passed to the computer only as exec ``env``; CLI sessions stay in
   the computer's home directory. No token is put in a command, a repo file, the card, the timeline or
   model-visible output (output is scrubbed).
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
        notes="Pass service=srv-… for an existing service; without one the deploy creates a free web service from the GitHub repo.",
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
        notes="Rollback is not in the Railway CLI; use Rollback on the deployment in the Railway dashboard (railway.com/dashboard).",
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
        login='if [ -n "${GCP_SERVICE_ACCOUNT_JSON:-}" ]; then K=$(mktemp); trap \'rm -f "$K"\' EXIT; printf %s "$GCP_SERVICE_ACCOUNT_JSON" > "$K"; '
        'gcloud auth activate-service-account --key-file="$K" --quiet >/dev/null 2>&1 || { echo "gcloud sign-in failed"; exit 4; }; fi',
        notes="App Engine (app.yaml) by default; pass flavor=run and service=<name> for Cloud Run (needs a Dockerfile or buildpacks).",
        flavors=("appengine", "run"),
    ),
    "azure": Platform(
        "azure", "Azure", ("azure.yaml",), "az",
        '(command -v uv >/dev/null && uv tool install -q azure-cli >/dev/null 2>&1) || python3 -m pip install -q --user azure-cli >/dev/null 2>&1; true',
        (("AZURE_CLIENT_ID", "Service principal app id"), ("AZURE_CLIENT_SECRET", "Service principal secret"),
         ("AZURE_TENANT_ID", "Tenant id"), ("AZURE_SUBSCRIPTION_ID", "Subscription id")),
        hints=("azure",), token_url="https://learn.microsoft.com/cli/azure/azure-cli-sp-tutorial-1",
        login='if [ -n "${AZURE_CLIENT_SECRET:-}" ]; then az login --service-principal -u "$AZURE_CLIENT_ID" -p "$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID" >/dev/null 2>&1 '
        '&& az account set -s "$AZURE_SUBSCRIPTION_ID" || { echo "az sign-in failed"; exit 4; }; fi',
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
        if getattr(c, "slug", "") in (coding.COMPUTER_SLUG, coding.CODING_SLUG) or getattr(c, "provider", "") in ("daytona", "composio_github", "github_app"):
            continue
        text = _conn_text(c)
        if any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", text) for w in words):
            return c
    return None


def route_for(p: Platform, connectors: dict[str, Any], saved: dict[str, list[str]]) -> dict:
    """How this platform is reached: its connector, a saved key, or the CLI (its session, or a sign-in link)."""
    conn = connector_for(connectors, p)
    if conn is not None:
        return {"route": "connector", "connector": getattr(conn, "slug", ""), "connector_name": getattr(conn, "name", ""),
                "how": f"Use the {getattr(conn, 'name', p.name)} connector's tools first (their writes show an approval card)."}
    have = set(saved.get(p.key) or [])
    if all(e in have for e, _ in p.env):
        return {"route": "cli", "auth": "saved_key", "cli": p.cli,
                "how": f"deploy(platform='{p.key}', …) installs the {p.cli} CLI if needed and uses the saved key; it asks for approval."}
    return {"route": "cli", "auth": "cli_login", "cli": p.cli,
            "how": f"deploy(platform='{p.key}', …) installs the {p.cli} CLI, reuses its session on the computer or starts its "
                   "sign-in and hands the user one link (the job resumes by itself), then asks for approval."}


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


RENDER_TOKEN = (
    # The Render CLI session token (or a saved key) for the REST calls the CLI has no command for. Never printed.
    'RK="${RENDER_API_KEY:-$(awk \'/^api:/{a=1;next} a&&/^[^ ]/{a=0} a&&/^ +key:/{print $2; exit}\' "$HOME/.render/cli.yaml" 2>/dev/null)}"; '
    '[ -n "$RK" ] || { echo "Not logged in to Render: run render login"; exit 4; }; '
)

RENDER_CREATE = r'''
import json, os, sys, time, urllib.request, urllib.error
spec = json.loads(sys.argv[1]); tok = os.environ["RK"]; API = os.environ.get("CHECKME_RENDER_API") or "https://api.render.com/v1"
def call(method, path, body=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + tok, "Accept": "application/json", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read(); return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try: return e.code, json.loads(raw)
        except Exception: return e.code, {"message": raw[:300].decode("utf-8", "replace")}
def out(**k): print("@@RENDER " + json.dumps(k)); sys.stdout.flush()
owner = os.environ.get("RENDER_WORKSPACE") or ""
if not owner:
    try:
        for line in open(os.path.expanduser("~/.render/cli.yaml")):
            if line.startswith("workspace:"): owner = line.split(":", 1)[1].strip().strip("'\""); break
    except OSError: pass
if not owner:
    st, owners = call("GET", "/owners?limit=20")
    items = [o.get("owner") or o for o in owners] if isinstance(owners, list) else []
    owner = (items[0].get("id") if items else "") or ""
if not owner: out(ok=False, error="no Render workspace found"); sys.exit(4)
spec["ownerId"] = owner
st, found = call("GET", "/services?limit=20&ownerId=" + owner + "&name=" + spec["name"])
svc = next(((x.get("service") or x) for x in (found if isinstance(found, list) else []) if (x.get("service") or x).get("name") == spec["name"]), None)
existing = bool(svc)
if svc:
    sid = svc["id"]; st, dep = call("POST", "/services/" + sid + "/deploys", {})
    if st >= 300: out(ok=False, status=st, error=dep.get("message"), service=sid); sys.exit(1)
    dep_id = dep.get("id")
else:
    st, made = call("POST", "/services", spec)
    if st == 402: out(ok=False, status=402, error="Render needs a payment method on this workspace", human="https://dashboard.render.com/billing"); sys.exit(7)
    if st >= 300:
        msg = str(made.get("message") or made)[:300]
        human = "https://dashboard.render.com/select-repo?type=" + ("static" if spec["type"] == "static_site" else "web") if "repo" in msg.lower() or "git" in msg.lower() else ""
        out(ok=False, status=st, error=msg, human=human); sys.exit(1)
    svc = made.get("service") or made; sid = svc["id"]; dep_id = made.get("deployId")
url = ((svc.get("serviceDetails") or {}).get("url")) or ""
out(service=sid, name=spec["name"], dashboard=svc.get("dashboardUrl"), url=url, deploy=dep_id, created=not existing)
status = ""
for _ in range(int(os.environ.get("CHECKME_RENDER_POLLS") or 84)):
    time.sleep(float(os.environ.get("CHECKME_RENDER_POLL_S") or 10))
    st, deps = call("GET", "/services/" + sid + "/deploys?limit=1")
    d = ((deps[0].get("deploy") or deps[0]) if isinstance(deps, list) and deps else {})
    status = d.get("status") or status
    if status in ("live", "build_failed", "update_failed", "canceled", "deactivated", "pre_deploy_failed"): break
ok = status == "live"
if not url:
    st, s2 = call("GET", "/services/" + sid); url = ((s2.get("serviceDetails") or {}).get("url")) or ""
out(ok=ok, status=status or "timeout", service=sid, url=url, dashboard=svc.get("dashboardUrl"))
if not ok:
    st, logs = call("GET", "/logs?ownerId=" + owner + "&resource=" + sid + "&limit=80&direction=backward")
    for l in reversed((logs or {}).get("logs") or []): print(str(l.get("message"))[:400])
sys.exit(0 if ok else 1)
'''


def render_create_command(spec_json: dict) -> str:
    return RENDER_TOKEN + "RK=\"$RK\" python3 - " + shlex.quote(json.dumps(spec_json)) + " <<'PY'\n" + RENDER_CREATE + "\nPY"


def command(p: Platform, action: str, params: dict, name: str = "", create: dict | None = None) -> str:
    """The CLI command for an action. Secrets appear only as $ENV references (and only when saved); without a saved
    key the CLI uses its own session on the computer. ``name`` / ``create`` cover first deploys of a new app."""
    q = shlex.quote
    k = p.key
    app_name = q(params.get("app") or params.get("service") or name or "app")
    if k == "render":
        if action == "deploy":
            if not params.get("service"):
                if not create:
                    raise DeployError("Missing service: pass service=srv-… for an existing Render service (deploy_status lists them).")
                return render_create_command(create)
            svc = q(params["service"])
            return f"render deploys create {svc} --wait --confirm -o json" + (f" --commit {q(params['ref'])}" if params.get("ref") else "")
        if action == "status":
            return f"render deploys list {q(params['service'])} --confirm -o json" if params.get("service") else "render services --confirm -o json"
        if action == "logs":
            return f"render logs -r {_need(params, 'service', 'the Render service id')} --limit 200 --confirm -o text"
        if action == "rollback":
            svc = _need(params, "service", "the Render service id")
            _need(params, "deployment", "the deploy id (dep-…) to roll back to; see deploy_status")
            body = q(json.dumps({"deployId": params["deployment"]}))
            return (RENDER_TOKEN + f'curl -fsS -X POST -H "Authorization: Bearer $RK" -H "Content-Type: application/json" '
                    f"https://api.render.com/v1/services/{svc}/rollback -d {body}")
    if k == "vercel":
        tok = '${VERCEL_TOKEN:+--token "$VERCEL_TOKEN"}'
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
            region = q(params.get("region") or "bom")
            # First deploy of a repo without fly.toml: fly launch writes it (and a Dockerfile) and creates the app.
            launch = (f"[ -f fly.toml ] || flyctl launch --no-deploy --yes --copy-config --name {app_name} --region {region} "
                      "--org personal --ha=false; ") if name or params.get("app") else ""
            return f"{launch}flyctl deploy --remote-only --yes --ha=false{app}"
        if action == "status":
            return f"flyctl status{app} && flyctl releases --image{app}"
        if action == "logs":
            return f"flyctl logs --no-tail{app}"
        if action == "rollback":
            return f"flyctl deploy --yes --image {_need(params, 'image', 'the earlier image (flyctl releases --image)')}{app}"
    if k == "netlify":
        site = '"${NETLIFY_SITE_ID:-' + (params.get("service") or "") + '}"'
        if action == "deploy":
            # Saved/explicit site id; else the linked site (.netlify/state.json); else create one named after the repo.
            pick = (f'S=""; if [ -n {site} ]; then S="--site "{site}; elif [ ! -f .netlify/state.json ]; then S="--site-name {app_name}"; fi; ')
            return pick + "netlify deploy --build $S --json" + (" --prod" if params.get("production", True) else "")
        if action == "status":
            return ("SID=" + site + '; [ -n "$SID" ] || SID=$(python3 -c "import json;print(json.load(open(\'.netlify/state.json\')).get(\'siteId\',\'\'))" 2>/dev/null); '
                    "netlify api listSiteDeploys --data \"{\\\"site_id\\\":\\\"$SID\\\",\\\"per_page\\\":10}\"")
        if action == "logs":
            _need(params, "deployment", "the deploy id; see deploy_status")
            return f"netlify api getDeploy --data {q(json.dumps({'deploy_id': params['deployment']}))}"
        if action == "rollback":
            _need(params, "deployment", "the deploy id to restore; see deploy_status")
            data = '"{\\"site_id\\":\\"' + "${NETLIFY_SITE_ID:-" + (params.get("service") or "") + "}" + '\\",\\"deploy_id\\":\\"' + params["deployment"] + '\\"}"'
            return f"netlify api restoreSiteDeploy --data {data}"
    if k == "railway":
        svc = f" --service {q(params['service'])}" if params.get("service") else ""
        if action == "deploy":
            init = f"railway status --json >/dev/null 2>&1 || railway init --name {app_name}; " if name else ""
            return f"{init}railway up --ci{svc} && (railway domain{svc} 2>/dev/null || true)"
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
                name_arg = f" --stack-name {q(stack)}" if stack else ""
                return f"uvx --from aws-sam-cli sam build && uvx --from aws-sam-cli sam deploy --no-confirm-changeset --no-fail-on-empty-changeset --resolve-s3{name_arg}"
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
        region = f" --region {q(params['region'])}" if params.get("region") else ' --region "${CLOUDSDK_RUN_REGION:-asia-south1}"'
        if flavor == "run":
            svc = _need(params, "service", "the Cloud Run service name")
            if action == "deploy":
                return f"gcloud run deploy {svc} --source .{region} --allow-unauthenticated --quiet"
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
            loc = q(params.get("region") or "centralindia")
            return f"az containerapp up --name {app} --resource-group {grp} --location {loc} --source . --ingress external"
        if action == "status":
            return f"az containerapp revision list --name {app} --resource-group {grp} -o table"
        if action == "logs":
            return f"az containerapp logs show --name {app} --resource-group {grp} --tail 100"
        if action == "rollback":
            return f"az containerapp ingress traffic set --name {app} --resource-group {grp} --revision-weight {_need(params, 'version', 'the revision name')}=100"
    raise DeployError(f"{action} is not supported for {p.name}")


def run_script(p: Platform, name: str, cwd: str, cmd: str) -> str:
    """Install the CLI if missing, sign in from a saved key if there is one (else the CLI's own session), run the command."""
    from app.agent.cli_login import SHIM

    where = (f'"$HOME/work/{name}"' + ("" if cwd == "." else f"/{shlex.quote(cwd)}")) if name else '"$HOME"'
    install = f"command -v {p.cli} >/dev/null 2>&1 || {{ echo 'Installing the {p.cli} CLI…'; {p.install}; }}"
    return "\n".join(
        x for x in (
            SHIM + "export CI=1",
            f"cd {where} || {{ echo 'The repo directory is missing on the computer.'; exit 3; }}",
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


async def _ensure_checkout(ctx, repo: str) -> dict:
    """Clone the repo onto the computer when it is not there yet (or hand the user a GitHub sign-in link)."""
    from app.agent import git_tools

    return await git_tools.ensure_checkout(ctx, repo)


async def _ensure_auth(ctx, p: Platform, why: str) -> dict:
    """{auth: saved_key|cli_session, env} when the CLI can act; else cli_login's handoff ({status:
    awaiting_user_login, url, ...}) or an error. A saved key (Connectors page) is only a shortcut."""
    from app.agent import cli_login

    env = await load_env(ctx, p)
    if env and all(e in env for e, _ in p.env):
        return {"auth": "saved_key", "env": env}
    login = await cli_login.ensure_login(ctx, p.key, why)
    if login.get("status") == "logged_in":
        return {"auth": "cli_session", "env": {}}
    return login


def _checkout_failed(res: dict) -> bool:
    return not res.get("ok")


async def _repo_info(ctx, name: str, cwd: str = ".") -> dict:
    """Stack facts plus the branch GitHub has (current branch if pushed, else the default branch)."""
    from app.agent import deploy_config

    script = deploy_config.stack_script(name, cwd) + (
        '\nB=$(git rev-parse --abbrev-ref HEAD 2>/dev/null); D=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed "s#^origin/##"); '
        'git rev-parse --verify -q "origin/$B" >/dev/null 2>&1 || B="${D:-main}"; echo "@@BRANCH ${B:-main}"'
    )
    res = await _exec(ctx, script, 60)
    if res.get("error"):
        return res
    out = res.get("output", "")
    branch = next((ln.split(" ", 1)[1].strip() for ln in out.splitlines() if ln.startswith("@@BRANCH ")), "main")
    facts = deploy_config.parse_stack(out)
    return {"facts": facts, "stack": deploy_config.stack_from_facts(facts) if facts else {"kind": "unknown"}, "branch": branch}


async def _targets(ctx, name: str) -> list[dict] | dict:
    res = await _exec(ctx, detect_script(name), 60)
    if res.get("error"):
        return res
    if res.get("exit_code") == 3:
        return {"error": res.get("output", "").strip()}
    return targets_from_files(parse_detect(res.get("output", "")))


async def detect_deploy_targets(ctx, repo: str) -> dict:
    from app.agent import deploy_config

    try:
        owner, name = coding.split_repo(repo)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    co = await _ensure_checkout(ctx, f"{owner}/{name}")
    if _checkout_failed(co):
        return co
    targets = await _targets(ctx, name)
    if isinstance(targets, dict):
        return targets
    saved = await saved_env_names(ctx)
    keys = [t["platform"] for t in targets if t["platform"] != "container"]
    if any(t["platform"] == "container" for t in targets):
        keys += [k for k in CONTAINER_PLATFORMS if k not in keys]
    out: dict[str, Any] = {"repo": f"{owner}/{name}", "targets": targets}
    if not [t for t in targets if t["platform"] != "container"]:
        info = await _repo_info(ctx, name)
        stack = info.get("stack") or {"kind": "unknown"}
        key, why = deploy_config.choose_platform("", targets, stack)
        keys = list(dict.fromkeys([key, "render", "vercel", *keys]))
        out["stack"] = {k: stack.get(k) for k in ("kind", "runtime", "build", "start", "publish", "env_keys") if stack.get(k)}
        out["suggested"] = {"platform": key, "why": why, **deploy_config.DEFAULTS.get(key, {})}
        out["note"] = (
            "No platform config in the repo, and none is needed to start: deploy(platform=…) works from the code "
            "(Render creates a free web service from the GitHub repo; Vercel/Netlify/Railway/Fly/Cloud Run/Azure upload the "
            "working tree). Use the platform the user named, else the suggested one, and say which and why in one line. "
            "generate_deploy_config adds a config file if you want it committed in the PR."
        )
    out["platforms"] = {k: {"name": PLATFORMS[k].name, **route_for(PLATFORMS[k], ctx.connectors, saved)} for k in dict.fromkeys(keys)}
    return out


def _confirmed(ctx, args: dict) -> bool:
    for c in getattr(ctx, "confirmations", None) or []:
        if c.get("slug") == coding.CODING_SLUG and c.get("tool") == "deploy_action" and (c.get("args") or {}) == args:
            return True
    return False


GRANT_RETRIES = 3  # re-runs of the same approved deploy (after a fix) in the same job without a new card


def _grant_key(args: dict) -> str:
    return json.dumps({k: args.get(k) for k in ("action", "repo", "platform", "params", "cwd")}, sort_keys=True)


def _grant(ctx, args: dict) -> None:
    job = getattr(ctx, "job", None)
    if job is None or args.get("action") != "deploy":
        return
    meta = dict(getattr(job, "meta", None) or {})
    grants = dict(meta.get("deploy_grants") or {})
    grants[_grant_key(args)] = GRANT_RETRIES
    meta["deploy_grants"] = grants
    job.meta = meta


def _use_grant(ctx, args: dict) -> bool:
    job = getattr(ctx, "job", None)
    if job is None or args.get("action") != "deploy":
        return False
    meta = dict(getattr(job, "meta", None) or {})
    grants = dict(meta.get("deploy_grants") or {})
    left = int(grants.get(_grant_key(args)) or 0)
    if left <= 0:
        return False
    grants[_grant_key(args)] = left - 1
    meta["deploy_grants"] = grants
    job.meta = meta
    return True


def _defaults(p: Platform, action: str, params: dict, name: str, targets: list[dict]) -> dict:
    """Fill app / service / group names and regions for a first deploy (free tier, near Bengaluru)."""
    from app.agent import deploy_config

    if action != "deploy":
        return params
    out = dict(params)
    app = deploy_config.slug(name)
    region = deploy_config.DEFAULTS.get(p.key, {}).get("region", "")
    if p.key == "fly":
        out.setdefault("app", next((t.get("app") for t in targets if t.get("platform") == "fly" and t.get("app")), app))
    elif p.key == "gcloud":
        if not any(t.get("platform") == "gcloud" for t in targets) and not out.get("flavor"):
            out["flavor"] = "run"
        if out.get("flavor") == "run":
            out.setdefault("service", app)
            out.setdefault("region", region)
    elif p.key == "azure":
        out.setdefault("app", app)
        out.setdefault("group", f"{app}-rg")
        out.setdefault("region", region)
    return clean_params(out)


def _summary(p: Platform, action: str, params: dict, create: dict | None, cmd: str) -> str:
    if create:
        sd = create.get("serviceDetails") or {}
        kind = "static site" if create.get("type") == "static_site" else f"{sd.get('runtime', '')} web service"
        return (f"Render API: create {kind} '{create['name']}' ({sd.get('plan', 'free')}, {sd.get('region', 'global CDN')}) from "
                f"{create['repo']} @ {create['branch']}, then wait for the deploy (an existing service with that name is redeployed)")
    return cmd


async def request_action(ctx, action: str, repo: str, platform_key: str, params: dict | None = None, cwd: str = ".", use_cli: bool = False) -> dict:
    """deploy / rollback: check the repo out, pick defaults, sign the CLI in (link handoff), then ONE approval card."""
    from app.agent import deploy_config
    from app.agent.confirm import sign_confirm

    try:
        if action not in ACTIONS:
            raise DeployError(f"action must be one of {ACTIONS}")
        owner, name = coding.split_repo(repo)
        params = clean_params(params)
        cwd = check_cwd(cwd)
        if platform_key:
            p = platform(platform_key)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    full = f"{owner}/{name}"
    if platform_key:
        conn = connector_for(ctx.connectors, p)
        if conn is not None and not use_cli:
            return {
                "status": "use_connector", "connector": getattr(conn, "slug", ""),
                "next": f"Prefer the {getattr(conn, 'name', p.name)} connector: search its tools for '{action}' and call that "
                        "(it shows an approval card). Call this again with use_cli=true only if the connector cannot do it.",
            }
    co = await _ensure_checkout(ctx, full)
    if _checkout_failed(co):
        return co
    targets = await _targets(ctx, name)
    targets = targets if isinstance(targets, list) else []
    chosen = ""
    info: dict = {}
    if not platform_key or (action == "deploy" and platform_key == "render" and not params.get("service")):
        info = await _repo_info(ctx, name, cwd)
    if not platform_key:
        key, why = deploy_config.choose_platform("", targets, info.get("stack") or {})
        p, chosen = platform(key), why
    create = None
    try:
        params = _defaults(p, action, params, name, targets)
        if p.key == "render" and action == "deploy" and not params.get("service"):
            stack = info.get("stack") or {}
            if stack.get("kind") in (None, "unknown"):
                raise DeployError("Could not tell how to build this repo for Render; pass service=srv-… or add a render.yaml / Dockerfile.")
            create = deploy_config.render_spec(stack, name, f"https://github.com/{full}", info.get("branch") or "main")
        cmd = command(p, action, params, name=deploy_config.slug(name), create=create)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    if p.key == "aws" and action == "deploy" and not any(t.get("platform") == "aws" for t in targets):
        return {"error": "AWS needs infrastructure-as-code in the repo (serverless.yml, SAM template or CDK app). "
                         "Deploy to Render or Vercel instead, or add one first.", "platform": "aws"}
    args = {"action": action, "repo": full, "platform": p.key, "params": params, "cwd": cwd}
    if create:
        args["create"] = create
    if _confirmed(ctx, args) or _use_grant(ctx, args):
        return await run_action(ctx, args)
    auth = await _ensure_auth(ctx, p, f"to {action} {full} on {p.name}")
    if "auth" not in auth:
        return auth  # a sign-in link for the user (the job waits and resumes) or an error
    d = deploy_config.DEFAULTS.get(p.key, {})
    target = params.get("service") or params.get("app") or params.get("stack") or (create or {}).get("name") or name
    preview = {
        "kind": "deploy", "action": action, "platform": p.name, "service": target, "repo": full,
        "branch": (create or {}).get("branch") or info.get("branch") or "working tree on the computer",
        "directory": cwd, "region": params.get("region") or d.get("region", ""), "cost": d.get("tier", ""),
        "target": params, "command": _summary(p, action, params, create, cmd),
        "credentials": (f"saved {p.name} key, passed as env only" if auth["auth"] == "saved_key"
                        else f"your {p.cli} CLI session on the computer"),
    }
    if chosen:
        preview["why_platform"] = chosen
    token = sign_confirm("tool", {"slug": coding.CODING_SLUG, "tool": "deploy_action", "args": args}, ttl=DEPLOY_CONFIRM_TTL)
    label = "Deploy" if action == "deploy" else "Roll back"
    ctx.pending.append({"needs_confirm": True, "token": token, "kind": "deploy", "title": f"{label} {target} on {p.name}", "preview": preview})
    out = {"status": "awaiting_user_confirmation", "preview": preview}
    if chosen:
        out["note"] = f"No platform was named; picked {p.name} because {chosen}. Say so in one line."
    return out


_AUTH_FAIL = re.compile(r"(not logged in|unauthori[sz]ed|\b401\b|authentication (failed|required)|token (is )?(invalid|expired)|please (log ?in|run .*login))", re.I)
_RENDER_LINE = re.compile(r"^@@RENDER (\{.*\})$", re.M)


def _render_events(output: str) -> dict:
    last: dict = {}
    for m in _RENDER_LINE.finditer(output or ""):
        try:
            last.update(json.loads(m.group(1)))
        except ValueError:
            pass
    return last


async def _probe(ctx, url: str) -> int | None:
    res = await _exec(ctx, f"curl -s -o /dev/null -L --max-time 25 -w '%{{http_code}}' {shlex.quote(url)}", 60)
    code = str(res.get("output") or "").strip()[-3:]
    return int(code) if code.isdigit() else None


async def run_action(ctx, args: dict) -> dict:
    """Runs only after the user approved this exact action, platform, repo, directory and target."""
    from app.agent import deploy_config

    try:
        p = platform(args.get("platform") or "")
        owner, name = coding.split_repo(args.get("repo") or "")
        params = clean_params(args.get("params"))
        cwd = check_cwd(args.get("cwd") or ".")
        action = args.get("action")
        if action not in ACTIONS:
            raise DeployError("bad action")
        create = args.get("create") if isinstance(args.get("create"), dict) else None
        cmd = command(p, action, params, name=deploy_config.slug(name), create=create)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    if _confirmed(ctx, args):
        _grant(ctx, args)
    auth = await _ensure_auth(ctx, p, f"to {action} {owner}/{name} on {p.name}")
    if "auth" not in auth:
        _grant(ctx, args)  # approved already: after sign-in the same deploy runs without a second card
        return auth
    res = await _exec(ctx, run_script(p, name, cwd, cmd), 900, auth.get("env") or None)
    if res.get("error"):
        return res
    output = res.get("output", "")
    ok = res.get("exit_code") == 0
    if not ok and auth["auth"] == "cli_session" and (res.get("exit_code") == 4 or _AUTH_FAIL.search(output[-3000:])):
        from app.agent import cli_login

        _grant(ctx, args)
        login = await cli_login.ensure_login(ctx, p.key, f"the {p.cli} session expired")
        if login.get("status") != "logged_in":
            return {**login, "note": "The saved CLI session no longer works; after sign-in the same approved deploy runs again."}
        res = await _exec(ctx, run_script(p, name, cwd, cmd), 900)
        output, ok = res.get("output", ""), res.get("exit_code") == 0
    ev = _render_events(output) if create or p.key == "render" else {}
    urls = ([ev["url"]] if ev.get("url") else []) + [u for u in urls_in(output) if u != ev.get("url")]
    out = {"platform": p.key, "action": action, "repo": f"{owner}/{name}", "ok": ok, "exit_code": res.get("exit_code"),
           "output": output[-6000:], "urls": urls[:5]}
    if ev.get("service"):
        out["service"] = ev["service"]
    if ev.get("dashboard"):
        out["dashboard"] = ev["dashboard"]
    if ev.get("human"):
        out["human_step"] = {"url": ev["human"], "why": ev.get("error") or ""}
    live = urls[0] if ok and urls else None
    if live and action == "deploy":
        out["http_status"] = await _probe(ctx, live)
        out["live_url"] = live
    verb = "Deployed" if action == "deploy" else "Rolled back"
    await _record(
        ctx, "deploy",
        (f"{verb} {owner}/{name} on {p.name}" if ok else f"{action.title()} on {p.name} failed (exit {res.get('exit_code')})")
        + (f": {live}" if live else ""),
        {"platform": p.key, "action": action, "ok": ok, "url": live, "output": out["output"][-3000:]},
    )
    if not ok:
        out["next"] = (
            "Read the output and logs (deploy_logs), fix the cause in the repo (build/start command, port, config) and call "
            f"deploy again with the same arguments: up to {GRANT_RETRIES} retries reuse this approval. Missing secret env "
            "values or a payment method are human steps: give the dashboard link in one sentence."
            if action == "deploy" else "Read the output; report it in one or two sentences."
        )
    elif live and out.get("http_status") and out["http_status"] >= 500:
        out["next"] = "The deploy is live but the URL returns an error: read deploy_logs and fix it (bounded retries)."
    return out


async def read_action(ctx, action: str, repo: str, platform_key: str, params: dict | None = None, cwd: str = ".") -> dict:
    """deploy_status / deploy_logs: read-only, no card. Signs the CLI in (link handoff) when needed."""
    try:
        p = platform(platform_key)
        owner, name = coding.split_repo(repo) if repo else ("", "")
        params = clean_params(params)
        cwd = check_cwd(cwd)
        cmd = command(p, action, params, name=name)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    conn = connector_for(ctx.connectors, p)
    env = await load_env(ctx, p)
    if conn is not None and any(e not in env for e, _ in p.env):
        return {"status": "use_connector", "connector": getattr(conn, "slug", ""), "next": f"Use the {conn.name} connector's {action} tools."}
    if name:
        co = await _ensure_checkout(ctx, f"{owner}/{name}")
        if _checkout_failed(co):
            return co
    auth = await _ensure_auth(ctx, p, f"to read {action} on {p.name}")
    if "auth" not in auth:
        return auth
    res = await _exec(ctx, run_script(p, name, cwd, cmd), 300, auth.get("env") or None)
    if res.get("error"):
        return res
    return {"platform": p.key, "action": action, "ok": res.get("exit_code") == 0, "exit_code": res.get("exit_code"), "output": res.get("output", "")[-8000:]}


async def generate_config(ctx, repo: str, platform_key: str = "", cwd: str = ".", write: bool = True) -> dict:
    """Detect the stack and write a deploy config (render.yaml, vercel.json, netlify.toml, fly.toml, railway.json,
    Dockerfile) into the working tree. Never overwrites an existing file. No approval: committing / pushing it
    still goes through git_commit + push_branch (approval card)."""
    import base64

    from app.agent import deploy_config

    try:
        owner, name = coding.split_repo(repo)
        cwd = check_cwd(cwd)
        if platform_key:
            platform_key = platform(platform_key).key
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    co = await _ensure_checkout(ctx, f"{owner}/{name}")
    if _checkout_failed(co):
        return co
    targets = await _targets(ctx, name)
    targets = targets if isinstance(targets, list) else []
    info = await _repo_info(ctx, name, cwd)
    if info.get("error"):
        return info
    stack = info.get("stack") or {}
    key, why = deploy_config.choose_platform(platform_key, targets, stack)
    gen = deploy_config.config_files(key, stack, name)
    out: dict[str, Any] = {"repo": f"{owner}/{name}", "platform": key, "why": why, "stack": stack.get("kind"),
                           "files": gen["files"], "notes": gen["notes"], **deploy_config.DEFAULTS.get(key, {})}
    if not gen["files"] or not write:
        return out
    where = f'"$HOME/work/{name}"' + ("" if cwd == "." else f"/{shlex.quote(cwd)}")
    lines = [f"cd {where} || exit 3"]
    for path, content in gen["files"].items():
        b = base64.b64encode(content.encode()).decode()
        qp = shlex.quote(path)
        lines.append(f"if [ -e {qp} ]; then echo 'kept existing {path}'; else printf %s {b} | base64 -d > {qp} && echo 'wrote {path}'; fi")
    res = await _exec(ctx, "\n".join(lines), 60)
    out["written"] = [ln[6:] for ln in str(res.get("output") or "").splitlines() if ln.startswith("wrote ")]
    out["kept"] = [ln[14:] for ln in str(res.get("output") or "").splitlines() if ln.startswith("kept existing ")]
    if out["written"]:
        await _record(ctx, "step", f"Generated {', '.join(out['written'])} for {key}", {"repo": f"{owner}/{name}", "files": out["written"]})
    out["next"] = ("To keep it in the repo: git_commit on an agent/* branch, then push_branch (approval card opens the PR). "
                   "Deploying does not need it committed except on Render, which builds from GitHub.")
    return out


def platforms_public(saved: dict[str, list[str]] | None = None) -> list[dict]:
    """For the Connectors page: optional keys per platform and which are saved (never values)."""
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
    """coding_workspace_status: every platform can deploy (connector, saved key, or the CLI's own sign-in)."""
    routes = {k: route_for(p, connectors, saved) for k, p in PLATFORMS.items()}
    out["deploy"] = {
        "ready": {k: (r.get("connector_name") or f"{PLATFORMS[k].cli} CLI ({r.get('auth', 'cli_login')})") for k, r in routes.items()},
        "keys_optional_at": connect_url("all"),
    }
    out.setdefault("can", []).append(
        "deploy, check status and logs, and roll back on Render, Vercel, Fly.io, Netlify, Railway, Google Cloud, Azure and AWS "
        "with the CLI on the computer: it installs the CLI, signs in through one link for the user if needed, and asks for approval "
        "before each deploy or rollback"
    )
    out["missing"] = [m for m in out.get("missing", []) if not m.startswith("No deploy connector")]
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
