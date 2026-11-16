# Why it did that: run traces for every job

Browser runs have long had a step-by-step history with screenshots (docs/BROWSING.md "Run history"). Every
background job now has one too: chat jobs, delegated sub-tasks, scheduled routines and webhook events.

## What you see

A job's timeline in the chat has a **Why it did that** button. It opens every step of the job and of the jobs it
delegated to, in the order they ran:

- **The action** in plain words ("Opening example.com"), the tool's name, who ran it (when teammates helped), how
  long it took and which try it was.
- **The reason**: what the model said or thought just before calling the tool. Some steps have none, because the
  model called the tool without saying anything.
- **What it sent**: the tool's inputs. Values under keys such as password, token, key, cookie or card are hidden, and
  so are strings that look like API keys or tokens. Long values are cut short.
- **What came back**: a short version of the result, open by default when the step didn't work. A failed step is red
  and shows its error.
- **A screenshot** of the computer after each browser or computer step.
- For a `browser_task` step, a link to that browser run's own step-by-step view (`/runs?run=<id>`).
- **The reply** at the end.

**Only what didn't work** filters the list to failed steps. While the job runs, the view refreshes every few seconds.

## How it works

- `app/jobs/trace.py`:
  - `Tracer` holds the trace of one job turn. `run_job` sets it as `CURRENT` for the turn.
  - `TraceCallback` is a LangChain callback that `run_deep_agent` adds to every agent graph it runs while a tracer is
    set, including teammates asked inside the job. It takes the reasoning from the model's last message before its
    tool calls (thinking blocks and text), and records each tool's start, end or error.
  - When a teammate's tool call is also reported through an outer graph, the call is only kept once (by LangChain's
    run id), under the teammate's name.
  - Steps are written when they end, in the background, as `job_trace_steps` rows. The runner waits for them at the
    end of the turn. A step still open when the turn ends (cut off or stopped) is saved as "Stopped before it
    finished".
  - Browser and computer tools get a screenshot from the same frame the browser run log uses, stored at
    `<user>/job-traces/<job>/<attempt>-<step>.jpg`.
- **Limits per turn**: 300 steps and 60 screenshots. Reasoning is kept up to 1,200 characters, results up to 1,500,
  and each input value up to 300.
- **Best effort**: a trace write that fails is logged and skipped, and never fails the job.
- **API**: `GET /api/jobs/{job_id}/trace` returns `{jobs, steps}` for the whole job tree. Another user gets 404.
- **Clean-up**: screenshots older than 30 days are deleted by the daily memory pass (the steps stay, without
  pictures). Deleting a job deletes its steps.

## Limits

- Only background jobs are traced. A normal chat reply that doesn't start a job has its live progress lines but no
  saved trace.
- Jobs that ran before this change have no trace.
- The reasoning is what the model wrote. It explains the step but isn't a guarantee of why the model chose it.
- Screenshot files of a deleted job stay in storage until the storage is cleaned by hand.

Tests: `apps/api/tests/test_run_traces.py` (redaction, results, a full job run against a real database, the route,
clean-up) and `test_run_traces_graph.py` (a real LangGraph agent with a scripted model, no paid calls).
