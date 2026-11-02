#!/usr/bin/env bash
# Install Lumio on this Mac without a browser download. Chrome quarantine is
# what makes macOS say the app is damaged. curl does not set that flag.
# ASCII only: macOS /bin/bash 3.2 treats a UTF-8 ellipsis as part of $var.
set -euo pipefail

REPO="specialshweta26/Checkme"
DEST="/Applications/Lumio.app"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is for macOS." >&2
  exit 1
fi

arch="$(uname -m)"
if [[ "${arch}" == "arm64" ]]; then
  match="aarch64"
else
  match="x64"
fi

echo "Looking up the latest Lumio for ${arch}..."
api="https://api.github.com/repos/${REPO}/releases/latest"
json="$(curl -fsSL -H "Accept: application/vnd.github+json" "${api}")"
url="$(printf '%s' "${json}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
want = sys.argv[1]
assets = data.get("assets") or []
def pick(pred):
    for a in assets:
        name = a.get("name") or ""
        if pred(name) and a.get("browser_download_url"):
            return a["browser_download_url"]
    return ""
url = pick(lambda n: n.endswith(".dmg") and want in n)
if not url:
    url = pick(lambda n: n.endswith(".dmg"))
print(url)
' "${match}")"

if [[ -z "${url}" ]]; then
  echo "Could not find a Mac disk image on the latest GitHub release." >&2
  echo "See https://github.com/${REPO}/releases/latest" >&2
  exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
dmg="${tmp}/lumio.dmg"
echo "Downloading..."
curl -fL --progress-bar -o "${dmg}" "${url}"

echo "Installing to ${DEST}..."
attach="$(hdiutil attach -nobrowse -readonly "${dmg}")"
volume="$(printf '%s\n' "${attach}" | awk '/\/Volumes\//{print substr($0, index($0, "/Volumes/"))}' | tail -1)"
if [[ -z "${volume}" || ! -d "${volume}/Lumio.app" ]]; then
  echo "The disk image did not contain Lumio.app." >&2
  hdiutil detach "${volume}" >/dev/null 2>&1 || true
  exit 1
fi
mkdir -p /Applications
rm -rf "${DEST}"
ditto "${volume}/Lumio.app" "${DEST}"
hdiutil detach "${volume}" >/dev/null
xattr -cr "${DEST}" || true
codesign --force --deep --sign - "${DEST}" >/dev/null 2>&1 || true

echo "Opening Lumio..."
open "${DEST}"
echo "Done."
