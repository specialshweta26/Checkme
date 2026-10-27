# GitHub in Checkme (Composio)

Checkme reaches GitHub through the user's **Composio GitHub** connection. There is no Checkme GitHub App and no
`GITHUB_APP_*` setting. Code: `apps/api/app/connectors/github.py` (tools, proxy, accounts),
`connectors/github_status.py` (status + connect routes), `agent/git_tools.py` (sandbox git).

## Setup

1. Set `COMPOSIO_API_KEY` on **checkme-api** and **checkme-worker** (Render → Environment).
2. In Checkme open **Connectors → GitHub (Composio) → Connect GitHub** and approve GitHub's OAuth screen. The
   `composio-github` connector appears for your agents. (An agent can also start this with `composio`
   `connect_app` and toolkit `github`.)
3. Optional, for real `git clone` / `git push` from the computer (see "Transports" below):
   - Composio dashboard → **Auth Configs → Create → GitHub → OAuth2**, with **your own** GitHub OAuth App client id
     and secret (not Composio-managed auth). Scopes: `repo`, `read:org`, `workflow` (if CI files are edited).
   - Composio dashboard → **Settings → Project Settings → Project Configuration** → turn **off** "Mask Connected
     Account Secrets".
   - Put the auth config id (`ac_...`) in `COMPOSIO_GITHUB_AUTH_CONFIG_ID` and reconnect GitHub in Checkme.

`GET /api/connectors/github/status` returns `{configured, connected, login, git_transport, managed_auth}`;
`git_transport` is `git` when a raw token is available, else `api`.

## How calls authenticate

- **GitHub REST / GraphQL** (all `composio-github` tools, PR creation, reviews, merges): Composio **Proxy
  Execute** (`POST https://backend.composio.dev/api/v3.1/tools/execute/proxy`) with the user's connected account.
  Composio injects the GitHub credential; Checkme never holds it for these calls.
- **Sandbox git**, two transports:
  - `git` (token unmasked): `GET /api/v3.1/connected_accounts/{id}` gives the OAuth token. It is passed only as the
    `CHECKME_GIT_TOKEN` exec env var for one command and read by a one-shot `credential.helper`. It is never in the
    command, the remote URL, `.git/config`, logs, the database or model output (output is scrubbed).
  - `api` (Composio masks the token, the default for managed auth): public repos clone anonymously; private repos
    are unpacked from a tarball whose short-lived URL is fetched through the proxy and passed as a hidden env var
    (snapshot: no history). `push_branch` bundles the committed changes since the GitHub base on the computer, and
    the server commits them with the git data API (blobs → tree → commit → non-force ref update pinned to the base
    sha). Local commits are squashed into one GitHub commit; submodules are not supported.

## Git actions and approvals

| Action | Tool | Approval card |
| --- | --- | --- |
| clone / fetch / branch | `git_checkout` | no |
| inspect, diff, tests, commit (agent/* only) | `git_inspect`, `git_diff`, `run_tests`, `run_checks`, `git_commit` | no |
| read repos, branches, trees, files, code search, commits, compare, blame, PRs, issues, merge status | `composio-github` reads, `check_merge` | no |
| comment, review, request reviewers | `comment_pull_request`, `create_review`, `request_reviewers` | no |
| push (agent/* or a named open, non-fork PR's branch) | `push_branch`, `push_files` | **yes**, always |
| open / update a PR | `push_branch(open_pr)`, `create_pull_request`, `update_pull_request` | **yes**, always |
| merge | `merge_pull_request(head_sha=...)` | **yes**, always, pinned to the reviewed head sha |

Git writes need a card even in `auto_confirm` runs (schedules, background jobs). A merge approval covers only that
repo, PR number, method, `delete_branch` and `head_sha`; if the branch moved, the merge is refused. Pushes are never
forced. Other GitHub routes (GitHub MCP, `gh` on the computer, generic Composio `GITHUB_*` tools) still work as
fallbacks and their writes go through the same gate.

## Legacy

Old `github_app` connector rows are routed to Composio, and the built-in `github-app` row is disabled. The
`github_installations` table is kept (unused) so no data is dropped.
