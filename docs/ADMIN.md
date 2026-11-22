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
