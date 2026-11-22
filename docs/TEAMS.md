# Teams

Teams (`/teams`, API `apps/api/app/teams/routes.py`) let people who work together share skills and keep one log of
what happened in the team.

## What a team shares, and what it doesn't

- **Shared:** skills a member chooses to share, the member list, and the team's activity log.
- **Not shared:** each person's mail, calendar, connectors, agents, memory, chats and sign-ins.
- A shared skill is a **copy**:
  - It keeps the steps, instructions, flow and text files.
  - It drops the screenshots and narration.
  - Anything typed that looked like a password is masked.
  - A skill that changes or hides figures (docs/GUARDRAILS.md) can't be shared.
- To use a shared skill, a member adds their own copy (**Add to my skills**). It then runs with their own connectors,
  and their copy remembers which team it came from.

## Roles

| Role | Can |
| --- | --- |
| owner (one) | everything, including making admins, transferring ownership and deleting the team |
| admin | invite and remove members and viewers, change their roles, see and export the activity log |
| member | share their skills, add shared skills, leave |
| viewer | add shared skills, leave |

- The owner can't leave or be demoted. They transfer ownership first.
- Deleting a team leaves every member's added skills in place.
- Someone not in a team gets a 404 for it, so a team's existence isn't revealed.

## Invitations

- An admin invites an email address with a role. Lumio emails a link to `/teams/join?token=…` (with no mail set up,
  the admin gets the link to send themselves).
- The link works once, for 7 days, and only when signed in with that exact address. Only a hash of the token is
  stored.
- While `SIGNUP_MODE=invite` (docs/ROLLOUT.md), an open team invitation also lets that address create an account.

## Activity log

Joins, departures, role changes, invitations, shared, removed and added skills, renames and ownership changes. Each
is recorded in `audit_events` with the team's id. Admins export 90 days as CSV from the team page
(`/api/teams/{id}/activity?fmt=csv`).
