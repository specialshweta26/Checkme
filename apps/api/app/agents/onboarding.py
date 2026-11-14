from __future__ import annotations

import json

from app.llm import chat_complete


def _fallback(agent) -> dict:
    name = (getattr(agent, "name", "") or "Your agent").strip()
    title = (getattr(agent, "title", "") or "").strip()
    identity = f" as your {title}" if title else ""
    return {
        "greeting": (
            f"Hi, I’m {name}{identity}. What would you like me to take ownership of first? "
            "Tell me the outcome you want and any preferences I should follow."
        ),
        "questions": [
            f"What is the first result you want {name} to deliver?",
            "What tools, people, or information should I use?",
            "Should I handle this once, or make it a repeatable task or skill?",
        ],
        "tasks": [],
        "skills": [],
    }


async def generate_onboarding(agent) -> dict:
    fallback = _fallback(agent)
    identity = {
        "name": getattr(agent, "name", ""),
        "title": getattr(agent, "title", ""),
        "instructions": getattr(agent, "instructions", ""),
    }
    prompt = (
        "Create a short, welcoming first-chat introduction for this newly created personal AI agent. "
        "Infer its role from its name, title, and instructions. Ask three specific discovery questions that help it "
        "understand what work to own, the desired outcome, and any preferences or tools. Suggest up to three useful "
        "first tasks and up to two reusable skills it could learn. Make suggestions relevant to this agent, not generic "
        "fixed categories. Do not claim tools are connected. Return JSON only with this shape: "
        '{"greeting":"...","questions":["..."],"tasks":[{"title":"...","prompt":"..."}],'
        '"skills":[{"title":"...","prompt":"..."}]}. Each task prompt should be something the user can ask now. '
        "Each skill prompt should invite the agent to help define and save that repeatable procedure.\n\n"
        + json.dumps(identity, ensure_ascii=False)
    )
    try:
        response = await chat_complete(
            [{"role": "user", "content": prompt}],
            max_tokens=650,
            response_format={"type": "json_object"},
        )
        raw = json.loads((response.choices[0].message.content or "{}").strip())
        if not isinstance(raw, dict):
            return fallback
        greeting = raw.get("greeting")
        if not isinstance(greeting, str) or not greeting.strip():
            greeting = fallback["greeting"]
        questions = raw.get("questions")
        if not isinstance(questions, list) or not questions:
            questions = fallback["questions"]
        return {
            "greeting": greeting.strip()[:700],
            "questions": [str(item).strip()[:220] for item in questions[:3] if str(item).strip()],
            "tasks": _suggestions(raw.get("tasks"), 3),
            "skills": _suggestions(raw.get("skills"), 2),
        }
    except Exception:  # noqa: BLE001
        return fallback


def _suggestions(value, limit: int) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()[:100]
        prompt = str(item.get("prompt") or "").strip()[:500]
        if title and prompt:
            out.append({"title": title, "prompt": prompt})
    return out


# ---------------------------------------------------------------- ready as soon as the agent exists

import asyncio
import hashlib
import logging

log = logging.getLogger(__name__)
CACHE_SECONDS = 7 * 24 * 3600
_PENDING: dict[str, asyncio.Future] = {}
_KEEP: set[asyncio.Task] = set()


def cache_key(agent) -> str:
    """Per agent and per identity: renaming it or changing its title or instructions gets fresh ideas."""
    who = "|".join(str(getattr(agent, k, "") or "") for k in ("name", "title", "instructions"))
    return f"agent:onboarding:{getattr(agent, 'id', '')}:{hashlib.sha1(who.encode()).hexdigest()[:16]}"


async def onboarding_for(agent) -> dict:
    """The agent's first-chat ideas, made once and kept (so its chat shows them at once, and the same ones each
    time). A fallback (the model was unavailable) is not kept, so the next visit tries again."""
    from app.cache import get_redis

    key = cache_key(agent)
    try:
        hit = await get_redis().get(key)
        if hit:
            return json.loads(hit)
    except Exception:  # noqa: BLE001 - no cache: generate as before
        log.info("onboarding cache unavailable", exc_info=False)
    if key in _PENDING:  # being made already (by the warm-up started at creation)
        return await asyncio.shield(_PENDING[key])
    fut = asyncio.get_running_loop().create_future()
    _PENDING[key] = fut
    try:
        data = await generate_onboarding(agent)
        if data.get("tasks") or data.get("skills"):
            try:
                await get_redis().set(key, json.dumps(data), ex=CACHE_SECONDS)
            except Exception:  # noqa: BLE001
                pass
        fut.set_result(data)
        return data
    except Exception as exc:  # noqa: BLE001
        fut.set_exception(exc)
        raise
    finally:
        _PENDING.pop(key, None)


def warm(agent) -> None:
    """Start making a new agent's ideas now, while the app opens its chat."""
    from types import SimpleNamespace

    snap = SimpleNamespace(**{k: getattr(agent, k, "") for k in ("id", "name", "title", "instructions")})
    task = asyncio.create_task(_warm(snap))
    _KEEP.add(task)
    task.add_done_callback(_KEEP.discard)


async def _warm(agent) -> None:
    try:
        await onboarding_for(agent)
    except Exception:  # noqa: BLE001 - the chat will ask again
        log.info("onboarding warm-up failed", exc_info=True)
