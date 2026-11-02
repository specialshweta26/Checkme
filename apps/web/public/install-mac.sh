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
attach="$(hdiutil attach -nobrowse -readonly -plist "${dmg}")"
parsed="$(printf '%s' "${attach}" | python3 -c '
import plistlib, sys
data = plistlib.loads(sys.stdin.buffer.read())
dev = ""
mount = ""
for ent in data.get("system-entities") or []:
    point = ent.get("mount-point") or ""
    if point:
        mount = point
        dev = ent.get("dev-entry") or dev
print(dev)
print(mount)
')"
device="$(printf '%s\n' "${parsed}" | sed -n '1p')"
volume="$(printf '%s\n' "${parsed}" | sed -n '2p')"
if [[ -z "${volume}" || ! -d "${volume}/Lumio.app" ]]; then
  echo "The disk image did not contain Lumio.app." >&2
  if [[ -n "${device}" ]]; then
    hdiutil detach -force "${device}" >/dev/null 2>&1 || true
  fi
  exit 1
fi
mkdir -p /Applications
rm -rf "${DEST}"
ditto "${volume}/Lumio.app" "${DEST}"
sync || true
detached=0
target="${device:-${volume}}"
for _try in 1 2 3 4 5; do
  if hdiutil detach "${target}" >/dev/null 2>&1; then
    detached=1
    break
  fi
  sleep 1
done
if [[ "${detached}" -ne 1 ]]; then
  if hdiutil detach -force "${target}" >/dev/null 2>&1; then
    detached=1
  fi
fi
if [[ "${detached}" -ne 1 ]]; then
  echo "Lumio is installed. The disk image is still mounted because it was busy; eject it from Finder when you can." >&2
fi
xattr -cr "${DEST}" || true
codesign --force --deep --sign - "${DEST}" >/dev/null 2>&1 || true

echo "Opening Lumio..."
open "${DEST}"
echo "Done."
