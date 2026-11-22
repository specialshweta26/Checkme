# Rolling Lumio out to people

Two server settings control who gets in and how much each person can spend. Set them on `checkme-api` **and** the
worker (`checkme-worker-kw68`) in Render.

## Sign-ups

| `SIGNUP_MODE` | Who can create an account |
| --- | --- |
| `open` (default) | Anyone, with Google, Microsoft or email |
| `invite` | Only the emails and domains in `SIGNUP_ALLOW` |
| `closed` | No one new |

- `SIGNUP_ALLOW` is a comma-separated list, for example `asha@gmail.com, acme.com`. A domain matches exactly:
  `acme.com` doesn't let in `eu.acme.com`.
- Only creating an account is gated. Everyone who already has one keeps signing in, whatever the mode.
- People who sign in through an SSO connection aren't gated: the operator set that connection up for their company.
- A confirmation link sent while sign-ups were open creates no account once the address is no longer allowed.
- A misspelt mode behaves like `invite`, so a typo never opens sign-ups to everyone.
- The sign-up page reads the mode from `/api/auth/status` (`"signup"`) and shows the matching notice.

A gradual rollout: start with `invite` and the first users' addresses, add more as you go, then switch to `open`.

## Daily cap

Each person's model use in a UTC day is capped, across chat, background tasks, inbox triage and memory, all together.

- `DAILY_USER_TOKEN_BUDGET`: input plus output tokens a day, for example `2000000`.
- `DAILY_USER_BUDGET_USD`: dollars a day, for example `3`. It needs `MODEL_PRICES` to price the models.
- `BUDGET_EXEMPT`: comma-separated emails with no cap (the owner, the team).

0 (the default) means no cap. When a person reaches it:

- chat answers "You've reached today's usage limit for Lumio. It resets at midnight UTC." (HTTP 429)
- a background task doesn't start, and says why in its chat
- inbox triage falls back to rules, and memory learning waits for the next day

Use is counted every few seconds, and the check is cached for a minute, so a turn already running can go a little
past the cap. Agent budgets (per agent, set by the user) and `DAILY_BACKGROUND_BUDGET_USD` still apply on top.

## Also set

- Mail for email sign-up (docs/AUTH.md "Mail").
- The web app sends `Referrer-Policy: no-referrer`, so sign-in and reset tokens in a URL never reach another site.
  It also sends `X-Content-Type-Options: nosniff` and, in production, HSTS.
