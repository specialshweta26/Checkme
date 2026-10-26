# Deploys from the Coder

A coding agent (focus `coding`) can deploy the repo it is working on, check the deploy, read logs and roll back.
Code: `apps/api/app/agent/deploy.py` (tools and commands), `apps/api/app/connectors/deploy_routes.py` (credentials),
`apps/web/app/components/DeployPlatforms.tsx` (Connectors page). Tests: `apps/api/tests/test_deploy.py` (sandbox and CLIs mocked).

## Rules

- **Deploy and rollback always show an approval card**, even when the user has turned on auto-confirm, and even from
  scheduled or delegated runs. The card shows the platform, repo, directory, target, the exact CLI command, and the
  *names* of the credentials it will use. It runs only if that exact action is approved (same platform, repo,
  directory and params). Approving one deploy never lets a different one through.
- **A deploy connector comes first.** If the user has a Render, Vercel, … MCP or Composio connector, `deploy` returns
  `use_connector`, and the agent uses that connector's tools. Deploy-changing tools on those connectors (`create_deploy`,
  `rollback`, `restart`, `update_env…`, `scale`, …) are forced behind a card by `git_write_gate` → `deploy.is_deploy_write`.
  Their reads (`list_*`, `get_*`) are not gated. The CLI is used when there is no connector, or when the connector
  can't do it (`use_cli=true`).
- **Credentials never reach the model.** The user enters them on **Connectors → Deploy platforms**, never in chat. They
  are stored Fernet-encrypted in `deploy_credentials` (one row per user and platform). They are read only inside
  `run_action` / `read_action`, passed to the sandbox as exec **env**, and scrubbed from all output (whole values, plus
  long pieces of JSON keys). Commands only contain `$ENV` references. The list API returns field *names* only.
- **Missing credentials** → `status: blocked`, the missing env names, and a link to `/connectors?deploy=<platform>`.
  The agent says exactly what is missing, then calls `ask_user(waiting_for='deploy_<platform>')`. When the user saves
  the credential, jobs waiting on `deploy_<platform>` are resumed (`resume_waiting_for`) and the worker is woken.
- `deploy_status` and `deploy_logs` are read-only, so they have no card.

## Flow

1. `detect_deploy_targets(repo)` runs one sandbox probe. It lists `render.yaml`, `vercel.json` / `.vercel/project.json`,
   `fly.toml`, `netlify.toml`, `railway.json|toml`, `serverless.yml`, SAM `template.yaml` + `samconfig.toml`, `cdk.json`,
   App Engine `app.yaml`, `azure.yaml` and `Dockerfile` (depth ≤ 3, no `node_modules`). For each platform it returns the
   route: `connector`, `cli` (credential saved) or `connect` (link). A Dockerfile offers the container platforms
   (Fly, Cloud Run, Azure Container Apps, Railway, Render).
2. Usually after the PR is merged (the planner brief puts the deploy after the merge), the agent calls
   `deploy(platform, repo, params, cwd)`, which creates the approval card. Once approved, the action replays through
   `git_tools.run_confirmed("deploy_action")`, which calls `deploy.run_action`.
3. `run_action` installs the CLI if it is missing (into `~/.local/bin`, idempotent), logs in from env, runs the command
   in `~/work/<repo>/<cwd>`, and records a `deploy` job event (🚀 in the timeline, with the live URL and the output).
   On failure it returns `next`: read the output, fix it, and propose again.
4. `deploy_status` / `deploy_logs` check the deploy. `deploy_rollback` creates a card, the same as a deploy.

`coding_workspace_status` reports `deploy {ready, connect_url}` and a "can deploy to …" line, so a new Coder's first
reply says which platforms are ready.

## Platforms

| Platform | CLI | Required | Optional | Get a token | Notes |
|---|---|---|---|---|---|
| Render | render | `RENDER_API_KEY` | — | https://dashboard.render.com/u/settings#api-keys | Pass `service=srv-…` (`deploy_status` with no service lists them). Rollback uses the API with a `deployment` id. |
| Vercel | vercel | `VERCEL_TOKEN` | `VERCEL_ORG_ID`, `VERCEL_PROJECT_ID` | https://vercel.com/account/tokens | `production=true` for `--prod`. |
| Fly.io | flyctl | `FLY_API_TOKEN` | — | https://fly.io/user/personal_access_tokens | Rollback redeploys an earlier image: `image=registry.fly.io/<app>:<tag>`. |
| Netlify | netlify | `NETLIFY_AUTH_TOKEN`, `NETLIFY_SITE_ID` | — | https://app.netlify.com/user/applications#personal-access-tokens | Rollback restores a deploy id. |
| Railway | railway | `RAILWAY_TOKEN` | — | https://docs.railway.com/guides/public-api#project-token | **No CLI rollback**: the tool explains how to use the dashboard. |
| AWS | aws (+ serverless / sam / cdk via npx/pip) | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` | `AWS_SESSION_TOKEN` | https://console.aws.amazon.com/iam/home#/security_credentials | `flavor=serverless|sam|cdk`. Rollback = CloudFormation `rollback-stack`, which only works for `UPDATE_FAILED`. |
| Google Cloud | gcloud | `GCP_SERVICE_ACCOUNT_JSON`, `CLOUDSDK_CORE_PROJECT` | `CLOUDSDK_RUN_REGION` | https://console.cloud.google.com/iam-admin/serviceaccounts | App Engine by default; `flavor=run service=<name>` for Cloud Run. The key is written to a temp file and removed on exit. |
| Azure | az | `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | — | https://learn.microsoft.com/cli/azure/azure-cli-sp-tutorial-1 | Container Apps from source: `app=<name> group=<rg>`. Rollback moves traffic to a `version` (revision). |

Allowed params: `service, app, stack, stage, region, group, ref, deployment, version, image, flavor, production`.
Values must match `^[A-Za-z0-9][A-Za-z0-9._:/@=+-]{0,199}$`, with no `..`. `cwd` is a relative path inside the repo.

## API

| Method | Path | Body | Notes |
|---|---|---|---|
| GET | `/api/connectors/deploy` | — | Platforms, their fields, saved field **names**, `connected` |
| PUT | `/api/connectors/deploy/{platform}` | `{values: {ENV: value}}` | Unknown fields → 400. A blank value keeps the saved one. All required fields must be set. Resumes waiting jobs. |
| DELETE | `/api/connectors/deploy/{platform}` | — | Forget the credential |

## Caveats

- CLIs are installed on demand. The install and deploy commands are best-effort per CLI version, and only the mocked tests cover them.
- Use least-privilege tokens (a project or site token, a scoped IAM user or service principal) where the platform supports them.
- MCP connector deploy tools are gated by name pattern. A connector with unusual tool names may need its own destructive flag.
