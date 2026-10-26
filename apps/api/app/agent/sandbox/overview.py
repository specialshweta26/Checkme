"""Repo overview, run inside the sandbox clone (python3 stdlib only).

Prints one JSON object: languages, frameworks, entry points, and the lint / type-check /
unit / integration / build commands, taken from CI workflows first and manifests second.
Imported by tests too (analyze(path)).
"""

import json
import os
import re
import subprocess
import sys

SKIP_DIRS = {".git", "node_modules", "dist", "build", ".next", "venv", ".venv", "__pycache__", "target", "vendor", ".turbo"}
EXT_LANG = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript",
    ".mjs": "JavaScript", ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin", ".rb": "Ruby",
    ".php": "PHP", ".cs": "C#", ".cpp": "C++", ".c": "C", ".swift": "Swift", ".scala": "Scala",
    ".sh": "Shell", ".sql": "SQL", ".vue": "Vue", ".svelte": "Svelte", ".dart": "Dart",
}
JS_FRAMEWORKS = {
    "next": "Next.js", "react": "React", "vue": "Vue", "svelte": "Svelte", "@sveltejs/kit": "SvelteKit",
    "nuxt": "Nuxt", "express": "Express", "fastify": "Fastify", "@nestjs/core": "NestJS", "astro": "Astro",
    "vite": "Vite", "remix": "Remix", "@remix-run/react": "Remix", "electron": "Electron", "hono": "Hono",
    "jest": "Jest", "vitest": "Vitest", "@playwright/test": "Playwright", "cypress": "Cypress",
    "typescript": "TypeScript", "eslint": "ESLint", "prisma": "Prisma",
}
PY_FRAMEWORKS = {
    "fastapi": "FastAPI", "django": "Django", "flask": "Flask", "sqlalchemy": "SQLAlchemy", "pytest": "pytest",
    "celery": "Celery", "pydantic": "Pydantic", "starlette": "Starlette", "langchain": "LangChain", "ruff": "Ruff", "mypy": "mypy",
}
KIND_ORDER = ("setup", "lint", "typecheck", "unit", "integration", "build")


def classify(cmd: str) -> str:
    c = cmd.lower()
    if re.search(r"\b(npm (ci|install)|pnpm install|yarn( install)?$|yarn install|pip install|uv sync|poetry install|bundle install|go mod download|apt-get|corepack|npx playwright install)\b", c):
        return "setup"
    if re.search(r"(e2e|integration|playwright|cypress)", c):
        return "integration"
    if re.search(r"\b(tsc|mypy|pyright|typecheck|type-check|check-types|vue-tsc)\b", c):
        return "typecheck"
    if re.search(r"\b(lint|eslint|ruff|flake8|pylint|black --check|prettier --check|golangci-lint|clippy|gofmt|rubocop)\b", c):
        return "lint"
    if re.search(r"\b(test|pytest|jest|vitest|mocha|go test|cargo test|rspec|phpunit|unittest)\b", c):
        return "unit"
    if re.search(r"\b(build|compile|cargo build|go build|next build|vite build|webpack|tsup|bundle)\b", c):
        return "build"
    return ""


def _walk(root, max_depth=4):
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".") or d == ".github"]
        if depth >= max_depth:
            dirnames[:] = []
        yield rel, filenames


def _read(path, limit=200_000):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def _files(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True, timeout=30).stdout
        files = [f for f in out.splitlines() if f]
        if files:
            return files
    except Exception:  # noqa: BLE001
        pass
    found = []
    for rel, names in _walk(root, 8):
        for n in names:
            found.append(os.path.normpath(os.path.join(rel, n)))
    return found


def workflow_commands(text: str) -> list[dict]:
    """`run:` steps from a GitHub Actions workflow (line-based: no YAML dependency)."""
    out = []
    lines = text.splitlines()
    i = 0
    workdir_default = ""
    while i < len(lines):
        line = lines[i]
        m_wd = re.match(r"^(\s*)working-directory:\s*(.+?)\s*$", line)
        m = re.match(r"^(\s*)(?:-\s+)?run:\s*(.*)$", line)
        if m_wd and not m:
            # applies to the step it sits in; also used as a job default
            wd = m_wd.group(2).strip("'\"")
            if out and out[-1].get("_indent") is not None and len(m_wd.group(1)) <= out[-1]["_indent"] + 2 and not out[-1]["cwd"]:
                out[-1]["cwd"] = wd
            else:
                workdir_default = wd
        if m:
            indent = len(m.group(1))
            val = m.group(2).strip()
            body = []
            if val in ("|", ">", "|-", ">-", "|+"):
                j = i + 1
                while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
                    if lines[j].strip():
                        body.append(lines[j].strip())
                    j += 1
                i = j - 1
            elif val:
                body = [val.strip("'\"")]
            for cmd in body:
                if cmd.startswith("#"):
                    continue
                out.append({"command": cmd, "cwd": "", "_indent": indent, "_default": workdir_default})
        i += 1
    for o in out:
        o["cwd"] = o["cwd"] or o.pop("_default", "")
        o.pop("_default", None)
        o.pop("_indent", None)
    return out


def _pm(dirpath, root):
    full = os.path.join(root, dirpath)
    if os.path.exists(os.path.join(full, "pnpm-lock.yaml")) or os.path.exists(os.path.join(root, "pnpm-lock.yaml")):
        return "pnpm"
    if os.path.exists(os.path.join(full, "yarn.lock")) or os.path.exists(os.path.join(root, "yarn.lock")):
        return "yarn"
    return "npm"


def needs(manifests: list[str]) -> list[str]:
    """Toolchains beyond node/python that this repo needs (installed on demand)."""
    base = [os.path.basename(m) for m in manifests]
    out = []
    if "go.mod" in base:
        out.append("go")
    if "Cargo.toml" in base:
        out.append("rust")
    if any(b in ("pom.xml", "build.gradle", "build.gradle.kts") for b in base):
        out.append("java")
    return out


def analyze(root: str) -> dict:
    root = os.path.abspath(root)
    files = _files(root)
    langs: dict[str, int] = {}
    for f in files:
        lang = EXT_LANG.get(os.path.splitext(f)[1].lower())
        if lang:
            langs[lang] = langs.get(lang, 0) + 1
    frameworks: set[str] = set()
    entry: list[str] = []
    manifests: list[str] = []
    commands: list[dict] = []
    seen = set()

    def add(kind, cmd, cwd, source):
        key = (cmd, cwd)
        if not kind or key in seen:
            return
        seen.add(key)
        commands.append({"kind": kind, "command": cmd, "cwd": cwd or ".", "source": source})

    # 1) CI workflows first
    wf_dir = os.path.join(root, ".github", "workflows")
    ci_files = []
    if os.path.isdir(wf_dir):
        for n in sorted(os.listdir(wf_dir)):
            if n.endswith((".yml", ".yaml")):
                ci_files.append(f".github/workflows/{n}")
                for step in workflow_commands(_read(os.path.join(wf_dir, n))):
                    add(classify(step["command"]), step["command"], step["cwd"], f"ci:{n}")
    for other in (".gitlab-ci.yml", ".circleci/config.yml", "azure-pipelines.yml"):
        if os.path.exists(os.path.join(root, other)):
            ci_files.append(other)

    # 2) manifests
    for rel, names in _walk(root, 4):
        d = "" if rel == "." else rel
        if "package.json" in names:
            manifests.append(os.path.join(d, "package.json"))
            try:
                pkg = json.loads(_read(os.path.join(root, d, "package.json")) or "{}")
            except ValueError:
                pkg = {}
            deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
            for dep, label in JS_FRAMEWORKS.items():
                if dep in deps:
                    frameworks.add(label)
            pm = _pm(d, root)
            lock = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock"}[pm]
            frozen = os.path.exists(os.path.join(root, d, lock)) or os.path.exists(os.path.join(root, lock))
            add("setup", {"npm": "npm ci" if frozen else "npm install", "pnpm": "pnpm install" + (" --frozen-lockfile" if frozen else ""),
                          "yarn": "yarn install" + (" --frozen-lockfile" if frozen else "")}[pm], d, "package.json")
            scripts = pkg.get("scripts") or {}
            for name in scripts:
                kind = "" if name in ("start", "dev", "prepare", "postinstall") else classify(name.replace(":", " ")) or classify(str(scripts[name]))
                if kind and kind != "setup":
                    add(kind, f"{pm} run {name}", d, "package.json")
            if pkg.get("main"):
                entry.append(os.path.join(d, str(pkg["main"])))
            if isinstance(pkg.get("bin"), dict):
                entry += [os.path.join(d, str(v)) for v in pkg["bin"].values()]
            if "start" in scripts:
                entry.append(f"{os.path.join(d, 'package.json') if d else 'package.json'} start: {scripts['start']}")
        if "pyproject.toml" in names or "requirements.txt" in names or "setup.py" in names:
            text = _read(os.path.join(root, d, "pyproject.toml")) + _read(os.path.join(root, d, "requirements.txt"))
            manifests += [os.path.join(d, n) for n in ("pyproject.toml", "requirements.txt", "setup.py") if n in names]
            low = text.lower()
            for dep, label in PY_FRAMEWORKS.items():
                if re.search(rf"(^|[\s\"'\[,]){re.escape(dep)}([\s\"'\]=<>~!,\[]|$)", low, re.M):
                    frameworks.add(label)
            uv = os.path.exists(os.path.join(root, d, "uv.lock"))
            runner = "uv run " if uv else "python3 -m "
            if uv:
                add("setup", "uv sync", d, "uv.lock")
            elif "requirements.txt" in names:
                add("setup", "python3 -m pip install -q -r requirements.txt", d, "requirements.txt")
            elif "pyproject.toml" in names:
                add("setup", "python3 -m pip install -q -e '.[dev]' || python3 -m pip install -q -e .", d, "pyproject.toml")
            if "[tool.ruff" in low or "ruff" in low:
                add("lint", f"{runner}ruff check .", d, "pyproject.toml")
            if "[tool.mypy" in low:
                add("typecheck", f"{runner}mypy .", d, "pyproject.toml")
            if "pytest" in low or os.path.isdir(os.path.join(root, d, "tests")):
                add("unit", f"{runner}pytest -q", d, "pyproject.toml")
            for cand in ("main.py", "app.py", "manage.py", "wsgi.py", "asgi.py"):
                if cand in names:
                    entry.append(os.path.join(d, cand))
        if "go.mod" in names:
            manifests.append(os.path.join(d, "go.mod"))
            frameworks.add("Go modules")
            add("lint", "go vet ./...", d, "go.mod")
            add("unit", "go test ./...", d, "go.mod")
            add("build", "go build ./...", d, "go.mod")
        if "Cargo.toml" in names:
            manifests.append(os.path.join(d, "Cargo.toml"))
            add("lint", "cargo clippy --all-targets", d, "Cargo.toml")
            add("unit", "cargo test", d, "Cargo.toml")
            add("build", "cargo build", d, "Cargo.toml")
            if "src" in os.listdir(os.path.join(root, d) or "."):
                entry.append(os.path.join(d, "src/main.rs"))
        if "pom.xml" in names or "build.gradle" in names or "build.gradle.kts" in names:
            manifests += [os.path.join(d, n) for n in ("pom.xml", "build.gradle", "build.gradle.kts") if n in names]
            if "pom.xml" in names:
                add("unit", "mvn -q test", d, "pom.xml")
            else:
                add("unit", "./gradlew test", d, "build.gradle")
        if "Makefile" in names:
            manifests.append(os.path.join(d, "Makefile"))
            targets = re.findall(r"^([A-Za-z][\w.-]*):(?!=)", _read(os.path.join(root, d, "Makefile")), re.M)
            for t in targets:
                kind = classify(t)
                if kind and kind != "setup":
                    add(kind, f"make {t}", d, "Makefile")
        if "Dockerfile" in names:
            entry.append(os.path.join(d, "Dockerfile"))
    for f in files:
        if re.match(r"^cmd/[^/]+/main\.go$", f):
            entry.append(f)
    from_ci = {(c["kind"], c["cwd"]) for c in commands if c["source"].startswith("ci:")}
    commands[:] = [c for c in commands if c["source"].startswith("ci:") or (c["kind"], c["cwd"]) not in from_ci]
    commands.sort(key=lambda c: (KIND_ORDER.index(c["kind"]), 0 if c["source"].startswith("ci:") else 1))
    top = sorted(langs.items(), key=lambda kv: -kv[1])
    readme = next((f for f in files if f.lower() in ("readme.md", "readme.rst", "readme")), "")
    return {
        "root": root,
        "files": len(files),
        "languages": [{"language": k, "files": v} for k, v in top[:8]],
        "frameworks": sorted(frameworks),
        "entry_points": list(dict.fromkeys(entry))[:15],
        "manifests": manifests[:30],
        "ci": ci_files,
        "readme": readme,
        "commands": commands[:40],
        "summary": {k: [c["command"] + (f"  (in {c['cwd']})" if c["cwd"] not in ("", ".") else "") for c in commands if c["kind"] == k][:6] for k in KIND_ORDER},
        "needs": needs(manifests),
    }


if __name__ == "__main__":
    print(json.dumps(analyze(sys.argv[1] if len(sys.argv) > 1 else "."), indent=None))
