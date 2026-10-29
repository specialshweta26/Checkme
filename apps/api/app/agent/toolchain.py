"""Self-healing toolchain on the cloud computer (E2B desktop, or Daytona).

Every agent's ``shell`` runs on the cloud computer. The default E2B ``desktop`` template has no Node.js, so
commands like ``npm install -g vercel`` used to fail and agents told the user to install things on their own
laptop. This module fixes that generically:

* ``ENV_PRELUDE`` puts user-space tool dirs on PATH (``~/.local/bin``, ``~/.npm-global/bin``,
  ``~/.checkme/node/bin``) and points npm's global prefix at ``~/.npm-global``, so ``npm install -g`` never
  needs sudo. It also writes the same lines once into ``~/.bashrc`` / ``~/.profile`` for terminals on the desktop.
* ``install_script(tools)`` builds one idempotent, non-interactive bash script that installs whatever is
  missing: apt via ``sudo -n`` when available (never prompts), else user-space installs (Node from the official
  nodejs.org tarball, gh from its release tarball, CLIs through npm / pip / uv). Already-present tools cost a
  ``command -v``; installed tools are recorded under ``~/.checkme/tools`` (per sandbox).
* ``missing_tool(output)`` reads a ``command not found`` (exit 127) and maps the command to a known tool so the
  shell can install it and retry once.

Nothing here prints or reads secrets; scripts only download public installers.
"""

from __future__ import annotations

import re
import shlex
import time
from typing import Any

TOOLS_DIR = "$HOME/.checkme/tools"
NPM_PREFIX = "$HOME/.npm-global"
NODE_DIR = "$HOME/.checkme/node"

ENV_PRELUDE = (
    'export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.checkme/node/bin:$HOME/.cargo/bin:$PATH"; '
    '[ -s "$HOME/.nvm/nvm.sh" ] || export NPM_CONFIG_PREFIX="$HOME/.npm-global"; export PIP_DISABLE_PIP_VERSION_CHECK=1; '
    "export DEBIAN_FRONTEND=noninteractive; export CI=${CI:-1}; "
)

# Lines appended once to shell rc files so terminals opened on the desktop see the same tools.
_RC_LINE = 'export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.checkme/node/bin:$PATH"; export NPM_CONFIG_PREFIX="$HOME/.npm-global"'

# tool -> (commands it provides, dependencies, bash install body). Bodies run inside install_script, where
# apt_i (sudo -n apt or root apt), npm_g (npm -g into ~/.npm-global) and pip_u (user pip / uv) are defined.
TOOLS: dict[str, dict[str, Any]] = {
    "curl": {"cmds": ("curl",), "deps": (), "install": "apt_i curl ca-certificates"},
    "git": {"cmds": ("git",), "deps": (), "install": "apt_i git ca-certificates"},
    "unzip": {"cmds": ("unzip",), "deps": (), "install": "apt_i unzip"},
    "jq": {"cmds": ("jq",), "deps": (), "install": "apt_i jq"},
    "rg": {"cmds": ("rg",), "deps": (), "install": "apt_i ripgrep"},
    "node": {
        # npm is the check: some images ship node without npm, and the nodejs.org tarball brings both.
        "cmds": ("npm", "node", "npx", "corepack"),
        "deps": ("curl",),
        "install": (
            'A=$(uname -m); case "$A" in x86_64) A=x64;; aarch64|arm64) A=arm64;; esac; '
            'F=$(curl -fsSL "https://nodejs.org/dist/latest-v${NODE_MAJOR}.x/SHASUMS256.txt" | '
            "grep -o \"node-v[0-9.]*-linux-$A.tar.gz\" | head -1); "
            '[ -n "$F" ] && rm -rf "$HOME/.checkme/node" && mkdir -p "$HOME/.checkme/node" && '
            'curl -fsSL "https://nodejs.org/dist/latest-v${NODE_MAJOR}.x/$F" | tar -xz -C "$HOME/.checkme/node" --strip-components=1'
        ),
    },
    "pnpm": {"cmds": ("pnpm", "pnpx"), "deps": ("node",), "install": "npm_g pnpm"},
    "yarn": {"cmds": ("yarn",), "deps": ("node",), "install": "npm_g yarn"},
    "vercel": {"cmds": ("vercel", "vc"), "deps": ("node",), "install": "npm_g vercel"},
    "netlify": {"cmds": ("netlify", "ntl"), "deps": ("node",), "install": "npm_g netlify-cli"},
    "wrangler": {"cmds": ("wrangler",), "deps": ("node",), "install": "npm_g wrangler"},
    "firebase": {"cmds": ("firebase",), "deps": ("node",), "install": "npm_g firebase-tools"},
    "tsc": {"cmds": ("tsc",), "deps": ("node",), "install": "npm_g typescript"},
    "python3": {
        "cmds": ("python3", "python"),
        "deps": (),
        "install": "apt_i python3 python3-pip python3-venv",
    },
    "pip": {
        "cmds": ("pip", "pip3"),
        "deps": ("python3",),
        "install": (
            "apt_i python3-pip || { python3 -m ensurepip --user >/dev/null 2>&1 || "
            "curl -fsSL https://bootstrap.pypa.io/get-pip.py | python3 - --user --break-system-packages; }; "
            'ln -sf "$(command -v pip3 2>/dev/null || echo "$HOME/.local/bin/pip3")" "$HOME/.local/bin/pip" 2>/dev/null; true'
        ),
    },
    "uv": {
        "cmds": ("uv", "uvx"),
        "deps": ("curl",),
        "install": 'curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh',
    },
    "gh": {
        "cmds": ("gh",),
        "deps": ("curl",),
        "install": (
            'V=2.62.0; A=$(uname -m); case "$A" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; esac; '
            'curl -fsSL "https://github.com/cli/cli/releases/download/v$V/gh_${V}_linux_$A.tar.gz" -o /tmp/gh.tgz && '
            'tar -xzf /tmp/gh.tgz -C /tmp && cp "/tmp/gh_${V}_linux_$A/bin/gh" "$HOME/.local/bin/gh"'
        ),
    },
    "render": {
        "cmds": ("render",),
        "deps": ("curl", "unzip"),
        "install": (
            "curl -fsSL https://raw.githubusercontent.com/render-oss/cli/refs/heads/main/bin/install.sh | "
            'env INSTALL_DIR="$HOME/.local/bin" sh || { '
            'A=$(uname -m); case "$A" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; esac; '
            'U=$(curl -fsSL https://api.github.com/repos/render-oss/cli/releases/latest | grep -o "https://[^\\"]*linux_$A.zip" | head -1); '
            '[ -n "$U" ] && curl -fsSL "$U" -o /tmp/render.zip && unzip -o -q /tmp/render.zip -d /tmp/render-cli && '
            'cp "$(ls /tmp/render-cli/cli_* /tmp/render-cli/render 2>/dev/null | head -1)" "$HOME/.local/bin/render" && '
            'chmod +x "$HOME/.local/bin/render"; }'
        ),
    },
}

# Groups the agent can ask for by one word.
GROUPS: dict[str, tuple[str, ...]] = {
    "web": ("node", "pnpm", "yarn"),
    "python": ("python3", "pip", "uv"),
    "deploy": ("node", "vercel", "render", "gh"),
    "dev": ("git", "curl", "jq", "rg", "node", "pnpm", "yarn", "python3", "pip", "uv", "gh"),
}

_CMD_TO_TOOL: dict[str, str] = {cmd: name for name, spec in TOOLS.items() for cmd in spec["cmds"]}
_NOT_FOUND = re.compile(
    r"(?:^|[\s:])([A-Za-z0-9][A-Za-z0-9._+-]{0,40}): (?:command not found|not found|No such file or directory)\b",
    re.M,
)
_NPM_PKG = {"vercel": "vercel", "netlify-cli": "netlify", "pnpm": "pnpm", "yarn": "yarn"}

# Per (user, tool) memory of a recent successful install in this process, so a burst of commands does not run
# the installer again. The sandbox itself is the source of truth (command -v), so a stale entry only costs one
# extra exec.
_RECENT: dict[tuple[str, str], float] = {}
_RECENT_TTL_S = 1800.0


def known(name: str) -> bool:
    return resolve(name) is not None


def resolve(name: str) -> str | None:
    """Tool name for a command or tool name (npm -> node, pip3 -> pip, netlify-cli -> netlify)."""
    key = (name or "").strip().lower()
    if key in TOOLS:
        return key
    if key in _CMD_TO_TOOL:
        return _CMD_TO_TOOL[key]
    if key in _NPM_PKG:
        return _NPM_PKG[key]
    return None


def expand(names: list[str] | tuple[str, ...] | None) -> tuple[list[str], list[str]]:
    """(tools in install order with dependencies first, unknown names)."""
    order: list[str] = []
    unknown: list[str] = []

    def add(tool: str) -> None:
        for dep in TOOLS[tool]["deps"]:
            add(dep)
        if tool not in order:
            order.append(tool)

    for raw in names or []:
        word = str(raw or "").strip().lower()
        if not word:
            continue
        if word in GROUPS:
            for t in GROUPS[word]:
                add(t)
            continue
        tool = resolve(word)
        if tool is None:
            unknown.append(word)
        else:
            add(tool)
    return order, unknown


def missing_tool(output: str, command: str = "") -> str | None:
    """The known tool behind a 'command not found' in this output, else None."""
    for m in _NOT_FOUND.finditer(output or ""):
        tool = resolve(m.group(1))
        if tool:
            return tool
    # `sh: 1: vercel: not found` is matched above; a bare exit 127 with no message: try the first word.
    first = (command or "").strip().split(" ", 1)[0] if command else ""
    if first and not (output or "").strip():
        return resolve(first)
    return None


def install_script(tools: list[str], *, force: bool = False, node_major: int = 22) -> str:
    """One idempotent, non-interactive bash script. Prints present=, installed=, failed= lines."""
    order, _ = expand(tools)
    q = shlex.quote
    parts = [
        ENV_PRELUDE,
        f'mkdir -p "{TOOLS_DIR}" "$HOME/.local/bin" "{NPM_PREFIX}/bin" "$HOME/.checkme"; ',
        f"NODE_MAJOR={int(node_major) if int(node_major or 0) > 0 else 22}; ",
        # rc files once, so desktop terminals see the same PATH
        f'for rc in "$HOME/.bashrc" "$HOME/.profile"; do grep -qs "npm-global" "$rc" || echo {q(_RC_LINE)} >> "$rc"; done; ',
        '[ -s "$HOME/.nvm/nvm.sh" ] || { command -v npm >/dev/null 2>&1 && npm config set prefix "$HOME/.npm-global" >/dev/null 2>&1; }; ',
        'INST=""; FAIL=""; PRES=""; ',
        'SUDO=""; if [ "$(id -u)" != 0 ]; then sudo -n true 2>/dev/null && SUDO="sudo -n"; fi; ',
        'APT_OK=0; if command -v apt-get >/dev/null 2>&1 && { [ "$(id -u)" = 0 ] || [ -n "$SUDO" ]; }; then APT_OK=1; fi; ',
        'APT_DONE=0; apt_i() { [ "$APT_OK" = 1 ] || return 1; '
        '[ "$APT_DONE" = 1 ] || { $SUDO apt-get update -qq >/dev/null 2>&1; APT_DONE=1; }; '
        '$SUDO env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "$@" >/dev/null 2>&1; }; ',
        'npm_g() { command -v npm >/dev/null 2>&1 || return 1; if [ -s "$HOME/.nvm/nvm.sh" ]; then npm install -g --no-fund --no-audit --loglevel=error "$@"; '
        'else npm install -g --no-fund --no-audit --loglevel=error --prefix "$HOME/.npm-global" "$@"; fi; }; ',
        "ensure() { t=\"$1\"; c=\"$2\"; shift 2; "
        f'if [ {"1" if force else "0"} = 0 ] && command -v "$c" >/dev/null 2>&1; then PRES="$PRES $t"; return 0; fi; '
        f'if ( "$@" ) >/tmp/checkme-install-$t.log 2>&1 && hash -r && command -v "$c" >/dev/null 2>&1; then '
        f'INST="$INST $t"; date -u +%FT%TZ > "{TOOLS_DIR}/$t"; else FAIL="$FAIL $t"; '
        'echo "log_$t=$(tail -c 300 /tmp/checkme-install-$t.log | tr \'\\n\' \' \')"; fi; }; ',
    ]
    for tool in order:
        spec = TOOLS[tool]
        fn = "inst_" + tool.replace("-", "_")
        parts.append(f"{fn}() {{ {spec['install']}; }}; ")
        parts.append(f"ensure {q(tool)} {q(spec['cmds'][0])} {fn}; ")
    parts.append('echo "present=${PRES# }"; echo "installed=${INST# }"; echo "failed=${FAIL# }"; ')
    parts.append('echo "node=$(node --version 2>/dev/null)"; echo "sudo=$([ -n "$SUDO" ] && echo yes || echo no)"; ')
    parts.append('[ -z "$FAIL" ]')
    return "".join(parts)


def parse(output: str) -> dict:
    info: dict[str, str] = {}
    logs: dict[str, str] = {}
    for line in (output or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k.startswith("log_"):
            logs[k[4:]] = v.strip()[:300]
        else:
            info[k] = v.strip()
    words = lambda k: [w for w in (info.get(k) or "").split() if w]  # noqa: E731
    return {
        "present": words("present"),
        "installed": words("installed"),
        "failed": words("failed"),
        "node": info.get("node", ""),
        "sudo": info.get("sudo", "") == "yes",
        "logs": logs,
    }


def wrap(command: str, tag: str = "") -> str:
    """Run a user command with the toolchain PATH, in bash (Daytona snapshots default to zsh).

    ``tag`` becomes bash's $0 (``checkme-run-<tag>``), so a stopped turn can find and kill this command's process
    tree on the computer (``kill_script``) without touching anything else.
    """
    out = "bash -c " + shlex.quote(ENV_PRELUDE + command)
    return f"{out} {run_name(tag)}" if tag else out


def run_name(tag: str) -> str:
    return "checkme-run-" + re.sub(r"[^A-Za-z0-9]", "", tag)[:24]


def kill_script(tag: str) -> str:
    """Kill the process tree of a command started with ``wrap(..., tag)``. Never touches the sandbox itself."""
    name = run_name(tag)
    pattern = "[" + name[0] + "]" + name[1:]  # so pgrep does not match this script's own command line
    return (
        "kt(){ for c in $(pgrep -P \"$1\" 2>/dev/null); do kt \"$c\"; done; kill -TERM \"$1\" 2>/dev/null; }; "
        f"for p in $(pgrep -f '{pattern}' 2>/dev/null); do kt \"$p\"; done; true"
    )


def _user_key(ctx) -> str:
    return str(getattr(getattr(ctx, "user", None), "id", "") or "")


def recently_installed(ctx, tool: str) -> bool:
    t = _RECENT.get((_user_key(ctx), tool))
    return bool(t and time.monotonic() - t < _RECENT_TTL_S)


def forget(ctx, tool: str | None = None) -> None:
    key = _user_key(ctx)
    for k in list(_RECENT):
        if k[0] == key and (tool is None or k[1] == tool):
            _RECENT.pop(k, None)


def _settings():
    from app.config import get_settings

    return get_settings()


def _stdout(result: Any) -> tuple[Any, str]:
    if not isinstance(result, dict):
        return None, str(result or "")
    inner = result.get("data") if isinstance(result.get("data"), dict) else result
    out = inner.get("stdout") or inner.get("output") or inner.get("result") or ""
    return inner.get("exit_code"), str(out)


async def ensure_tools(ctx, tools: list[str], *, force: bool = False, timeout: int | None = None) -> dict:
    """Install whatever of ``tools`` is missing on the cloud computer. Idempotent and cheap when present."""
    from app.agent import computer_harness as computer

    order, unknown = expand(tools)
    if not order:
        return {
            "ok": not unknown,
            "error": f"Unknown tool(s): {', '.join(unknown)}. Known: {', '.join(sorted([*TOOLS, *GROUPS]))}. "
            "Install anything else yourself with shell (apt via sudo -n, pip --user, npm -g, or a release download).",
            "unknown": unknown,
        }
    s = _settings()
    limit = int(timeout or getattr(s, "computer_install_timeout_s", 600) or 600)
    script = install_script(order, force=force, node_major=int(getattr(s, "computer_node_major", 22) or 22))
    raw = await computer.run_computer(ctx, "exec", {"command": "bash -c " + shlex.quote(script), "timeout": max(60, min(limit, 900))})
    if isinstance(raw, dict) and raw.get("error"):
        return {"ok": False, "error": str(raw["error"])[:600], "tools": order}
    code, out = _stdout(raw)
    res = parse(out)
    for t in res["installed"] + res["present"]:
        _RECENT[(_user_key(ctx), t)] = time.monotonic()
    for t in res["failed"]:
        _RECENT.pop((_user_key(ctx), t), None)
    res.update({"ok": not res["failed"] and not unknown, "exit_code": code, "tools": order})
    if unknown:
        res["unknown"] = unknown
    if res["failed"]:
        res["note"] = (
            "Some tools could not be installed on the cloud computer: " + ", ".join(res["failed"]) + ". Read logs, then "
            "try another way with shell (a different package, a release tarball into ~/.local/bin, pip --user, npm -g). "
            "Do not ask the user to install anything on their own machine."
        )
    return res
