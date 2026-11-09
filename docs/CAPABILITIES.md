# Browser and computer capabilities

What Lumio can do today against the target list, and the order to build the rest. "Cloud" is the E2B computer, "Desktop" is the Tauri app on the user's machine. Last updated 2026-11-09. "Have" means the code exists; it doesn't mean every site or app has been tested.

## Browser

| Capability | Cloud | Desktop | Notes |
|---|---|---|---|
| Open URL, reload, back, tabs, URL and title, scroll | Have | Have | |
| Forward | Have | New | Step ran in the real app and the page changed; the agent's report didn't include the address |
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
| JavaScript alerts, confirm and prompt | New | New | Reported to the agent, declined unless it was told to accept. A pop-up raised while a page loads doesn't freeze it, but isn't reported on the desktop |
| Screenshots as evidence in the chat | New | Have | Page only; payment pages skipped. Cloud not yet tested in a real run |
| One tab per chat | New | Have | Agents running together keep to their own tabs. Cloud not yet tested in a real run |
| Console errors, failed requests | New | New | `browser_page_log`. Only failed requests; no successful requests or response bodies |
| Zoom, bookmarks, full-screen, broken-link check | Missing | Missing | Low value |

## Computer

| Capability | Status | Notes |
|---|---|---|
| Screenshot, mouse, keyboard, launching apps, opening files | Have | |
| Files: read, write, copy, move, rename, delete, search | Have | Limited to folders the user grants |
| Terminal, scripts, Git | Have | Interactive terminal sessions not tested |
| Teach a task and replay it | Have | |
| Windows: list, focus, move, resize, minimize | New | Listing tested on macOS; the rest not tested. Closing isn't offered, because it can lose unsaved work |
| Clipboard | New | Tested on macOS. Read only when the user's own message mentions it |
| Several monitors | New | `computer_screens` lists them, tested with one screen. Choosing a monitor to work on isn't built |
| Native accessibility | New | `computer_ui`: read, press, set value, focus, menus. Tested on macOS. Windows compiles, never run. Linux not built |
| OCR | Partial | The vision model reads screens; no separate OCR |
| Notifications | New | Tool call succeeded on macOS; display not confirmed |
| Screen recording, process monitoring | Missing | |
| Native dialogs, unexpected popups, app crash recovery | Partial | Clear errors and prompt guidance; nothing detects or recovers automatically |
| Excel, Word, PowerPoint files | New | `create_office_file`: formulas, charts, tables, slides, notes. Files checked by reading them back, not opened in Office. No pivot tables |
| Numbers, Pages, Keynote | New | `office_app` by script. Tested on macOS, new documents only |
| Excel and Word on Mac; Excel, Word, PowerPoint on Windows | Untested | Written, never run against the real apps |
| PowerPoint on Mac, Outlook, pivot tables, charts inside apps | Missing | |

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
| Real OrangeHRM login | Not yet rerun since these changes |
| Real Swag Labs login, desktop app 0.1.35 | **Failed at Add to cart:** signed in and sorted, but clicks had no effect. The local copy passes, cause unknown. Not rerun since |
| MakeMyTrip flight search, desktop app, working from the screen | Three fares returned and no booking made, from the user's own run. Fares not checked separately |
| Cloud browser, full run | Not yet run since these changes |
| Pop-ups, right-click, error log, drag, upload on a practice page, desktop app 0.1.31 | All passed, 35 seconds |
| Clicking a canvas page by sight, desktop app 0.1.31 | Clicked the right button; the page's own state confirmed it |
| Monitors, window list, clipboard, desktop app 0.1.32 | Passed; clipboard confirmed separately |
| TextEdit through menus and native controls, desktop app 0.1.33 | Passed |
| Calculator through named controls, desktop app 0.1.34 | 7 × 8 = 56 |
| Numbers by script, desktop app 0.1.35 | New sheet, SUM = 125, CSV export, 23.5 seconds |
| Pages and Keynote by script, desktop app 0.1.35 | Text and slides written, docx and pptx exported and read back. New Keynote decks start with an empty first slide |
| Excel, Word and PowerPoint file creation | Checked by reading the files back |
| New Numbers spreadsheet, formula and CSV export through native controls, desktop app 0.1.34 | Not finished: stopped after 7.5 minutes. 36 control calls took 226 seconds. Phase 4 needs app scripting, not control-by-control work |
| Stop button on a running turn, 0.1.34 | Passed: the stopped message saved, no error |

## Real-site test suite

Ten harder tasks run by the real agent on the cloud browser, on public sites with no login. Correct answers were worked out separately first. Every fix is generic: nothing in the code names one of these sites. These are practice sites, so this shows the agent can handle these kinds of problem, not that it will work on every B2B system.

Each result is from a single run, not all on the same build: T1 and T9 on the latest, the rest one or two builds earlier. T4 passed, failed part of the task, then passed, so it isn't reliably stable. T10's figure is from the latest build.

| Test | Skills | Result | Time |
|---|---|---|---|
| T1 Sort a table and open a row | Clickable headers, toggles, reading state | Pass | 40 s |
| T2 Filter, change page size, calculate | Grid controls, calculation | Pass: 12 people, Michael Silva 66, $3,339,260 | 42 s |
| T3 Long form with dependent dropdowns | Date picker, type-ahead, styled dropdowns, confirmation | Pass: all fields correct | 102 s |
| T4 Slow content and changing controls | Waiting for loaders, disabled to enabled fields | Pass, but see the note above | 87 s |
| T5 Frames and shadow DOM | Nested frames, editor frame, shadow DOM | **Partial:** frames correct, shadow DOM answer wrong | 334 s |
| T6 Several windows | New tab, switching back | Pass | 38 s |
| T7 Drag, hover, right-click | Drag and drop, hover content, context menu pop-up | Pass | 62 s |
| T8 Upload and download | File upload form, file download | Pass | 56 s |
| T9 Shop flow, stop before paying | Cart edits, native pop-ups, stopping before payment | **Partial:** cart and $1,890 total correct, stopped before payment, but pop-up messages not captured | 144 s |
| T10 Count across every page | Script-loaded pages, pagination, counting | **Fail:** 10 pages and 100 quotes correct, but counted 6 Einstein quotes, not 10, and reported no failures | 101 s |

**7 pass, 2 partial, 1 fail.**

### Problems found and fixed

| Problem | Fix |
|---|---|
| Clickable table headers and styled elements weren't offered to the agent | Listed, including images and elements with hover or context-menu handlers |
| Results were read before slow content finished loading | Loading indicators are waited for |
| Styled dropdowns had no name, so the agent skipped them | Fields take their name from what's around them |
| Ticked state wasn't visible | Controls show checked, selected, expanded, disabled and value |
| Text inside frames wasn't read on the cloud browser, causing an 8-minute loop | Frame text is included, and reopening a page with nothing done is refused |
| A reply could promise work and end with nothing done | The turn continues once |
| A re-sorted page looked unchanged | The check compares page text, not its length |
| An automatic click retry flipped a sort, opened extra tabs and added an item twice | Removed on the cloud browser. On the desktop it runs only if the click never reached the control |
| The double-order guard blocked Delete on a second row, and a test's Confirm button | The guard tells rows apart and only matches labels starting with a final word |

### Still open

- **Counting across many pages:** the agent can be confidently wrong (T10). A long task needs a way to collect and add up data in code rather than from the model's memory.
- **Shadow DOM:** it can report a hidden default instead of the text on screen (T5).
- **Pop-up messages:** a pop-up that appears after a delay can be missed (T9).
- **Speed on frame-heavy pages:** T5 took over 5 minutes.
- **Restart cost:** the first browser use after an update to the cloud browser takes about 4.6 minutes.
- **Tabs:** chat tabs aren't closed, so they build up (10 to 16 seen).
- **Not covered:** logins, the desktop browser for this suite, and real business systems.

## Reliability checklist

| Item | Status |
|---|---|
| Reliable element targeting | Refs stay stable; a moved control is found again if exactly one matches. Tested in unit tests |
| State inspection | Done on both browsers; checked on a real site |
| Native dialogs | Accept, dismiss and prompt text checked on a real site |
| Navigation tracking | Each result says if the page and address changed |
| Wait strategy | Waits for loaders and page changes; some fixed waits remain in rarely used paths |
| Retry | Deliberately limited, because automatic retries caused double actions |
| Verification after each action | Results carry the evidence, but the model decides what to do with it. T10 shows it can still be wrong |
| Evidence capture | Screenshots on request, and up to 5 on failed steps per turn |

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
