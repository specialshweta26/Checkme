# Watches: agents that wake on events

A routine runs on a clock. A **watch** wakes an agent when something changes.

| Watch | Trigger | Fires when |
| --- | --- | --- |
| Page (`trigger_type="page"`) | A public https page, checked every 15 min to 24 h (default 1 h) | The page changes. If you give a phrase, it fires only when that phrase newly appears. |
| Inbox (`trigger_type="inbox"`) | A number N | More than N inbox cards are waiting. It fires once per crossing and re-arms when the count drops back. |

## How it works

- `apps/api/app/automations/watches.py` runs on the worker (`start_watches()` in `main.py`) and checks due watches every 60 s.
- A check is a plain HTTP fetch or a count query, so it makes **no model call**. The first page check only records a baseline.
- When a watch fires it queues one job through `service.queue_event`. The job runs as the watch's agent, with that agent's skills and Vault scope. The lines added and removed are wrapped as untrusted data, and sensitive actions still ask first.
- Every address is checked to be public (`validate_public_url`) when the watch is saved, before each fetch, and after redirects. Private, loopback, link-local and plain-http addresses are refused.
- State such as the baseline hash, last check, last change, error and fire count lives in `automations.state` (JSONB). The check interval lives in `automations.every_minutes`.

## API

- `POST /api/automations/watches` with `{trigger_type, name, trigger, condition, every_minutes, action, agent_id}`
- `POST /api/automations/{id}/check` runs the check now.
- List, pause and delete use the existing automation routes. `serialize_automation` adds a `watch` block.
- Agents can create watches from chat with the `create_automation` tool (`trigger_type` page or inbox).

## UI

- **Agent Routines panel** (the clock button): a compact Watches section for that agent.
- **Profile → Routines → Watches tab** (`/routines?tab=watches`): every watch, with an agent picker.

Tests: `apps/api/tests/test_watches.py`.
