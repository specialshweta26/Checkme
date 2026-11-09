# Browser and computer capabilities

What Lumio can do today against the target list, and the order to build the rest. "Cloud" is the E2B computer, "Desktop" is the Tauri app on the user's machine. Last updated 2026-11-09. "Have" means the code exists; it doesn't mean every site or app has been tested.

## Browser

| Capability | Cloud | Desktop | Notes |
|---|---|---|---|
| Open URL, reload, back, tabs, URL and title, scroll | Have | Have | |
| Forward | Have | New | |
| Element tree, page text, shadow DOM, iframes, coordinates | Have | Have | Desktop reads other-site frames separately |
| Click, double-click, hover, typing, key presses | Have | Have | |
| Right-click, drag | Have | New | Desktop not yet tested in the real app |
| Text fields, native and styled dropdowns, tick boxes, dates, type-ahead, multi-step forms | Have | Have | Styled dropdowns are new and need more real sites |
| File upload | Have | New | Desktop limited to granted folders; not yet tested in the real app |
| Multi-select, time pickers, rich-text editors | Untested | Untested | |
| Saved logins, sessions, CAPTCHA detection and hand-off | Have | Have | |
| Downloads | Have | Have | |
| Infinite scroll, pagination, lazy content | Have | Have | `browser_collect` |
| Working from the screen (canvas, unreadable pages) | Have | New | Desktop sees only the browser page; not yet tested in the real app |
| JavaScript alerts, confirm and prompt | New | New | Reported to the agent, declined unless it was told to accept |
| Screenshots as evidence in the chat | New | Have | Page only; payment pages skipped. Cloud not yet tested in a real run |
| One tab per chat | New | Have | Agents running together keep to their own tabs. Cloud not yet tested in a real run |
| Console errors, failed requests | New | New | `browser_page_log`; response bodies are not captured |
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
| Evidence capture | New | Screenshots in the chat |
| Full audit trail | Missing | |

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

## Test results

| Test | Result |
|---|---|
| Shop flow on a local copy of the demo site, desktop app 0.1.29 | 3 of 3 correct, 67 to 74 seconds, one order each |
| Two chats at once on the desktop | Each stayed in its own tab |
| Real Swag Labs and OrangeHRM logins | Not yet rerun since these changes |
| Cloud browser, full run | Not yet run since these changes |

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
