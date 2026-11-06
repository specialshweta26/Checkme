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
