# Approvals: one queue for everything waiting on you

Every agent's requests for your decision appear on one page, **Approvals** in the sidebar. The sidebar link shows a count and refreshes every 30 s and when the window regains focus.

| Kind | What it is | From the queue |
| --- | --- | --- |
| Plan to start | A background task waiting for its plan ([PLANS.md](PLANS.md)) | Start / Don't run it, or batch |
| Needs your OK | A background job paused on an action (send, push, merge, pay…) | Approve / Decline, or batch |
| Needs your OK · in chat | A chat reply prepared an action; these expire in minutes | Approve (sends the same "Yes" the chat sends) / Decline |
| Question | A background job asked something | Pick one of its options, or answer in the chat |
| Your turn on the computer | A hand-over (sign in, solve a check) | Finish in the chat |

- **Batch:** select plans and actions, then approve or decline them together (up to 50 at a time). Each one runs the job's own transition (`plans.approve`, `jobs.approve` / `jobs.decline`, `jobs.cancel_tree`), so nothing is approved that the job wouldn't accept. If one fails, the rest still go through, and the result says which failed and why.
- Expired action approvals are shown as expired, with a pointer to ask again in the chat.
- A chat approval is listed only while it is valid and still on that chat's latest reply, the same rule the chat uses.
- Each approval shows only to the user it belongs to.

## API

- `GET /api/approvals` returns `{items, count, counts}`.
- `GET /api/approvals/count` returns the count.
- `POST /api/approvals/decide` takes `{decisions: [{id, kind, job_id, token?, approve}]}`.
- `POST /api/approvals/answer` takes `{job_id, text}`.
- `POST /api/approvals/dismiss` takes `{message_id, token}` and declines a chat approval.

Code: `apps/api/app/approvals/`, `apps/web/app/approvals/page.tsx`. Tests: `apps/api/tests/test_approvals.py`.
