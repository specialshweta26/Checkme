# Lumio desktop (Mac and Windows)

Wraps the Lumio website in a Tauri 2 window and runs a sidecar on this machine.

- **Website:** always the cloud computer (E2B).
- **This app:** files inside user-granted folders, Lumio's own Chrome profile, live screen, and teaching on this computer. Terminal is off unless enabled. Shell/files fall back to E2B if the sidecar is offline. Browser, boot, and teaching never open E2B from this app.

The sidecar polls `/api/desktop/jobs` (outbound), so it works behind NAT. Sign in inside the window; pairing is automatic.

## Run locally

Needs [Rust](https://rustup.rs/) and the Tauri [prerequisites](https://v2.tauri.app/start/prerequisites/) (Xcode CLT on Mac; WebView2 on Windows).

```bash
# API + web already running (docker compose up, or local Next on :3000)
cd apps/desktop
npm install
npm run dev
```

`LUMIO_URL` defaults to `http://localhost:3000` in debug. Point at production:

```bash
LUMIO_URL=https://checkme-web-kw68.onrender.com npm run dev
```

## Build installers

Same codebase. Build on the OS you want the installer for (Tauri does not cross-compile this app from Mac to Windows).

```bash
# On this Mac → .dmg
# On Windows (WebView2 + Rust) → NSIS .exe
LUMIO_URL=https://checkme-web-kw68.onrender.com npm run build
```

Artifacts land in `src-tauri/target/release/bundle/`. File tools stay inside granted folders. Terminal uses `bash` on Mac and `cmd.exe` on Windows only when the user has enabled it.

macOS Gatekeeper treats a browser-downloaded unsigned app as damaged. Install with `curl -fsSL <site>/install-mac.sh | bash`, or after copying to Applications run `xattr -cr /Applications/Lumio.app`.

## Folder grants

There is no automatic home-folder or `~/Lumio` jail. The user grants folders with the native picker (**This computer** in the app, or the agent calls `grant_folder`). List, search, read, write, copy, move, and delete run only inside those folders. Terminal and Python stay off until enabled in settings. The Lumio browser uses a separate profile under the app data directory; it does not read Chrome/Edge/Safari profiles.
