# Routines

A routine is a scheduled task that belongs to one agent ("every weekday at 9, brief me on my inbox"). Each agent has
its own routines, and every run is recorded, so you can see how routines are doing and investigate the failures.

## Where to find them

| Place | What it shows |
| --- | --- |
| **Clock button** in a chat's header (top bar on phones) | Only that agent's routines: active, succeeded and failed counts for the last 30 days, a box to schedule something for this agent, and each routine's actions. Nothing from other agents. In a group chat, or with no agent open, the button links to the Routines page. |
| **Profile → Routines** (`/routines`) | Every agent's routines and the dashboard (below). `/tasks` redirects here. |

### The dashboard

- **Tiles** for the chosen period (7, 30 or 90 days): runs, succeeded, failed, waiting for approval, success rate and
  active routines. Clicking a tile opens the run history filtered to it, so **Failed** lists the failed runs.
- **Last failure** banner: the most recent failed run and its error. **Investigate** opens the failed runs.
- **Agent cards**: routines, successes and failures per agent. Click one to see only that agent.
- **Routines** tab: routines grouped by agent. Open one to see its instruction and last result, and to **Run now**,
  **Pause**/**Resume**, **Open result** or **Delete** it.
- **Run history** tab: each run with its outcome, time, agent, duration and whether it ran on schedule or was started
  by you. Opening a failed run shows **What went wrong** (the error), what the agent said, and **Open conversation**
  (the chat the run happened in).

The page's URL holds the view (`?agent=<id>&tab=history&status=failed&days=7`), so a shared link opens the same filtered
view.

## Results on web and desktop

The desktop app shows the same website (it loads the web app), so a routine's result is stored once on the server and
is the same everywhere. It used to look different between devices because:

1. **The run didn't count as activity.** A successful run added its messages without updating the conversation's
   `updated_at`, so the server's "latest conversation" for the agent was still the last one you chatted in. Runs now
   update it. Every run (succeeded, failed or waiting) also marks the agent unread.
2. **Each device remembered its own conversation.** The browser kept the last conversation you opened for each agent
   and went back to it. It now also stores *when* you opened it (`<id>@<ms>` in `localStorage`). If another
   conversation for that agent has had activity since, such as a scheduled run's result or a message you sent from the
   other device, that conversation opens instead. So web and desktop open the same conversation (`chooseSession` in
   `apps/web/app/chat/page.tsx`).

A chat that's already open doesn't update live when a run finishes. Reopen the agent, or use **Open result**.

## How it works

| Piece | Where |
| --- | --- |
| Run history table `scheduled_runs` | `apps/api/app/models.py` (`ScheduledRun`), created at startup |
| Recording a run | `record_run` in `apps/api/app/tasks/service.py`, called by `execute_task` on success, failure and approval holds |
| Run list and dashboard | `list_runs`, `summary`; `GET /api/tasks/runs?agent_id=&task_id=&status=&days=`, `GET /api/tasks/summary?agent_id=&days=` |
| Agent panel, rows, clock button | `apps/web/app/components/Routines.tsx` (`AgentRoutinesDrawer`, `RoutineRow`, `RunRow`, `RoutinesButton`) |
| Page | `apps/web/app/routines/page.tsx` |
| Tests | `apps/api/tests/test_routines.py` |

- **Outcomes.** `succeeded`, `failed` (the run raised an error, which is recorded as shown to the user) and
  `needs_approval` (the run paused a background job for an action you must approve). A waiting run takes its job's
  outcome when the job ends: done means succeeded, failed or cancelled means failed.
- **Earlier runs.** Routines that ran before run history existed get one row rebuilt from their last run (marked
  "Before run history"), so the dashboard isn't empty after an upgrade.
