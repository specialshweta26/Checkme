# Quality loop

How Lumio measures whether its agents are doing a good job, and turns what went wrong into tests the agents must pass.

## In the app

- **Thumbs up / down under every reply** (chat, group chats, scheduled-run results). A thumbs-down asks what went wrong:
  wrong or made up, incomplete, didn't do what I asked, did something I didn't want, too slow, wrong tone, plus an
  optional note on what it should have done. Clicking the same thumb again clears the rating.
- **Profile → Quality** (`/quality`), for 7, 30 or 90 days, overall or per agent:
  - **Replies rated good**: share of thumbs up.
  - **Routine runs succeeded**: from Routines' run history.
  - **Inbox drafts kept**: drafts sent unchanged or lightly edited (similarity ≥ 0.7, the Usage page's buckets).
  - **Why replies went wrong**: the reasons, counted.
  - **Replies that went wrong**: what you asked, what it replied, the tools it used and your note, with Open
    conversation, Export as test case and Clear rating.

Nothing here calls a model: every number comes from what the user already did.

## From feedback to a test

1. Export from the Quality page (one reply or all), or `GET /api/quality/export`. Each case keeps the request, the
   turns before it, the bad reply and why it was bad.
2. `cd apps/api && uv run python -m evals.from_feedback feedback.json` adds them to `evals/feedback_cases.json`.
3. Fill in what the agent must or mustn't do (`must_call`, `must_not_call`, `reply_has`, `reply_lacks`, as in
   `evals/scenarios.py`). `evals.run` runs every case that says, alongside the built-in scenarios, and lists the rest.
4. Run them locally with `uv run python -m evals.run`. To run them on GitHub too, copy
   `apps/api/evals/evals.workflow.yml` to `.github/workflows/evals.yml` and add the `OPENAI_API_KEY` secret; then
   **Actions → Agent quality checks → Run workflow**. It has no schedule because each run costs model calls; add the
   commented `schedule` to run it nightly.

## How it works

| Piece | Where |
| --- | --- |
| Ratings table `agent_feedback` (one per conversation, turn and agent) | `apps/api/app/models.py` (`AgentFeedback`), created at startup |
| Rate, list, summary, export | `apps/api/app/quality/service.py`; `POST /api/quality/feedback`, `GET /api/quality/feedback`, `GET /api/quality/feedback/session/{id}`, `GET /api/quality/summary`, `GET /api/quality/export` |
| Thumbs under replies | `apps/web/app/components/ReplyFeedback.tsx` |
| Quality page | `apps/web/app/quality/page.tsx` |
| Feedback → test cases | `apps/api/evals/from_feedback.py` |
| Tests | `apps/api/tests/test_quality.py` |

A reply is identified by its conversation and turn (`request_id`, now included in every message the API returns) and
its agent, so each bot in a group chat is rated separately. Ratings are the user's own data: they're in the data
download and removed with the account.
