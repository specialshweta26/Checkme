# Browser and computer capabilities

What Lumio can do today against the target list, and the order to build the rest. "Cloud" is the E2B computer, "Desktop" is the Tauri app on the user's machine. Last checked against the code on 2026-11-09. "Have" means the code exists; it doesn't mean every site or app has been tested.

## Browser

| Capability | Cloud | Desktop | Notes |
|---|---|---|---|
| Open URL, reload, back, tabs, URL and title, scroll | Have | Have | |
| Forward | Have | Missing | |
| Element tree, page text, shadow DOM, iframes, coordinates | Have | Have | Desktop reads other-site frames separately |
| Click, double-click, hover, typing, key presses | Have | Have | |
| Right-click, drag | Have | Missing | |
| Text fields, native and styled dropdowns, tick boxes, dates, type-ahead, multi-step forms | Have | Have | Styled dropdowns are new and need more real sites |
| File upload | Have | Missing | |
| Multi-select, time pickers, rich-text editors | Untested | Untested | |
| Saved logins, sessions, CAPTCHA detection and hand-off | Have | Have | |
| Downloads | Have | Have | |
| Infinite scroll, pagination, lazy content | Have | Have | `browser_collect` |
| Working from the screen (canvas, unreadable pages) | Have | Missing | Desktop needs the screen scale |
| JavaScript alerts and confirm dialogs | Missing | Missing | Not reported to the agent |
| Screenshots as evidence in the chat | Missing | Missing | |
| One browser per agent or task | Missing | Missing | Agents running together share one browser and collide |
| Console errors, network capture | Missing | Missing | |
| Zoom, bookmarks, full-screen, broken-link check | Missing | Missing | Low value |

## Computer

| Capability | Status | Notes |
|---|---|---|
| Screenshot, mouse, keyboard, launching apps, opening files | Have | |
| Files: read, write, copy, move, rename, delete, search | Have | Limited to folders the user grants |
| Terminal, scripts, Git | Have | Interactive terminal sessions not tested |
| Teach a task and replay it | Have | |
| Windows: list, switch, resize, move | Missing | Only the front window's title today |
| Clipboard | Missing | |
| Several monitors | Missing | |
| Native accessibility (macOS, Windows, Linux) | Missing | Needed to work reliably inside native apps |
| OCR | Partial | The vision model reads screens; no separate OCR |
| Screen recording, notifications, process monitoring | Missing | |
| Native dialogs, unexpected popups, app crash recovery | Missing | |
| Excel, Word, PowerPoint files | Partial | Read as text, created by scripts; no pivot tables or charts inside the apps |
| Driving Office and Outlook apps | Missing | Build on accessibility and app scripting |

## Agent behaviour

| Capability | Status | Notes |
|---|---|---|
| Planning, memory, teammates, schedules, resume after restart | Have | |
| State tracking within a browser task | New | Done-so-far list, facts kept per page |
| Retry and changing approach | Partial | Repeat guard stops loops; recovery is still weak |
| Checking each important action | Partial | Verify from page state, but not always |
| Asking before final or sensitive actions | Have | |
| Run history | Have | For `browser_task` runs |
| Evidence capture, full audit trail | Missing | |

## Security

| Capability | Status | Notes |
|---|---|---|
| Sign-in, separate cloud computer per user | Have | |
| Saved login storage, hiding secrets in logs | Have | |
| Folder limits on the desktop | Have | |
| Approval for sensitive actions, stop button | Have | |
| Roles and permissions | Missing | |
| Website allowlist, app permissions | Missing | |
| Audit log of every action | Missing | |
| Turn time limit | Missing | Step limits only |
| Network limits, data-loss rules, download scanning | Missing | |

## Build order

1. **Pass real end-to-end tests.** Swag Labs and OrangeHRM on both browsers, one browser per task, screenshots in reports.
2. **Same browser powers on both sides.** Desktop forward, right-click, drag, upload and screen-based working; JavaScript dialogs; console and network capture.
3. **Operate the computer.** Windows, clipboard, several monitors, native accessibility, notifications, popups and crashes.
4. **Office and native apps.** Excel, Word, PowerPoint and Outlook through accessibility and app scripting, then other apps.
5. **Enterprise controls.** Roles, allowlists, audit log, time limits, network limits, data-loss rules.

## Technology

Keep what's already here rather than adding the suggested libraries:

- **Browser:** Playwright in the cloud and Chrome's debugging protocol on the desktop.
- **Desktop control:** native Rust in the Tauri app. PyAutoGUI, pywinauto and Robot Framework would mean shipping Python to every user's machine.
- **Accessibility:** Apple Accessibility, Windows UI Automation and AT-SPI, called from Rust.
- **Isolation, storage, orchestration:** E2B, Cloudflare R2, Postgres and LangGraph, already in use.
