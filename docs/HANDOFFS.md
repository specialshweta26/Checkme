# Handoffs: rules for agents messaging each other

Agents hand work to each other in two ways: `delegate` inside a background job (a child job for another agent), and `ask_teammate` in a group chat. Every handoff now leaves a receipt, finished work reports back as an event, and each agent has a rule for who may send it work.

## What you see

In an agent's **About** panel, under **Handoffs**:

- **Who can hand it work**:
  - **Any of your agents** (the default, same as before).
  - **Only the agents I pick**.
  - **Only you**.

  You can always message any agent yourself, and an agent can always hand work to itself.
- **Recent handoffs** to and from that agent, each with a status:
  - **Sent**: waiting in the queue.
  - **Working on it**: picked up.
  - **Done**, **Didn't finish** or **Cancelled**.
  - **Refused**: the receiver's rule doesn't allow it.

  Open one to see when it was sent, picked up and finished, and the start of the reply or the reason it was refused.

On a background task's timeline, the parent job shows a **⇄** line when a delegated job finishes ("Marki finished: Review PR 7"), before it carries on.

## How it works

- **Receipts** live in `agent_messages`, one row per cross-agent handoff (`apps/api/app/handoffs/service.py`):
  - `delegate` writes a `sent` row with the child job id.
  - The worker marks it `received` when it starts the child (`jobs/runner.py`).
  - `after_terminal` closes it as `done`, `failed` or `cancelled` with the first part of the result.
  - `ask_teammate` runs inline, so its row goes `received` and then `done` (or `failed`) in the same turn.
  - Delegating to yourself (for example, a planner starting its own coder) writes no receipt.
- **Completion events**: when a delegated job ends, a `handoff` event is added to the parent job, carrying the receipt and the reply. The parent still resumes with every child's result, as before.
- **Rules** live in `workspace_agents.accepts_from` (`{"mode": "anyone" | "selected" | "nobody", "agents": [ids]}`):
  - They are checked before anything runs.
  - A refused handoff returns the reason to the sending agent, so it can do the part itself or ask you.
  - A refused handoff also writes a `refused` receipt.
- **API**:
  - `GET /api/handoffs?agent_id=&limit=` lists receipts, newest first, only your own.
  - `PUT /api/handoffs/rules/{agent_id}` with `{mode, agents}` sets a rule. Only your own agents can be listed, and `selected` needs at least one.
  - Agents include `accepts_from` in `GET /api/agents`.

Tests: `apps/api/tests/test_handoffs.py`.
