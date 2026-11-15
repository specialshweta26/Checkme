# Plan cards: approve a long task before it runs

When an agent moves work to the background from a chat, it shows a plan first. Nothing runs until you start it.

## What you see

Under the agent's reply, a **Plan ready** card shows:

- **Steps**, in order. **Edit steps** lets you reword, reorder, add (up to 12) or remove them.
- **Uses**: the tools the agent expects to use ("the browser", "web search", "Gmail").
- **Asks you first**: the actions it will need your yes for, such as sending, paying, pushing or merging. Anything sensitive still asks, even if it isn't listed here.
- **Likely cost**: your average background spend per task over the last 14 days. It is hidden until there is priced history.

**Start**, or a plain "go ahead" / "yes" in the chat, runs the plan. **Don't run it**, or "no" / "stop", cancels it. Any other message goes to the agent as normal, and the plan keeps waiting.

## How it works

- `start_background_task` and `start_coding_job` take `steps`, `tools` and `approvals`. From a chat turn with steps, the job is created as `planned` (`apps/api/app/jobs/plans.py`). The worker only claims `queued` jobs, so nothing runs and nothing is spent.
- `POST /api/jobs/{id}/plan` with `{steps?}` approves the plan:
  - The final steps are appended to the job's task, with the instruction to follow them, keep the checklist current, and re-plan with a one-line reason if a step fails.
  - The steps become the timeline checklist.
  - The job is queued.
- Jobs without steps, jobs started inside other jobs (delegation), schedules and watches start immediately, as before.
- The plan is stored in `agent_jobs.meta.plan` and returned as `plan` by the jobs API.

Tests: `apps/api/tests/test_plans.py`.
