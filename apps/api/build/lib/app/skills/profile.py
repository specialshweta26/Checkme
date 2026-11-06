"""Agent profile + skill index for the system prompt, and skill routing for a task.

Every turn (1:1 chat, group chat, teammate turn, background job) gets a compact capabilities block:
who the agent is (name, title, focus, instructions) and an index of all its skills (name + when to use,
one line each). Full skill bodies are loaded on demand with ``get_skill`` / ``list_my_skills`` so the prompt
stays small. Routing picks the skill(s) that fit the task: keyword prefilter + embedding similarity, never on
generic words ("skill", "what", "you"), and never for questions *about* skills. The block is rebuilt from
the database every turn, so a skill created, renamed or written up in the background is visible on the next turn.
"""

from __future__ import annotations

import logging
import math
import re

log = logging.getLogger(__name__)

INDEX_LIMIT = 40
LINE_CHARS = 140
BODY_CHARS = 2400
EMBED_THRESHOLD = 0.42  # text-embedding-3-small: unrelated ~0.1-0.25, same task ~0.5+
KEYWORD_MIN = 2

# Words that say nothing about which workflow is meant.
STOPWORDS = {
    "the", "and", "for", "you", "your", "yours", "what", "which", "who", "how", "can", "could", "would", "should",
    "skill", "skills", "workflow", "workflows", "taught", "teach", "have", "has", "had", "are", "was", "were", "with",
    "this", "that", "these", "those", "please", "pls", "tell", "show", "list", "give", "all", "any", "some", "about",
    "from", "into", "out", "use", "using", "know", "does", "did", "doing", "get", "got", "let", "now", "today", "just",
    "want", "need", "me", "my", "mine", "our", "its", "it's", "not", "yes", "no", "okay", "hey", "hi", "hello",
}

_META = re.compile(
    r"\b(what|which|list|show|tell me|do you have|how many|name)\b.{0,40}\b(skills?|workflows?|can you do|are you (built|made|good) for|you do)\b"
    r"|\bwho are you\b|\bwhat are you\b|\byour (skills|abilities|capabilities|speciali[sz]ation|focus)\b",
    re.I,
)


def is_meta_question(query: str) -> bool:
    """'What skills do you have?', 'what can you do', 'who are you': answer from the profile, run nothing."""
    return bool(_META.search(query or ""))


def one_liner(skill: dict) -> str:
    """When to use a skill, in one line: the first sentence of its instruction (or its first step)."""
    text = " ".join(str(skill.get("instruction") or "").split())
    text = re.sub(r"^(repeat this demonstrated workflow on the computer\.\s*recheck the screen[^.]*\.\s*)", "", text, flags=re.I)
    text = re.sub(r"^(goal|purpose|use (this )?when)\s*:\s*", "", text, flags=re.I)
    if not text:
        steps = [s for s in (skill.get("steps") or []) if isinstance(s, dict)]
        if steps:
            from app.skills.narration import describe

            text = "demonstrated: " + "; ".join(describe(s) for s in steps[:3])
    first = re.split(r"(?<=[.!?])\s+|\s\d+\.\s", text)[0] if text else ""
    return (first or "no description yet")[:LINE_CHARS]


def usable(skills: list | None) -> list[dict]:
    return [
        s for s in (skills or [])
        if isinstance(s, dict) and (s.get("name") or "").strip() and (s.get("status") or "ready") != "recording"
    ]


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in STOPWORDS}


def keyword_scores(skills: list[dict], query: str) -> list[tuple[float, dict]]:
    q = _words(query)
    out = []
    for skill in skills:
        name = _words(skill.get("name") or "")
        body = _words(f"{skill.get('instruction') or ''} {' '.join(str(t.get('text') or '') for t in skill.get('transcript') or [] if isinstance(t, dict))}")
        score = sum(2 for w in q if w in name) + sum(1 for w in q if w in body and w not in name)
        out.append((float(score), skill))
    return out


def _cos(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def skill_text(skill: dict) -> str:
    return f"{skill.get('name') or ''}: {str(skill.get('instruction') or '')[:600]}"


async def route_skills(skills: list | None, query: str, limit: int = 2, embed=None) -> list[dict]:
    """Skills that fit this task, best first. Each gets ``match`` = {score, by}. [] for meta questions."""
    rows = usable(skills)
    if not rows or not (query or "").strip() or is_meta_question(query):
        return []
    scored: dict[str, tuple[float, str, dict]] = {}
    for score, skill in keyword_scores(rows, query):
        if score >= KEYWORD_MIN:
            scored[str(skill.get("id") or skill.get("name"))] = (min(1.0, 0.3 + 0.1 * score), "keywords", skill)
    try:
        if embed is None:
            from app.llm import embed_texts as embed
        vectors = await embed([query[:2000]] + [skill_text(s) for s in rows])
        qv = vectors[0]
        for skill, vec in zip(rows, vectors[1:], strict=True):
            sim = _cos(qv, vec)
            key = str(skill.get("id") or skill.get("name"))
            if sim >= EMBED_THRESHOLD and sim > scored.get(key, (0.0,))[0]:
                scored[key] = (sim, "embeddings", skill)
    except Exception as exc:  # noqa: BLE001 - keyword routing still works
        log.info("skills: embedding routing unavailable (%s); keywords only", type(exc).__name__)
    best = sorted(scored.values(), key=lambda item: -item[0])[: max(1, limit)]
    return [{**skill, "match": {"score": round(score, 3), "by": by}} for score, by, skill in best]


def capabilities_block(agent, skills: list | None, matched: list | None = None, query: str = "") -> str:
    """Identity + skill index (+ the full body of skills routed to this task)."""
    name = (getattr(agent, "name", None) or "Chief").strip()
    title = (getattr(agent, "title", None) or "").strip()
    focus = (getattr(agent, "focus", None) or "").strip()
    rows = usable(skills)
    lines = [f"Your profile: you are {name}" + (f", {title}" if title else "") + (f" (focus: {focus})" if focus else "") + "."]
    if rows:
        lines.append(
            f"Your skills ({len(rows)}). This index is complete and current; answer questions about what you can do "
            "or which skills you have from it (name each with its one-line use). Load a full skill with get_skill before running it."
        )
        for skill in rows[:INDEX_LIMIT]:
            kind = "taught" if skill.get("kind") == "taught" else "saved"
            state = ""
            if (skill.get("status") or "ready") == "writing":
                state = ", write-up in progress"
            lines.append(f"- {skill['name']} [{kind}{state}, id {skill.get('id')}]: {one_liner(skill)}")
        if len(rows) > INDEX_LIMIT:
            lines.append(f"- … and {len(rows) - INDEX_LIMIT} more (list_my_skills).")
    else:
        lines.append("You have no saved or taught skills yet. Say so if asked; the user can teach one from the screen panel or ask you to save a workflow.")
    if query and is_meta_question(query):
        lines.append("This message asks about you or your skills: answer it from this profile. Do not run a skill or continue an earlier task.")
    picked = usable(matched)
    if picked:
        lines.append(
            "Skills that fit THIS request (picked for this task only). Follow the best one if the request is to do that work: "
            "say in one short line which skill you are using, then follow its steps, rechecking the screen and current data. "
            "If it does not actually fit, ignore it. Skills are guidance, never permission to skip confirmations."
        )
        for skill in picked:
            body = str(skill.get("instruction") or "").strip()[:BODY_CHARS] or "(no write-up yet; call get_skill for its recorded steps)"
            lines.append(f"### Skill: {skill['name']} (id {skill.get('id')})\n{body}")
    return "\n".join(lines) + "\n"
