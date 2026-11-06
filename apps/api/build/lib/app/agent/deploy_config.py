"""Pick a platform and generate a deploy config when a repo has none.

``stack_script`` collects facts about the checked-out repo (package.json scripts and framework, Python web
framework, Dockerfile, static site, env var names from .env.example) as JSON; ``stack_from_facts`` turns
them into build/start commands; ``config_files`` renders render.yaml / vercel.json / netlify.toml /
fly.toml / railway.json / Dockerfile for that stack with free-tier, India-near defaults (stated to the user).
"""

from __future__ import annotations

import json
import re
from typing import Any

# Free tier and the region nearest Bengaluru, per platform. Shown on the approval card and to the user.
DEFAULTS: dict[str, dict[str, str]] = {
    "render": {"region": "singapore", "tier": "Render free instance (sleeps when idle)"},
    "vercel": {"region": "bom1", "tier": "Vercel Hobby (free)"},
    "netlify": {"region": "global CDN", "tier": "Netlify Free"},
    "fly": {"region": "bom", "tier": "Fly.io shared-cpu-1x 256MB, auto-stop (pay-as-you-go, needs a card on file)"},
    "railway": {"region": "asia-southeast1", "tier": "Railway trial / Hobby credits"},
    "gcloud": {"region": "asia-south1", "tier": "Cloud Run free tier (needs a billing account)"},
    "azure": {"region": "centralindia", "tier": "Container Apps consumption free grant"},
    "aws": {"region": "ap-south-1", "tier": "AWS free tier"},
}

_SLUG = re.compile(r"[^a-z0-9-]+")


def slug(name: str) -> str:
    s = _SLUG.sub("-", str(name or "app").lower()).strip("-")[:40].strip("-")
    return s or "app"


def stack_script(name: str, cwd: str = ".") -> str:
    """Prints one JSON object with what the repo is (no secrets: only env var NAMES from example files)."""
    where = f'"$HOME/work/{name}"' + ("" if cwd in ("", ".") else f"/{cwd}")
    py = r'''
import json, os, re
def read(p, n=200000):
    try:
        with open(p, encoding="utf-8", errors="replace") as f: return f.read(n)
    except Exception: return ""
out = {"files": sorted(os.listdir("."))[:200]}
pkg = read("package.json")
if pkg:
    try:
        d = json.loads(pkg)
        out["package"] = {"name": d.get("name"), "scripts": d.get("scripts") or {}, "deps": sorted({**(d.get("dependencies") or {}), **(d.get("devDependencies") or {})}.keys())[:300], "engines": d.get("engines") or {}}
    except Exception: out["package"] = {"invalid": True}
for f in ("requirements.txt", "pyproject.toml", "Pipfile"):
    t = read(f)
    if t: out.setdefault("python", {})[f] = t[:4000].lower()
for f in ("Dockerfile", "Procfile"):
    t = read(f)
    if t: out[f.lower()] = t[:2000]
for f in (".env.example", ".env.sample", ".env.template", "env.example"):
    t = read(f)
    if t: out["env_keys"] = sorted({m.group(1) for m in re.finditer(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]{1,60})\s*=", t, re.M)})[:40]; break
out["go"] = os.path.exists("go.mod")
out["index_html"] = os.path.exists("index.html") or os.path.exists("public/index.html")
for f in ("pnpm-lock.yaml", "yarn.lock", "package-lock.json", "bun.lockb", "uv.lock", "poetry.lock"):
    if os.path.exists(f): out.setdefault("locks", []).append(f)
print("@@STACK " + json.dumps(out))
'''
    return f"cd {where} || {{ echo 'repo directory is missing'; exit 3; }}; python3 - <<'PY'\n{py}\nPY"


def parse_stack(output: str) -> dict:
    for line in (output or "").splitlines():
        if line.startswith("@@STACK "):
            try:
                return json.loads(line[8:])
            except ValueError:
                return {}
    return {}


def _pm(facts: dict) -> tuple[str, str, str]:
    """(install, run-prefix, ci install) for the repo's package manager."""
    locks = facts.get("locks") or []
    if "pnpm-lock.yaml" in locks:
        return "pnpm install --frozen-lockfile", "pnpm", "corepack enable && pnpm install --frozen-lockfile"
    if "yarn.lock" in locks:
        return "yarn install --frozen-lockfile", "yarn", "yarn install --frozen-lockfile"
    if "package-lock.json" in locks:
        return "npm ci", "npm run", "npm ci"
    return "npm install", "npm run", "npm install"


def stack_from_facts(facts: dict) -> dict:
    """{kind, runtime, build, start, publish, port, env_keys, static} from stack_script facts."""
    pkg = facts.get("package") or {}
    deps = set(pkg.get("deps") or [])
    scripts = pkg.get("scripts") or {}
    env_keys = list(facts.get("env_keys") or [])
    py = " ".join((facts.get("python") or {}).values())
    out: dict[str, Any] = {"env_keys": env_keys}
    if facts.get("dockerfile"):
        m = re.search(r"^\s*EXPOSE\s+(\d+)", facts["dockerfile"], re.M | re.I)
        out.update(kind="docker", runtime="docker", port=int(m.group(1)) if m else 8080, build="", start="")
        if pkg or py:
            out["also"] = "node" if pkg else "python"
        return out
    if pkg and not pkg.get("invalid"):
        install, run, _ = _pm(facts)
        build = f"{install} && {run} build" if "build" in scripts else install
        if "next" in deps:
            out.update(kind="next", runtime="node", build=build, start=f"{run} start" if "start" in scripts else "npx next start -p $PORT", port=3000)
        elif "vite" in deps or "react-scripts" in deps or "@angular/cli" in deps or "astro" in deps:
            publish = "build" if "react-scripts" in deps else "dist"
            if "start" in scripts and any(d in deps for d in ("express", "fastify", "koa", "@nestjs/core", "hono")):
                out.update(kind="node", runtime="node", build=build, start=f"{run} start", port=3000)
            else:
                out.update(kind="static", runtime="static", build=build, publish=publish, static=True)
        else:
            start = f"{run} start" if "start" in scripts else ("node server.js" if "server.js" in (facts.get("files") or []) else "node index.js")
            out.update(kind="node", runtime="node", build=build, start=start, port=3000)
        return out
    if py:
        req = "pip install -r requirements.txt" if "requirements.txt" in (facts.get("python") or {}) else "pip install ."
        if "fastapi" in py:
            mod = "main:app" if "main.py" in (facts.get("files") or []) else "app.main:app"
            out.update(kind="fastapi", runtime="python", build=req, start=f"uvicorn {mod} --host 0.0.0.0 --port $PORT", port=8000)
        elif "django" in py:
            out.update(kind="django", runtime="python", build=req, start="gunicorn $(basename $(dirname $(find . -maxdepth 2 -name wsgi.py | head -1))).wsgi --bind 0.0.0.0:$PORT", port=8000)
        elif "flask" in py:
            out.update(kind="flask", runtime="python", build=req + " && pip install gunicorn", start="gunicorn app:app --bind 0.0.0.0:$PORT", port=8000)
        else:
            out.update(kind="python", runtime="python", build=req, start="python main.py", port=8000)
        return out
    if facts.get("go"):
        out.update(kind="go", runtime="go", build="go build -o app .", start="./app", port=8080)
        return out
    if facts.get("index_html"):
        out.update(kind="static", runtime="static", build="", publish="." if "index.html" in (facts.get("files") or []) else "public", static=True)
        return out
    out.update(kind="unknown", runtime="", build="", start="")
    return out


def choose_platform(requested: str, targets: list[dict], stack: dict) -> tuple[str, str]:
    """(platform key, reason). The user's choice wins; then an existing config; then a default for the stack."""
    if requested:
        return requested, "you asked for it"
    for t in targets:
        if t.get("platform") not in ("container", ""):
            return t["platform"], f"the repo already has {t.get('config')}"
    kind = stack.get("kind")
    if kind in ("next", "static"):
        return "vercel", f"it is a {'Next.js' if kind == 'next' else 'static'} app and Vercel's free Hobby tier fits that best"
    return "render", "Render has a free tier for web services and builds straight from GitHub"


def render_spec(stack: dict, name: str, repo_url: str, branch: str) -> dict:
    """Body for POST https://api.render.com/v1/services (ownerId is added on the computer)."""
    d = DEFAULTS["render"]
    if stack.get("static"):
        return {"type": "static_site", "name": slug(name), "repo": repo_url, "branch": branch, "autoDeploy": "yes",
                "serviceDetails": {"buildCommand": stack.get("build") or "", "publishPath": stack.get("publish") or "dist"}}
    details: dict[str, Any] = {"runtime": stack.get("runtime") or "node", "plan": "free", "region": d["region"]}
    if stack.get("runtime") == "docker":
        details["envSpecificDetails"] = {"dockerfilePath": "./Dockerfile", "dockerContext": "."}
    else:
        details["envSpecificDetails"] = {"buildCommand": stack.get("build") or "true", "startCommand": stack.get("start") or "true"}
    return {"type": "web_service", "name": slug(name), "repo": repo_url, "branch": branch, "autoDeploy": "yes", "serviceDetails": details}


def _dockerfile(stack: dict) -> str:
    k = stack.get("kind")
    if k == "next":
        return (
            "FROM node:20-alpine AS build\nWORKDIR /app\nCOPY . .\nRUN npm install && npm run build\n\n"
            "FROM node:20-alpine\nWORKDIR /app\nENV NODE_ENV=production PORT=3000\nCOPY --from=build /app ./\n"
            'EXPOSE 3000\nCMD ["npm", "start"]\n'
        )
    if stack.get("runtime") == "node":
        return (
            "FROM node:20-alpine\nWORKDIR /app\nCOPY . .\n"
            f"RUN {stack.get('build') or 'npm install'}\nENV PORT=3000\nEXPOSE 3000\n"
            f'CMD ["sh", "-c", "{stack.get("start") or "npm start"}"]\n'
        )
    if stack.get("runtime") == "python":
        return (
            "FROM python:3.12-slim\nWORKDIR /app\nCOPY . .\n"
            f"RUN {stack.get('build') or 'pip install -r requirements.txt'}\nENV PORT=8000\nEXPOSE 8000\n"
            f'CMD ["sh", "-c", "{stack.get("start")}"]\n'
        )
    if stack.get("runtime") == "go":
        return (
            "FROM golang:1.23-alpine AS build\nWORKDIR /src\nCOPY . .\nRUN go build -o /app .\n\n"
            'FROM alpine:3.20\nCOPY --from=build /app /app\nENV PORT=8080\nEXPOSE 8080\nCMD ["/app"]\n'
        )
    return ""


def config_files(platform_key: str, stack: dict, name: str) -> dict:
    """{files: {path: content}, notes: [...]} for a repo without a deploy config."""
    app = slug(name)
    d = DEFAULTS.get(platform_key, {})
    notes = [f"Defaults: {d.get('tier', 'free tier')}, region {d.get('region', 'default')}."] if d else []
    env_keys = stack.get("env_keys") or []
    files: dict[str, str] = {}
    k = platform_key
    if stack.get("kind") == "unknown":
        return {"files": {}, "notes": ["Could not tell what this repo runs (no package.json, Python project, Dockerfile, go.mod or index.html)."]}
    if k == "render":
        if stack.get("static"):
            body = (f"services:\n  - type: web\n    name: {app}\n    runtime: static\n"
                    f"    buildCommand: {json.dumps(stack.get('build') or '')}\n    staticPublishPath: {stack.get('publish') or 'dist'}\n")
        elif stack.get("runtime") == "docker":
            body = f"services:\n  - type: web\n    name: {app}\n    runtime: docker\n    plan: free\n    region: {d['region']}\n    dockerfilePath: ./Dockerfile\n"
        else:
            body = (f"services:\n  - type: web\n    name: {app}\n    runtime: {stack.get('runtime')}\n    plan: free\n    region: {d['region']}\n"
                    f"    buildCommand: {json.dumps(stack.get('build') or '')}\n    startCommand: {json.dumps(stack.get('start') or '')}\n")
        if env_keys and not stack.get("static"):
            body += "    envVars:\n" + "".join(f"      - key: {e}\n        sync: false\n" for e in env_keys)
        files["render.yaml"] = body
    elif k == "vercel":
        cfg: dict[str, Any] = {"$schema": "https://openapi.vercel.sh/vercel.json"}
        if not stack.get("static"):
            cfg["regions"] = [d["region"]]
        if stack.get("static") and stack.get("publish"):
            cfg["outputDirectory"] = stack["publish"]
        files["vercel.json"] = json.dumps(cfg, indent=2) + "\n"
    elif k == "netlify":
        if stack.get("kind") == "next":
            files["netlify.toml"] = '[build]\n  command = "npm run build"\n  publish = ".next"\n\n[[plugins]]\n  package = "@netlify/plugin-nextjs"\n'
        else:
            files["netlify.toml"] = f'[build]\n  command = {json.dumps(stack.get("build") or "")}\n  publish = {json.dumps(stack.get("publish") or "dist")}\n'
    elif k == "fly":
        port = stack.get("port") or 8080
        files["fly.toml"] = (
            f'app = "{app}"\nprimary_region = "{d["region"]}"\n\n[http_service]\n  internal_port = {port}\n  force_https = true\n'
            '  auto_stop_machines = "stop"\n  auto_start_machines = true\n  min_machines_running = 0\n\n'
            '[[vm]]\n  size = "shared-cpu-1x"\n  memory = "256mb"\n'
        )
        if stack.get("runtime") != "docker" and _dockerfile(stack):
            files["Dockerfile"] = _dockerfile(stack)
    elif k == "railway":
        cfg = {"$schema": "https://railway.com/railway.schema.json", "build": {"builder": "RAILPACK"}, "deploy": {}}
        if stack.get("start"):
            cfg["deploy"]["startCommand"] = stack["start"]
        if stack.get("build") and stack.get("runtime") != "docker":
            cfg["build"]["buildCommand"] = stack["build"]
        files["railway.json"] = json.dumps(cfg, indent=2) + "\n"
    elif k in ("gcloud", "azure"):
        if stack.get("runtime") != "docker" and _dockerfile(stack):
            files["Dockerfile"] = _dockerfile(stack)
        notes.append("Builds the container from source on deploy (Cloud Run --source / az containerapp up --source).")
    elif k == "aws":
        return {"files": {}, "notes": ["AWS needs infrastructure-as-code (serverless.yml, SAM or CDK) that I won't guess; deploy to Render or Vercel instead, or add one."]}
    if env_keys:
        notes.append("Set these on the platform before it works: " + ", ".join(env_keys) + " (secret values are never put in the repo).")
    return {"files": files, "notes": notes}
