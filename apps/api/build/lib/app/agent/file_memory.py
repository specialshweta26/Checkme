"""Remembers the file paths tools returned, so a later message like "analyze that ppt" goes straight to the file.

Earlier turns reach the model only as reply text, and replies often name a file without its folder. These paths are
kept in the chat session's state and shown to the model on the next turns.
"""

from __future__ import annotations

from typing import Any

KEEP = 40
_PATH_KEYS = ("path", "dest", "src")


def _looks_like_path(value: Any) -> bool:
    return isinstance(value, str) and 1 < len(value) < 500 and ("/" in value or "\\" in value) and "\n" not in value


def paths_in(result: Any, depth: int = 0) -> list[str]:
    """Paths in a file tool's result, in order: its own path, entries listed under it, and any nested matches."""
    found: list[str] = []
    if depth > 4:
        return found
    if isinstance(result, dict):
        base = next((result[k] for k in _PATH_KEYS if _looks_like_path(result.get(k))), "")
        if base:
            found.append(base)
        for key in ("entries", "matches", "files", "results", "items"):
            for item in result.get(key) or []:
                if isinstance(item, str) and base and "/" not in item:
                    found.append(f"{base.rstrip('/')}/{item}")
                else:
                    found.extend(paths_in(item, depth + 1))
    elif isinstance(result, list):
        for item in result:
            found.extend(paths_in(item, depth + 1))
    return found


def merge(earlier: list[str] | None, new: list[str] | None, keep: int = KEEP) -> list[str]:
    """Most recent last, each once, at most ``keep``."""
    out: list[str] = []
    for path in [*(earlier or []), *(new or [])]:
        if isinstance(path, str) and path:
            if path in out:
                out.remove(path)
            out.append(path)
    return out[-keep:]


def remember(ctx: Any, result: Any) -> None:
    ctx.files_seen = merge(getattr(ctx, "files_seen", None), paths_in(result))


def prompt_block(paths: list[str] | None) -> str:
    if not paths:
        return ""
    listed = "\n".join(f"- {p}" for p in paths[-KEEP:])
    return (
        "\n\n## Files seen earlier in this chat\n"
        "Tools returned these paths in earlier messages. When the user refers to one of them (\"that ppt\", \"the "
        "invoice\"), use its path directly instead of searching again.\n" + listed
    )
