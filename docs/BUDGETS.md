# Budgets and limits per agent

Each agent can have its own spending caps and a time limit for background work. Spend is counted per agent, including when it answers as a teammate in a group.

## What you see

In an agent's **About** panel, under **Limits**:

- **Meters** for what it has used. Dollar meters (today and this month) appear only when the server has model prices; tokens today always shows. A bar appears once you set a cap. It turns amber at 80% and red when over.
- **Daily budget** and **Monthly budget**, in dollars. These need model prices on the server (`MODEL_PRICES`); without them the panel says so.
- **Tokens per day**. This works without model prices.
- **Time per task run**, in minutes.

Leave a field empty, or set it to 0, for no limit. Days are UTC.

When an agent is over a budget or its token limit:

- **Chat** (one-to-one or in a group): it replies with one line saying it reached its limit and when the limit resets. No model is called.
- **Background tasks** pause before they start, with a question: **Run it anyway today** or **Leave it for now**. A task that is already running is checked about every 30 seconds and pauses the same way. The question appears in the chat and on the **Approvals** page. "Run it anyway today" lets that one task go past the limit until the day ends.

When a task run reaches **Time per task run**, it stops and asks **Keep going** (another run of the same length) or **Stop it**.

## How it works

- **Counting.**
  - `run_deep_agent` sets the agent on the meter (`app/agent/meter.py`, `AGENT` / `as_agent`). A teammate's nested run sets its own agent, so its use counts as its own.
  - Model calls and searches are added per agent in memory and written to `agent_usage_daily` (agent, day, model) every few seconds, alongside the per-user `usage_daily`.
  - `agent_usage()` reads stored and not-yet-written use together.
- **Limits.** Limits are stored in `workspace_agents.limits` (`{daily_usd, monthly_usd, daily_tokens, task_minutes}`) and validated in `app/budgets/service.py`. A daily budget can't be more than the monthly one.
- **Chat.** `run_deep_agent` checks the budget before the turn when it isn't a background job (`_budget_block`).
- **Tasks.**
  - Before a run, the job runner (`jobs/runner.py`, `_over_budget`) pauses the job as `waiting_input` with a question whose `waiting_for` is `budget`.
  - During a run, the worker's `_watch_limits` stops it with a `limit:` reason. The runner then turns that stop into the same kind of pause (`budget` or `time_limit`), not a cancel.
  - Answers go through `jobs.answer`, which hands budget and time-limit questions to `budgets.answer`:
    - **Run it anyway today** sets `budget_ok_day`.
    - **Keep going** requeues with an instruction to continue from the checklist.
    - Any other answer cancels the task.
- **API.**
  - `GET /api/budgets` returns every agent's spend and limits.
  - `GET /api/budgets/{agent_id}` returns one agent's.
  - `PUT /api/budgets/{agent_id}` with `{daily_usd, monthly_usd, daily_tokens, task_minutes}` sets the limits.
  - Agents include `limits` in `GET /api/agents`.

Limits:

- A run that pauses mid-way starts its next run from the job's saved history and checklist, so the work since the last saved step is redone.
- Dollar budgets need model prices. Without them, use the token limit.

Tests: `apps/api/tests/test_budgets.py`.
