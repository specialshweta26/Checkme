# Resilient browsing

How Checkme agents read and use websites that push back on automated visitors (for example Myntra, Ajio,
Policybazaar, BikeWale, CarDekho, Amazon), without site-specific code. Applies to every client (web, mobile, WhatsApp),
because it all runs in the API and the agent's cloud computer.

Code: `apps/api/app/connectors/block_detect.py` (detector), `browsing.py` (ladder, Tavily, hosted browsers,
config for the computer), `computer_daemon.py` (the computer's browser), `browser_profile.py` (saved cookies),
`daytona_box.py` (`_open_in_browser`, `web_fetch`), `agent/web_tools.py` (`web_search`, `web_fetch`),
`agent/research.py` (`http_fetch`).

## Why sites said "access denied"

Bot managers (Akamai, Cloudflare, PerimeterX/HUMAN, DataDome, AWS WAF...) score each request on *where it comes
from* (datacenter IP or home connection), *what it looks like* (TLS and HTTP/2 fingerprint, headers, browser
properties) and *how it behaves*. Before this change the computer's browser gave itself away on several counts:

| Signal | Before | Now |
| --- | --- | --- |
| User agent | Fixed "Mac, Chrome 129" string on a Linux Chromium of another version: UA, `sec-ch-ua`, `sec-ch-ua-platform` and `navigator.platform` disagreed | The browser's own UA; all four agree |
| HTTP version | `--disable-http2 --disable-quic` forced HTTP/1.1, which no real Chrome does | HTTP/2 and QUIC on (`BROWSER_HTTP2`) |
| Automation flag | `--enable-automation` (Playwright default) | Removed |
| Language / timezone | en-US / UTC | User's locale and timezone (en-IN, Asia/Kolkata by default) |
| Bot checks | A passive "Just a moment" check was read at once and reported as a wall | Up to 15 s for the check to clear, with small mouse moves |
| "Access Denied" pages | Not recognised as a block: the agent read them as an empty page and told the user the browser was "restricted" | Classified as `denied`, then the fallback ladder |
| Profile | Fresh profile on every new sandbox | Cookies saved per user (encrypted) and restored |
| Daemon threads | Playwright's sync API was called from a new HTTP thread per request, so every browser command after the first in a daemon's life failed with "cannot switch to a different thread" and fell back to screenshots | All browser work runs on one dedicated thread |
| Server fetch | UA + Accept over HTTP/1.1 | Short header set, then a full Chrome header set, over HTTP/2 |

The IP is the one thing code cannot change: E2B and Render run in datacenters. Some sites (Ajio/Akamai is the
clearest example) block those ranges intermittently or always. That is what the proxy, Tavily and hosted-browser
steps are for.

## The detector

`detect_block(status, url, title, text, html)` returns `None` or
`{blocked, kind, vendor, interactive, host, reason}`:

- `captcha` (interactive): reCAPTCHA / hCaptcha / Turnstile widgets on a short page, PerimeterX press-and-hold,
  DataDome, Google `/sorry`, Amazon `validateCaptcha`, "verify you are human".
- `challenge`: passive JS checks (Cloudflare `_cf_chl_opt`, Akamai `sec-cpt`, AWS WAF, Kasada, Imperva, F5).
- `denied`: "Access Denied", "you have been blocked", Cloudflare 1020, 401/403/451, 503 on a short page.
- `rate_limited`: 429 / "too many requests".
- `empty_shell`: a near-empty JS shell (only when the caller asks; the ladder does).

Text markers count only on short pages and HTML markers only on error statuses or short pages, so an article
about "access denied" errors or a form with reCAPTCHA is not a wall. Cookie consent pages are never walls.

## The ladder

`fetch_with_ladder(url, steps)` tries each step until one returns a usable (unblocked, non-empty) page. Every
step has a time limit (`BROWSE_STEP_TIMEOUT_S`, default 20 s; hosted browser `BROWSE_REMOTE_TIMEOUT_S`, 60 s) and
the whole ladder `BROWSE_TOTAL_TIMEOUT_S` (60 s). Unconfigured steps are skipped silently. A step that errors does
not stop the ladder. The result records `attempts` (step, status, block kind or error, ms).

| Step | What | Needs |
| --- | --- | --- |
| `cache` | Page text Tavily returned with the search results (15 min, in memory) | `TAVILY_API_KEY` |
| `server` | Direct fetch from the API, short headers then full Chrome headers, HTTP/2 | nothing |
| `proxy` | The same through the proxy | `BROWSER_PROXY_URL` |
| `tavily` | Tavily Extract (Tavily's crawler network) | `TAVILY_API_KEY` |
| `remote` | Steel `/v1/scrape`, or a Browserbase session read over CDP by the computer | `REMOTE_BROWSER_PROVIDER` + key |
| `alternates` | The page's AMP link or mobile alternate, then the `m.` host | nothing |
| `reader` | `r.jina.ai` renders the page and returns text | nothing (`JINA_API_KEY` raises limits) |
| `archive` | Latest Internet Archive copy, labelled with its date | nothing |

Where it is used:

- **`browser_navigate` on the computer**: the real browser first (it is interactive and shows on
  the live screen). If the site still blocks: the ladder without `server`; the copy that worked is painted into
  the tab and marked `read_only: true` with `live_site_blocked` and a note. A CAPTCHA gets a shorter ladder (35 s)
  and, if nothing worked, the usual take-control handoff. Other blocks never trigger a handoff: the agent gets
  `blocked_source` and is told to name the site and use equivalent sources.
- **`web_fetch` / `web_read`** (with or without a computer): server-side only, `LADDER_QUICK` = `cache, server,
  proxy, tavily, alternates, reader` (12 s total, 6 s per step; `web_read` reads up to 5 pages in parallel). A page
  that is still blocked uses the computer only when it is already running (`WEB_FETCH_BROWSER_FALLBACK=warm`);
  otherwise the result hints `browser_navigate` if the page is essential. See LLD §7.7 Fast answers.
- **`research_crawl`**: `cache, server, proxy, tavily` within 30 s per page; pages still blocked are listed under
  `needs_browser` with the blocked site.
- **`web_search`**: unchanged stages, new order (below).

When everything fails the result is
`{ok: false, blocked: true, blocked_source: "ajio.com", attempts: [...], error, hint}`. The prompt tells the agent to
say plainly "Ajio blocked automated access", never "the browser is restricted", never invent the page, and to
continue with other sources (other retailers, the brand's or insurer's own site, comparison and review sites).

## Search order

`SEARCH_ORDER=paid,browser,free` (default). Paid APIs run first, Tavily first among them (`tavily, brave_api,
serper, exa`, only keyed ones), then the computer browser's search, then the keyless engines. Tavily searches ask
for `include_raw_content` so the result pages' text is cached and `web_fetch` on a result answers instantly. Stage
budgets are unchanged (`SEARCH_PAID_TIMEOUT_S` 20 s, `SEARCH_BROWSER_TIMEOUT_S` 45 s / cold 120 s,
`SEARCH_FREE_TIMEOUT_S` 15 s). Set `SEARCH_ORDER=paid,browser` to drop the free engines entirely.

## The computer's browser

`launch_options()` in the daemon: no `--enable-automation`, `--disable-blink-features=AutomationControlled`, the
browser's own UA (headless: the real version without "HeadlessChrome"), HTTP/2, the user's locale and timezone,
fixed 1280x800 viewport (the live screen depends on it), optional Google Chrome (`BROWSER_CHANNEL=chrome`, installed
on first use, falls back to Chromium) and optional proxy. One small init script only fixes `navigator.webdriver` /
`window.chrome` if they would give automation away; nothing else is spoofed (spoofing values a real Chrome already
has is itself detectable). Typing has a small random delay.

The API writes the settings to `/tmp/checkme_browser_config.json`; the daemon reads it once and deletes it. Its
`/health` reports a hash of the settings and the API restarts a daemon whose settings changed.

**CAPTCHAs are not solved or clicked automatically.** Passive checks get time to clear on their own; anything
interactive goes to the user (take control) only when the user needs that exact site.

## Saved browser profile

After a successful navigation (at most every 5 minutes per user) the API exports the browser's cookies, encrypts
them with the app key and stores them in `browser_profiles` (one row per user, ≤ 256 KB, deleted with the user).
A new computer imports them when its daemon starts. Only cookies: no history, local storage or saved passwords.
Never shared between users. `BROWSER_PROFILE_PERSIST=0` turns it off. Failures are ignored.

## Fast browser

Why: logins, forms and downloads were slow. Each step was a big model call reading a 150-element JSON list (plus
screenshot paths), each browser command cost 4–6 sandbox round trips (Playwright import probe, health curl, payload
upload, curl, screenshot download), and fixed sleeps after every action (and an 8 s settle plus two 3 s scroll
waits on any short page such as a sign-in form) added seconds. All of the below works the same on web and mobile
(it is server-side), is generic (no per-site code), and each piece has its own switch and falls back to the old
behaviour on any error.

| Piece | What it does | Switch |
| --- | --- | --- |
| DOM-first output | The model reads the numbered element tree (`- e3 textbox Email [email]`, `[password]`, `[file]`); the raw `elements` list and screenshot fields are dropped from tool output when the tree is present. `computer_observe` / `computer_act` (screenshots, x,y) stay as the fallback. | `BROWSER_COMPACT_SNAPSHOT` |
| Batched actions | `browser_act` already runs a list of actions and stops at the first failure; the prompt now asks for one batch per step (fill email, fill password, click Sign in). Each result carries `took_ms`. | always |
| `browser_task` | A small fast model plans a batch of actions per call from the tree, the daemon runs it; after two failed rounds it escalates to the main model, then returns `fallback` with the page so the main agent continues with `browser_act`. Values (email, password, OTP) travel as `{{name}}` placeholders: the planner sees only names, code substitutes them right before typing; results and logs are scrubbed. Never clicks Pay/Buy/Place order/Book/Send/Transfer/Delete unless `allow_final_action`. | `BROWSER_FAST_LOOP`, `BROWSER_FAST_MODEL` (default `OPENAI_SMALL_MODEL`, then `OPENAI_MODEL`), `BROWSER_ESCALATE_MODEL` (default `OPENAI_MODEL`), `BROWSER_FAST_MAX_STEPS` (12) |
| Saved runs + replay | A finished `browser_task` is saved in `browser_traces` (per user and site) as selector steps: test id → stable id → name → aria-label → label → placeholder → role+name → href → text, never refs or coordinates. Secret fields (password, OTP, CVV…) keep only a parameter name. The next same task replays with no model call per step; each step is verified (target found, typed value stuck; secret fields only non-empty) and a mismatch stops and hands the page to the model. `browser_replay(name / url / skill_id, values)` replays explicitly. | `BROWSER_REPLAY` |
| Teach recordings | While a skill is taught, a DOM recorder in the page browser logs clicks, field edits (never secret values), selects and Enter with the same stable descriptors. On finish they become a replay trace for that skill and its instructions say to call `browser_replay(skill_id=…)` first. Only starts when the page browser is already running. The screen/xinput recording is unchanged. | `BROWSER_TEACH_DOM` |
| Direct downloads | `browser_download(url or ref)` fetches the file with the browser context's cookies (`context.request`, no click, no dialog); a `download` step on a file-looking link does the same first. HTML answers or errors fall back to clicking and capturing the Playwright download. | `BROWSER_DIRECT_DOWNLOADS` |
| Warm browser | A turn classified as computer work (§7.7) starts the daemon and restores the saved cookies (`browser_profiles`) in the background while the model plans. After a step that typed into a secret field succeeds, cookies are saved soon (4 s later) so the next session stays signed in. | `BROWSER_PREWARM` (`computer` boots if needed, `warm` only when already running, `off`) |
| Fewer round trips | A daemon proven healthy for this sandbox, release and settings is trusted for `BROWSER_READY_TTL_S` (600 s): no Playwright import probe or health curl per command (the helper was effectively re-checked on every call). Small payloads go inline in the curl command (one exec instead of upload + exec); the screenshot is cached in the background. Any delivery failure drops the cache entry. | `BROWSER_FAST_RPC`, `BROWSER_READY_TTL_S` |
| Event waits | After each action: DOMContentLoaded when the URL changed, then DOM quiet (no mutations for 80–250 ms, capped at 0.9–2.5 s), instead of fixed pauses. A page is ready once quiet and it has enough text **or** a visible form field, so sign-in and OTP pages no longer wait the full settle and scroll time. | `BROWSER_SMART_WAITS` |
| Browser basics | Generic, no per-site code, on the cloud browser and the desktop app. **Dates:** a native date / time field gets the ISO value from the usual ways a date is written (13/05/2026, 13 May 2026, May 13 2026; day-first when unclear); a text box that holds a date (date placeholder, name or picker class) gets Tab after typing so the picker commits it. **Values that must stick:** after `fill`, the value is read back; a field that dropped it (React-controlled, input masks) is typed key by key. Password fields are never read back. **Loading:** after a click or navigation, a visible spinner, progress bar or `aria-busy` region is waited out (up to 8 s). **No-op clicks:** a click, hover or check that changes nothing on the page (address, title, size, text, focus, open menus, checked controls, scroll) adds a `warnings` line, and the fast loop counts it like a failure so it changes approach and escalates. **Long pages:** the element list says `… N more elements are not listed`, and `browser_snapshot(find="words")` (fast loop: a `find` action) lists the matching elements first. Cloud cap: `max_elements` in the browser config (150, 40–400). **Desktop mouse:** clicks and hovers on the main page use a real mouse (CDP) at the element's centre when nothing covers it, so menus that open on pointer events or CSS `:hover` work; covered elements and other-site frames keep the DOM event. | `BROWSER_SMART_WAITS` (loading wait); the rest always |
| Timing logs | `browser.timing command=… steps=<kinds> ensure_ms rpc_ms total_ms` per command, `browser.task step=… plan_ms act_ms actions ok` per fast-loop round, `browser.replay … ms`. Kinds and times only, never typed values. | `BROWSER_TIMING_LOGS` |

The live screen still follows the active tab: every path drives the same tabs as `browser_act`.

Local measurement (headless Chromium on the dev box, a local sign-in form whose submit waits 150 ms for an API and
a dashboard that renders 300 ms after load; median of 5, not E2B): navigate to the sign-in page 15.5 s → 0.64 s;
fill email + password + click Sign in 1.79 s → 0.70 s (and the new page was rendered in the result, it was not
before); open the dashboard 2.3–3.2 s → 0.65 s; a direct file download 4 ms. Not measured: E2B round trips and
model latency.

## Healing saved runs

Why: a saved run (a finished `browser_task`, or a taught skill's recording) replays by stable selectors with no
model call per step. When a site renames or moves a control, the old replay stopped at that step and the whole task
fell back to the model, and the saved run stayed broken for every later run.

Now (`BROWSER_HEAL`, on by default), when a replay step's element is gone or its value does not stick:

1. The fast loop (`browser_task`, at most 4 rounds, never saved, saved runs off) is asked to do **only that step**
   on the current page, described in plain English from the saved step (`click the button "Order Status"`,
   `fill the textbox "From" with {{from_date}}`). It may open the menu or close the popup the step needs first, and
   is told not to do later steps. The final-action rule is unchanged.
2. The replay carries on with the next saved step. At most 3 steps are healed per run; a step the model cannot do
   stops the replay as before and hands the page over.
3. A run that finished thanks to a fix stores the fixed steps as a **proposal** on the saved run
   (`proposed_steps`, `proposed_note`, `proposed_at`). Replays keep using the old steps until the user accepts it, and a
   healed run is not counted as a clean success. The tool result carries `healed`, `proposed_fix` and a `heal_note`
   telling the agent to say what changed and ask before accepting.
4. The user (or the agent, after the user says so in chat) accepts or rejects with `browser_saved_runs`
   (`list`, `accept_fix`, `reject_fix`, `undo`) or the API: `GET /api/browser/saved-runs`,
   `POST /api/browser/saved-runs/{id}/fix {"accept": true|false}`, `POST …/{id}/undo`, `DELETE …/{id}`. Accepting
   keeps the replaced steps in `prior_steps` for one undo.

The schema change is additive (`ALTER TABLE browser_traces ADD COLUMN IF NOT EXISTS …`), so old and new API
instances can overlap during a deploy.

### Editing saved runs: exact steps and AI steps

The **Saved runs** page (`/runs`, side bar) lists every saved run with its steps in plain English, the values it
asks for, clean-run counts, and any fix waiting for approval (**Keep the fix** / **Discard**). **Edit** lets the user
rename the run, reorder or remove steps, and **rewrite** a step in their own words, which turns it into an **AI step**
(`{"kind": "goal", "text": "Set the date range to last month"}`). Exact steps replay by selector with no model
call; an AI step is done by the fast loop on the current page every run (`GOAL_ROUNDS` = 6, never saved), so the parts
of a run that change from day to day (pick a date, choose a row) don't break the exact parts. `{{name}}` in an AI
step is a value given at run time; the planner sees only the name. An AI step that mentions a final action (Pay,
Send, Delete…) makes the run count as final, so it is not replayed without `allow_final_action`.

`PUT /api/browser/saved-runs/{id}/steps {"steps": [{"keep": 0}, {"goal": "…"}, …], "name": "…"}` saves the edit (at
most 200 steps), keeps the old steps for one undo, and drops a pending fix (it was made for the old steps).

## Saved sign-ins

Why: a scheduled or saved run stopped at every sign-in page and waited for the user (`browser_handoff`), so nothing
could run unattended, and users were tempted to paste passwords into chat.

The user adds a site's username and password under **Saved sign-ins** on the Saved runs page
(`POST /api/browser/logins {host, username, password, label}`; `GET` lists site, label and a username hint such as
`o•••@example.com`; `DELETE /api/browser/logins/{id}`). Values never pass through chat or a model, and the API never
returns them. Both are encrypted with the app key (`site_logins.username_enc` / `password_enc`, one row per user and
site, deleted with the user). Saving a new username with an empty password keeps the saved password.

When `browser_task` or `browser_replay` runs on a site with a saved sign-in (or a subdomain of it), code adds
`login_username` / `login_password` to the run's values (`BROWSER_SAVED_LOGINS`). The planner only sees the
`{{login_username}}` / `{{login_password}}` names, the daemon or desktop app types the values, and results, logs and
saved runs are scrubbed, so a saved run keeps the names and fills them again next time. Values the caller passes win.
`browser_logins` tells the agent which sites have one; the prompt says never to ask for a password in chat. One-time
codes and CAPTCHAs still go to the user.

## Long tasks

Why: a skill with many phases (sign in, open a menu, open a report, set dates, apply, export, check) ran inside one
chat turn. The fast loop gave the small model 12 rounds, and the turn's step limit (about 160 graph steps) ran out
partway through.

- **Fast loop sizing:** `browser_task` treats a goal with 5 or more listed steps, 5 or more "then"s, or more than 120
  words as long. A long goal starts on the main model (`BROWSER_ESCALATE_MODEL`) with 30 rounds instead of 12. A
  caller's `max_steps` wins, up to 40.
- **Long skills move to the background:** in a chat turn that can start jobs, a matched skill with 12 or more steps
  that hasn't started yet gets a prompt line to hand it to `start_background_task`, with the skill name, the
  request and every value the user gave. The job has no chat timeout, survives restarts (durable checkpoints) and
  shows a live timeline.
- **Phase checkpoints:** the background worker makes one todo per phase of the skill and calls `skill_progress` after
  each one, so a restart continues from the next phase instead of starting over.
- **Step budget:** a background worker job gets 320 graph steps, growing to at most 400 when it hits the limit,
  instead of 160–200.

## Downloads on your computer

Why: a file the cloud computer downloaded only arrived as a chat attachment, so a skill step like "check it in
Finder → Downloads" could never be done, and users had to save each file by hand.

When the cloud computer downloads a file and the user's desktop app is connected (`DOWNLOADS_TO_DESKTOP`, on by
default), the API sends the app a `save_download` job with the file's signed Lumio link. The app saves the file into
the Mac's or PC's Downloads folder. It only fetches `https://<app host>/api/files?t=…` links, makes the file name
safe (no paths or special characters), never overwrites an existing file (`Report (2).csv`), stops at 250 MB, and
writes to a temporary name before renaming. At most 5 files per result. The download gets
`saved_on_your_computer` (the path) or `desktop_save_error`, and the agent says where the file is. The chat attachment
is still there either way. Files the desktop app's own browser downloads are already on the computer and are skipped.
An older desktop app answers that it can't do this, so nothing breaks.

## Run history

Why: when a browser task stopped, the only record was the agent's summary in chat, so nobody could see where or
why it went wrong.

Every `browser_task` run is kept (`BROWSER_RUN_LOG`, table `browser_runs`, the newest 100 per user):

- **Each step:** each batch the planner ran, in words (`fill "Email" with {{login_username}}, click "Sign in"`);
  the planner's one-line reason (`why`, a new field in its JSON); whether it worked; the error and any no-op
  warnings; the page title and address; and a 720 px JPEG of the screen afterwards (taken from the frame the cloud
  computer or desktop app already sends, at most 60 per run).
- **Saved runs:** replays and healed steps are logged as steps too. Healing and AI steps run inside the run they
  belong to (`runlog.CURRENT`) and add to it instead of starting their own.
- **Status:** `completed`, `replayed`, `recovered` (fixed itself), `stopped` or `needs_you`. The run is linked to
  its saved run (`trace_id`), and the tool result carries `run_id`.
- **Screenshots and safety:** screenshots are stored with the other files under `{user}/browser-runs/{run}/` and
  deleted with old runs. Typed values never appear in the log: actions show `{{names}}` or `•••`.

The Saved runs page lists **Recent runs** (or a saved run's runs, via **History**). Opening one shows the timeline
with screenshots. API: `GET /api/browser/runs[?trace_id=]`, `GET /api/browser/runs/{id}` (with signed screenshot
links). Only the owner can read a run.

## Running saved runs

A saved run can be started without a chat:

- **From the app:** **Run** on the Saved runs page asks for the run's values, or takes a list of rows (a header line
  with the value names, then one line per run, at most 50). `POST /api/browser/saved-runs/{id}/run {values, rows}`.
- **From another system:** **Make a key** gives a run key, shown once and stored only as a SHA-256 hash, plus a ready
  `curl` command. `POST /api/run-hooks/{id}` with `Authorization: Bearer <key>` and the same body. **New key**
  replaces it and **Remove key** turns it off.
- **Values:** only the run's own, non-secret parameters are accepted, at most 500 characters each. Secret ones
  (passwords, codes, `login_*`) are refused: the saved sign-in for the site covers them, so nothing secret goes into
  the job's prompt.
- **How it runs:** one background worker job (its own chat, with the live timeline) runs the saved run once per row,
  in order, with `browser_replay(run_id=…)`, and finishes with a table of each row's result. A failed row is noted
  and the next one runs. Rows don't run in parallel: each user has one computer at a time.

**Approval steps:** in **Edit**, **Add approval step** adds `{"kind": "approve", "text": "Check the totals before
export"}`.
- The replay stops there with `needs_approval {step, text}`, and the agent asks the user (`ask_user`, or the job's
  question).
- Only on a clear yes does it continue with `browser_replay(run_id=…, start_step=N)`. That carries on from the same
  page without reopening the start page.
- A pause isn't counted as a failure, and `browser_task` never works past an approval step on its own.

## Deep links

A search-results address the agent builds itself (route, date and filters in the URL) often won't open: the site
refuses direct links, wants its own session first, or has changed its link format. `browser_navigate` (cloud and
desktop) now marks such a failure (`ok: false`, a bot wall, HTTP 400 or above, a browser error page, or an empty page)
with `deep_link_failed` and `fallback_url` (the site's home page), plus a hint: open the home page, use the site's own
search form and filters like a person (`browser_task` does this quickly), and only switch sources if the home page is
blocked too. A home page that fails is left as it is. No site names are involved.

## Variables

A saved run is a form filled once, kept as a template. When a run is saved (a finished `browser_task`, a taught
skill's recording, or an accepted fix), every value typed or picked in a form field becomes a **variable**: a
`{{name}}` named after the field (`from`, `to`, `departure_date`), with the field's label and the recorded value as
its example. Secret fields keep only a name, as before.

When the run is used again, the variables the caller didn't pass are filled from what the user wrote
(`BROWSER_FILL_VARIABLES`). The fast model gets the user's message (and the task text), today's date, and each
variable's name, label and example. It writes each value in the example's format, works out relative dates, and
leaves a variable empty rather than invent one.
- **Never filled this way:** secret variables. They come from the caller or a saved sign-in.
- **Not mentioned:** a variable the user didn't mention keeps its example (the value it was recorded with).
- **Reported back:** the result lists what was filled in `variables_filled`.

The Saved runs page shows each run's variables with label and example. **Run** and run keys pass them by name.

### New values in an old run

Saved runs recorded before variables existed get them when they load, too. When a run is used with values that
differ from the ones it was recorded with:
- **Clicks on old values:** a recorded click that refers to an old value becomes an AI step that picks the new one
  ("Choose “Chennai” for From"). That covers the old suggestion ("Bengaluru, India") and the old calendar day ("Sat
  Dec 19 2026" for `2026-12-19`).
- **Start address:** a start address with a query (a results page carrying the old search) is opened without it.

## Pickers

Many forms show a value in a box that takes no typing: a read-only input, a label or a div. Clicking it opens a
search box with suggestions, or a calendar, and a site often pre-fills these boxes with the user's last search.
`fill` on such a box, in the cloud browser and the desktop app, now works like this:
1. Note the text boxes already on the page, then click the box (with a real mouse on desktop).
2. If the value is a date and a calendar opened, click the cell whose `aria-label` / `data-date` / `title` reads
   as that date, and click the next-month arrow (up to 14 times) until it shows. Disabled days are skipped.
3. Otherwise, type into the box that has focus, or the one that just appeared, then click the best-matching
   suggestion (or press Down and Enter).

If nothing opens, the step fails with a hint to look at what did open.

**Calendars** are read three ways, so they work however they're built:
1. Cells with a full date label (`aria-label`, `data-date`, `datetime`, `title`, such as "Mon Jan 18 2027" or
   "2027-01-18").
2. Month grids: a "January 2027" / "Jan '27" heading plus the day numbers in the nearest block holding at least 28 of
   them. Disabled and other-month days are skipped.
3. The next and previous month arrows, chosen by comparing the shown months with the wanted one.

With no dated cell or month heading on screen, the step clicks no arrow (so never a carousel's). An unavailable day
stops it rather than picking another.

**`pick_date`** (`{"kind": "pick_date", "ref": <date field>, "text": "2027-01-18"}`) opens the field's calendar unless one is
already open, then picks the day. It never types. The fast loop and the main prompt use it for every date instead of
clicking days and arrows one at a time, and recorded `pick_date` steps become date variables. Calendar day cells
(`[role=gridcell]`, `[data-date]`, `td[title]`, focusable labelled cells) are now listed in the page snapshot, so a day
can also be clicked by ref. Checked in real Chromium on four calendar styles (labelled cells; heading plus numbers
needing the previous arrow; titled table; two months side by side with disabled days), with `fill` and `pick_date`,
in both the cloud browser and the desktop app. The prompt says to change every pre-filled
field to what the user asked, and never to hand the screen over because a form is awkward (only sign-in, MFA,
CAPTCHA and payment are handed over). No site names are involved.

## Checks

Saved runs are a graph, not only a list. A **check** step (`{"kind": "check", "text": "Does the page say no flights
found?", "yes": 0, "no": 6}`) asks the fast model a yes/no question about the current page. The replay then jumps to
the `yes` or `no` step (1-based; `0` is the end), and every other step goes on to the next one. Anything unclear is
answered "no". A step that runs more than 3 times stops the replay as a loop.

The page draws the run as a graph: numbered nodes with arrows, and checks show their yes/no branches. In **Edit**,
**Add check** adds a question with a "go to" picker for each answer. Checks are logged in the run history with their
answer.

## Configuration

All optional. Defaults need no new account.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SEARCH_ORDER` | `paid,browser,free` | Search stages |
| `TAVILY_API_KEY` | – | Tavily search, cached result text, Extract |
| `TAVILY_RAW_CONTENT` / `TAVILY_EXTRACT` | `true` / `true` | Use Tavily page text / Extract |
| `BROWSER_LOCALE` | `en-IN` | Browser language |
| `BROWSER_TIMEZONE` | user's profile timezone | Browser timezone |
| `BROWSER_HTTP2` | `true` | HTTP/2 + QUIC in the computer browser |
| `BROWSER_CHANNEL` | (Chromium) | `chrome` installs and uses Google Chrome |
| `BROWSER_CHALLENGE_WAIT_S` | `15` | Wait for a passive bot check |
| `BROWSER_PROXY_URL` | – | `http://user:pass@host:port` (or `https://`, `socks5://`) |
| `BROWSER_PROXY_FOR_FETCH` | `true` | Also use the proxy for the server-side `proxy` step |
| `BROWSER_PROFILE_PERSIST` | `true` | Save cookies per user |
| `REMOTE_BROWSER_PROVIDER` | – | `steel` or `browserbase` |
| `STEEL_API_KEY` | – | Steel |
| `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | – | Browserbase |
| `REMOTE_BROWSER_USE_PROXY` | `false` | Use the provider's residential proxies (paid) |
| `BROWSE_ALTERNATES` / `BROWSE_ARCHIVE` | `true` / `true` | Mobile/AMP and archive steps |
| `BROWSE_READER_URL` | `https://r.jina.ai/` | Reader step (empty disables) |
| `JINA_API_KEY` | – | Higher reader rate limit |
| `BROWSE_STEP_TIMEOUT_S` / `BROWSE_TOTAL_TIMEOUT_S` / `BROWSE_REMOTE_TIMEOUT_S` | `20` / `60` / `60` | Ladder limits |
| `BROWSER_FAST_RPC` / `BROWSER_READY_TTL_S` | `true` / `600` | Ready cache and inline commands |
| `BROWSER_SMART_WAITS` | `true` | Event waits instead of fixed sleeps |
| `BROWSER_COMPACT_SNAPSHOT` | `true` | Element tree only in tool output |
| `BROWSER_FAST_LOOP` | `true` | `browser_task` tool |
| `BROWSER_FAST_MODEL` / `BROWSER_ESCALATE_MODEL` | `OPENAI_SMALL_MODEL`, then `OPENAI_MODEL` / `OPENAI_MODEL` | Planner model / model after repeated failures |
| `BROWSER_FAST_MAX_STEPS` | `12` | Planner rounds per `browser_task` |
| `BROWSER_REPLAY` | `true` | Save and replay runs (`browser_replay`) |
| `BROWSER_TEACH_DOM` | `true` | DOM recording while teaching a skill |
| `BROWSER_DIRECT_DOWNLOADS` | `true` | `browser_download` and direct file fetches |
| `BROWSER_PREWARM` | `computer` | `computer`, `warm` or `off` |
| `BROWSER_TIMING_LOGS` | `true` | Per-command timing lines |

## Providers and costs

Prices as published by the providers in October 2026; check before buying.

| Option | What it fixes | Cost |
| --- | --- | --- |
| Tavily (already configured) | Search first; page text with results; Extract for blocked pages | 1,000 free credits/month, then about $0.005–0.008 per credit. Basic search 1 credit; Extract 1 credit per 5 successful URLs (advanced 2) |
| Residential / ISP proxy (`BROWSER_PROXY_URL`, any provider) | Datacenter-IP blocks (the Ajio case), for both the live browser and the server fetch | Usually billed per GB, a few USD per GB; a full page load in the browser is often 2–5 MB |
| Steel (`steel`) | A hosted browser with its own network; scrape stays server-side | Free $30 one-time credits; `/scrape` $5 per 1k; proxies $10/GB and need a $10 deposit on the free plan |
| Browserbase (`browserbase`) | A hosted browser over CDP | Free plan 1 browser hour, no proxy; Developer $20/month (100 h, 1 GB proxy) |
| r.jina.ai reader | JS-heavy pages that block the server | Free with rate limits; a key raises them |
| Internet Archive | Last-resort read of a blocked page (dated) | Free |

## Limits

- No code can make a datacenter IP look residential. Sites that block those ranges outright (Ajio was blocked
  intermittently from the test box) need the proxy, Tavily or a hosted browser, and even those are not guaranteed.
- Read-only copies (Tavily, reader, archive) cannot log in, add to cart or fill forms; the agent says so and the
  user can take control on the live screen.
- Archive copies can be old; prices and stock must be labelled with the copy's date.
- Interactive CAPTCHAs are never solved by the agent.
- A proxy URL and the proxy password reach the user's own sandbox (the browser needs them), so code the user runs
  on their computer could read them. Use a proxy account dedicated to Checkme with a spending cap. Steel's key never
  leaves the API; Browserbase is used through a per-session connect URL.
- A proxy in another country makes the IP disagree with the en-IN / Asia/Kolkata identity; choose Indian exit IPs.
