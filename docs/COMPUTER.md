# The cloud computer: shell, toolchain and template

Every agent has a cloud computer: an E2B Desktop sandbox per user (or Daytona when configured, see LLD §7.2).
Agents run commands and install tools there. The user's own laptop is only used when they connect it as a local computer.

## Routing

| Tool | Who has it | Where it runs |
| --- | --- | --- |
| `shell` | every agent | cloud computer (`computer_harness.cloud_exec`) |
| `sandbox_shell` | coding agents | cloud computer (same path) |
| `ensure_toolchain(tools)` | every agent | cloud computer |
| `user_computer_shell` | only when a local computer is connected | the user's machine (approval required) |

- When no local computer is connected, `user_computer_shell` is not offered. If an older prompt or skill still calls it, the
  command runs on the cloud computer (`LOCAL_SHELL_FALLBACK_TO_CLOUD=1`) and the result says `ran_on: cloud_computer`.
- The system prompt (`deep.shell_routing_hint`) says: install CLIs and dependencies on the cloud computer, never tell the
  user to install anything locally. Non-coding agents keep shell, files and the toolchain. Repo, PR, merge and deploy
  tools stay with coding agents, so non-coding agents hand that work to a coding agent (`ask_teammate`, or `delegate`
  inside a background job). Approval cards are unchanged.
- Turns like "install vercel cli", "install all requirements", "npm i", "pip install -r requirements.txt", and "run the
  tests" are classified as `computer` turns (`fast_path._SHELL`), so they skip the quick-web hint.

## Self-healing toolchain (`app/agent/toolchain.py`)

- Every cloud command runs as `bash -c '<PATH prelude>; <command>' checkme-run-<tag>`. The prelude puts `~/.local/bin`,
  `~/.npm-global/bin`, `~/.checkme/node/bin` and `~/.cargo/bin` on the PATH. It sets `NPM_CONFIG_PREFIX=~/.npm-global`
  (unless nvm is installed), `DEBIAN_FRONTEND=noninteractive` and `CI=1`, so nothing waits on a prompt.
- **Exit 127.** If a command exits 127 and the missing command is a known tool, `cloud_exec` installs it with
  `ensure_tools` and re-runs the command once. If the install fails, the agent gets a note telling it to try another
  install route itself. An unknown missing command gets the same note. Neither case loops.
- **`ensure_toolchain`** accepts groups (`web`, `python`, `deploy`, `dev`) or tools: `node` (npm, npx), `pnpm`, `yarn`,
  `python3`, `pip`, `uv`, `git`, `gh`, `vercel`, `render`, `netlify`, `wrangler`, `firebase`, `tsc`, `curl`, `jq`, `rg`,
  `unzip`. Dependencies install first (e.g. `vercel` needs `node`).
  - System packages: `sudo -n apt-get`, falling back to user space (official Node tarball into `~/.checkme/node`,
    `npm -g` into `~/.npm-global`, `pip --user`, release binaries into `~/.local/bin`).
  - It is idempotent: `command -v` runs first, and markers live in `~/.checkme/tools/<tool>`.
  - Results are cached per user in-process for 30 min.
  - Each install is capped at `COMPUTER_INSTALL_TIMEOUT_S` (600 s).
  - It prints `present=`, `installed=`, `failed=` and a per-tool log tail, which are parsed into the tool result.

## Stopping commands

When the user presses Stop, or sends a new message that redirects the agent, the turn's asyncio task is cancelled. The
running cloud command is then killed by its tag, along with every child process (`toolchain.kill_script`). The sandbox, its
desktop and other processes (browser daemon, dev servers) keep running. See LLD §7.9.

## Flags

| Env var | Default | Effect |
| --- | --- | --- |
| `COMPUTER_TOOLCHAIN_ENV` | on | Wrap cloud commands with the toolchain PATH prelude |
| `COMPUTER_AUTO_INSTALL` | on | Install a missing known tool on exit 127 and retry once |
| `COMPUTER_INSTALL_TIMEOUT_S` | 600 | Cap for one `ensure_toolchain` run |
| `COMPUTER_NODE_MAJOR` | 22 | Node.js LTS major for runtime installs |
| `LOCAL_SHELL_FALLBACK_TO_CLOUD` | on | `user_computer_shell` without a local computer runs on the cloud |
| `E2B_TEMPLATE` | `desktop` | E2B template for new sandboxes |

## Prebuilt template (`deploy/e2b/`)

Runtime install works with the stock `desktop` template, but the first `npm`/`vercel` call in a fresh sandbox then takes
about 20–60 s. The template preinstalls everything so that call is instant.

The template contents are:

- Base: E2B `desktop` (Ubuntu 22.04 and XFCE).
- Node.js 22 LTS (NodeSource), with corepack.
- pnpm, yarn and vercel, installed in `~/.npm-global`. The npm prefix is set, so `npm -g` never needs sudo.
- python3, pip, venv and uv; git, gh, curl, jq, ripgrep, build-essential.
- The Render CLI.
- Playwright and Chromium, including system libraries. The `/tmp/checkme-pw-ready` marker is set, so the browser
  daemon skips its install step.
- The PATH lines are written to `.bashrc` and `.profile`.

The computer helper (`computer_daemon.py`) is not baked in. It is uploaded at runtime with a version check, so a daemon
bump never needs a template rebuild.

Build it (this needs the E2B API key, which lives only on Render):

```bash
cd deploy/e2b
pip install e2b            # or: uv pip install e2b
E2B_API_KEY=e2b_*** python build.py checkme-desktop
```

The API image does not include `deploy/`. To build from a Render shell on the API service, where `E2B_API_KEY` is
already set, first fetch the repo there (for example `git clone` it into `/tmp`). Then run `python build.py` in
`deploy/e2b`. The build runs on E2B's side and takes a few minutes.

To activate it, set `E2B_TEMPLATE=checkme-desktop` on the API service (Render → checkme-api → Environment) and redeploy.
- New sandboxes use the template. Existing users keep their current sandbox, which still self-heals through runtime
  install, until it expires or is recreated.
- To roll back, unset `E2B_TEMPLATE` (back to `desktop`).

Check it with `python apps/api/scripts/e2b_smoke.py`, then ask an agent to "run node -v && vercel --version". The result
should come back with no `auto_installed` field.

## Limits

- Runtime installs need outbound internet from the sandbox: nodejs.org, the npm registry, GitHub releases, astral.sh and
  Render's install script.
- A tool with no install recipe is not auto-installed. The agent gets a note and installs it with `shell` itself.
- `sudo -n` must be passwordless. That holds on the E2B desktop; on images without it, only user-space routes work.
- The in-process install cache is per API process. The markers in `~/.checkme/tools` and `command -v` keep a reinstall
  cheap anyway.
