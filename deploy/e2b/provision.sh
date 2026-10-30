#!/usr/bin/env bash
# Provision the Checkme computer template on top of E2B's public "desktop" template (Ubuntu 22.04 + XFCE).
# Runs once at template build time as root (see template.py). Everything here is also installed on demand at
# runtime by apps/api/app/agent/toolchain.py, so the default "desktop" template keeps working without this.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
NODE_MAJOR="${NODE_MAJOR:-22}"
U="${CHECKME_USER:-user}"
H="$(getent passwd "$U" | cut -d: -f6)"

apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  ca-certificates curl wget git unzip jq ripgrep fd-find tree universal-ctags build-essential \
  python3 python3-pip python3-venv gnupg

# Node.js LTS (system-wide, from NodeSource) + corepack (pnpm, yarn)
curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash -
apt-get install -y -qq nodejs
corepack enable || true

# GitHub CLI (official apt repo)
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  > /etc/apt/sources.list.d/github-cli.list
apt-get update -qq && apt-get install -y -qq gh

# Playwright's Chromium system libraries (the browser itself is installed per user below)
python3 -m pip install -q playwright
python3 -m playwright install-deps chromium

# Per-user setup: npm global prefix in the home dir (npm -g never needs sudo), CLIs, uv, Playwright Chromium.
sudo -u "$U" -H bash -euxo pipefail <<'USER'
mkdir -p "$HOME/.npm-global/bin" "$HOME/.local/bin" "$HOME/.checkme/tools" "$HOME/work"
npm config set prefix "$HOME/.npm-global"
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"
npm install -g --no-fund --no-audit --loglevel=error pnpm yarn vercel
curl -fsSL https://raw.githubusercontent.com/render-oss/cli/refs/heads/main/bin/install.sh | sh || true
command -v render || {
  A=$(uname -m); case "$A" in x86_64) A=amd64;; aarch64|arm64) A=arm64;; esac
  U=$(curl -fsSL https://api.github.com/repos/render-oss/cli/releases/latest | grep -o "https://[^\"]*linux_$A.zip" | head -1)
  curl -fsSL "$U" -o /tmp/render.zip && unzip -o -q /tmp/render.zip -d /tmp/render-cli
  cp "$(ls /tmp/render-cli/cli_* /tmp/render-cli/render 2>/dev/null | head -1)" "$HOME/.local/bin/render" && chmod +x "$HOME/.local/bin/render"
}
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" UV_NO_MODIFY_PATH=1 sh
python3 -m pip install -q --user playwright
python3 -m playwright install chromium
for rc in "$HOME/.bashrc" "$HOME/.profile"; do
  grep -qs npm-global "$rc" || echo 'export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.checkme/node/bin:$PATH"; export NPM_CONFIG_PREFIX="$HOME/.npm-global"' >> "$rc"
done
for t in node pnpm yarn vercel render gh uv python3 git; do date -u +%FT%TZ > "$HOME/.checkme/tools/$t"; done
echo "checkme-template $(date -u +%FT%TZ)" > "$HOME/.checkme/template"
USER

# Checkme's browser helper (computer_daemon) is uploaded at runtime with a version check, so it is not baked in;
# this marker only tells the runtime that Playwright is already installed.
touch /tmp/checkme-pw-ready && chmod 666 /tmp/checkme-pw-ready
mkdir -p /home/daytona && chown "$U":"$U" /home/daytona
apt-get clean && rm -rf /var/lib/apt/lists/*

# Smoke test
sudo -u "$U" -H bash -lc 'node -v && npm -v && pnpm -v && yarn -v && vercel --version && render --version && gh --version && uv --version && python3 -c "import playwright"'
