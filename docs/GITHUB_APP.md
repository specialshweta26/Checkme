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
| Contents | Read and write | Explore (tree, files, code search, history, diffs, blame), clone, push `agent/*` branches (or a PR branch you name), and merge |
| Pull requests | Read and write | List, open, update, review (inline comments), request reviewers, and merge (merge only after your approval) |
| Issues | Read and write | Nightly triage and comments |
| Metadata | Read (mandatory) | Repo listing |
| Checks | Read (recommended) | `check_merge` and merges refuse failing check runs |
| Commit statuses | Read (recommended) | Same, for status-API CI (e.g. Render, Vercel previews) |
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

- Reads need no approval: `list_repos`, `list_branches`, `get_tree`, `get_file`, `search_code`, `list_commits`,
  `get_commit`, `compare_refs`, `blame_file`, `list_prs`, `get_pull_request`, `get_pr_comments`, `list_issues`,
  `get_issue` and `check_merge`. Without Checks / Commit statuses access, `check_merge` still reports
  mergeability and says the checks could not be read.
- `comment_pull_request`, `create_review` and `request_reviewers` post without a card (they don't change code).
- `create_pull_request`, `update_pull_request`, `push_files` and `merge_pull_request` are destructive. They always show an approval
  card, including in scheduled runs and background jobs, where `auto_confirm` is ignored for git writes.
- Pushes, whether through `push_files` or the sandbox `push_branch`, only go to branches named `agent/...`. The one
  exception: you name an existing open PR and the agent calls `push_branch(..., pr_number=N)`; the head branch is
  verified through the App (open, same repo, not a fork) and the push still waits for your approval.
- Merging is never automatic. The agent asks "Merge it?" after opening a PR; only an explicit yes leads to
  `check_merge` and then `merge_pull_request` (default a merge commit; `squash` / `rebase` and `delete_branch` on
  request), which shows a card with the live mergeability and checks. Conflicts are refused and reported, failing
  checks are refused unless you ask to merge anyway, and the merge is pinned to the reviewed head sha. The approval
  covers only that exact PR, method and branch-deletion choice.
- If a push is blocked because the App isn't installed, the work is saved as patches on the computer
  (`~/work/_patches/<repo>-<branch>/`) and the agent links the install page. Installing the App resumes the job.
- The same git-write gate covers other GitHub connectors (MCP, Composio): any tool whose name looks like
  merge, push, create pull request, commit, or update file waits for approval.


## Deploys

The GitHub App doesn't deploy. Deploys use a platform connector or the platform CLI, with the user's own credentials saved on Connectors → Deploy platforms, and always go through an approval card. See [DEPLOY.md](DEPLOY.md).
