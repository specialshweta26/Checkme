# Agent quality checks

Real requests run through the real agent: the real model, system prompt and tools. Files, the computer and web search
are fakes (`harness.py`), so every run starts the same way and nothing touches real files or accounts.

```bash
cd apps/api && uv run python -m evals.run                     # every scenario
cd apps/api && uv run python -m evals.run "list" --repeat 3   # names containing "list", three times each
```

Needs `OPENAI_API_KEY` and costs a few model calls per scenario, so it doesn't run in CI. Run it before a release, and
after changing prompts, tools or models. Results also go to `evals/last_run.json`.

Each scenario in `scenarios.py` says which tools must or mustn't be called, what the reply must or mustn't say, and a
time limit. **When the agent gets something wrong in real use, add it as a scenario.** Mark a failure you know about but
haven't fixed with `known_issue`: it's reported as KNOWN and doesn't fail the run.

Model answers vary, so one pass proves little. Use `--repeat 3` on anything you're changing.

Not covered yet: mail, calendar, approvals, jobs and the real browser.

## Cases from user feedback

A thumbs-down in the app keeps the request, the turns before it and the bad reply. Export them from the Quality page
(or `GET /api/quality/export`), then:

```bash
cd apps/api && uv run python -m evals.from_feedback feedback.json
```

They land in `evals/feedback_cases.json`. Fill in what the agent must or mustn't do for each (`must_call`,
`must_not_call`, `reply_has`, `reply_lacks`); `evals.run` runs the ones that say, and lists the rest. To run
everything on GitHub on demand, copy `evals/evals.workflow.yml` to `.github/workflows/evals.yml` (it has no schedule
because each run costs model calls).
