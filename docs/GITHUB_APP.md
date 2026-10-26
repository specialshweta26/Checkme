# Checkme GitHub App: setup

Checkme talks to GitHub through its own GitHub App. The App signs a short JWT with its private key, then
mints an **installation token per call**. Each token is scoped to the one repo the call touches and to the
permissions that call needs (read for reads, `contents`/`pull_requests: write` for writes). Tokens live in
memory for under an hour. They are never written to the database, `.git/config`, logs, or model-visible output.

If the App is not configured, everything still works the old way: the Coder falls back to a GitHub MCP
connector, Composio GitHub, or `gh`/git credentials already on the sandbox.

## 1. Create the App (once, by the Checkme admin)

GitHub → **Settings → Developer settings → GitHub Apps → New GitHub App**. For an org-owned App, use
**Org settings → Developer settings** instead.

| Field | Value |
| --- | --- |
| GitHub App name | `Checkme` (or `Checkme-<you>`; it must be globally unique). The URL slug is your `GITHUB_APP_SLUG`. |
| Homepage URL | `https://checkme-web-kw68.onrender.com` |
| Callback URL | `https://checkme-web-kw68.onrender.com/api/connectors/github/callback` |
| Request user authorization (OAuth) during installation | **On**. Checkme uses the code to prove the installer can see the installation. |
| Expire user authorization tokens | On (the user token is used once and thrown away). |
| Setup URL | Leave blank. GitHub disables it when the OAuth option above is on, and sends everyone to the Callback URL. |
| Redirect on update | On |
| Webhook → Active | On |
| Webhook URL | `https://checkme-web-kw68.onrender.com/api/connectors/github/webhook` |
| Webhook secret | A long random string: `openssl rand -hex 32`. This is `GITHUB_APP_WEBHOOK_SECRET`. |
| Where can this App be installed? | "Any account" if other people's orgs will use it, otherwise "Only on this account". |

**Repository permissions**

| Permission | Access | Why |
| --- | --- | --- |
| Contents | Read and write | Clone, read files, and push `agent/*` branches |
| Pull requests | Read and write | List, open, review-comment, and merge (merge only after your approval) |
| Issues | Read and write | Nightly triage and comments |
| Metadata | Read (mandatory) | Repo listing |
| Checks | Read (optional) | Lets reviewers see CI state |
| Actions | Read (optional) | Same |

Account permissions: none. Subscribe to the **Installation** and **Installation repositories** events.

Then:

1. Note the **App ID** (`GITHUB_APP_ID`) and **Client ID** (`GITHUB_APP_CLIENT_ID`).
2. **Generate a new client secret** and save it as `GITHUB_APP_CLIENT_SECRET`.
3. **Generate a private key**. GitHub downloads a `.pem` file, which goes in `GITHUB_APP_PRIVATE_KEY`. Render
   accepts multi-line values. You can also paste it with `\n` escapes, or as `base64 -w0 key.pem`.

## 2. Set the Render env vars (service **checkme-api**)

`render.yaml` declares these as `sync: false`. Set them in the dashboard under
**checkme-api → Environment**, then redeploy:

```
GITHUB_APP_ID=123456
GITHUB_APP_SLUG=checkme
GITHUB_APP_CLIENT_ID=Iv23li...
GITHUB_APP_CLIENT_SECRET=...
GITHUB_APP_PRIVATE_KEY=-----BEGIN RSA PRIVATE KEY-----...
GITHUB_APP_WEBHOOK_SECRET=...
```

`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` and `GITHUB_APP_SLUG` turn the App on. The client id and secret
are required to finish an install: without them the callback refuses, because it can't verify the installer.

## 3. Install it (each user)

Open **Connectors → GitHub → Install GitHub App**, or go to
`https://checkme-web-kw68.onrender.com/api/connectors/github/install`. Pick the account or org and the repos,
then approve. GitHub sends you back to `/connectors?github=connected`. The installation is saved in
`github_installations`, and a `github-app` connector appears for your agents.

If you aren't an org owner, GitHub sends the owner an install request (`setup_action=request`). Come back
once they approve it.

## Safety rules enforced in code

- Reads (`list_repos`, `get_file`, `list_prs`, `get_pull_request`, `list_issues`) need no approval.
- `create_pull_request`, `push_files` and `merge_pull_request` are destructive. They always show an approval
  card, including in scheduled runs and background jobs, where `auto_confirm` is ignored for git writes.
- Pushes, whether through `push_files` or the sandbox `push_branch`, only go to branches named `agent/...`.
- Merging is never automatic. A reviewer agent can only propose a merge, and you click approve.
- The same git-write gate covers other GitHub connectors (MCP, Composio): any tool whose name looks like
  merge, push, create pull request, commit, or update file waits for approval.
