# Admin console

`/admin` in the web app (`apps/web/app/admin/page.tsx`), API under `/api/admin/*` (`apps/api/app/admin/routes.py`).

Only people whose email is in `ADMIN_EMAILS` (comma-separated, on `checkme-api`) can open it. Everyone else gets a
404 from the API, and the menu item only shows for admins.

## What it shows

- **Overview:** people, new this week, active today, tokens today, the sign-up mode and the daily cap.
- **Invites:** while `SIGNUP_MODE=invite`, an email or a whole domain added here can create an account, on top of
  `SIGNUP_ALLOW`. "Email them" sends an invitation with a link to `/signup` (it needs mail, docs/AUTH.md).
- **People:** the latest 200, searchable. For each: provider, joined, last sign-in, today's tokens and cost, whether
  they're at the cap or exempt from it.
- **Suspend / Restore:** a suspended person is signed out everywhere. Every request of theirs gets 403. Email, Google,
  Microsoft and SSO sign-in are refused. Their tasks and background work stop, because they count as over the daily
  cap. Restore clears it. You can't suspend yourself.

## Reply times

Every chat reply saves where its time went in its `meta.timing`:
- `reply_ms`: the whole reply, from the request arriving to the reply being saved;
- the agent's stages and its model time and number of model calls;
- each connector call, with how long its first answer took and how many pages it read (`calls`).

`GET /api/admin/latency?days=1` adds these up. It returns how many replies took longer than `REPLY_TARGET_S` (45 by default), the median, the 95th percentile and the slowest reply. For the replies over the target, it splits their time into model, connectors and other tools, which shows what to make faster first. It also lists the 20 slowest replies with their three slowest connector calls, each tool the agent called (how many times, how long in all), the checks before connector calls (`prep_ms`), the finish and fact checks, and the time nothing accounts for (`other_ms`). It never returns message text, only the person's email and the time.

The admin page shows this as **Reply times**, for the last 24 hours, 7 days or 30 days. A turn that was stopped (by the person, or by the app because it went quiet) or that failed keeps its timing up to that point, marked "stopped" or "failed". Replies saved before this existed have no timing and are left out. The chat API strips `timing` from the messages it sends to the browser.

## Test scenarios

The admin console's **Test scenarios** panel runs about 20 everyday questions through the real agent, as you, one
after another, each in its own chat titled "Test: …". The questions cover:
- instant replies;
- data connectors: one figure, a trend, by category, top products, brand and period comparisons, stock, a report, and a follow-up in the same chat;
- mail and calendar;
- the web;
- several connectors in one question;
- the workspace.

Tick the ones to run and add your own questions, one per line (up to 10). Each result shows:
- the reply time;
- model time and calls, connectors, tools and the rest;
- the tools used;
- whether it waited for an approval;
- a link to open that chat.

The replies also count in Reply times.

- **Reads only:** nothing is confirmed for you, so anything that would send, change or delete stays an approval card in that chat.
- **Cost:** it uses your connectors and tokens, like asking the questions yourself.
- **Limits:** one run at a time. "Stop after this question" ends it. A question may take `SCENARIO_TIMEOUT_S` (300) at most.
- **Where they live:** the questions are in `apps/api/app/admin/scenarios.json`.
- **Restarts:** the run lives in the API process, so a restart or deploy ends it.

## Audit log

The `audit_events` table records:

- sign-ups, sign-ins (and how), mailbox connections
- password resets and changes
- suspensions and restores
- invites added and removed

Each row holds who it concerns, who did it, a target (an email or domain), small details and the IP. It never holds
passwords, tokens or message text.

- Admins: the last 7 days in the console, and **Export CSV** for 90 days (`/api/admin/audit?days=90&fmt=csv`, up to
  5,000 rows). Filter with `action=` or `user=<id>`.
- Everyone: **Your data → Your activity** downloads their own log (`/api/me/audit?fmt=csv`).
- CSV cells that start with `=`, `+`, `-` or `@` are quoted, so opening the file in a spreadsheet can't run a formula.
