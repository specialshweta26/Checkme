"""Computer-use contract for the desktop app the user already paired.

One observe-act-verify step on this machine. Official APIs and the existing browser
tools stay preferred. A denied method is not replaced by a weaker one. High-risk
and prohibited actions are not executed here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

METHODS = ("api", "browser", "script", "accessibility", "visual", "pointer")

_PROHIBITED = (
    "bypass captcha",
    "bypass mfa",
    "bypass 2fa",
    "steal password",
    "steal cookie",
    "export cookies",
    "dump cookies",
    "disable antivirus",
    "evade endpoint",
    "turn off screen recording permission",
)
_HIGH = (
    "send message",
    "send the message",
    "send whatsapp",
    "send email",
    "send a text",
    "publish",
    "purchase",
    "buy ",
    "pay ",
    "submit form",
    "submit the form",
    "delete ",
    "post to",
)
_ALIASES = {
    "excel": "microsoft excel",
    "word": "microsoft word",
    "powerpoint": "microsoft powerpoint",
    "code": "visual studio code",
    "vscode": "visual studio code",
    "vs code": "visual studio code",
    "chrome": "google chrome",
    "browser": "google chrome",
}
# Routing hints for names people say. Any other installed app or https address still works.
_SITES = {
    "gmail": "https://mail.google.com/",
    "linkedin": "https://www.linkedin.com/",
    "instagram": "https://www.instagram.com/",
    "facebook": "https://www.facebook.com/",
    "whatsapp web": "https://web.whatsapp.com/",
    "salesforce": "https://login.salesforce.com/",
    "shopify": "https://admin.shopify.com/",
    "outlook": "https://outlook.office.com/",
}
_API = {
    "gmail": "gmail",
    "slack": "slack",
    "salesforce": "salesforce",
    "shopify": "shopify",
    "outlook": "outlook",
    "github": "github",
}
_TASKS: dict[str, dict] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def envelope(status: str, result: dict | None = None, error: dict | None = None, evidence: dict | None = None) -> dict:
    stamp = _now()
    return {
        "status": status,
        "job_id": "",
        "result": result or {},
        "error": error,
        "evidence": evidence or {},
        "started_at": stamp,
        "completed_at": stamp,
    }


def assess(action: str, detail: str = "") -> str:
    """low, medium, high, or prohibited. Prohibited never runs. High never runs inside this tool."""
    blob = f"{action} {detail}".lower()
    evasion = any(word in blob for word in ("bypass", "evade", "steal", "dump cookies", "export cookies", "disable antivirus"))
    protected = any(word in blob for word in ("captcha", "mfa", "2fa", "password", "cookie", "antivirus", "endpoint"))
    if evasion and protected or any(phrase in blob for phrase in _PROHIBITED):
        return "prohibited"
    if any(phrase in blob for phrase in _HIGH) or action in {"send", "delete", "publish", "purchase", "pay"}:
        return "high"
    if action in {"discover", "inspect", "observe"}:
        return "low"
    return "medium"


def categorize(name: str) -> str:
    n = (name or "").strip().lower()
    if any(word in n for word in ("chrome", "safari", "firefox", "edge", "brave", "arc", "browser")):
        return "browser"
    if n in {"terminal", "iterm", "iterm2", "warp", "powershell"}:
        return "terminal"
    if any(word in n for word in ("code", "slack", "discord", "notion", "figma", "spotify", "whatsapp", "electron")):
        return "hybrid"
    return "native"


def capabilities_for(category: str) -> dict[str, Any]:
    if category == "browser":
        caps: dict[str, Any] = {"api": False, "browser": True, "script": False, "accessibility": False, "visual": True, "pointer": False}
        notes = ["Uses Lumio's own Chrome profile. Does not read cookies or passwords from the user's everyday browser."]
    elif category == "terminal":
        caps = {"api": False, "browser": False, "script": True, "accessibility": False, "visual": False, "pointer": False}
        notes = ["Terminal stays off until the user enables it in This computer settings."]
    elif category == "hybrid":
        caps = {"api": False, "browser": False, "script": False, "accessibility": False, "visual": True, "pointer": True}
        notes = ["No official API is connected for this app. Visual control needs Screen Recording and Accessibility, which are not bypassed."]
    else:
        caps = {"api": False, "browser": False, "script": True, "accessibility": False, "visual": True, "pointer": True}
        notes = ["Scripting is preferred. If the app has no script, say so instead of clicking blindly."]
    caps["limitations"] = notes
    return caps


def website_target(target: str) -> str:
    raw = (target or "").strip()
    if raw.startswith(("http://", "https://")):
        return raw
    return _SITES.get(raw.lower(), "")


def with_api(capabilities: dict, target: str, connectors: set[str] | None) -> dict:
    """Prefer an authorized connector when one is connected. Does not invent one."""
    hint = _API.get((target or "").strip().lower())
    if not hint or hint not in (connectors or set()):
        return capabilities
    out = dict(capabilities)
    out["api"] = True
    notes = list(out.get("limitations") or [])
    notes.insert(0, f"The {hint} connector is authorized. Use it instead of clicking the app.")
    out["limitations"] = notes
    return out


def task_view(user_id) -> dict:
    row = _TASKS.get(str(user_id or ""))
    if not row:
        return {"status": "idle", "front": "", "active": "", "steps": []}
    steps = list(row["steps"])[-12:]
    return {
        "status": row["status"],
        "front": row.get("front") or "",
        "active": steps[-1]["action"] if steps else "",
        "steps": steps,
    }


def task_control(user_id, op: str) -> dict:
    row = _TASKS.setdefault(str(user_id or "default"), {"status": "running", "front": "", "steps": [], "failures": {}})
    if op == "pause":
        row["status"] = "paused"
    elif op == "resume":
        row["status"] = "running"
    elif op == "stop":
        row["status"] = "stopped"
    else:
        return {"status": "failed", "error": "Use pause, resume, or stop."}
    return task_view(user_id)


def _task(user_id) -> dict:
    return _TASKS.setdefault(str(user_id or "default"), {"status": "running", "front": "", "steps": [], "failures": {}})


def _remember(user_id, action: str, target: str, status: str, front: str = "") -> None:
    row = _task(user_id)
    if front:
        row["front"] = front
    row["steps"].append({"action": action, "target": target, "status": status})
    del row["steps"][:-40]
    sig = f"{action}:{target}".lower()
    if status == "failed":
        row["failures"][sig] = int(row["failures"].get(sig) or 0) + 1
    elif status == "succeeded":
        row["failures"].pop(sig, None)


def choose_method(capabilities: dict, denied: set[str] | None = None) -> str | None:
    """First available method. A denied method stops the search so a block is not evaded."""
    blocked = set(denied or ())
    for method in METHODS:
        if method in blocked:
            return None
        if capabilities.get(method):
            return method
    return None


def names_match(wanted: str, front: str) -> bool:
    left = _ALIASES.get((wanted or "").strip().lower(), (wanted or "").strip().lower())
    right = (front or "").strip().lower()
    if not left or not right:
        return False
    return left in right or right in left


def _find(installed: list, target: str) -> str:
    wanted = _ALIASES.get(target.strip().lower(), target.strip().lower())
    for name in installed:
        if names_match(wanted, str(name)):
            return str(name)
    return ""


async def _default_call(user_id, tool: str, args: dict) -> dict:
    from app.desktop.agent import call_local

    out = await call_local(user_id, tool, args)
    return out if isinstance(out, dict) else {"ok": False, "error": "invalid desktop result"}


async def run(
    user_id,
    action: str,
    target: str = "",
    text: str = "",
    call: Callable[..., Awaitable[dict]] | None = None,
    connectors: set[str] | None = None,
) -> dict:
    """One computer-use step. ``call`` is the sidecar, injected in tests."""
    action = (action or "").strip().lower()
    target = (target or "").strip()
    detail = f"{target} {text}".strip()
    risk = assess(action, detail)
    if risk == "prohibited":
        return envelope(
            "failed",
            error={"code": "prohibited", "message": "That would bypass a security check or take credentials. It was not attempted."},
        )
    if risk == "high":
        message = "Ask the user before sending, deleting, paying, or publishing. This step was not run."
        if "send" in f"{action} {detail}".lower():
            message = "Confirm the recipient, then ask the user before anything is sent. This step was not run."
        return envelope(
            "needs_approval",
            result={"action": action, "target": target},
            error={"code": "needs_approval", "message": message},
        )
    row = _task(user_id)
    if row["status"] == "paused":
        return envelope("failed", error={"code": "paused", "message": "Computer use is paused."})
    if row["status"] == "stopped":
        return envelope("failed", error={"code": "stopped", "message": "Computer use was stopped."})
    sig = f"{action}:{target}".lower()
    if int(row["failures"].get(sig) or 0) >= 2:
        return envelope(
            "failed",
            error={"code": "recover", "message": "That step failed twice. Observe the screen and choose a different action instead of repeating it."},
        )
    if action not in {"discover", "inspect", "launch", "observe"}:
        return envelope("unsupported", error={"code": "unknown_action", "message": "Use discover, inspect, launch, or observe."})
    if action in {"inspect", "launch"} and not target:
        return envelope("failed", error={"code": "target_required", "message": "Name the app."})
    if action == "launch" and categorize(target) == "browser" and not website_target(target):
        out = envelope(
            "unsupported",
            error={"code": "use_browser_tools", "message": "Open a site with browser_navigate and a full https address. Lumio does not drive the user's everyday browser."},
        )
        _remember(user_id, action, target, out["status"])
        return out
    site = website_target(target)
    invoke = call or _default_call
    if site and action in {"inspect", "launch"}:
        caps = with_api(capabilities_for("browser"), target, connectors)
        if action == "inspect":
            out = envelope(
                "succeeded",
                result={"name": target, "category": "browser", "method": choose_method(caps), "url": site, "capabilities": caps},
                evidence={"limitations": list(caps["limitations"])},
            )
            _remember(user_id, action, target, out["status"])
            return out
        try:
            opened = await invoke(user_id, "browser_navigate", {"url": site})
        except Exception as exc:  # noqa: BLE001
            out = envelope("failed", error={"code": "desktop_offline", "message": str(exc)[:240]})
            _remember(user_id, action, target, "failed")
            return out
        seen = str((opened or {}).get("url") or "")
        host = site.split("/")[2] if "://" in site else site
        if not isinstance(opened, dict) or opened.get("ok") is False or opened.get("error") or (seen and host not in seen):
            out = envelope(
                "failed",
                error={"code": "not_verified", "message": f"Could not verify {site} is open."},
                evidence={"url": seen},
            )
            _remember(user_id, action, target, "failed")
            return out
        out = envelope("succeeded", result={"url": seen or site, "method": choose_method(caps)}, evidence={"url": seen or site})
        _remember(user_id, action, target, "succeeded", front=host)
        return out
    try:
        if action == "observe":
            raw = await invoke(user_id, "computer_observe", {})
            return _checked(raw, "observe", evidence={"summary": "Observed this computer."})
        found = await invoke(user_id, "app_discover", {})
    except Exception as exc:  # noqa: BLE001 - LocalOffline and transport errors stay on this computer
        return envelope("failed", error={"code": "desktop_offline", "message": str(exc)[:240] or "The desktop app is not connected."})
    if not isinstance(found, dict) or found.get("ok") is False:
        return envelope("failed", error={"code": "discover_failed", "message": str((found or {}).get("error") or "Could not list apps.")[:240]})
    installed = [str(n) for n in (found.get("installed") or []) if n]
    limitations = [str(n) for n in (found.get("limitations") or [])]
    front = str(found.get("front") or "")
    if action == "discover":
        electron = {str(n) for n in (found.get("electron") or [])}
        running = {str(n).lower() for n in (found.get("running") or [])}
        apps = []
        for name in installed[:60]:
            category = "hybrid" if name in electron else categorize(name)
            caps = with_api(capabilities_for(category), name, connectors)
            apps.append({
                "name": name,
                "category": category,
                "method": choose_method(caps),
                "running": name.lower() in running,
            })
        out = envelope(
            "succeeded",
            result={
                "platform": found.get("platform") or "",
                "front": front,
                "apps": apps,
                "tabs": list(found.get("tabs") or [])[:12],
            },
            evidence={"front": front, "limitations": limitations},
        )
        _remember(user_id, action, target, "succeeded", front=front)
        return out
    match = _find(installed, target)
    incomplete = not installed and any("not in this build" in note for note in limitations)
    if action == "inspect":
        if not match and incomplete:
            return envelope(
                "unsupported",
                error={"code": "discovery_incomplete", "message": "This operating system cannot list installed apps in this build. Do not claim the app is missing."},
                evidence={"limitations": limitations},
            )
        if not match:
            out = envelope("failed", error={"code": "not_installed", "message": f"{target} was not found among installed apps."}, evidence={"front": front})
            _remember(user_id, action, target, "failed", front=front)
            return out
        electron = {str(n) for n in (found.get("electron") or [])}
        category = "hybrid" if match in electron else categorize(match)
        caps = with_api(capabilities_for(category), match, connectors)
        out = envelope(
            "succeeded",
            result={"name": match, "category": category, "method": choose_method(caps), "capabilities": caps, "front": names_match(match, front)},
            evidence={"front": front, "limitations": list(caps["limitations"]) + limitations},
        )
        _remember(user_id, action, target, "succeeded", front=front)
        return out
    if not match and incomplete:
        return envelope("unsupported", error={"code": "discovery_incomplete", "message": "Cannot verify that app on this operating system yet."})
    if not match:
        out = envelope("failed", error={"code": "not_installed", "message": f"{target} was not found, so it was not opened."})
        _remember(user_id, action, target, "failed", front=front)
        return out
    try:
        opened = await invoke(user_id, "launch_app", {"name": match})
    except Exception as exc:  # noqa: BLE001
        return envelope("failed", error={"code": "desktop_offline", "message": str(exc)[:240]})
    if not isinstance(opened, dict) or opened.get("ok") is False or opened.get("error"):
        return envelope("failed", error={"code": "launch_failed", "message": str((opened or {}).get("error") or "Could not open the app.")[:240]})
    try:
        again = await invoke(user_id, "app_discover", {})
    except Exception as exc:  # noqa: BLE001
        return envelope("failed", result={"launched": match}, error={"code": "verify_failed", "message": str(exc)[:240]})
    seen = str((again or {}).get("front") or "")
    if not names_match(match, seen):
        out = envelope(
            "failed",
            result={"launched": match},
            error={"code": "not_in_front", "message": f"Opened {match}, but {seen or 'another app'} is in front. The task is not verified."},
            evidence={"front": seen},
        )
        _remember(user_id, action, target, "failed", front=seen)
        return out
    category = "hybrid" if match in {str(n) for n in (again or {}).get("electron") or []} else categorize(match)
    out = envelope(
        "succeeded",
        result={"launched": match, "method": choose_method(with_api(capabilities_for(category), match, connectors))},
        evidence={"front": seen},
    )
    _remember(user_id, action, target, "succeeded", front=seen)
    return out


def _checked(raw: dict, action: str, evidence: dict) -> dict:
    if not isinstance(raw, dict) or raw.get("ok") is False or raw.get("error"):
        return envelope("failed", error={"code": f"{action}_failed", "message": str((raw or {}).get("error") or "Could not observe this computer.")[:240]})
    return envelope("succeeded", result={"action": action}, evidence=evidence)
