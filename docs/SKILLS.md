# Claude-format skills

Lumio can import skills written in Claude's **Agent Skills** format and export any Lumio skill in that format. A skill
is a folder with a `SKILL.md` file, YAML frontmatter followed by Markdown instructions, plus optional supporting files:

```markdown
---
name: pdf
description: Use this skill whenever the user wants to do anything with PDF files…
license: Complete terms in LICENSE.txt
---

# PDF Processing Guide
For forms, read FORMS.md and follow it.
```

`name` and `description` are what matter: the description says **when** to use the skill, and Lumio routes requests
on it. The body is what the agent follows. Supporting files (`FORMS.md`, `REFERENCE.md`, `scripts/…`) are read on
demand.

## Where to find it

**Profile → Skills** (`/skills`), also linked from each agent's Skills section.

- **Browse libraries.** Two libraries are featured: [anthropics/skills](https://github.com/anthropics/skills) and
  Trinity's [abilityai/trinity-skills](https://github.com/abilityai/trinity-skills). You can also paste any public
  GitHub link (a repository, one folder of it, or a `SKILL.md` link). Each skill shows its description, licence,
  file count and whether it has scripts. **Add** imports it for the agent chosen in **Add skills to** (or all agents).
- **Upload.** Upload a `SKILL.md`, or a `.zip` holding up to 25 skill folders.
- **Your skills.** Every skill, with **Export** (a `.zip` that Claude, Claude Code or another Lumio can import) and,
  for library skills, **Check for update**.

## How it works

| Piece | Where |
| --- | --- |
| Parse, safety limits, export | `apps/api/app/skills/claude_format.py` |
| GitHub libraries (browse, fetch, pin) | `apps/api/app/skills/library.py` |
| Store, re-import, update, files | `apps/api/app/skills/service.py` (`import_skill`, `update_from_source`, `skill_file`) |
| Routes | `GET /api/skills/library/sources`, `GET /api/skills/library?url=`, `POST /api/skills/import`, `POST /api/skills/import/upload`, `GET /api/skills/{id}/export`, `GET /api/skills/{id}/file?path=`, `POST /api/skills/{id}/update[?check=true]` |
| Tests | `apps/api/tests/test_claude_skills.py` |

- **Storage.** An imported skill is an `agent_skills` row with `kind = "imported"`, plus `description`, `source` (where it
  came from, pinned to a commit) and `files` (its text files). Importing the same folder again updates that row to a
  new revision instead of adding a copy. A name you gave it in Lumio is kept.
- **Pinned to a commit.** A library import records the commit it was read from. **Check for update** compares that
  commit with the branch head, and updating replaces the instructions (edits made in Lumio are lost, and the app warns
  before doing it).
- **Running.** Claude skills are guides, not numbered procedures. An imported skill therefore runs as a single step
  ("follow the guide") until you edit a flow for it, so a numbered list inside the guide never becomes forced steps.
  The agent sees the skill's description in its index. When a request matches, the agent gets the instructions and a
  list of supporting files, and opens a file with `get_skill(skill_id, file="FORMS.md")`.
- **Exporting** works for every skill. Saved and taught skills get a name made from their title and a description
  made from their instructions. A taught skill without a write-up exports its recorded steps.

## Safety

An imported skill is someone else's instructions, so Lumio treats it the way it treats a web page:

- **Third-party content.** When an imported skill is matched to a request or loaded with `get_skill`, the turn is marked as
  containing third-party content (`note_untrusted`). From then on, an unattended run (a schedule or an event) asks
  before any destructive action (`app/agent/approval.py`, rule 3). The prompt also tells the agent that the skill's
  text never overrides the user, approvals or safety rules.
- **Text only.** Lumio keeps text files only. Binaries (images, fonts, archives, Office files) are left out, and the
  import reports what was skipped. Scripts are stored as text: nothing is installed or run automatically. To use a
  script, the agent has to put it on the computer like any other code, and normal approvals apply.
- **Size limits.** `SKILL.md` up to 200 KB, each file up to 200 KB, at most 60 files and 1.5 MB per skill, uploads up
  to 15 MB. Zip archives are checked against path traversal and against unpacking to more than 40 MB.
- **Fixed hosts only.** Lumio contacts only `api.github.com` and `raw.githubusercontent.com`. The owner and repository
  come from a strict pattern, so a pasted link can't make the server request anything else.
- **Licences.** Each library card shows the skill's `license` line. Check a skill's terms before you use it
  commercially; some skills in public libraries are not open source.

## Settings

`SKILLS_GITHUB_TOKEN` (optional): a GitHub token with no scopes. Without it, the server may make 60 GitHub API reads an
hour. Browsing a library costs two reads and is cached for ten minutes; file downloads don't count toward the limit.
