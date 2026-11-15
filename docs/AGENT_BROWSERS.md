# A separate browser per agent

By default every agent drives the one shared browser on the cloud computer. A site you sign into by hand there is
open to all of them. An agent can instead have **its own browser**: a separate Chrome profile on the same computer,
with its own cookies and sign-ins.

## What you see

In an agent's **About** panel, under **Browser**:

- **Shared browser** (the default): the agent uses the cloud computer's shared browser and any site signed in there.
- **Its own browser**: the agent starts signed out of every site. What you or it signs into in its browser stays
  there, and sites signed into in the shared browser stay closed to it.
- With its own browser, the panel says whether its signed-in state has been saved, and has **Sign out of everything**.
  That asks first, then starts the agent on a new, empty profile. It can't be undone.

If an agent with its own browser needs you to sign in, it asks as usual. Its browser is a separate window on the cloud
computer's live screen.

## How it works

- **Setting.** `workspace_agents.browser` is `{mode: "shared" | "own", epoch}`. `app/agent_browsers/service.py`
  turns it into the daemon's name for the profile, `a<agent id>-<epoch>`, or `""` for shared.
- **Per run.** `run_deep_agent` sets `BROWSER_SPACE` for the turn (chat, group, background task, nested teammate). A
  teammate on the shared browser inside another agent's turn goes back to shared.
- **Every browser command.** `daytona_box._browser_rpc` adds `space` to each command (`with_space`). Commands about
  the whole computer don't get it: the screen (`desktop_act`, `status`, `wake`), recording a skill, search result
  pages, and remote fetch.
- **The daemon** (`computer_daemon.py`, version 46):
  - Each space is its own persistent Chrome profile under `<browser home>/spaces/<space>`, launched with the same
    options as the shared browser.
  - A space only sees its own tabs. A command that names another browser's tab is answered as if that tab were
    closed, so the API picks one of the agent's own.
  - At most three agent browsers are open at once. Opening a fourth closes the one used longest ago, and its profile
    stays on disk, so it reopens signed in.
  - Space names are checked (`[A-Za-z0-9_-]`, at most 64 characters) because they become folder names.
- **Tabs per chat.** `browser_tabs` keeps one chat-to-tab map per browser, so a chat's tab is always in the right
  profile.
- **Saved sign-ins.**
  - Cookies of an agent's browser are saved encrypted in `agent_browser_profiles` (one row per agent, tied to its
    current space). They are saved after page loads (at most every 5 minutes) and soon after a sign-in.
  - When an agent's browser starts on an empty profile (a new or recycled computer), the daemon says so once and
    the API imports the saved cookies.
  - Sign out of everything moves to the next epoch and deletes the saved row, so old cookies are never restored.
- **API.**
  - `GET /api/agent-browsers/{agent_id}` returns `{mode, saved_cookies, saved_at}`.
  - `PUT /api/agent-browsers/{agent_id}` with `{mode}` sets the mode.
  - `POST /api/agent-browsers/{agent_id}/reset` signs it out of everything (only with its own browser).
  - Agents include `browser: {mode}` in `GET /api/agents`.

Limits:

- **The desktop app** still uses its one Lumio browser profile on your computer for every agent. Separate profiles
  there need a desktop app release.
- **Screen-level actions** (`computer_act`) click whatever is on the live screen, and **teaching a skill** records
  in the shared browser.
- After Sign out of everything, the old profile's folder stays on the cloud computer until it is recycled. No agent
  uses it again.
- Each open agent browser is another Chrome process on the cloud computer, which is why at most three stay open.

Tests: `apps/api/tests/test_agent_browsers.py`, and `test_agent_browsers_daemon.py`, which runs the real daemon on
Chrome where Playwright is installed (it is skipped in CI).
