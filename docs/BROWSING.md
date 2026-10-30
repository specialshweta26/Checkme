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
| `browser_task` | A small fast model plans a batch of actions per call from the tree, the daemon runs it; after two failed rounds it escalates to the main model, then returns `fallback` with the page so the main agent continues with `browser_act`. Values (email, password, OTP) travel as `{{name}}` placeholders: the planner sees only names, code substitutes them right before typing; results and logs are scrubbed. Never clicks Pay/Buy/Place order/Book/Send/Transfer/Delete unless `allow_final_action`. | `BROWSER_FAST_LOOP`, `BROWSER_FAST_MODEL` (default `OPENAI_MODEL`), `BROWSER_ESCALATE_MODEL` (default `OPENAI_MODEL`), `BROWSER_FAST_MAX_STEPS` (12) |
| Saved runs + replay | A finished `browser_task` is saved in `browser_traces` (per user and site) as selector steps: test id → stable id → name → aria-label → label → placeholder → role+name → href → text, never refs or coordinates. Secret fields (password, OTP, CVV…) keep only a parameter name. The next same task replays with no model call per step; each step is verified (target found, typed value stuck; secret fields only non-empty) and a mismatch stops and hands the page to the model. `browser_replay(name / url / skill_id, values)` replays explicitly. | `BROWSER_REPLAY` |
| Teach recordings | While a skill is taught, a DOM recorder in the page browser logs clicks, field edits (never secret values), selects and Enter with the same stable descriptors. On finish they become a replay trace for that skill and its instructions say to call `browser_replay(skill_id=…)` first. Only starts when the page browser is already running. The screen/xinput recording is unchanged. | `BROWSER_TEACH_DOM` |
| Direct downloads | `browser_download(url or ref)` fetches the file with the browser context's cookies (`context.request`, no click, no dialog); a `download` step on a file-looking link does the same first. HTML answers or errors fall back to clicking and capturing the Playwright download. | `BROWSER_DIRECT_DOWNLOADS` |
| Warm browser | A turn classified as computer work (§7.7) starts the daemon and restores the saved cookies (`browser_profiles`) in the background while the model plans. After a step that typed into a secret field succeeds, cookies are saved soon (4 s later) so the next session stays signed in. | `BROWSER_PREWARM` (`computer` boots if needed, `warm` only when already running, `off`) |
| Fewer round trips | A daemon proven healthy for this sandbox, release and settings is trusted for `BROWSER_READY_TTL_S` (600 s): no Playwright import probe or health curl per command (the helper was effectively re-checked on every call). Small payloads go inline in the curl command (one exec instead of upload + exec); the screenshot is cached in the background. Any delivery failure drops the cache entry. | `BROWSER_FAST_RPC`, `BROWSER_READY_TTL_S` |
| Event waits | After each action: DOMContentLoaded when the URL changed, then DOM quiet (no mutations for 80–250 ms, capped at 0.9–2.5 s), instead of fixed pauses. A page is ready once quiet and it has enough text **or** a visible form field, so sign-in and OTP pages no longer wait the full settle and scroll time. | `BROWSER_SMART_WAITS` |
| Timing logs | `browser.timing command=… steps=<kinds> ensure_ms rpc_ms total_ms` per command, `browser.task step=… plan_ms act_ms actions ok` per fast-loop round, `browser.replay … ms`. Kinds and times only, never typed values. | `BROWSER_TIMING_LOGS` |

The live screen still follows the active tab: every path drives the same tabs as `browser_act`.

Local measurement (headless Chromium on the dev box, a local sign-in form whose submit waits 150 ms for an API and
a dashboard that renders 300 ms after load; median of 5, not E2B): navigate to the sign-in page 15.5 s → 0.64 s;
fill email + password + click Sign in 1.79 s → 0.70 s (and the new page was rendered in the result, it was not
before); open the dashboard 2.3–3.2 s → 0.65 s; a direct file download 4 ms. Not measured: E2B round trips and
model latency.

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
| `BROWSER_FAST_MODEL` / `BROWSER_ESCALATE_MODEL` | `OPENAI_MODEL` / `OPENAI_MODEL` | Planner model / model after repeated failures |
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
