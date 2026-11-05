#!/usr/bin/env bash
# Creates the certificate that signs Lumio's Mac builds and stores it as GitHub secrets. Run this once.
#
# macOS tracks permissions such as Screen Recording by an app's signature. An ad-hoc signature changes with every build,
# so each update loses them. One fixed certificate keeps the signature's identity the same across releases.
#
# Usage: create-signing-cert.sh [output dir]     (default ~/.tauri; keep a backup of the .p12 somewhere safe)
#        NO_UPLOAD=1 skips storing the secrets, which the signing check workflow uses.
set -euo pipefail

OUT="${1:-$HOME/.tauri}"
P12="$OUT/lumio-mac-signing.p12"
REPO="specialshweta26/Checkme"
NAME="Lumio Code Signing"

if [[ -e "$P12" ]]; then
  echo "$P12 already exists. Replacing it would make every installed copy lose its permissions, so this stops here." >&2
  exit 1
fi
mkdir -p "$OUT"
WORK="$(mktemp -d)"
trap 'rm -f "$WORK/key.pem" "$WORK/cert.pem" "$WORK/openssl.cnf"; rmdir "$WORK"' EXIT

cat > "$WORK/openssl.cnf" <<CONF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $NAME
[ext]
basicConstraints = critical,CA:false
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
CONF

# The system's openssl writes a .p12 format that the macOS keychain can import.
PASSWORD="$(/usr/bin/openssl rand -hex 24)"
/usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 7300 -config "$WORK/openssl.cnf" \
  -keyout "$WORK/key.pem" -out "$WORK/cert.pem" 2>/dev/null
( umask 077; /usr/bin/openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -name "$NAME" \
  -out "$P12" -passout "pass:$PASSWORD" )

if [[ -n "${NO_UPLOAD:-}" ]]; then
  printf '%s' "$PASSWORD" > "$OUT/lumio-mac-signing.password"
  echo "Created $P12"
  exit 0
fi
base64 < "$P12" | tr -d '\n' | gh secret set MAC_SIGNING_P12 --repo "$REPO"
printf '%s' "$PASSWORD" | gh secret set MAC_SIGNING_PASSWORD --repo "$REPO"
echo "Created $P12 and stored the GitHub secrets. Back up that file: losing it costs users their permissions once."
