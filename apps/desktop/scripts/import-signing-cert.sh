#!/usr/bin/env bash
# Makes the signing certificate usable on a throwaway GitHub Mac runner, then tells later steps which identity to sign
# with. Trusting a self-signed certificate changes the machine's settings, so never run this on your own computer.
# With no certificate set, builds stay ad-hoc signed.
set -euo pipefail

if [[ -z "${MAC_SIGNING_P12:-}" ]]; then
  echo "No signing certificate is set, so this build is ad-hoc signed and users lose permissions on update." >&2
  exit 0
fi
if [[ "${CI:-}" != "true" ]]; then
  echo "This changes system trust settings, so it only runs in CI." >&2
  exit 1
fi

WORK="$(mktemp -d)"
KEYCHAIN="$WORK/signing.keychain-db"
KEYCHAIN_PASSWORD="$(openssl rand -hex 24)"
printf '%s' "$MAC_SIGNING_P12" | base64 --decode > "$WORK/cert.p12"

security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security set-keychain-settings -lut 3600 "$KEYCHAIN"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN"
security import "$WORK/cert.p12" -k "$KEYCHAIN" -P "$MAC_SIGNING_PASSWORD" -T /usr/bin/codesign >/dev/null
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN" >/dev/null
# shellcheck disable=SC2046
security list-keychains -d user -s "$KEYCHAIN" $(security list-keychains -d user | tr -d '"')

security find-certificate -p -c "Lumio Code Signing" "$KEYCHAIN" > "$WORK/cert.pem"
sudo security add-trusted-cert -d -r trustRoot -p codeSign -k "$KEYCHAIN" "$WORK/cert.pem"

IDENTITY="$(openssl x509 -in "$WORK/cert.pem" -noout -fingerprint -sha1 | cut -d= -f2 | tr -d :)"
rm -f "$WORK/cert.p12" "$WORK/cert.pem"
{
  echo "APPLE_SIGNING_IDENTITY=$IDENTITY"
  echo "LUMIO_MAC_IDENTITY=$IDENTITY"
} >> "$GITHUB_ENV"
echo "Mac builds will sign with certificate $IDENTITY"
