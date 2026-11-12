"""A skill as a flow of nodes, and a run that moves through it strictly.

Each node is one step: what to do, what shows it worked ("done when"), and which node comes next. A run is at exactly
one node. A node passes only with evidence, words that really appeared in what the computer showed during the task
(page text, a pop-up, a download), and a passed node never runs again. A step that waits (refresh until ready) is one
node that waits inside itself, so waiting never sends the run back through earlier nodes.

Generic: the nodes come from the skill's own numbered steps, written on separate lines or inline ("1. Open … 2. Pick …").
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

MAX_NODES = 60
MIN_EVIDENCE = 3
RECENT_READS = 3  # positive evidence must be on screen after the step: one of the latest reads, not any earlier one
_STEP_START = re.compile(r"(?:(?<=\s)|^)(?:step\s*)?(\d{1,2})[.)]\s+", re.I)
_DONE_WHEN = re.compile(r"\b(?:done when|it worked when|success(?: is)? when|check)\s*:\s*", re.I)
_WAITS = re.compile(
    r"\b(until|till|wait (?:for|until)|keep refreshing|refresh .* (?:until|till)|once it (?:appears|shows)|takes? (?:some )?time"
    r"|when [^.]{0,80}\bappears?|click (?:on )?refresh)\b",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def split_numbered(text: str) -> list[str]:
    """The skill's steps in order. Numbers must run 1, 2, 3 …, so a date (05/10) or a "2 items" can't start a step."""
    text = str(text or "")
    marks = []
    want = 1
    for m in _STEP_START.finditer(text):
        if int(m.group(1)) == want:
            marks.append(m)
            want += 1
    if len(marks) < 2:
        return []
    out = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = " ".join(text[m.end():end].split())
        if body:
            out.append(body)
    return out[:MAX_NODES]


def _node(n: int, body: str, nxt: str | None) -> dict:
    parts = _DONE_WHEN.split(body, maxsplit=1)
    do, done_when = (parts[0].strip(" .;"), parts[1].strip(" .;")) if len(parts) == 2 else (body.strip(), "")
    title = re.split(r"(?<=[.!?])\s", do, maxsplit=1)[0][:90]
    return {
        "id": f"n{n}",
        "n": n,
        "title": title,
        "do": do[:600],
        "done_when": done_when[:300],
        "kind": "wait" if _WAITS.search(do) else "step",
        "next": [nxt] if nxt else [],
    }


def build(skill: dict) -> dict:
    """The skill's flow: {start, nodes}. A flow edited in the app wins; otherwise numbered steps become a chain, and a
    skill with none is one node."""
    saved = skill.get("flow") if isinstance(skill.get("flow"), dict) else None
    if saved and saved.get("edited") and saved.get("nodes"):
        clean, errors = normalize(saved)
        if not errors:
            return clean
    steps = split_numbered(skill.get("instruction") or "")
    if not steps:
        body = " ".join(str(skill.get("instruction") or skill.get("name") or "Do the task").split())
        steps = [body[:600]]
    nodes = [_node(i + 1, body, f"n{i + 2}" if i + 1 < len(steps) else None) for i, body in enumerate(steps)]
    return {"start": nodes[0]["id"], "nodes": nodes}


# ---------------------------------------------------------------- editing


MAX_TEXT = 600


def normalize(flow: dict) -> tuple[dict, list[str]]:
    """An edited flow, cleaned and checked. Nodes are numbered in their order. Each step goes on to the next one unless
    it is the last; a branch ("if <when>, go to step N") may only jump forward, because a passed step never runs again,
    so a loop back can't happen. Returns (flow, errors)."""
    errors: list[str] = []
    raw = [x for x in (flow or {}).get("nodes") or [] if isinstance(x, dict)]
    if not raw:
        return {"start": None, "nodes": []}, ["A flow needs at least one step."]
    if len(raw) > MAX_NODES:
        errors.append(f"A flow can have at most {MAX_NODES} steps.")
        raw = raw[:MAX_NODES]
    old_ids = [str(x.get("id") or "") for x in raw]
    if len(set(old_ids)) != len(old_ids) or not all(old_ids):
        # Ids are only how branches point at steps; without unique ones, refer to steps by position.
        old_ids = [f"p{i}" for i in range(len(raw))]
    new_id = {old: f"n{i + 1}" for i, old in enumerate(old_ids)}
    nodes = []
    for i, x in enumerate(raw):
        do = " ".join(str(x.get("do") or x.get("title") or "").split())[:MAX_TEXT]
        title = " ".join(str(x.get("title") or "").split())[:90] or re.split(r"(?<=[.!?])\s", do, maxsplit=1)[0][:90]
        if not do:
            errors.append(f"Step {i + 1} says nothing to do.")
        nxt = [f"n{i + 2}"] if i + 1 < len(raw) else []
        branches = []
        for b in x.get("branches") or []:
            if not isinstance(b, dict):
                continue
            to = new_id.get(str(b.get("to") or ""))
            when = " ".join(str(b.get("when") or "").split())[:200]
            if not to or not when:
                errors.append(f"Step {i + 1} has a branch without a condition or a step to go to.")
                continue
            if int(to[1:]) <= i + 1:
                errors.append(f"Step {i + 1}'s branch goes back to step {to[1:]}: a passed step never runs again, so branches only go forward.")
                continue
            branches.append({"to": to, "when": when})
        kind = "wait" if str(x.get("kind") or "") == "wait" else "step"
        nodes.append({"id": f"n{i + 1}", "n": i + 1, "title": title, "do": do,
                      "done_when": " ".join(str(x.get("done_when") or "").split())[:300], "kind": kind,
                      "next": nxt, "branches": branches})  # fmt: skip
    return {"start": "n1", "nodes": nodes, "edited": True}, errors


def to_instruction(flow: dict, preamble: str = "") -> str:
    """The flow written back as the skill's numbered steps, so the text and the flow always say the same thing."""
    lines = [preamble.strip()] if preamble.strip() else []
    for x in flow.get("nodes") or []:
        do = x["do"].rstrip()
        line = f"{x['n']}. {do}" + ("" if do.endswith((".", "!", "?")) else ".")
        if x.get("kind") == "wait" and not _WAITS.search(x["do"]):
            line += " Wait until it is ready."
        for b in x.get("branches") or []:
            line += f" If {b['when']}, go to step {b['to'][1:]}."
        if x.get("done_when"):
            line += f" Done when: {x['done_when']}."
        lines.append(line)
    return "\n".join(lines)


def preamble_of(instruction: str) -> str:
    """Text before step 1, kept when the flow is written back."""
    m = _STEP_START.search(str(instruction or ""))
    return str(instruction or "")[: m.start()].strip() if m and m.group(1) == "1" else ""


# ---------------------------------------------------------------- a run through the flow


def fresh_run(flow: dict) -> dict:
    nodes = [{**node, "status": "pending"} for node in flow.get("nodes") or []]
    return {"start": flow.get("start"), "current": flow.get("start"), "nodes": nodes}


def node(run: dict, node_id: str | None) -> dict | None:
    return next((x for x in run.get("nodes") or [] if x.get("id") == node_id), None)


def passed(run: dict) -> list[dict]:
    return [x for x in run.get("nodes") or [] if x.get("status") == "passed"]


def current(run: dict) -> dict | None:
    return node(run, run.get("current"))


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[“”\"'`]", "", str(text or "")).casefold().split())


_GONE = re.compile(r"^\s*(?:gone|absent|removed|not shown)\s*:\s*", re.I)


def evidence_seen(evidence: str, observed: list[str]) -> bool:
    """The evidence must be words that really appeared in what the computer showed during this task. For a step that
    removes something, "gone: <words>" passes when those words were shown earlier in the task and the latest page
    doesn't show them."""
    raw = str(evidence or "")
    gone = bool(_GONE.match(raw))
    want = _norm(_GONE.sub("", raw)).strip(" .,:;!?")
    if len(want) < MIN_EVIDENCE:
        return False
    pages = [_norm(seen) for seen in observed or [] if str(seen or "").strip()]
    if not gone:
        return any(want in page for page in pages[-RECENT_READS:])
    return len(pages) >= 2 and want not in pages[-1] and any(want in page for page in pages[:-1])


def advance(run: dict, n: int, evidence: str, observed: list[str] | None, *, checked: bool = True, go_to: int = 0) -> dict[str, Any]:
    """Pass node ``n`` (its number) and move to the next one. Returns {"ok", "run", "message"}.

    Refused: a node already passed (it never repeats), a node that isn't the current one, or evidence that wasn't seen.
    ``checked`` False skips the evidence check, for a task that didn't use the computer at all."""
    target = next((x for x in run.get("nodes") or [] if x.get("n") == n), None)
    here = current(run)
    if target is None:
        return {"ok": False, "reason": "no_step", "run": run, "message": f"This skill has no step {n}."}
    if target.get("status") == "passed":
        nxt = here
        tail = f" Carry on with step {nxt['n']}: {nxt['title']}" if nxt else " Every step is done: finish and report."
        return {"ok": False, "reason": "repeat", "repeat": True, "run": run, "message": f"Step {n} is already done (evidence: {target.get('evidence') or '-'}). It never runs again.{tail}"}
    if here is None:
        return {"ok": False, "reason": "finished", "run": run, "message": "Every step is done: finish and report."}
    if target["id"] != here["id"]:
        return {"ok": False, "reason": "order", "run": run, "message": f"The current step is {here['n']}: {here['title']}. Do that one first; steps run in order."}
    if not str(evidence or "").strip():
        return {"ok": False, "reason": "no_evidence", "run": run, "message": f"Give evidence for step {n}: words from the page, pop-up or file that show it worked{_hint(here)}."}
    if checked and not evidence_seen(evidence, observed or []):
        return {"ok": False, "reason": "unseen", "run": run, "message": (
            f"Not passed: '{str(evidence)[:120]}' isn't on the screen now (the latest page reads). Read the page again "
            f"and check step {n} really worked{_hint(here)}, then give words that are on it."
        )}  # fmt: skip
    nxt = (here.get("next") or [None])[0]
    branches = here.get("branches") or []
    if go_to:
        allowed = {b["to"] for b in branches} | ({nxt} if nxt else set())
        if f"n{int(go_to)}" not in allowed:
            options = ", ".join(f"step {b['to'][1:]} if {b['when']}" for b in branches) or "none"
            return {"ok": False, "reason": "branch", "run": run, "message": f"Step {n} can't go to step {go_to}. Its branches: {options}; otherwise step {nxt[1:] if nxt else '-'}."}
        nxt = f"n{int(go_to)}"
    jumped = {x["id"] for x in run.get("nodes") or [] if nxt and here["n"] < x["n"] < int(nxt[1:])}
    nodes = []
    for x in run.get("nodes") or []:
        if x["id"] == here["id"]:
            x = {**x, "status": "passed", "evidence": str(evidence)[:300], "at": _now(), "checked": bool(checked)}
        elif x["id"] in jumped and x.get("status") != "passed":
            x = {**x, "status": "skipped"}
        nodes.append(x)
    out = {**run, "nodes": nodes, "current": nxt}
    after = node(out, nxt)
    msg = f"Step {n} passed. Next is step {after['n']}: {after['title']}" if after else "That was the last step: finish and report."
    return {"ok": True, "run": out, "message": msg}


def _hint(here: dict) -> str:
    return f" (done when: {here['done_when']})" if here.get("done_when") else ""


def prompt_lines(run: dict) -> list[str]:
    """The flow for the agent: what's passed (never redo it), the one step to do now, what follows."""
    nodes = run.get("nodes") or []
    if not nodes:
        return []
    lines = ["Steps (a flow: one step at a time, in order; a passed step never runs again):"]
    for x in nodes:
        if x.get("status") == "passed":
            lines.append(f"  ✓ {x['n']}. {x['title']} — passed (evidence: {str(x.get('evidence') or '')[:80]}). Don't redo it.")
    here = current(run)
    if here is None:
        lines.append("All steps have passed: report the result.")
        return lines
    lines.append(f"  → {here['n']}. NOW: {here['do']}")
    if here.get("done_when"):
        lines.append(f"     Done when: {here['done_when']}")
    if here.get("kind") == "wait":
        lines.append(
            "     This step waits for something to be ready: use browser_poll(click=<the refresh control>, until=<words of what "
            "appears, such as download>, max_s=300). It refreshes for you for up to 5 minutes; don't go back to earlier steps."
        )
    for b in here.get("branches") or []:
        lines.append(f"     If {b['when']}: go to step {b['to'][1:]} (pass this step with skill_progress(..., next_step={b['to'][1:]})).")
    upcoming = [x for x in nodes if x.get("status") not in {"passed", "skipped"} and x["id"] != here["id"]]
    for x in upcoming[:3]:
        lines.append(f"  · {x['n']}. {x['title']}")
    if len(upcoming) > 3:
        lines.append(f"  · … {len(upcoming) - 3} more")
    lines.append(
        f"When step {here['n']} has worked, check the page shows it, then call skill_progress(step={here['n']}, evidence=<words "
        "from the page, pop-up or file that show it>). If the step removes something, read the page again and give "
        "evidence=\"gone: <words that were there before>\". Without matching evidence the step doesn't pass."
    )
    return lines
